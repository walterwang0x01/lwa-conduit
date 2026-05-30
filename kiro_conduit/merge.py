"""串行 Merge Orchestrator：按 DAG 拓扑序串行 merge 各 task 分支回主分支。

设计要点（来自 ARCHITECTURE.md 模式 6）：
- 严格串行：行业共识"自动语义冲突解决不可靠"，所以遇到冲突就停下交人工
- 顺序：拓扑序（depends_on 在前）
- 每个分支：先在 base_repo 上 commit worktree 改动 → checkout 该分支 → rebase onto main →
  checkout main → merge --no-ff
- 失败处理：rebase / merge 冲突 → 标记冲突，停下并把信息返回给调用方
- 集成测试在每次 merge 后跑（M1.0 暂用 task.acceptance；M1.1 可单独配 integration tests）

M1.0 范围：
- 不做自动冲突解决（行业共识：不可能可靠）
- 不做 PR 创建（git 命令够用，封装 GitHub/GitLab 客户端是过度产品化）
- 不做 push 到 remote（demo 都是本地，远程推 留给用户）
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_conduit.dag import Workspace, topological_waves
from kiro_conduit.git_utils import run_git
from kiro_conduit.worktree import WorktreeHandle

if TYPE_CHECKING:
    from kiro_conduit.events import EventBus

logger = logging.getLogger(__name__)


class MergeError(RuntimeError):
    """merge 操作失败（冲突 / 命令错误等）。"""

    def __init__(self, message: str, diagnostic: MergeDiagnostic | None = None) -> None:
        super().__init__(message)
        self.diagnostic = diagnostic


@dataclass(frozen=True, slots=True)
class MergeDiagnostic:
    """一次 merge 冲突的结构化诊断（诊断模式下产出，辅助人工 review）。"""

    conflicted_files: tuple[str, ...]  # 冲突的文件路径
    detail: str  # 带冲突标记的 diff（git diff --diff-filter=U）

    def to_message(self) -> str:
        files = "\n".join(f"  - {f}" for f in self.conflicted_files) or "  (none)"
        head = f"conflicted files ({len(self.conflicted_files)}):\n{files}"
        return f"{head}\n\n{self.detail}" if self.detail else head


@dataclass(frozen=True, slots=True)
class TaskMergeResult:
    task_id: str
    merged: bool
    error: str | None = None
    diagnostic: MergeDiagnostic | None = None


@dataclass(frozen=True, slots=True)
class MergeReport:
    results: dict[str, TaskMergeResult]
    stopped_at: str | None  # 第一个失败 task 的 id（None = 全成功）

    @property
    def all_merged(self) -> bool:
        return self.stopped_at is None and all(r.merged for r in self.results.values())


class MergeOrchestrator:
    """串行 merge：按拓扑序把每个成功 task 的分支 merge 回 base 分支。

    用法：
        mo = MergeOrchestrator(workspace, base_repo)
        report = await mo.merge(handles, base_branch="main", commit_messages={...})
    """

    def __init__(
        self,
        workspace: Workspace,
        base_repo: Path,
        event_bus: EventBus | None = None,
        *,
        diagnose: bool = False,
    ) -> None:
        if not base_repo.is_absolute():
            raise ValueError(f"base_repo must be absolute, got {base_repo}")
        self._workspace = workspace
        self._base_repo = base_repo
        self._git_lock = asyncio.Lock()
        self._event_bus = event_bus
        # 诊断模式：merge 冲突时在 abort 前抓取结构化诊断（冲突文件 + 内容）
        self._diagnose = diagnose

    async def merge(
        self,
        handles: dict[str, WorktreeHandle],
        successful_task_ids: set[str],
        base_branch: str = "main",
        commit_messages: dict[str, str] | None = None,
    ) -> MergeReport:
        """按拓扑序串行 merge。

        - handles: task_id -> WorktreeHandle（来自 ParallelOrchestrator 跑出的 worktree）
        - successful_task_ids: 只 merge 这些（通常是 outcome.passed=True 的）
        - commit_messages: task_id -> commit message（worktree 内 commit 用），缺省自动生成
        """
        commit_messages = commit_messages or {}
        order = self._merge_order(successful_task_ids)
        logger.info("[merge] order: %s", order)

        results: dict[str, TaskMergeResult] = {}
        stopped_at: str | None = None

        async with self._git_lock:
            for tid in order:
                if stopped_at is not None:
                    # 一旦停下，剩下的不再尝试（保证状态可控）
                    results[tid] = TaskMergeResult(
                        task_id=tid,
                        merged=False,
                        error=f"skipped: previous task {stopped_at!r} failed",
                    )
                    continue

                handle = handles.get(tid)
                if handle is None:
                    results[tid] = TaskMergeResult(
                        task_id=tid, merged=False, error="no worktree handle"
                    )
                    stopped_at = tid
                    continue

                msg = commit_messages.get(tid, f"kiro-conduit: {tid}")
                self._publish_merge_started(tid)
                try:
                    await self._merge_one(handle, base_branch, msg)
                    results[tid] = TaskMergeResult(task_id=tid, merged=True)
                    self._publish_merge_finished(tid, merged=True, error=None)
                except MergeError as exc:
                    logger.error("[merge] %s failed: %s", tid, exc)
                    results[tid] = TaskMergeResult(
                        task_id=tid,
                        merged=False,
                        error=str(exc),
                        diagnostic=exc.diagnostic,
                    )
                    self._publish_merge_finished(tid, merged=False, error=str(exc))
                    stopped_at = tid

        return MergeReport(results=results, stopped_at=stopped_at)

    # ------------------------------------------------------------ internal

    def _publish_merge_started(self, task_id: str) -> None:
        if self._event_bus is None:
            return
        from kiro_conduit.events import MergeStarted

        self._event_bus.publish(MergeStarted(task_id=task_id))

    def _publish_merge_finished(
        self, task_id: str, merged: bool, error: str | None
    ) -> None:
        if self._event_bus is None:
            return
        from kiro_conduit.events import MergeFinished

        self._event_bus.publish(
            MergeFinished(task_id=task_id, merged=merged, error=error)
        )

    def _merge_order(self, successful: set[str]) -> list[str]:
        """对成功的 task 求拓扑序（保留 dag.py 算出来的相对顺序）。"""
        waves = topological_waves(self._workspace)
        order: list[str] = []
        for wave in waves:
            # wave 内顺序无所谓（互相不依赖），按字母序稳定输出
            for tid in sorted(wave):
                if tid in successful:
                    order.append(tid)
        return order

    async def _merge_one(
        self, handle: WorktreeHandle, base_branch: str, commit_message: str
    ) -> None:
        """对单个 worktree 做：commit -> rebase -> merge。"""
        # 1) 在 worktree 里 commit 改动（如果有）
        await self._commit_worktree(handle, commit_message)

        # 2) 切到 worktree 对应分支，rebase onto base
        # 注意：worktree 自身的工作目录是 detached/branch 状态，我们直接在 base_repo 操作分支
        code, _stdout, stderr = await run_git(
            self._base_repo, ["fetch", ".", base_branch]
        )
        # local fetch 会失败（没 remote），忽略——直接用 refs/heads/base_branch 也行

        # 3) 在 base_repo 上 checkout base_branch
        code, _stdout, stderr = await run_git(
            self._base_repo, ["checkout", base_branch]
        )
        if code != 0:
            raise MergeError(
                f"checkout {base_branch} failed: {stderr.strip()}"
            )

        # 4) merge 该 task 分支 (--no-ff 保留并行历史)
        code, stdout, stderr = await run_git(
            self._base_repo,
            [
                "merge",
                "--no-ff",
                "-m",
                commit_message,
                handle.branch,
            ],
        )
        if code != 0:
            # 冲突：诊断模式下先抓取冲突信息，再 abort 让 base 回到 clean 状态
            diagnostic = (
                await self._capture_conflict_diagnostic() if self._diagnose else None
            )
            await run_git(self._base_repo, ["merge", "--abort"])
            raise MergeError(
                f"merge {handle.branch} into {base_branch} conflicted: "
                f"{stderr.strip() or stdout.strip()}",
                diagnostic=diagnostic,
            )

    async def _capture_conflict_diagnostic(self) -> MergeDiagnostic:
        """在 merge --abort 之前抓取当前冲突状态（诊断模式专用）。"""
        _code, files_out, _err = await run_git(
            self._base_repo, ["diff", "--name-only", "--diff-filter=U"]
        )
        files = tuple(f for f in files_out.splitlines() if f.strip())
        _code, detail, _err = await run_git(
            self._base_repo, ["diff", "--diff-filter=U"]
        )
        return MergeDiagnostic(conflicted_files=files, detail=detail.strip())

    async def _commit_worktree(
        self, handle: WorktreeHandle, message: str
    ) -> None:
        """在 worktree 内 stage 全部改动并 commit。

        用 pathspec 直接排除 __pycache__ / *.pyc / .pytest_cache 等构建产物，
        防止 verifier 跑 pytest 时生成的产物被 commit 进去。
        """
        # add all，但用 pathspec 排除噪音
        # Git 的 :(exclude) 魔法 pathspec 语法
        code, _, stderr = await run_git(
            handle.path,
            [
                "add",
                "-A",
                "--",
                ".",
                ":(exclude)__pycache__",
                ":(exclude)**/__pycache__",
                ":(exclude)*.pyc",
                ":(exclude)**/*.pyc",
                ":(exclude).pytest_cache",
                ":(exclude)**/.pytest_cache",
                ":(exclude).mypy_cache",
                ":(exclude).ruff_cache",
            ],
        )
        if code != 0:
            raise MergeError(f"git add failed in {handle.path}: {stderr.strip()}")

        # 看看有没有改动
        code, stdout, _ = await run_git(handle.path, ["diff", "--cached", "--name-only"])
        if not stdout.strip():
            logger.warning(
                "[merge] task %s has no staged changes; nothing to commit",
                handle.task_id,
            )
            return

        # commit
        code, _, stderr = await run_git(
            handle.path,
            ["commit", "-m", message],
        )
        if code != 0:
            raise MergeError(f"git commit failed in {handle.path}: {stderr.strip()}")
