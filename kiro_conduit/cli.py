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
from pathlib import Path

from kiro_conduit.dag import Workspace, load_workspace
from kiro_conduit.events import EventBus
from kiro_conduit.git_utils import run_git
from kiro_conduit.merge import MergeOrchestrator, MergeReport
from kiro_conduit.orchestrator import ParallelOrchestrator, ParallelRunReport

logger = logging.getLogger(__name__)


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


def _print_parallel_report(ws: Workspace, report: ParallelRunReport) -> None:
    print(f"\n✓ parallel phase: passed={report.passed_count} "
          f"failed={report.failed_count} skipped={len(report.skipped)}")
    for tid in sorted(ws.tasks):
        out = report.outcomes.get(tid)
        if out is None:
            print(f"  - {tid}: skipped (upstream failed)")
            continue
        mark = "✓" if out.passed else "✗"
        print(f"  {mark} {tid}: passed={out.passed} attempts={out.attempts}")


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
    print(f"✓ workspace: {dag_path}")
    print(f"  base repo: {base_repo}")
    base_branch = await _resolve_base_branch(base_repo, args.base_branch)
    print(f"  base branch: {base_branch}")
    summary = f"  {len(ws.tasks)} tasks, {len(ws.phases)} phases"
    if ws.repos:
        summary += f", repos: {sorted(ws.repos)}"
    print(summary)

    bus = EventBus() if args.dashboard else None
    orch = ParallelOrchestrator(
        workspace=ws,
        base_repo=base_repo,
        max_concurrency=args.max_concurrency,
        max_attempts=args.max_attempts,
        kiro_cli_path=args.kiro_cli,
        resume=args.resume,
        event_bus=bus,
    )

    report = await _run_parallel(orch, ws, bus, base_branch)
    _print_parallel_report(ws, report)

    if not report.all_passed:
        print("\n✗ not all tasks passed; not merging")
        _print_review_hint(report, base_branch)
        return 1

    if not args.merge:
        # 默认：产出分支供 review，不自动合并（review-and-accept）
        _print_review_hint(report, base_branch)
        return 0

    successful = {tid for tid, out in report.outcomes.items() if out.passed}
    merger = MergeOrchestrator(ws, base_repo, event_bus=bus, diagnose=args.diagnose)
    merge_report = await merger.merge(
        handles=report.handles,
        successful_task_ids=successful,
        base_branch=base_branch,
    )
    _print_merge_report(merge_report)
    return 0 if merge_report.all_merged else 1


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
    run_p.add_argument("--max-concurrency", type=int, default=4)
    run_p.add_argument("--max-attempts", type=int, default=3)
    run_p.add_argument("--kiro-cli", default="kiro-cli", help="path to kiro-cli binary")
    run_p.add_argument("--resume", action="store_true", help="resume from prior run-state")
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

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.WARNING if args.dashboard else logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
