"""单元测试：MergeOrchestrator。

不调真 Kiro，但用真 git 操作（worktree 创建 + 提交 + merge）。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from kiro_conduit.dag import load_workspace
from kiro_conduit.merge import MergeOrchestrator
from kiro_conduit.worktree import WorktreeManager


def init_repo(path: Path) -> None:
    """初始化一个带初始提交的最小 git repo。"""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, capture_output=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


@pytest.fixture
def base_repo(tmp_path: Path) -> Path:
    """初始化一个真 git repo 作 base。"""
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
    (tmp_path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
    return tmp_path


def make_simple_workspace(workspace_dir: Path) -> Path:
    body = dedent(
        """
        phases:
          - name: A
            type: serial
            tasks: [t1]
          - name: B
            type: parallel
            tasks: [t2, t3]
        tasks:
          t1: {spec: specs/t1.md}
          t2: {spec: specs/t2.md, depends_on: [t1]}
          t3: {spec: specs/t3.md, depends_on: [t1]}
        shared_files: []
        """
    ).lstrip()
    p = workspace_dir / "dag.yaml"
    p.write_text(body, encoding="utf-8")
    specs = workspace_dir / "specs"
    specs.mkdir(exist_ok=True)
    for tid in ("t1", "t2", "t3"):
        (specs / f"{tid}.md").write_text(f"task {tid}\n")
    return p


class TestMergeOrchestrator:
    @pytest.mark.asyncio
    async def test_merge_single_task(self, base_repo: Path, tmp_path: Path) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            handle = await wm.create("t1")
            # 在 worktree 写一个文件
            (handle.path / "from_t1.txt").write_text("hi from t1\n")

            mo = MergeOrchestrator(ws, base_repo)
            report = await mo.merge(
                handles={"t1": handle},
                successful_task_ids={"t1"},
                base_branch="main",
            )

            assert report.all_merged
            assert report.results["t1"].merged
            assert report.stopped_at is None
            # main 分支上看到 t1 的改动
            assert (base_repo / "from_t1.txt").is_file()

    @pytest.mark.asyncio
    async def test_merge_multiple_in_topological_order(
        self, base_repo: Path, tmp_path: Path
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            ht1 = await wm.create("t1")
            (ht1.path / "t1.txt").write_text("t1\n")
            ht2 = await wm.create("t2")
            (ht2.path / "t2.txt").write_text("t2\n")
            ht3 = await wm.create("t3")
            (ht3.path / "t3.txt").write_text("t3\n")

            mo = MergeOrchestrator(ws, base_repo)
            report = await mo.merge(
                handles={"t1": ht1, "t2": ht2, "t3": ht3},
                successful_task_ids={"t1", "t2", "t3"},
            )

            assert report.all_merged
            for f in ("t1.txt", "t2.txt", "t3.txt"):
                assert (base_repo / f).is_file(), f"{f} not on main after merge"

    @pytest.mark.asyncio
    async def test_skip_unsuccessful_tasks(
        self, base_repo: Path, tmp_path: Path
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            ht1 = await wm.create("t1")
            (ht1.path / "t1.txt").write_text("t1\n")
            ht2 = await wm.create("t2")
            (ht2.path / "t2.txt").write_text("t2\n")

            mo = MergeOrchestrator(ws, base_repo)
            # 只 merge t1，不 merge t2
            report = await mo.merge(
                handles={"t1": ht1, "t2": ht2},
                successful_task_ids={"t1"},
            )

            assert report.all_merged
            assert "t1" in report.results
            assert "t2" not in report.results
            assert (base_repo / "t1.txt").is_file()
            assert not (base_repo / "t2.txt").is_file()

    @pytest.mark.asyncio
    async def test_conflict_stops_merge(
        self, base_repo: Path, tmp_path: Path
    ) -> None:
        """t2 和 t3 都改 README.md 的同一行 → t3 应该冲突。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            ht2 = await wm.create("t2")
            (ht2.path / "README.md").write_text("base\nfrom-t2\n")
            ht3 = await wm.create("t3")
            (ht3.path / "README.md").write_text("base\nfrom-t3\n")

            mo = MergeOrchestrator(ws, base_repo)
            report = await mo.merge(
                handles={"t2": ht2, "t3": ht3},
                successful_task_ids={"t2", "t3"},
            )

            # t2 应该 merge 成功，t3 冲突
            assert report.results["t2"].merged
            assert not report.results["t3"].merged
            assert "conflict" in (report.results["t3"].error or "").lower()
            assert report.stopped_at == "t3"
            assert not report.all_merged

    @pytest.mark.asyncio
    async def test_conflict_diagnostic_when_enabled(
        self, base_repo: Path, tmp_path: Path
    ) -> None:
        """诊断模式开启时，冲突的 t3 应带结构化诊断（冲突文件 + 内容）。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            ht2 = await wm.create("t2")
            (ht2.path / "README.md").write_text("base\nfrom-t2\n")
            ht3 = await wm.create("t3")
            (ht3.path / "README.md").write_text("base\nfrom-t3\n")

            mo = MergeOrchestrator(ws, base_repo, diagnose=True)
            report = await mo.merge(
                handles={"t2": ht2, "t3": ht3},
                successful_task_ids={"t2", "t3"},
            )

            diag = report.results["t3"].diagnostic
            assert diag is not None
            assert "README.md" in diag.conflicted_files
            assert "README.md" in diag.to_message()
            # base 应已回到 clean（abort 生效）
            assert (base_repo / "README.md").read_text() == "base\nfrom-t2\n"

    @pytest.mark.asyncio
    async def test_no_diagnostic_by_default(
        self, base_repo: Path, tmp_path: Path
    ) -> None:
        """默认（diagnose=False）冲突时不产出诊断，行为不变。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            ht2 = await wm.create("t2")
            (ht2.path / "README.md").write_text("base\nfrom-t2\n")
            ht3 = await wm.create("t3")
            (ht3.path / "README.md").write_text("base\nfrom-t3\n")

            mo = MergeOrchestrator(ws, base_repo)
            report = await mo.merge(
                handles={"t2": ht2, "t3": ht3},
                successful_task_ids={"t2", "t3"},
            )

            assert not report.results["t3"].merged
            assert report.results["t3"].diagnostic is None

    @pytest.mark.asyncio
    async def test_cross_repo_merge(self, base_repo: Path, tmp_path: Path) -> None:
        """t1 落 base_repo、t2 落 other 仓库，各自 merge 回自己的 main。"""
        other = tmp_path / "other"
        init_repo(other)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        body = dedent(
            f"""
            repos:
              other: {other}
            phases:
              - name: A
                type: parallel
                tasks: [t1, t2]
            tasks:
              t1: {{spec: specs/t1.md}}
              t2: {{spec: specs/t2.md, repo: other}}
            shared_files: []
            """
        ).lstrip()
        (ws_dir / "dag.yaml").write_text(body, encoding="utf-8")
        specs = ws_dir / "specs"
        specs.mkdir()
        (specs / "t1.md").write_text("t1\n")
        (specs / "t2.md").write_text("t2\n")
        ws = load_workspace(ws_dir / "dag.yaml")

        async with WorktreeManager(base_repo) as wm_base, WorktreeManager(other) as wm_other:
            h1 = await wm_base.create("t1")
            (h1.path / "t1.txt").write_text("from t1\n")
            h2 = await wm_other.create("t2")
            (h2.path / "t2.txt").write_text("from t2\n")

            mo = MergeOrchestrator(ws, base_repo)
            report = await mo.merge(
                handles={"t1": h1, "t2": h2},
                successful_task_ids={"t1", "t2"},
            )

            assert report.all_merged
            assert (base_repo / "t1.txt").is_file()
            assert (other / "t2.txt").is_file()
            # t2 的改动不该落到 base_repo
            assert not (base_repo / "t2.txt").exists()

    @pytest.mark.asyncio
    async def test_no_changes_in_worktree_skipped(
        self, base_repo: Path, tmp_path: Path
    ) -> None:
        """worktree 没改任何东西时不应崩，merge 跳过。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))

        async with WorktreeManager(base_repo) as wm:
            handle = await wm.create("t1")
            # 不写任何文件

            mo = MergeOrchestrator(ws, base_repo)
            report = await mo.merge(
                handles={"t1": handle},
                successful_task_ids={"t1"},
            )

            # M1.0 行为：没 commit 的话，merge 还是会 fast-forward 一个空分支（一致 branch tip）
            # 但这不该 fail
            assert report.results["t1"].merged or report.results["t1"].error is None

    @pytest.mark.asyncio
    async def test_constructor_validates_absolute(self, tmp_path: Path) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        ws = load_workspace(make_simple_workspace(ws_dir))
        with pytest.raises(ValueError, match="absolute"):
            MergeOrchestrator(ws, Path("relative"))
