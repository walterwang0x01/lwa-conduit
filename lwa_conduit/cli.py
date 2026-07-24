"""lwa-conduit 命令行入口。

把一份 workspace（含 dag.yaml）跑成完整流程：ParallelOrchestrator（按 DAG 波次
并行跑 CIV）→ MergeOrchestrator（按拓扑序 / 按仓库串行 merge 回主分支）。

用法：
    lwa-conduit run --workspace <dir> [--resume] [--dashboard] [--no-merge]

<dir> 是含 dag.yaml 的目录（也可直接传 dag.yaml 路径）。默认 base repo 为该目录，
跨仓库时 repos 在 dag.yaml 里声明、相对 workspace 解析。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from lwa_conduit.dag import Workspace, load_workspace
from lwa_conduit.events import EventBus
from lwa_conduit.git_utils import run_git
from lwa_conduit.merge import MergeOrchestrator, MergeReport
from lwa_conduit.metrics import (
    RuntimeMetricRecord,
    load_metrics,
    metrics_path,
    recommend_strategy,
    save_metrics,
    summarize_metrics,
)
from lwa_conduit.orchestrator import ParallelOrchestrator, ParallelRunReport
from lwa_conduit.run_state import load_state, state_path
from lwa_conduit.runtime import RuntimeConfig
from lwa_conduit.runtime.quota import (
    fallback_kinds_for_bucket,
    is_quota_blocked,
    pick_first_available_kind,
    probe_all_runtime_kinds,
    probe_runtime_kind,
)
from lwa_conduit.runtime.types import coerce_runtime_kind

logger = logging.getLogger(__name__)

_LOG_FMT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def _rec_int(data: dict[str, object], key: str) -> int:
    value = data.get(key, 0)
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _rec_float(data: dict[str, object], key: str) -> float:
    value = data.get(key, 0)
    if isinstance(value, (int, float)):
        return float(value)
    return 0.0


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


def _runtime_from_args(
    args: argparse.Namespace,
    *,
    role: str,
    adaptive_bucket: str,
    default_kind: str,
    model: str | None,
    timeout: float = 600.0,
) -> RuntimeConfig:
    kind = getattr(args, f"{role}_runtime_kind", None) or default_kind
    bin_override = getattr(args, f"{role}_bin", None)
    if bin_override is None:
        bin_override = args.kiro_cli
    adaptive_mode = getattr(args, "adaptive_mode", "suggest")
    adaptive_runtime_by_bucket = getattr(args, "_adaptive_runtime_kind_by_bucket", {})
    adaptive_model_by_bucket = getattr(args, "_adaptive_model_by_bucket", {})
    recommended_runtime = adaptive_runtime_by_bucket.get(adaptive_bucket)
    recommended_model = adaptive_model_by_bucket.get(adaptive_bucket)
    if adaptive_mode in {"apply-safe", "apply-aggressive"} and recommended_runtime:
        kind = recommended_runtime
    resolved_model = (
        recommended_model if adaptive_mode in {"apply-safe", "apply-aggressive"} else model
    )
    if resolved_model is None:
        resolved_model = model
    allowed_kinds = {"kiro-cli-acp", "cursor-agent-cli", "gemini-cli"}
    runtime_kind = coerce_runtime_kind(kind if kind in allowed_kinds else "kiro-cli-acp")
    if is_quota_blocked(probe_runtime_kind(runtime_kind)):
        fallback = pick_first_available_kind(fallback_kinds_for_bucket(adaptive_bucket))
        if fallback:
            runtime_kind = coerce_runtime_kind(fallback)
    return RuntimeConfig.from_cli(
        kiro_cli=bin_override,
        runtime_kind=runtime_kind,
        model=resolved_model,
        timeout=timeout,
        simple_tier=getattr(args, "kiro_simple_tier", "balanced"),
        medium_tier=getattr(args, "kiro_medium_tier", "strong"),
        hard_tier=getattr(args, "kiro_hard_tier", "max"),
        medium_threshold=getattr(args, "kiro_medium_threshold", 4),
        hard_threshold=getattr(args, "kiro_hard_threshold", 7),
    )


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


def _collect_runtime_metrics(
    report: ParallelRunReport, *, bucket: str = "implementor"
) -> list[RuntimeMetricRecord]:
    records: list[RuntimeMetricRecord] = []
    for tid, out in sorted(report.outcomes.items()):
        tr = out.last_task_result
        records.append(
            RuntimeMetricRecord(
                task_id=tid,
                task_bucket=bucket,
                runtime_kind=tr.runtime_kind or "unknown",
                model=tr.model or "(default)",
                passed=out.passed,
                attempts=out.attempts,
                files_changed=len(tr.files_changed),
                duration_ms=out.duration_ms,
            )
        )
    return records


def _collect_reviewer_metrics(report: ParallelRunReport) -> list[RuntimeMetricRecord]:
    """从 per-task semantic review 层抽出 reviewer metrics。

    passed / execution_ok = runtime 是否跑通；
    verdict_pass = 审查结论（FAIL 不代表 runtime 失败）。
    """
    from lwa_conduit.types import VerifyLayer

    records: list[RuntimeMetricRecord] = []
    for tid, out in sorted(report.outcomes.items()):
        for layer in out.last_verify_result.layers:
            if layer.layer is not VerifyLayer.SEMANTIC or layer.skipped:
                continue
            # execution_ok 缺省时按"拿到了结论"算成功（兼容旧 LayerResult）
            execution_ok = True if layer.execution_ok is None else layer.execution_ok
            records.append(
                RuntimeMetricRecord(
                    task_id=f"review:{tid}",
                    task_bucket="reviewer",
                    runtime_kind=layer.runtime_kind or "unknown",
                    model=layer.model or "(default)",
                    # passed=execution_ok：自适应只看 runtime 可靠性
                    passed=execution_ok,
                    attempts=1,
                    files_changed=0,
                    execution_ok=execution_ok,
                    verdict_pass=layer.passed,
                )
            )
    return records


def _bucket_candidates(bucket: str) -> tuple[str, ...]:
    if bucket == "implementor":
        return ("implementor", "conduit-run")
    return (bucket,)


def _print_runtime_metrics_report(records: list[RuntimeMetricRecord]) -> None:
    buckets = sorted({record.task_bucket for record in records})
    for bucket in buckets:
        rows = summarize_metrics(records, bucket=bucket)
        if not rows:
            continue
        print(f"\n✓ runtime/model metrics [{bucket}]:")
        for row in rows:
            extra = ""
            if "verdict_pass_rate" in row:
                extra = f" verdict_pass_rate={_rec_float(row, 'verdict_pass_rate'):.0%}"
            duration_s = round(_rec_float(row, "avg_duration_ms") / 1000, 1)
            print(
                "  "
                f"{row['runtime_kind']} / {row['model']}: "
                f"total={row['total']} success={row['success']} failed={row['failed']} "
                "success_rate="
                f"{_rec_float(row, 'success_rate'):.0%} "
                f"avg_files={row['avg_files_changed']} "
                f"avg_duration={duration_s}s "
                f"score={_rec_float(row, 'score'):.2f}"
                f"{extra}"
            )
        recommendation = recommend_strategy(records, bucket=bucket)
        if _rec_int(recommendation, "sample_size"):
            print(
                f"\n✓ adaptive recommendation [{bucket}]:"
                f" runtime={recommendation.get('preferred_runtime_kind') or '(keep current)'}"
                f" model={recommendation.get('preferred_model') or '(keep current)'}"
                f" samples={recommendation['sample_size']}"
            )


def _apply_adaptive_recommendation(
    args: argparse.Namespace, records: list[RuntimeMetricRecord], *, bucket: str
) -> None:
    recommendation = {"sample_size": 0, "reason": "insufficient-history"}
    for candidate in _bucket_candidates(bucket):
        recommendation = recommend_strategy(records, bucket=candidate)
        if _rec_int(recommendation, "sample_size") > 0:
            break
    if not hasattr(args, "_adaptive_runtime_kind_by_bucket"):
        args._adaptive_runtime_kind_by_bucket = {}
    if not hasattr(args, "_adaptive_model_by_bucket"):
        args._adaptive_model_by_bucket = {}
    mode = getattr(args, "adaptive_mode", "suggest")
    if mode == "off" or _rec_int(recommendation, "sample_size") <= 0:
        return
    if mode == "apply-aggressive":
        args._adaptive_runtime_kind_by_bucket[bucket] = recommendation.get(
            "preferred_runtime_kind"
        )
        args._adaptive_model_by_bucket[bucket] = recommendation.get("preferred_model")
        return
    if (
        mode == "apply-safe"
        and _rec_int(recommendation, "sample_size") >= 8
        and _rec_float(recommendation, "runtime_success_rate") >= 0.9
    ):
        args._adaptive_runtime_kind_by_bucket[bucket] = recommendation.get(
            "preferred_runtime_kind"
        )
        if _rec_float(recommendation, "model_success_rate") >= 0.9:
            args._adaptive_model_by_bucket[bucket] = recommendation.get(
                "preferred_model"
            )

def _warn_unowned_shared_files(ws: Workspace, report: ParallelRunReport) -> list[str]:
    """预警：被 ≥2 个任务创建、却不在任何 files_owned 的文件。

    各任务由独立 Kiro 实例创建，谁都没认领的共享基建文件（如 app/services/db.py）
    会被多个任务各造一份、内容分歧 → 合并时 add/add 冲突。这里在合并前用各任务已有的
    files_changed 数据预测此类冲突并给出修法：把该文件归给某个 foundation 任务独家所有。
    返回命中的文件列表（便于测试），并打印告警。
    """
    owned: set[str] = set()
    for t in ws.tasks.values():
        owned.update(t.files_owned)
    creators: dict[str, list[str]] = {}
    for tid, out in report.outcomes.items():
        if not out.passed:
            continue
        for f in out.last_task_result.files_changed:
            if f not in owned:
                creators.setdefault(f, []).append(tid)
    hits = sorted(f for f, ts in creators.items() if len(ts) >= 2)
    if hits:
        print(
            "\n⚠ 合并风险：以下文件被多个任务创建却不属于任何任务（无 owner），"
            "合并时很可能 add/add 冲突——建议把它归给一个 foundation 任务的 files_owned："
        )
        for f in hits:
            print(f"    {f}  ← {', '.join(sorted(creators[f]))}")
    return hits


async def _review_integration(
    args: argparse.Namespace, base_repo: Path, base_branch: str, specs_dir: Path
) -> RuntimeMetricRecord | None:
    """merge 后对组装好的集成结果做一次 AI 初审，写 .lwa-conduit/review.md。

    返回 reviewer metric（execution_ok ≠ verdict）；失败开路径可能仍有 metric。
    """
    from lwa_conduit.paths import conduit_dir, resolve_integration_ref
    from lwa_conduit.semantic import KiroSemanticReviewer, review_integration

    ref = await resolve_integration_ref(base_repo, base_branch)
    review_runtime = _runtime_from_args(
        args,
        role="reviewer",
        adaptive_bucket="reviewer",
        default_kind="kiro-cli-acp",
        model=args.review_model,
    )
    reviewer = KiroSemanticReviewer(
        runtime=review_runtime,
        max_diff_chars=120000,
        model=args.review_model,
    )
    from rich.console import Console

    with Console().status("[bold]集成 AI 初审中（对照 spec 审整条 diff）…", spinner="dots"):
        result = await review_integration(
            base_repo=base_repo, base_branch=base_branch, integration_ref=ref,
            specs_dir=specs_dir, reviewer=reviewer,
        )
    report_path = conduit_dir(base_repo) / "review.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    verdict = "PASS" if result.passed else "CONCERNS"
    report_path.write_text(
        f"# 集成 AI 初审\n\nverdict: {verdict}\n\n{result.feedback}\n", encoding="utf-8"
    )
    flag = "✅ 无明显问题" if result.passed else "⚠ 有发现，请看报告"
    print(f"\n🔎 集成 AI 初审: {flag} — 详见 {report_path}")
    logger.info(
        "[review] integration AI review verdict=%s report=%s",
        "PASS" if result.passed else "CONCERNS", report_path,
    )
    execution_ok = True if result.execution_ok is None else result.execution_ok
    return RuntimeMetricRecord(
        task_id="review:integration",
        task_bucket="reviewer",
        runtime_kind=result.runtime_kind or review_runtime.kind,
        model=result.model or review_runtime.model or "(default)",
        passed=execution_ok,
        attempts=1,
        files_changed=0,
        execution_ok=execution_ok,
        verdict_pass=result.passed,
    )


async def _integration_check(
    ws: Workspace, base_repo: Path, base_branch: str
) -> bool | None:
    """合并后对集成结果跑全量验证命令（独立 worktree，带 copy_files）。

    返回 True/False=跑了且通过/失败；None=没配 integration_check 或没法建 worktree。
    """
    cmd = ws.integration_check
    if not cmd:
        return None
    import shutil

    from lwa_conduit.git_utils import run_git
    from lwa_conduit.paths import conduit_dir, resolve_integration_ref

    ref = await resolve_integration_ref(base_repo, base_branch)
    wt = conduit_dir(base_repo) / "intcheck"
    await run_git(base_repo, ["worktree", "remove", "--force", str(wt)])
    code, _o, err = await run_git(base_repo, ["worktree", "add", "--detach", str(wt), ref])
    if code != 0:
        print(f"\n⚠ 集成全量验证：无法创建 worktree：{err.strip()}")
        return None
    try:
        for rel in ws.copy_files:
            src = base_repo / rel
            if src.is_file():
                dst = wt / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
        from rich.console import Console

        from lwa_conduit.proc_util import reap

        with Console().status(f"[bold]集成全量验证中… ($ {cmd})", spinner="dots"):
            proc = await asyncio.create_subprocess_shell(
                cmd, cwd=str(wt),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True,
            )
            try:
                out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=1800)
            except TimeoutError:
                await reap(proc)  # 连根杀（install/build/test 子进程不留孤儿）
                print("\n🧪 集成全量验证: ✗ 超时（30min）")
                return False
        ok = proc.returncode == 0
        print(f"\n🧪 集成全量验证: {'✅ PASS' if ok else '✗ FAIL'}  ($ {cmd})")
        logger.info("[integration-check] %s ($ %s)", "PASS" if ok else "FAIL", cmd)
        if not ok:
            print(out_b.decode("utf-8", errors="replace")[-1500:])
        return ok
    finally:
        await run_git(base_repo, ["worktree", "remove", "--force", str(wt)])


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
    *,
    dashboard: bool = False,
) -> ParallelRunReport:
    if bus is None or not dashboard:
        return await orch.run(base_branch=base_branch)
    # dashboard 模式：rich.live 实时渲染（EventBus 仍可同时挂 NDJSON writer）
    from lwa_conduit.dashboard import Dashboard

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
        else base_repo / ".lwa-conduit" / "run.log"
    )
    _configure_run_logging(args.dashboard, log_path)
    await _preflight(base_repo)
    await _warn_if_dirty_overlap(ws, base_repo)
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
    prior_metrics = load_metrics(metrics_path(base_repo))
    _apply_adaptive_recommendation(args, prior_metrics, bucket="implementor")
    _apply_adaptive_recommendation(args, prior_metrics, bucket="reviewer")
    impl_runtime = _runtime_from_args(
        args,
        role="implementor",
        adaptive_bucket="implementor",
        default_kind=getattr(args, "runtime_kind", "kiro-cli-acp"),
        model=None,
    )
    review_runtime = _runtime_from_args(
        args,
        role="reviewer",
        adaptive_bucket="reviewer",
        default_kind="kiro-cli-acp",
        model=args.review_model,
    )
    print(f"  implementor runtime: {impl_runtime.kind} ({impl_runtime.bin})")
    print(f"  reviewer runtime: {review_runtime.kind} ({review_runtime.bin})")
    summary = f"  {len(ws.tasks)} tasks, {len(ws.phases)} phases"
    if ws.repos:
        summary += f", repos: {sorted(ws.repos)}"
    print(summary)

    events_mode = getattr(args, "events", "none")
    need_bus = bool(args.dashboard) or events_mode != "none"
    bus = EventBus() if need_bus else None
    ndjson_writer = None
    if bus is not None and events_mode == "ndjson":
        from lwa_conduit.event_export import NdjsonEventWriter

        ndjson_writer = NdjsonEventWriter()
        ndjson_writer.attach(bus)
        print("  events: ndjson → stderr")
    if args.review:
        print("  semantic review: ON（合并后对集成结果对照 spec 初审）")
    task_reviewer = None
    if args.review_tasks:
        from lwa_conduit.semantic import KiroSemanticReviewer

        task_reviewer = KiroSemanticReviewer(
            runtime=_runtime_from_args(
                args,
                role="reviewer",
                adaptive_bucket="reviewer",
                default_kind="kiro-cli-acp",
                model=args.review_model,
            ),
            model=args.review_model,
        )
        print("  per-task semantic review: ON（每任务对照 spec 审，超时 600s）")
    orch = ParallelOrchestrator(
        workspace=ws,
        base_repo=base_repo,
        max_concurrency=args.max_concurrency,
        max_attempts=args.max_attempts,
        implementor_runtime=_runtime_from_args(
            args,
            role="implementor",
            adaptive_bucket="implementor",
            default_kind=getattr(args, "runtime_kind", "kiro-cli-acp"),
            model=None,
            timeout=600.0,
        ),
        kiro_cli_path=args.kiro_cli,
        runtime_kind=getattr(args, "runtime_kind", "kiro-cli-acp"),
        resume=args.resume,
        event_bus=bus,
        semantic_reviewer=task_reviewer,
        review_timeout=600.0,
        sandbox=args.sandbox,
    )

    try:
        report = await _run_parallel(
            orch, ws, bus, base_branch, dashboard=bool(args.dashboard)
        )
        _print_parallel_report(ws, report)
        current_metrics = _collect_runtime_metrics(report, bucket="implementor")
        current_metrics.extend(_collect_reviewer_metrics(report))
        all_metrics_path = metrics_path(base_repo)
        all_metrics = prior_metrics + current_metrics
        save_metrics(all_metrics_path, all_metrics)
        _print_runtime_metrics_report(all_metrics)

        successful = {tid for tid, out in report.outcomes.items() if out.passed}

        if not args.merge:
            # 默认：产出分支供 review，不自动合并（review-and-accept）
            _print_review_hint(report, base_branch)
            return 0 if report.all_passed else 1

        if not successful:
            print("\n✗ 没有任何任务通过，无可合并")
            return 1

        _warn_unowned_shared_files(ws, report)

        # 即便部分任务失败/跳过，也把已通过的组装进 lwa-conduit/integration，
        # 给一个可 review / 可用的集成结果（而不是因一个失败丢掉全部成果）。
        merger = MergeOrchestrator(ws, base_repo, event_bus=bus, diagnose=args.diagnose)
        merge_report = await merger.merge(
            handles=report.handles,
            successful_task_ids=successful,
            base_branch=base_branch,
        )
        _print_merge_report(merge_report)
        if args.review:
            review_metric = await _review_integration(
                args, base_repo, base_branch, dag_path.parent / "specs"
            )
            if review_metric is not None:
                all_metrics = [*load_metrics(all_metrics_path), review_metric]
                save_metrics(all_metrics_path, all_metrics)
                _print_runtime_metrics_report([review_metric])
        check_ok = await _integration_check(ws, base_repo, base_branch)
        if not report.all_passed:
            print(
                "\n⚠ 部分任务失败/跳过：已把通过的合进 integration，失败项见上方报告。"
            )
        ok = report.all_passed and merge_report.all_merged and check_ok is not False
        return 0 if ok else 1
    finally:
        if ndjson_writer is not None:
            ndjson_writer.detach()


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


async def _warn_if_dirty_overlap(ws: Workspace, base_repo: Path) -> None:
    """base 仓库未提交(脏)文件与任务 files_owned 重叠 → 告警。

    worktree 从已提交 HEAD 起、跑起来安全，但**合并阶段**这些重叠文件会冲突
    （你的未提交改动 vs 任务的新改动）。提前提示先提交/stash。
    """
    code, out, _e = await run_git(base_repo, ["status", "--porcelain"])
    if code != 0:
        return
    dirty = {ln[3:].strip() for ln in out.splitlines() if ln.strip() and ln[0] != "?"}
    if not dirty:
        return
    owned: set[str] = set()
    for t in ws.tasks.values():
        if t.repo is None:  # 默认仓库 = base_repo
            owned.update(t.files_owned)
    clash = sorted(dirty & owned)
    if clash:
        print(
            "\n⚠ 警告：你有未提交改动，且与下列任务会改的文件重叠——合并阶段会冲突。"
            "\n  建议先 commit 或 stash 这些改动再跑：\n    " + "\n    ".join(clash)
        )


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
    from lwa_conduit.planner import KiroPlanner, PlanError, write_plan

    spec_path = Path(args.spec).expanduser()
    if not spec_path.is_file():
        raise SystemExit(f"spec file not found: {spec_path}")
    spec_text = spec_path.read_text(encoding="utf-8")
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"✓ planning from spec: {spec_path}")
    all_metrics_path = metrics_path(out_dir)
    prior_metrics = load_metrics(all_metrics_path)
    _apply_adaptive_recommendation(args, prior_metrics, bucket="planner")
    planner_runtime = _runtime_from_args(
        args,
        role="planner",
        adaptive_bucket="planner",
        default_kind=getattr(args, "planner_runtime_kind", "kiro-cli-acp"),
        model=args.model,
        timeout=args.timeout,
    )
    planner = KiroPlanner(
        runtime=planner_runtime,
        model=args.model,
        prompt_timeout=args.timeout,
    )
    from rich.console import Console

    console = Console()
    try:
        with console.status(
            "[bold]asking Kiro to decompose the spec into a DAG…[/] "
            "(可能要几分钟；拆错会自动喂回重拆)",
            spinner="dots",
        ):
            tasks = await planner.generate_plan(spec_text, cwd=out_dir)
            dag_path = write_plan(tasks, out_dir)
    except PlanError as exc:
        save_metrics(
            all_metrics_path,
            [
                *prior_metrics,
                RuntimeMetricRecord(
                    task_id=f"plan:{spec_path.name}",
                    task_bucket="planner",
                    runtime_kind=planner_runtime.kind,
                    model=planner_runtime.model or "(default)",
                    passed=False,
                    attempts=1,
                    files_changed=0,
                )
            ],
        )
        print(f"\n✗ planning failed: {exc}")
        return 1
    except (TimeoutError, ConnectionError) as exc:
        save_metrics(
            all_metrics_path,
            [
                *prior_metrics,
                RuntimeMetricRecord(
                    task_id=f"plan:{spec_path.name}",
                    task_bucket="planner",
                    runtime_kind=planner_runtime.kind,
                    model=planner_runtime.model or "(default)",
                    passed=False,
                    attempts=1,
                    files_changed=0,
                )
            ],
        )
        print(
            f"\n✗ planning 中断（{type(exc).__name__}）：Kiro 拆分超过了 "
            f"{args.timeout:.0f}s。大 spec 拆分较慢——用更大的 --timeout 重试"
            f"（如 --timeout 1800），或确认 kiro-cli 能正常跑。"
        )
        return 1

    save_metrics(
        all_metrics_path,
        [
            *prior_metrics,
            RuntimeMetricRecord(
                task_id=f"plan:{spec_path.name}",
                task_bucket="planner",
                runtime_kind=planner_runtime.kind,
                model=planner_runtime.model or "(default)",
                passed=True,
                attempts=1,
                files_changed=len(tasks),
            )
        ],
    )

    print(f"\n✓ generated {dag_path}  ({len(tasks)} tasks)")
    for t in tasks:
        deps = f" (after {', '.join(t.depends_on)})" if t.depends_on else ""
        print(f"  - {t.id}{deps}")
    print("\n下一步：review 上面的 dag.yaml + specs/，确认后执行：")
    print(f"  lwa-conduit run --workspace {out_dir} --base-repo <你的仓库>")
    return 0


def _print_quota_report() -> None:
    print("\n✓ runtime quota status:")
    for status in probe_all_runtime_kinds():
        ratio = ""
        if status.remaining_ratio is not None:
            ratio = f" remaining={status.remaining_ratio:.0%}"
        print(f"  {status.runtime_kind}: {status.state}{ratio} ({status.detail})")


def _report(args: argparse.Namespace) -> int:
    if getattr(args, "quota_only", False):
        _print_quota_report()
        return 0
    base_repo = Path(args.base_repo).expanduser().resolve()
    path = metrics_path(base_repo)
    records = load_metrics(path)
    if not records:
        print(f"✗ no runtime metrics found: {path}")
        if not getattr(args, "no_quota", False):
            _print_quota_report()
        return 1
    print(f"✓ runtime metrics: {path}")
    _print_runtime_metrics_report(records)
    if not getattr(args, "no_quota", False):
        _print_quota_report()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="lwa-conduit",
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
    run_p.add_argument(
        "--review-tasks", action="store_true",
        help="[expensive] also run a per-task semantic review during execution "
             "(each task reviewed against its spec, 600s timeout); --review only "
             "reviews the assembled integration (default off)",
    )
    run_p.add_argument(
        "--sandbox", action="store_true",
        help="[experimental] confine kiro-cli writes to the task worktree via an "
             "OS sandbox (macOS Seatbelt / Linux bwrap); reads+network stay open; "
             "no-op if the OS tool is unavailable (default off)",
    )
    run_p.add_argument("--max-concurrency", type=int, default=4)
    run_p.add_argument("--max-attempts", type=int, default=3)
    run_p.add_argument(
        "--adaptive-mode",
        choices=("off", "suggest", "apply-safe", "apply-aggressive"),
        default="suggest",
        help="how aggressively to apply historical runtime/model recommendations",
    )
    run_p.add_argument(
        "--kiro-cli",
        default="kiro-cli",
        help="default agent binary path (used when a role-specific bin is not set)",
    )
    run_p.add_argument(
        "--kiro-simple-tier",
        choices=("fast", "balanced", "strong", "max"),
        default="balanced",
        help="preferred Kiro model tier for simple tasks",
    )
    run_p.add_argument(
        "--kiro-medium-tier",
        choices=("fast", "balanced", "strong", "max"),
        default="strong",
        help="preferred Kiro model tier for medium tasks",
    )
    run_p.add_argument(
        "--kiro-hard-tier",
        choices=("fast", "balanced", "strong", "max"),
        default="max",
        help="preferred Kiro model tier for hard tasks",
    )
    run_p.add_argument(
        "--kiro-medium-threshold",
        type=int,
        default=4,
        help="complexity score threshold for medium Kiro routing",
    )
    run_p.add_argument(
        "--kiro-hard-threshold",
        type=int,
        default=7,
        help="complexity score threshold for hard Kiro routing",
    )
    run_p.add_argument(
        "--runtime-kind",
        choices=("kiro-cli-acp", "cursor-agent-cli", "gemini-cli"),
        default="kiro-cli-acp",
        help="default implementor runtime if no role-specific override is set",
    )
    run_p.add_argument(
        "--implementor-runtime-kind",
        choices=("kiro-cli-acp", "cursor-agent-cli", "gemini-cli"),
        default=None,
        help="runtime for task execution workers (defaults to --runtime-kind)",
    )
    run_p.add_argument(
        "--implementor-bin",
        default=None,
        help="binary for implementor runtime (e.g. agent or kiro-cli)",
    )
    run_p.add_argument(
        "--reviewer-runtime-kind",
        choices=("kiro-cli-acp", "cursor-agent-cli", "gemini-cli"),
        default="kiro-cli-acp",
        help="runtime for semantic reviewer (default: kiro-cli-acp)",
    )
    run_p.add_argument(
        "--reviewer-bin",
        default="kiro-cli",
        help="binary for semantic reviewer runtime (default: kiro-cli)",
    )
    run_p.add_argument(
        "--planner-runtime-kind",
        choices=("kiro-cli-acp", "cursor-agent-cli", "gemini-cli"),
        default="kiro-cli-acp",
        help="reserved for future plan reuse; current run path does not invoke planner",
    )
    run_p.add_argument("--resume", action="store_true", help="resume from prior run-state")
    run_p.add_argument(
        "--fresh", action="store_true",
        help="discard prior run-state and start over (overwrites old branches)",
    )
    run_p.add_argument(
        "--log-file", default=None,
        help="log file path (default: <base-repo>/.lwa-conduit/run.log; always written)",
    )
    run_p.add_argument("--dashboard", action="store_true", help="show rich TUI dashboard")
    run_p.add_argument(
        "--events",
        choices=("none", "ndjson"),
        default="none",
        help="emit structured EventBus events to stderr as NDJSON "
             "(schema lwa.conduit.event/v1); for Bridge / parent-process progress",
    )
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
    plan_p.add_argument("--kiro-cli", default="kiro-cli", help="default planner binary")
    plan_p.add_argument(
        "--adaptive-mode",
        choices=["off", "suggest", "apply-safe", "apply-aggressive"],
        default="suggest",
        help="adaptive runtime/model strategy for planner based on historical metrics",
    )
    plan_p.add_argument(
        "--kiro-simple-tier",
        choices=("fast", "balanced", "strong", "max"),
        default="balanced",
        help="preferred Kiro model tier for simple tasks",
    )
    plan_p.add_argument(
        "--kiro-medium-tier",
        choices=("fast", "balanced", "strong", "max"),
        default="strong",
        help="preferred Kiro model tier for medium tasks",
    )
    plan_p.add_argument(
        "--kiro-hard-tier",
        choices=("fast", "balanced", "strong", "max"),
        default="max",
        help="preferred Kiro model tier for hard tasks",
    )
    plan_p.add_argument(
        "--kiro-medium-threshold",
        type=int,
        default=4,
        help="complexity score threshold for medium Kiro routing",
    )
    plan_p.add_argument(
        "--kiro-hard-threshold",
        type=int,
        default=7,
        help="complexity score threshold for hard Kiro routing",
    )
    plan_p.add_argument(
        "--planner-runtime-kind",
        choices=("kiro-cli-acp", "cursor-agent-cli", "gemini-cli"),
        default="kiro-cli-acp",
        help="runtime for planning (default: kiro-cli-acp)",
    )
    plan_p.add_argument(
        "--planner-bin",
        default=None,
        help="binary for planner runtime (defaults to --kiro-cli)",
    )
    plan_p.add_argument(
        "--model", default=None, help="model id for planning (default: Kiro default)"
    )
    plan_p.add_argument(
        "--timeout", type=float, default=900.0,
        help="seconds to wait for each Kiro decomposition call (default: 900; "
             "raise it for big specs)",
    )

    report_p = sub.add_parser("report", help="show aggregated runtime/model metrics")
    report_p.add_argument(
        "--base-repo",
        required=True,
        help="git repo whose .lwa-conduit/runtime-metrics.json should be reported",
    )
    report_p.add_argument(
        "--adaptive-mode",
        choices=("off", "suggest", "apply-safe", "apply-aggressive"),
        default="suggest",
        help="report-only flag reserved for consistency with run mode",
    )
    report_p.add_argument(
        "--quota-only",
        action="store_true",
        help="only print runtime quota probe results",
    )
    report_p.add_argument(
        "--no-quota",
        action="store_true",
        help="skip runtime quota status section",
    )

    args = parser.parse_args(argv)
    if args.command == "plan":
        logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
        return _run_with_signal_handling(_plan(args))
    if args.command == "report":
        logging.basicConfig(level=logging.INFO, format=_LOG_FMT)
        return _report(args)
    # run：日志（控制台 + 文件）由 _run 内部配置（需要 base_repo 定位日志文件）
    return _run_with_signal_handling(_run(args))


def _run_with_signal_handling(coro: Coroutine[Any, Any, int]) -> int:
    """跑 coro 直到完成，SIGTERM/SIGINT 时优雅取消而不是硬退出。

    背景：不装信号处理器时，Python 收到 SIGTERM 直接终止进程——正在跑的
    `async with await AcpClient.spawn(...)` 块的 __aexit__（负责 terminate
    子进程）根本不会执行，已经 spawn 的 kiro-cli acp 子进程会变孤儿残留。

    这里把信号转换成对主 task 的 asyncio 取消：取消会像异常一样沿 await 链
    传播，途经的每个 `async with AcpClient` 块的 __aexit__ 都会正常跑到，
    子进程按现有的 terminate→5s→kill 逻辑被清理。不改动 orchestrator /
    AcpClient 本身——它们的清理路径本来就是对的，只是从未被信号触发过。
    """
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    task = loop.create_task(coro)

    def _cancel(*_: Any) -> None:
        task.cancel()

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _cancel)
        except (NotImplementedError, AttributeError):
            # Windows 等不支持 add_signal_handler 的平台：跳过，退化为默认行为
            break

    try:
        return loop.run_until_complete(task)
    except asyncio.CancelledError:
        print("\n⏹ 已中止（收到终止信号），子进程正在收尾…")
        return 130  # 128 + SIGINT，shell 惯例
    except SystemExit:
        # _run()/_plan() 内部用 `raise SystemExit(msg)` 表示"参数/前置条件错误"——
        # 调用方（包括测试）依赖 main() 继续 raise SystemExit，这里原样透传，
        # 保持跟旧版 asyncio.run() 一致的对外契约。
        # task.exception() 主动取走一次，避免 asyncio 在 GC 时打
        # "Task exception was never retrieved" 噪声（run_until_complete 抛出的
        # 是同一个异常对象，但 Task 自身的"已取走"标记要单独消费）。
        with contextlib.suppress(BaseException):
            task.exception()
        raise
    finally:
        loop.close()


if __name__ == "__main__":
    raise SystemExit(main())
