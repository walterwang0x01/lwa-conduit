"""kiro-conduit 命令行入口。

把一份 workspace（含 dag.yaml）跑成完整流程：ParallelOrchestrator（按 DAG 波次
并行跑 CIV）→ MergeOrchestrator（按拓扑序 / 按仓库串行 merge 回主分支）。

用法：
    kiro-conduit run --workspace <dir> [--resume] [--dashboard] [--no-merge]

<dir> 是含 dag.yaml 的目录（也可直接传 dag.yaml 路径）。默认 base repo 为该目录，
跨仓库时 repos 在 dag.yaml 里声明、相对 workspace 解析。
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
from pathlib import Path

from kiro_conduit.dag import Workspace, load_workspace
from kiro_conduit.events import EventBus
from kiro_conduit.git_utils import run_git
from kiro_conduit.merge import MergeOrchestrator, MergeReport
from kiro_conduit.orchestrator import ParallelOrchestrator, ParallelRunReport
from kiro_conduit.run_state import load_state, state_path

logger = logging.getLogger(__name__)

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _configure_run_logging(dashboard: bool, log_path: Path) -> None:
    """控制台按 dashboard 决定详略；始终额外写一份完整 INFO 日志到文件（二者不互斥）。"""
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter(_LOG_FMT)
    console = logging.StreamHandler()
    console.setLevel(logging.WARNING if dashboard else logging.INFO)
    console.setFormatter(fmt)
    root.addHandler(console)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setLevel(logging.INFO)
    fh.setFormatter(fmt)
    root.addHandler(fh)


def _resolve_dag(workspace: str) -> Path:
    p = Path(workspace).expanduser().resolve()
    if p.is_dir():
        dag = p / "dag.yaml"
        if not dag.is_file():
            raise SystemExit(f"no dag.yaml in workspace dir: {p}")
        return dag
    if p.is_file():
        return p
    raise SystemExit(f"workspace not found: {p}")


def _venv_path_prepend(venv: Path, current_path: str) -> str:
    """把 venv 的 bin 目录前置到 PATH，让 verifier / kiro-cli 用该 venv 的工具。"""
    bin_dir = (venv / "bin").expanduser()
    if not bin_dir.is_dir():
        raise SystemExit(f"--venv: {bin_dir} 不存在（不是有效的 venv）")
    return f"{bin_dir.resolve()}{os.pathsep}{current_path}"


def _print_parallel_report(ws: Workspace, report: ParallelRunReport) -> None:
    print(f"\n✓ parallel phase: passed={report.passed_count} "
          f"failed={report.failed_count} skipped={len(report.skipped)}")
    tids = sorted(ws.tasks)
    tw = max((len(t) for t in tids), default=4)
    mw = max((len(ws.tasks[t].model or "<default>") for t in tids), default=5)
    print(f"  {'':1} {'task':<{tw}}  {'model':<{mw}}  {'status':<7}  att  files")
    for tid in tids:
        model = ws.tasks[tid].model or "<default>"
        out = report.outcomes.get(tid)
        if out is None:
            print(f"  - {tid:<{tw}}  {model:<{mw}}  {'skipped':<7}")
            continue
        mark = "✓" if out.passed else "✗"
        status = "passed" if out.passed else "failed"
        files = len(out.last_task_result.files_changed)
        print(f"  {mark} {tid:<{tw}}  {model:<{mw}}  {status:<7}  "
              f"{out.attempts:<3}  {files}")

async def _review_integration(
    args: argparse.Namespace, base_repo: Path, base_branch: str, specs_dir: Path
) -> None:
    """merge 后对组装好的集成结果做一次 AI 初审，写 .kiro-conduit/review.md。"""
    from kiro_conduit.git_utils import run_git
    from kiro_conduit.semantic import KiroSemanticReviewer, review_integration

    code, _o, _e = await run_git(
        base_repo,
        ["rev-parse", "--verify", "--quiet", "refs/heads/kiro-conduit/integration"],
    )
    ref = "kiro-conduit/integration" if code == 0 else base_branch
    reviewer = KiroSemanticReviewer(
        kiro_cli_path=args.kiro_cli, model=args.review_model, max_diff_chars=120000
    )
    result = await review_integration(
        base_repo=base_repo, base_branch=base_branch, integration_ref=ref,
        specs_dir=specs_dir, reviewer=reviewer,
    )
    report_path = base_repo / ".kiro-conduit" / "review.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    verdict = "PASS" if result.passed else "CONCERNS"
    report_path.write_text(
        f"# 集成 AI 初审\n\nverdict: {verdict}\n\n{result.feedback}\n", encoding="utf-8"
    )
    flag = "✅ 无明显问题" if result.passed else "⚠ 有发现，请看报告"
    print(f"\n🔎 集成 AI 初审: {flag} — 详见 {report_path}")



def _print_merge_report(report: MergeReport) -> None:
    print("\n✓ merge phase:")
    for tid, mr in report.results.items():
        mark = "✓" if mr.merged else "✗"
        err = f" — {mr.error}" if mr.error else ""
        print(f"  {mark} {tid}{err}")
        if mr.diagnostic is not None:
            print(f"      conflicts: {list(mr.diagnostic.conflicted_files)}")


async def _run_parallel(
    orch: ParallelOrchestrator,
    ws: Workspace,
    bus: EventBus | None,
    base_branch: str,
) -> ParallelRunReport:
    if bus is None:
        return await orch.run(base_branch=base_branch)
    # dashboard 模式：rich.live 实时渲染
    from kiro_conduit.dashboard import Dashboard

    dash = Dashboard(workspace=ws)
    dash.attach(bus)
    with dash.live():
        report = await orch.run(base_branch=base_branch)
        await asyncio.sleep(0.3)  # 留一帧给 dashboard 渲染最终状态
    return report


async def _run(args: argparse.Namespace) -> int:
    dag_path = _resolve_dag(args.workspace)
    ws = load_workspace(dag_path)
    base_repo = (
        Path(args.base_repo).expanduser().resolve()
        if args.base_repo
        else dag_path.parent
    )
    if args.venv:
        os.environ["PATH"] = _venv_path_prepend(
            Path(args.venv).expanduser(), os.environ.get("PATH", "")
        )
    print(f"✓ workspace: {dag_path}")
    print(f"  base repo: {base_repo}")
    log_path = (
        Path(args.log_file).expanduser()
        if args.log_file
        else base_repo / ".kiro-conduit" / "run.log"
    )
    _configure_run_logging(args.dashboard, log_path)
    await _preflight(base_repo)
    base_branch = await _resolve_base_branch(base_repo, args.base_branch)

    # 裸重跑守卫：发现上次进度但既没 --resume 也没 --fresh → 提示而非默删重来
    prior = load_state(state_path(base_repo))
    if prior is not None and prior.passed_ids() and not args.resume and not args.fresh:
        print(
            f"\n✗ 发现上次运行的进度（{len(prior.passed_ids())} 个 task 已完成）。"
            "\n  --resume 从断点续跑（复用已完成的分支）；"
            "\n  --fresh  丢弃旧进度、从头重跑（会覆盖旧分支）。"
        )
        return 1

    print(f"  base branch: {base_branch}")
    print(f"  log file: {log_path}")
    dest = "merge into base branch" if args.merge else "leave branches for review (no merge)"
    print(f"  on success: {dest}")
    summary = f"  {len(ws.tasks)} tasks, {len(ws.phases)} phases"
    if ws.repos:
        summary += f", repos: {sorted(ws.repos)}"
    print(summary)

    bus = EventBus() if args.dashboard else None
    reviewer = None
    if args.review:
        from kiro_conduit.semantic import KiroSemanticReviewer

        reviewer = KiroSemanticReviewer(
            kiro_cli_path=args.kiro_cli, model=args.review_model
        )
        print("  semantic review: ON（Layer 3 对照 spec 审查 diff）")
    orch = ParallelOrchestrator(
        workspace=ws,
        base_repo=base_repo,
        max_concurrency=args.max_concurrency,
        max_attempts=args.max_attempts,
        kiro_cli_path=args.kiro_cli,
        resume=args.resume,
        event_bus=bus,
        semantic_reviewer=reviewer,
    )

    report = await _run_parallel(orch, ws, bus, base_branch)
    _print_parallel_report(ws, report)

    successful = {tid for tid, out in report.outcomes.items() if out.passed}

    if not args.merge:
        # 默认：产出分支供 review，不自动合并（review-and-accept）
        _print_review_hint(report, base_branch)
        return 0 if report.all_passed else 1

    if not successful:
        print("\n✗ 没有任何任务通过，无可合并")
        return 1

    # 即便部分任务失败/跳过，也把已通过的组装进 kiro-conduit/integration，
    # 给一个可 review / 可用的集成结果（而不是因一个失败丢掉全部成果）。
    merger = MergeOrchestrator(ws, base_repo, event_bus=bus, diagnose=args.diagnose)
    merge_report = await merger.merge(
        handles=report.handles,
        successful_task_ids=successful,
        base_branch=base_branch,
    )
    _print_merge_report(merge_report)
    if args.review:
        await _review_integration(args, base_repo, base_branch, dag_path.parent / "specs")
    if not report.all_passed:
        print(
            "\n⚠ 部分任务失败/跳过：已把通过的合进 integration，失败项见上方报告。"
        )
    return 0 if (report.all_passed and merge_report.all_merged) else 1


def _print_review_hint(report: ParallelRunReport, base_branch: str) -> None:
    """打印产出的 task 分支 + 如何 review / 合并（默认不自动合时用）。"""
    passed = [
        (tid, report.handles[tid].branch)
        for tid, out in sorted(report.outcomes.items())
        if out.passed and tid in report.handles
    ]
    if not passed:
        return
    print("\n产出分支（未合并，供 review）:")
    for tid, branch in passed:
        print(f"  {tid}: {branch}")
    print(f"\n  查看改动:  git diff {base_branch}...<branch>")
    print("  合并:      重跑时加 --merge（或自行 git merge / 开 PR）")


async def _preflight(base_repo: Path) -> None:
    """启动预检：确认是 git 仓库（否则报错早退），打印当前分支与脏区状态。"""
    code, _o, _e = await run_git(base_repo, ["rev-parse", "--is-inside-work-tree"])
    if code != 0:
        raise SystemExit(f"not a git repository: {base_repo}")
    _c, cur, _ = await run_git(base_repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    _c, status, _ = await run_git(base_repo, ["status", "--porcelain"])
    print(f"  current branch: {cur.strip() or '(detached)'}")
    if status.strip():
        print(
            "  working tree: dirty — 安全（worktree 从已提交 HEAD 起，"
            "你的工作区与当前分支全程不动）"
        )
    else:
        print("  working tree: clean")


async def _resolve_base_branch(base_repo: Path, override: str | None) -> str:
    """base 分支：显式 --base-branch 优先；否则跟随 base_repo 当前分支；探测不到回退 main。"""
    if override:
        return override
    code, out, _ = await run_git(base_repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    cur = out.strip()
    if code == 0 and cur and cur != "HEAD":
        return cur
    logger.warning("[cli] could not detect current branch; falling back to 'main'")
    return "main"


async def _plan(args: argparse.Namespace) -> int:
    from kiro_conduit.planner import KiroPlanner, PlanError, write_plan

    spec_path = Path(args.spec).expanduser()
    if not spec_path.is_file():
        raise SystemExit(f"spec file not found: {spec_path}")
    spec_text = spec_path.read_text(encoding="utf-8")
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"✓ planning from spec: {spec_path}")
    print("  asking Kiro to decompose it into a DAG (this may take ~1 min)...")
    planner = KiroPlanner(kiro_cli_path=args.kiro_cli, model=args.model)
    try:
        tasks = await planner.generate_plan(spec_text, cwd=out_dir)
        dag_path = write_plan(tasks, out_dir)
    except PlanError as exc:
        print(f"\n✗ planning failed: {exc}")
        return 1

    print(f"\n✓ generated {dag_path}  ({len(tasks)} tasks)")
    for t in tasks:
        deps = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
        print(f"  - {t.id}{deps}")
    print("\n下一步：review 上面的 dag.yaml + specs/，确认后执行：")
    print(f"  kiro-conduit run --workspace {out_dir} --base-repo <你的仓库>")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kiro-conduit",
        description="Parallel spec executor for Kiro CLI: spec → DAG → workers → merge.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    run_p = sub.add_parser("run", help="run a workspace (dag.yaml) end-to-end")
    run_p.add_argument(
        "--workspace", required=True,
        help="directory containing dag.yaml (or a path to dag.yaml)",
    )
    run_p.add_argument(
        "--base-repo", default=None,
        help="git repo to run against (default: the workspace directory)",
    )
    run_p.add_argument(
        "--base-branch", default=None,
        help="branch to base work on / integrate into (default: the repo's current branch)",
    )
    run_p.add_argument(
        "--venv", default=None,
        help="venv whose bin/ is prepended to PATH so verification (pytest/lint) "
             "and kiro-cli run with your project's tools (default: inherit current PATH)",
    )
    run_p.add_argument(
        "--review", action="store_true",
        help="enable Layer 3 semantic review: a separate kiro-cli reviews each "
             "task's diff against its spec (catches spec drift tests miss; default off)",
    )
    run_p.add_argument(
        "--review-model", default=None,
        help="model id for the semantic reviewer (default: Kiro default)",
    )
    run_p.add_argument("--max-concurrency", type=int, default=4)
    run_p.add_argument("--max-attempts", type=int, default=3)
    run_p.add_argument("--kiro-cli", default="kiro-cli", help="path to kiro-cli binary")
    run_p.add_argument("--resume", action="store_true", help="resume from prior run-state")
    run_p.add_argument(
        "--fresh", action="store_true",
        help="discard prior run-state and start over (overwrites old branches)",
    )
    run_p.add_argument(
        "--log-file", default=None,
        help="log file path (default: <base-repo>/.kiro-conduit/run.log; always written)",
    )
    run_p.add_argument("--dashboard", action="store_true", help="show rich TUI dashboard")
    run_p.add_argument(
        "--diagnose", action="store_true",
        help="capture structured conflict diagnostics on merge failure",
    )
    run_p.add_argument(
        "--merge", action="store_true",
        help="merge passed task branches into the base branch "
             "(default: leave branches for review)",
    )

    plan_p = sub.add_parser(
        "plan", help="LLM-assisted: turn a markdown spec into a dag.yaml workspace"
    )
    plan_p.add_argument("--spec", required=True, help="markdown spec file to plan from")
    plan_p.add_argument(
        "--out", required=True, help="output workspace dir (dag.yaml + specs/)"
    )
    plan_p.add_argument("--kiro-cli", default="kiro-cli", help="path to kiro-cli binary")
    plan_p.add_argument(
        "--model", default=None, help="model id for planning (default: Kiro default)"
    )

    args = parser.parse_args(argv)
    if args.command == "plan":
        logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
        return asyncio.run(_plan(args))
    # run：日志（控制台 + 文件）由 _run 内部配置（需要 base_repo 定位日志文件）
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
