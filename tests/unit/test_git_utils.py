"""单元测试：git_utils 在临时 git repo 上的行为。"""

from __future__ import annotations

from pathlib import Path

import pytest

from kiro_conduit.git_utils import collect_diff, list_changed_files, run_git


class TestRunGit:
    @pytest.mark.asyncio
    async def test_status_on_clean_repo(self, tmp_git_repo: Path) -> None:
        code, stdout, _ = await run_git(tmp_git_repo, ["status", "--porcelain"])
        assert code == 0
        assert stdout == ""

    @pytest.mark.asyncio
    async def test_invalid_command_returns_nonzero(self, tmp_git_repo: Path) -> None:
        code, _, stderr = await run_git(tmp_git_repo, ["nope-nonexistent-cmd"])
        assert code != 0
        assert "nope-nonexistent-cmd" in stderr or "not a git command" in stderr.lower()


class TestListChangedFiles:
    @pytest.mark.asyncio
    async def test_clean_repo(self, tmp_git_repo: Path) -> None:
        files = await list_changed_files(tmp_git_repo)
        assert files == []

    @pytest.mark.asyncio
    async def test_modified_file(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "README.md").write_text("# changed\n")
        files = await list_changed_files(tmp_git_repo)
        assert files == ["README.md"]

    @pytest.mark.asyncio
    async def test_untracked_file(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "new.txt").write_text("hi")
        files = await list_changed_files(tmp_git_repo)
        assert "new.txt" in files

    @pytest.mark.asyncio
    async def test_multiple_changes(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "README.md").write_text("# changed\n")
        (tmp_git_repo / "new.txt").write_text("hi")
        files = await list_changed_files(tmp_git_repo)
        assert set(files) == {"README.md", "new.txt"}


class TestCollectDiff:
    @pytest.mark.asyncio
    async def test_clean_repo_returns_empty(self, tmp_git_repo: Path) -> None:
        diff = await collect_diff(tmp_git_repo)
        assert diff == ""

    @pytest.mark.asyncio
    async def test_tracked_modification(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "README.md").write_text("# CHANGED\n")
        diff = await collect_diff(tmp_git_repo)
        assert "README.md" in diff
        # 应该有 git diff 标志
        assert "+# CHANGED" in diff or "+++ b/README.md" in diff

    @pytest.mark.asyncio
    async def test_untracked_file_included(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "novel.py").write_text("print('hi')\n")
        diff = await collect_diff(tmp_git_repo, include_untracked=True)
        assert "novel.py" in diff

    @pytest.mark.asyncio
    async def test_untracked_excluded_when_disabled(self, tmp_git_repo: Path) -> None:
        (tmp_git_repo / "novel.py").write_text("print('hi')\n")
        diff = await collect_diff(tmp_git_repo, include_untracked=False)
        assert "novel.py" not in diff
