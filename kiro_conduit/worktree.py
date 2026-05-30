"""Git worktree 管理：每个并行 task 一个隔离的工作目录。

设计要点（来自 ARCHITECTURE.md 模式 2）：
- 每个 task 一个 worktree：路径 `<conduit-dir>/worktrees/<task-id>`
- 每个 worktree 一个独立分支：`kiro-conduit/<task-id>`
- 所有 worktree 共享同一个 .git 对象库（git worktree 的标准语义）
- **git 操作必须串行化**：多 worker 并发跑 `git fetch / pull / commit` 可能损坏 .git 元数据
  → 用一个全局 asyncio.Lock 包住所有 git 命令

M1.0 范围：
- create_worktree / cleanup_worktree / cleanup_all
- 全局 git 锁（WorktreeManager 持有）
- 错误恢复：清理失败不抛硬错（cleanup 是 best-effort）
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from kiro_conduit.git_utils import run_git

logger = logging.getLogger(__name__)


# 默认 worktree 根目录名（相对 base repo）。也可以由调用方覆盖。
DEFAULT_WORKTREE_ROOT_NAME = ".kiro-conduit"
WORKTREE_SUBDIR = "worktrees"
BRANCH_PREFIX = "kiro-conduit"


class WorktreeError(RuntimeError):
    """worktree 操作失败。"""


@dataclass(frozen=True, slots=True)
class WorktreeHandle:
    """一个已创建的 worktree 的句柄。"""

    task_id: str
    path: Path  # 工作目录绝对路径
    branch: str  # 分支名


class WorktreeManager:
    """管理一个 base repo 下的所有 worktree。

    用法：
        async with WorktreeManager(base_repo) as wm:
            wt = await wm.create("task-1", base_branch="main")
            # ... worker 在 wt.path 下干活 ...
            await wm.cleanup("task-1")

    退出 context 时，所有未清理的 worktree 都会被 cleanup（best-effort）。
    """

    def __init__(
        self,
        base_repo: Path,
        worktree_root: Path | None = None,
    ) -> None:
        if not base_repo.is_absolute():
            raise ValueError(f"base_repo must be absolute, got {base_repo}")
        self._base_repo = base_repo
        self._root = (
            worktree_root
            if worktree_root is not None
            else base_repo / DEFAULT_WORKTREE_ROOT_NAME / WORKTREE_SUBDIR
        )
        self._git_lock = asyncio.Lock()
        self._handles: dict[str, WorktreeHandle] = {}

    @property
    def base_repo(self) -> Path:
        return self._base_repo

    @property
    def git_lock(self) -> asyncio.Lock:
        """全局 git 锁。Implementor 跑 `git status` 之类不会冲突的只读命令也建议持锁，
        简化心智模型（绝大部分 git 操作都很快）。"""
        return self._git_lock

    async def __aenter__(self) -> WorktreeManager:
        self._root.mkdir(parents=True, exist_ok=True)
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.cleanup_all()

    # ----------------------------------------------------------- public api

    async def create(self, task_id: str, base_branch: str = "main") -> WorktreeHandle:
        """创建一个新 worktree（基于 base_branch）。"""
        if task_id in self._handles:
            raise WorktreeError(f"worktree for {task_id!r} already exists")

        path = self._root / task_id
        branch = f"{BRANCH_PREFIX}/{task_id}"

        # 防御：清理上次跑可能留下的残留——
        # 1. 物理 worktree 目录还在（上次 SIGKILL / 磁盘问题等）
        # 2. .git/worktrees/<task_id>/ 元数据残留
        # 3. 同名分支残留
        async with self._git_lock:
            await self._cleanup_residual_worktree(path)
            await self._cleanup_residual_branch(branch)
            code, _stdout, stderr = await run_git(
                self._base_repo,
                ["worktree", "add", str(path), "-b", branch, base_branch],
            )
            if code != 0:
                raise WorktreeError(
                    f"git worktree add failed for {task_id}: {stderr.strip()}"
                )

        handle = WorktreeHandle(task_id=task_id, path=path, branch=branch)
        self._handles[task_id] = handle
        logger.info("[worktree] created task=%s path=%s branch=%s", task_id, path, branch)
        return handle

    async def cleanup(self, task_id: str) -> None:
        """清理指定 worktree（best-effort）。"""
        handle = self._handles.pop(task_id, None)
        if handle is None:
            logger.debug("[worktree] no handle for %s, skip cleanup", task_id)
            return

        async with self._git_lock:
            # remove worktree（带 --force 以应对 worktree 内有未提交内容的情况）
            code, _stdout, stderr = await run_git(
                self._base_repo,
                ["worktree", "remove", "--force", str(handle.path)],
            )
            if code != 0:
                logger.warning(
                    "[worktree] git worktree remove failed for %s: %s",
                    task_id,
                    stderr.strip(),
                )
            # 删分支（如果还存在）
            await self._cleanup_residual_branch(handle.branch)

        # 物理路径残留也清掉（worktree remove 失败时）
        if handle.path.exists():
            with suppress(OSError):
                _force_rmtree(handle.path)
        logger.info("[worktree] cleaned task=%s", task_id)

    async def cleanup_all(self) -> None:
        for tid in list(self._handles):
            with suppress(Exception):
                await self.cleanup(tid)

    def list_active(self) -> list[WorktreeHandle]:
        return list(self._handles.values())

    # ------------------------------------------------------------ internal

    async def _cleanup_residual_worktree(self, path: Path) -> None:
        """清理上次跑残留的 worktree（物理目录 + .git 元数据）。无锁内部调用。

        场景：上次进程被 SIGKILL / 磁盘异常，没走正常 cleanup，留下：
        - 物理目录 path 还在
        - .git/worktrees/<name>/ 元数据还在——git 仍认为该路径注册着 worktree，
          导致后续 `git worktree add` 同路径直接报错
        """
        # 先让 git 按注册信息移除（处理大多数正常残留）
        if path.exists():
            await run_git(
                self._base_repo,
                ["worktree", "remove", "--force", str(path)],
            )
        # prune 掉指向已消失目录的 .git/worktrees/<name>/ 元数据
        await run_git(self._base_repo, ["worktree", "prune"])
        # worktree remove 没认这个路径（物理目录还在）就强删
        if path.exists():
            _force_rmtree(path)

    async def _cleanup_residual_branch(self, branch: str) -> None:
        """如果分支已存在（上次跑残留），强删。无锁内部调用。"""
        # 查分支是否存在
        code, _stdout, _stderr = await run_git(
            self._base_repo,
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        )
        if code == 0:
            # 删之前先 prune 可能的残留 worktree 引用
            await run_git(self._base_repo, ["worktree", "prune"])
            await run_git(self._base_repo, ["branch", "-D", branch])


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------


def _force_rmtree(path: Path) -> None:
    """强制递归删除目录。"""
    import shutil

    shutil.rmtree(path, ignore_errors=True)
