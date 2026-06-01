"""并行编排器：按 DAG 波次调度 task，每波内并行跑。

设计要点（来自 ARCHITECTURE.md）：
- 输入：Workspace（已经过 dag.py 的 load_workspace 校验）
- 切波次：topological_waves(workspace)
- 每波：
  - 并行起 N 个 worker（每个 worker 一个 worktree + Implementor + Verifier + 重试）
  - asyncio.Semaphore 限并发，避免一波太宽把机器跑爆
- 任意 task 失败：默认继续跑同波其他 task，但下游波次（依赖失败 task 的）会自动跳过
- 最终返回 ParallelRunReport，含每个 task 的结果

M1.0 范围：
- 不做 merge（merge 是 step 5 的 MergeOrchestrator）
- 不做 stub-first 接口锁定（M1.1）
- 不做 dashboard / TUI（M1.1）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_conduit.dag import TaskDef, Workspace, topological_waves
from kiro_conduit.events import (
    EventBus,
    RunCompleted,
    TaskFinished,
    TaskStarted,
    WaveStarted,
)
from kiro_conduit.locks import SharedFileLockManager
from kiro_conduit.roles.coordinator import Coordinator, CoordinatorOutcome
from kiro_conduit.roles.implementor import Implementor
from kiro_conduit.roles.verifier import Verifier
from kiro_conduit.run_state import (
    RunState,
    TaskRunStatus,
    load_state,
    save_state,
    state_path,
)
from kiro_conduit.types import Task
from kiro_conduit.worktree import WorktreeHandle, WorktreeManager

if TYPE_CHECKING:
    from kiro_conduit.semantic import SemanticReviewer

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ParallelRunReport:
    """一次 run_workspace 的总结报告。"""

    outcomes: dict[str, CoordinatorOutcome]
    skipped: tuple[str, ...]  # 因上游失败被跳过的 task ids
    handles: dict[str, WorktreeHandle]  # 每个 task 的 worktree（merge 阶段用）

    @property
    def all_passed(self) -> bool:
        return not self.skipped and all(o.passed for o in self.outcomes.values())

    @property
    def passed_count(self) -> int:
        return sum(1 for o in self.outcomes.values() if o.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for o in self.outcomes.values() if not o.passed)


class ParallelOrchestrator:
    """波次并行调度器。"""

    def __init__(
        self,
        workspace: Workspace,
        base_repo: Path,
        max_concurrency: int = 4,
        max_attempts: int = 3,
        kiro_cli_path: str = "kiro-cli",
        prompt_timeout: float = 600.0,
        semantic_reviewer: SemanticReviewer | None = None,
        review_timeout: float = 180.0,
        model_routing: dict[str, str] | None = None,
        event_bus: EventBus | None = None,
        resume: bool = False,
        isolation_base_port: int = 4100,
    ) -> None:
        if not base_repo.is_absolute():
            raise ValueError(f"base_repo must be absolute, got {base_repo}")
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        self._workspace = workspace
        self._base_repo = base_repo
        self._max_concurrency = max_concurrency
        self._max_attempts = max_attempts
        self._kiro_cli_path = kiro_cli_path
        self._prompt_timeout = prompt_timeout
        self._semantic_reviewer = semantic_reviewer
        self._review_timeout = review_timeout
        # BYOA 模型路由：role 名 → model id。已知 role：'implementor'。
        # 'reviewer' role 由 semantic_reviewer 自身构造参数控制，不在此覆盖。
        # 不在 routing 里的 role 用 None（= Kiro 默认模型）。
        self._model_routing = dict(model_routing or {})
        self._event_bus = event_bus
        # resume：True 时读上次 run-state，跳过已 passed 的 task。
        # 写 state 始终开启（不受此标志控制），让任意一次跑都能被后续 resume。
        self._resume = resume
        # 运行时隔离：每个 task 的验证命令拿到一个不冲突的端口区间起点。
        self._isolation_base_port = isolation_base_port

    async def run(self, base_branch: str = "main") -> ParallelRunReport:
        """跑全工作区：所有波次依次执行，波内并行。

        注意：worktree 不会被自动清理（merge 阶段还要用）。调用方通过返回的
        ParallelRunReport.handles 拿到所有 worktree，最后自己决定何时调
        await wm.cleanup_all() 或类似清理。
        """
        waves = topological_waves(self._workspace)
        logger.info(
            "[orchestrator] %d waves total: %s",
            len(waves),
            [len(w) for w in waves],
        )

        # resume：读上次 state（仅 resume=True 时）。写 state 始终开启。
        state_file = state_path(self._base_repo)
        prior = load_state(state_file) if self._resume else None
        resume_passed = prior.passed_ids() if prior is not None else set()
        state = prior if prior is not None else RunState(base_branch=base_branch)
        if resume_passed:
            logger.info(
                "[orchestrator] resume: %d task(s) already passed, will skip: %s",
                len(resume_passed),
                sorted(resume_passed),
            )

        outcomes: dict[str, CoordinatorOutcome] = {}
        skipped: list[str] = []
        failed_tasks: set[str] = set()
        handles: dict[str, WorktreeHandle] = {}

        # per-repo WorktreeManager：repo=None 用 base_repo，其余按 workspace.repos 解析。
        # 不进 async-with，因为我们不希望它自动清理（merge 阶段还要用）。
        managers = self._build_managers()
        for mgr in managers.values():
            await mgr.__aenter__()
        try:
            lock_manager = SharedFileLockManager(
                self._workspace, self._base_repo, event_bus=self._event_bus
            )
            sem = asyncio.Semaphore(self._max_concurrency)

            for wave_idx, wave in enumerate(waves, start=1):
                wave_skipped, wave_runnable = self._partition_wave(
                    wave, failed_tasks
                )
                skipped.extend(wave_skipped)

                # resume：已 passed 的不重跑，重建 worktree + 恢复 outcome
                wave_to_run: list[str] = []
                for tid in wave_runnable:
                    if tid in resume_passed:
                        wm = managers[self._workspace.task(tid).repo]
                        wt = await wm.create(tid, reuse_branch=True)
                        handles[tid] = wt
                        outcomes[tid] = self._make_resumed_outcome(
                            tid, state.tasks[tid].attempts
                        )
                        logger.info(
                            "[orchestrator] resume: skip passed task %s "
                            "(rebuilt worktree from branch %s)",
                            tid,
                            wt.branch,
                        )
                    else:
                        wave_to_run.append(tid)

                if not wave_to_run:
                    if wave_skipped:
                        logger.warning(
                            "[orchestrator] wave %d: all skipped due to "
                            "upstream failures",
                            wave_idx,
                        )
                    self._persist_state(state, state_file, outcomes, skipped, handles)
                    continue

                logger.info(
                    "[orchestrator] wave %d/%d: running %s, skipping %s",
                    wave_idx,
                    len(waves),
                    wave_to_run,
                    wave_skipped or "[]",
                )
                self._publish(
                    WaveStarted(
                        wave_index=wave_idx,
                        total_waves=len(waves),
                        task_ids=tuple(wave_to_run),
                        skipped_ids=tuple(wave_skipped),
                    )
                )

                # 并行跑这波。把已完成 task 的 worktree handles 传给后续 task，
                # 让 Layer 4 契约校验能从 owner worktree 读 baseline。
                wave_results = await asyncio.gather(
                    *(
                        self._run_one_task(
                            self._workspace.task(tid),
                            managers[self._workspace.task(tid).repo],
                            lock_manager,
                            sem,
                            base_branch,
                            owner_handles=dict(handles),
                        )
                        for tid in wave_to_run
                    ),
                    return_exceptions=True,
                )

                for tid, result in zip(wave_to_run, wave_results, strict=True):
                    if isinstance(result, BaseException):
                        logger.exception(
                            "[orchestrator] task %s crashed: %s", tid, result
                        )
                        failed_tasks.add(tid)
                        outcomes[tid] = self._make_crash_outcome(tid, result)
                    else:
                        outcomes[tid] = result
                        if not result.passed:
                            failed_tasks.add(tid)

                # 收集这波的 handles（成功失败都收，调用方按需用）
                for tid in wave_to_run:
                    wm = managers[self._workspace.task(tid).repo]
                    h = wm._handles.get(tid)
                    if h is not None:
                        handles[tid] = h

                # 增量写 state：每波跑完落盘，崩溃后可 resume
                self._persist_state(state, state_file, outcomes, skipped, handles)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # 用户中断：保留 worktree + 分支 + run-state，下次 --resume 可续。不清理。
            logger.warning(
                "[orchestrator] interrupted — worktrees/branches kept; "
                "rerun with --resume to continue"
            )
            raise
        except BaseException:
            # 其他异常：清理防 worktree 泄漏
            for mgr in managers.values():
                await mgr.cleanup_all()
                await mgr.__aexit__(None, None, None)
            raise

        # 正常路径：不清理，留给调用方
        report = ParallelRunReport(
            outcomes=outcomes,
            skipped=tuple(skipped),
            handles=handles,
        )
        self._publish(
            RunCompleted(
                passed_count=report.passed_count,
                failed_count=report.failed_count,
                skipped_count=len(report.skipped),
            )
        )
        return report

    async def cleanup_handles(self, handles: dict[str, WorktreeHandle]) -> None:
        """显式清理 worktree（merge 完成后调用）。按 task.repo 路由到对应仓库。"""
        managers = self._build_managers()
        for tid, handle in handles.items():
            repo = self._workspace.tasks[tid].repo if tid in self._workspace.tasks else None
            managers[repo]._handles[tid] = handle
        for mgr in managers.values():
            await mgr.cleanup_all()

    # ------------------------------------------------------------ internal

    def _publish(self, event: object) -> None:
        """转发事件给可选的 EventBus（None 时无操作）。"""
        if self._event_bus is not None:
            self._event_bus.publish(event)  # type: ignore[arg-type]

    def _build_managers(self) -> dict[str | None, WorktreeManager]:
        """构建 per-repo WorktreeManager：key=None 用 base_repo（task.repo 缺省），
        其余按 workspace.repos 解析。单仓库（无 repos）时只有 None 一个 manager。"""
        managers: dict[str | None, WorktreeManager] = {
            None: WorktreeManager(self._base_repo)
        }
        for name in self._workspace.repos:
            managers[name] = WorktreeManager(self._workspace.resolved_repo_path(name))
        return managers

    def _persist_state(
        self,
        state: RunState,
        state_file: Path,
        outcomes: dict[str, CoordinatorOutcome],
        skipped: list[str],
        handles: dict[str, WorktreeHandle],
    ) -> None:
        """把当前进度整体重写进 run-state.json（幂等，每波跑完调一次）。"""
        for tid, oc in outcomes.items():
            h = handles.get(tid)
            state.record(
                tid,
                TaskRunStatus.PASSED if oc.passed else TaskRunStatus.FAILED,
                branch=h.branch if h is not None else None,
                attempts=oc.attempts,
            )
        for tid in skipped:
            state.record(tid, TaskRunStatus.SKIPPED)
        save_state(state_file, state)

    @staticmethod
    def _make_resumed_outcome(task_id: str, attempts: int) -> CoordinatorOutcome:
        """为上次已 passed 的 task 造一个 restored outcome（不重跑 CIV）。"""
        from kiro_conduit.types import TaskResult, VerifyResult

        tr = TaskResult(task_id=task_id, success=True, diff="", files_changed=[])
        vr = VerifyResult(
            task_id=task_id,
            passed=True,
            layers=[],
            feedback="resumed: passed in a prior run",
        )
        return CoordinatorOutcome(
            task_id=task_id,
            passed=True,
            attempts=attempts,
            last_task_result=tr,
            last_verify_result=vr,
            history=[(tr, vr)],
        )

    def _partition_wave(
        self, wave: list[str], failed_tasks: set[str]
    ) -> tuple[list[str], list[str]]:
        """把这波 task 分成"跳过"和"要跑"两组。

        跳过条件：task 的 effective_deps 里有 failed_tasks 命中的项。
        """
        skipped: list[str] = []
        to_run: list[str] = []
        for tid in wave:
            t = self._workspace.task(tid)
            if any(dep in failed_tasks for dep in t.depends_on):
                skipped.append(tid)
            else:
                to_run.append(tid)
        return skipped, to_run

    async def _run_one_task(
        self,
        task_def: TaskDef,
        wm: WorktreeManager,
        lock_manager: SharedFileLockManager,
        sem: asyncio.Semaphore,
        base_branch: str,
        owner_handles: dict[str, WorktreeHandle] | None = None,
    ) -> CoordinatorOutcome:
        """单 task 全流程：起 worktree → Implementor → Verifier → 重试。

        worktree 不在这里清理（merge 阶段还要用，由 ParallelOrchestrator.run
        的调用方决定何时清）。

        owner_handles: 已完成的 owner task 的 worktree handles。
        - 如果 task_def 是某个 interface_lock 的 consumer，**从 owner 分支起 worktree**，
          这样 consumer 一开始就能看到 owner 写的 stub 文件。
        - 同时把 owner 的 baseline 文件传给 Verifier 做 Layer 4 契约校验。
        """
        owner_handles = owner_handles or {}
        # 选 base：consumer 默认基于第一个 owner 的分支（M1.1 简化：每个 consumer
        # 通常只在一个 lock 里，多 owner 的复杂场景留给 M1.2）
        effective_base = self._effective_base_branch(
            task_def.id, owner_handles, base_branch
        )
        async with sem:
            wt = await wm.create(task_def.id, base_branch=effective_base)
            # 让 task 站在其依赖的真实产出之上：把每个依赖分支 merge 进本 worktree
            await self._merge_dependencies(wt, task_def, owner_handles)
            self._publish(
                TaskStarted(
                    task_id=task_def.id,
                    attempt=1,
                    max_attempts=self._max_attempts,
                )
            )
            task = self._materialize_task(task_def, wt.path)
            contract_baselines = self._collect_contract_baselines(
                task_def.id, owner_handles
            )
            coord = Coordinator(
                implementor=_LockAwareImplementor(
                    kiro_cli_path=self._kiro_cli_path,
                    prompt_timeout=self._prompt_timeout,
                    lock_manager=lock_manager,
                    shared_files=task_def.shared_files_to_modify,
                    model=self._model_routing.get("implementor"),
                ),
                verifier=Verifier(
                    contract_baselines=contract_baselines,
                    semantic_reviewer=self._semantic_reviewer,
                    review_timeout=self._review_timeout,
                ),
                max_attempts=self._max_attempts,
            )
            outcome = await coord.run_task(task)
            self._publish(
                TaskFinished(
                    task_id=task_def.id,
                    attempt=outcome.attempts,
                    passed=outcome.passed,
                    failed_layer=(
                        str(outcome.last_verify_result.failed_layer)
                        if outcome.last_verify_result.failed_layer
                        else None
                    ),
                )
            )
            # task 跑成功后立刻把改动 commit 到它自己的分支：
            # - 让下游 consumer 能基于（owner）分支起 worktree
            # - 让 review / merge 阶段分支上有内容（不再依赖 merge 阶段才 commit）
            if outcome.passed:
                await self._commit_task(wt)
            return outcome

    def _effective_base_branch(
        self,
        task_id: str,
        owner_handles: dict[str, WorktreeHandle],
        default_base: str,
    ) -> str:
        """如果 task_id 是 consumer，返回 owner 的分支；否则用默认 base。"""
        for phase in self._workspace.phases:
            for lock in phase.interface_locks:
                if task_id in lock.consumers and lock.owner in owner_handles:
                    return owner_handles[lock.owner].branch
        return default_base

    async def _merge_dependencies(
        self,
        wt: WorktreeHandle,
        task_def: TaskDef,
        owner_handles: dict[str, WorktreeHandle],
    ) -> None:
        """把 task 的每个依赖分支 merge 进它的 worktree，让它基于依赖的产出工作。

        依赖分支本身已累积了各自的（传递）依赖，所以只 merge 直接依赖即可。
        依赖间若有真实文件冲突 → abort 并抛错，作为该 task 的失败上报。
        """
        from kiro_conduit.git_utils import run_git

        for dep in task_def.depends_on:
            handle = owner_handles.get(dep)
            if handle is None:
                continue  # 依赖未完成（理论上该 task 已被上游跳过），跳过
            # 跨仓库依赖的分支不在本 task 的仓库里，无法 git merge —— 跳过
            dep_def = self._workspace.tasks.get(dep)
            if dep_def is not None and dep_def.repo != task_def.repo:
                continue
            code, _out, stderr = await run_git(
                wt.path, ["merge", "--no-edit", handle.branch]
            )
            if code != 0:
                await run_git(wt.path, ["merge", "--abort"])
                raise RuntimeError(
                    f"task {task_def.id!r} could not merge dependency "
                    f"{dep!r} ({handle.branch}): {stderr.strip()}"
                )

    async def _commit_task(self, wt: WorktreeHandle) -> None:
        """task 跑成功后把改动 commit 到它自己的分支（review / merge 都依赖它）。"""
        from kiro_conduit.git_utils import run_git

        # add 全部（用 :(exclude) 排除 __pycache__ 等噪音）
        code, _, stderr = await run_git(
            wt.path,
            [
                "add", "-A", "--",
                ".",
                ":(exclude)__pycache__",
                ":(exclude)**/__pycache__",
                ":(exclude)*.pyc",
                ":(exclude)**/*.pyc",
                ":(exclude).pytest_cache",
                ":(exclude)**/.pytest_cache",
            ],
        )
        # git add 对被目标仓库 .gitignore 忽略的路径会返回非 0，但合法文件仍会被 staged。
        # 不能因此放弃提交——下面用 diff --cached 判断是否真有内容可提交。
        if code != 0:
            logger.debug(
                "[orchestrator] add reported ignored paths for %s (continuing): %s",
                wt.task_id,
                stderr.strip(),
            )
        # 检查有没有改动
        code, stdout, _ = await run_git(wt.path, ["diff", "--cached", "--name-only"])
        if not stdout.strip():
            logger.debug(
                "[orchestrator] %s has no staged changes, skip commit",
                wt.task_id,
            )
            return
        code, _, stderr = await run_git(
            wt.path,
            ["commit", "-m", f"kiro-conduit: {wt.task_id}"],
        )
        if code != 0:
            logger.warning(
                "[orchestrator] commit failed for %s: %s",
                wt.task_id,
                stderr.strip(),
            )
            return
        logger.info(
            "[orchestrator] committed %s on branch %s",
            wt.task_id,
            wt.branch,
        )

    def _collect_contract_baselines(
        self,
        consumer_id: str,
        owner_handles: dict[str, WorktreeHandle],
    ) -> dict[str, str]:
        """给 consumer 收集"它被锁定的所有接口文件的 baseline 内容"。"""
        out: dict[str, str] = {}
        for phase in self._workspace.phases:
            for lock in phase.interface_locks:
                if consumer_id not in lock.consumers:
                    continue
                handle = owner_handles.get(lock.owner)
                if handle is None:
                    # owner 没跑成功（理论上不该到这里：consumer 跳过应在上层处理）
                    logger.warning(
                        "[orchestrator] consumer %s expects baseline from owner %s "
                        "but owner has no worktree handle",
                        consumer_id,
                        lock.owner,
                    )
                    continue
                file_path = handle.path / lock.file
                if not file_path.is_file():
                    logger.warning(
                        "[orchestrator] consumer %s baseline file missing: %s",
                        consumer_id,
                        file_path,
                    )
                    continue
                try:
                    out[lock.file] = file_path.read_text(encoding="utf-8")
                except OSError as exc:
                    logger.warning(
                        "[orchestrator] failed to read baseline %s: %s",
                        file_path,
                        exc,
                    )
        return out

    def _materialize_task(self, task_def: TaskDef, worktree_path: Path) -> Task:
        """TaskDef → Task：读 spec 文件填 prompt + 设 cwd 到 worktree。"""
        spec_path = self._workspace.workspace_root / task_def.spec
        if not spec_path.is_file():
            raise FileNotFoundError(f"spec file not found: {spec_path}")
        prompt = spec_path.read_text(encoding="utf-8")
        return Task(
            id=task_def.id,
            prompt=prompt,
            cwd=worktree_path,
            acceptance=list(task_def.acceptance),
            env=self._isolation_env(task_def.id),
        )

    def _isolation_env(self, task_id: str) -> dict[str, str]:
        """每个 task 的确定性运行时隔离 env：不冲突的端口区间 + 独立 scratch 目录。

        用户在测试/应用配置里读这些变量，避免并行 task 撞端口/DB/共享状态。
        """
        index = sorted(self._workspace.tasks).index(task_id)
        port_base = self._isolation_base_port + index * 100
        scratch = self._base_repo / ".kiro-conduit" / "scratch" / task_id
        scratch.mkdir(parents=True, exist_ok=True)
        return {
            "KIRO_CONDUIT_TASK_ID": task_id,
            "KIRO_CONDUIT_PORT_BASE": str(port_base),
            "KIRO_CONDUIT_SCRATCH": str(scratch),
        }

    @staticmethod
    def _make_crash_outcome(task_id: str, exc: BaseException) -> CoordinatorOutcome:
        """task 在 orchestrator 层崩溃时的兜底 outcome。"""
        from kiro_conduit.types import LayerResult, TaskResult, VerifyLayer, VerifyResult

        tr = TaskResult(
            task_id=task_id,
            success=False,
            diff="",
            files_changed=[],
            error=f"orchestrator crash: {type(exc).__name__}: {exc}",
        )
        vr = VerifyResult(
            task_id=task_id,
            passed=False,
            layers=[
                LayerResult(
                    layer=VerifyLayer.STATIC,
                    passed=False,
                    output=str(exc),
                )
            ],
            feedback=f"orchestrator crash: {exc}",
        )
        return CoordinatorOutcome(
            task_id=task_id,
            passed=False,
            attempts=0,
            last_task_result=tr,
            last_verify_result=vr,
            history=[(tr, vr)],
        )


# ---------------------------------------------------------------------------
# 锁感知的 Implementor 包装
# ---------------------------------------------------------------------------


class _LockAwareImplementor(Implementor):
    """在 Implementor 之外加一层：跑 prompt 前先抢所有需要的 shared file 锁。

    M1.0 简化策略：在 Implementor 整个 run 期间持锁，最大化简单性。
    （更好做法：只在 worker 真正写文件时持锁，但需要 Kiro 配合，M1.1 再优化。）
    """

    def __init__(
        self,
        *,
        kiro_cli_path: str,
        prompt_timeout: float,
        lock_manager: SharedFileLockManager,
        shared_files: tuple[str, ...],
        model: str | None = None,
    ) -> None:
        super().__init__(
            kiro_cli_path=kiro_cli_path,
            prompt_timeout=prompt_timeout,
            model=model,
        )
        self._lock_manager = lock_manager
        self._shared_files = shared_files

    async def run(self, task: Task) -> object:  # type: ignore[override]
        # 对所有需要的 shared file 依次（按字典序去抖死锁）抢锁
        sorted_files = sorted(self._shared_files)
        return await self._with_locks(sorted_files, task)

    async def _with_locks(self, files_to_lock: list[str], task: Task) -> object:
        if not files_to_lock:
            return await super().run(task)
        head, *tail = files_to_lock
        async with self._lock_manager.acquire(head, task.id):
            return await self._with_locks(tail, task)
