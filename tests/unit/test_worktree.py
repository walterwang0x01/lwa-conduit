"""单元测试：WorktreeManager。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_conduit.worktree import WorktreeError, WorktreeManager


class TestWorktreeManager:
    @pytest.mark.asyncio
    async def test_create_and_cleanup(self, tmp_git_repo: Path) -> None:
        async with WorktreeManager(tmp_git_repo) as wm:
            handle = await wm.create("task-a")
            assert handle.task_id == "task-a"
            assert handle.path.is_dir()
            assert handle.branch == "kiro-conduit/task-a"
            assert (handle.path / "README.md").exists()  # 基础提交里的文件可见
            assert wm.list_active() == [handle]

            await wm.cleanup("task-a")
            assert not handle.path.exists()
            assert wm.list_active() == []

    @pytest.mark.asyncio
    async def test_two_worktrees_isolated(self, tmp_git_repo: Path) -> None:
        async with WorktreeManager(tmp_git_repo) as wm:
            wt_a = await wm.create("task-a")
            wt_b = await wm.create("task-b")

            # 两个 worktree 路径不同
            assert wt_a.path != wt_b.path

            # A 写文件，B 看不到（工作目录隔离）
            (wt_a.path / "only-a.txt").write_text("hi")
            assert (wt_a.path / "only-a.txt").exists()
            assert not (wt_b.path / "only-a.txt").exists()

    @pytest.mark.asyncio
    async def test_create_duplicate_raises(self, tmp_git_repo: Path) -> None:
        async with WorktreeManager(tmp_git_repo) as wm:
            await wm.create("dup")
            with pytest.raises(WorktreeError, match="already exists"):
                await wm.create("dup")

    @pytest.mark.asyncio
    async def test_cleanup_nonexistent_silent(self, tmp_git_repo: Path) -> None:
        async with WorktreeManager(tmp_git_repo) as wm:
            # 没创建过的 task-id，cleanup 应该静默跳过
            await wm.cleanup("never-created")

    @pytest.mark.asyncio
    async def test_context_manager_cleans_on_exit(self, tmp_git_repo: Path) -> None:
        path: Path
        async with WorktreeManager(tmp_git_repo) as wm:
            handle = await wm.create("auto-clean")
            path = handle.path
            assert path.exists()
        # 退出 context 后应该清理掉
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_residual_branch_recovered(self, tmp_git_repo: Path) -> None:
        """模拟上次跑残留：手动建一个同名分支，再创建 worktree 应该能成功。"""
        from kiro_conduit.git_utils import run_git

        # 制造残留：创建一个 kiro-conduit/recover 分支
        code, _, _ = await run_git(
            tmp_git_repo, ["branch", "kiro-conduit/recover"]
        )
        assert code == 0

        async with WorktreeManager(tmp_git_repo) as wm:
            # 应该能成功（manager 会先清理残留分支）
            handle = await wm.create("recover")
            assert handle.path.is_dir()

    @pytest.mark.asyncio
    async def test_residual_worktree_recovered(self, tmp_git_repo: Path) -> None:
        """模拟上次进程被 kill：worktree 物理目录 + .git 元数据残留，
        再 create 同名 task 应该能先清理再重建成功。"""
        from kiro_conduit.git_utils import run_git

        root = tmp_git_repo / ".kiro-conduit" / "worktrees"
        root.mkdir(parents=True, exist_ok=True)
        residual = root / "ghost"
        # 制造残留：直接 git worktree add（不经过 manager，模拟上次跑遗留）
        code, _, _ = await run_git(
            tmp_git_repo,
            ["worktree", "add", str(residual), "-b", "kiro-conduit/ghost", "main"],
        )
        assert code == 0
        assert residual.is_dir()

        async with WorktreeManager(tmp_git_repo) as wm:
            # 同名 task：manager 应先清残留（含 .git/worktrees 元数据）再重建
            handle = await wm.create("ghost")
            assert handle.path == residual
            assert handle.path.is_dir()

    @pytest.mark.asyncio
    async def test_git_lock_serializes_operations(self, tmp_git_repo: Path) -> None:
        """同一个 manager 上并发跑多个 create，git_lock 保证不撞车。"""
        import asyncio

        async with WorktreeManager(tmp_git_repo) as wm:
            results = await asyncio.gather(
                wm.create("c1"),
                wm.create("c2"),
                wm.create("c3"),
            )
            assert {h.task_id for h in results} == {"c1", "c2", "c3"}
            assert all(h.path.is_dir() for h in results)

    @pytest.mark.asyncio
    async def test_base_repo_must_be_absolute(self) -> None:
        with pytest.raises(ValueError, match="absolute"):
            WorktreeManager(Path("relative/path"))
