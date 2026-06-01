"""单元测试：ParallelOrchestrator。

不调真 Kiro：用 monkeypatch 替换 ParallelOrchestrator._run_one_task 直接产出结果。
这样能测到 wave 调度、跳过逻辑、失败传播，而不依赖任何外部进程。
"""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable
from pathlib import Path
from textwrap import dedent

import pytest

from kiro_conduit.dag import load_workspace
from kiro_conduit.orchestrator import ParallelOrchestrator
from kiro_conduit.roles.coordinator import CoordinatorOutcome
from kiro_conduit.types import LayerResult, TaskResult, VerifyLayer, VerifyResult

# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def real_repo(tmp_path: Path) -> Path:
    """初始化一个真 git repo（worktree manager 需要）。"""
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t.com",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t.com"}
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=tmp_path, check=True, capture_output=True
    )
    for k, v in env.items():
        subprocess.run(["git", "config", k.lower().replace("git_", "").replace("_", "."), v],
                       cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, capture_output=True)
    (tmp_path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=tmp_path, check=True, capture_output=True,
        env={**dict(__import__("os").environ), **env},
    )
    return tmp_path


def write_workspace_with_specs(tmp_path: Path, dag_yaml: str) -> Path:
    """把 dag.yaml 写到 tmp_path，并为每个 task 创建空的 spec 文件。"""
    dag = tmp_path / "dag.yaml"
    dag.write_text(dedent(dag_yaml).lstrip(), encoding="utf-8")
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir(exist_ok=True)
    # 简单粗暴：扫一遍 yaml 找出所有 spec: 路径，建空文件
    import re
    for m in re.finditer(r"spec:\s*(\S+)", dag.read_text()):
        target = tmp_path / m.group(1)
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(f"# stub spec for testing: {m.group(1)}\n")
    return dag


def init_git_repo(path: Path) -> None:
    """初始化一个带初始提交的最小 git repo。"""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, capture_output=True)
    (path / "README.md").write_text("base\n")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


# ---------------------------------------------------------------------------
# Helpers: 用 monkeypatch 替换 _run_one_task
# ---------------------------------------------------------------------------


def make_outcome(task_id: str, *, passed: bool) -> CoordinatorOutcome:
    tr = TaskResult(task_id=task_id, success=passed, diff="x", files_changed=["f.py"])
    vr = VerifyResult(
        task_id=task_id,
        passed=passed,
        layers=[LayerResult(layer=VerifyLayer.STATIC, passed=passed, output="ok")],
        feedback="ok" if passed else "fail",
    )
    return CoordinatorOutcome(
        task_id=task_id,
        passed=passed,
        attempts=1,
        last_task_result=tr,
        last_verify_result=vr,
        history=[(tr, vr)],
    )


def fake_run_factory(
    behaviors: dict[str, bool],
    started_order: list[str],
) -> Callable[..., object]:
    """生成一个能塞进 ParallelOrchestrator._run_one_task 的 fake。"""

    async def fake(self, task_def, wm, lock_manager, sem, base_branch, owner_handles=None):  # type: ignore[no-untyped-def]
        async with sem:
            started_order.append(task_def.id)
            await asyncio.sleep(0.01)  # 模拟工作
            return make_outcome(task_def.id, passed=behaviors.get(task_def.id, True))

    return fake


# ---------------------------------------------------------------------------
# tests
# ---------------------------------------------------------------------------


class TestParallelOrchestrator:
    @pytest.mark.asyncio
    async def test_all_pass_simple(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
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
            """,
        )
        ws = load_workspace(dag)

        started: list[str] = []
        monkeypatch.setattr(
            ParallelOrchestrator,
            "_run_one_task",
            fake_run_factory({"t1": True, "t2": True, "t3": True}, started),
        )

        orch = ParallelOrchestrator(ws, real_repo, max_concurrency=4)
        report = await orch.run()

        assert report.all_passed
        assert report.passed_count == 3
        assert report.failed_count == 0
        assert set(report.outcomes) == {"t1", "t2", "t3"}
        # t1 必须先于 t2/t3 启动
        assert started[0] == "t1"
        assert set(started[1:]) == {"t2", "t3"}

    @pytest.mark.asyncio
    async def test_failure_propagates_to_dependents(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
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
            """,
        )
        ws = load_workspace(dag)

        started: list[str] = []
        monkeypatch.setattr(
            ParallelOrchestrator,
            "_run_one_task",
            fake_run_factory({"t1": False}, started),
        )

        orch = ParallelOrchestrator(ws, real_repo, max_concurrency=4)
        report = await orch.run()

        # t1 失败，t2 t3 应被跳过
        assert report.outcomes["t1"].passed is False
        assert "t2" in report.skipped
        assert "t3" in report.skipped
        # t2 t3 没真正跑过
        assert started == ["t1"]

    @pytest.mark.asyncio
    async def test_partial_failure_independent_branch_runs(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同波内 t2 失败不影响并行的 t3。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: parallel
                tasks: [t2, t3]
            tasks:
              t2: {spec: specs/t2.md}
              t3: {spec: specs/t3.md}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)

        started: list[str] = []
        monkeypatch.setattr(
            ParallelOrchestrator,
            "_run_one_task",
            fake_run_factory({"t2": False, "t3": True}, started),
        )

        orch = ParallelOrchestrator(ws, real_repo)
        report = await orch.run()

        assert report.outcomes["t2"].passed is False
        assert report.outcomes["t3"].passed is True
        assert report.skipped == ()

    @pytest.mark.asyncio
    async def test_concurrency_limit(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """max_concurrency=1 时同波 task 也变成串行。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: parallel
                tasks: [a, b, c]
            tasks:
              a: {spec: specs/a.md}
              b: {spec: specs/b.md}
              c: {spec: specs/c.md}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)

        # 用一个能记录"同时在跑"数量的 fake
        in_flight = 0
        max_seen = 0
        lock = asyncio.Lock()

        async def fake(self, task_def, wm, lock_manager, sem, base_branch, owner_handles=None):  # type: ignore[no-untyped-def]
            nonlocal in_flight, max_seen
            async with sem:
                async with lock:
                    in_flight += 1
                    max_seen = max(max_seen, in_flight)
                await asyncio.sleep(0.05)
                async with lock:
                    in_flight -= 1
                return make_outcome(task_def.id, passed=True)

        monkeypatch.setattr(ParallelOrchestrator, "_run_one_task", fake)

        orch = ParallelOrchestrator(ws, real_repo, max_concurrency=1)
        report = await orch.run()
        assert report.all_passed
        assert max_seen == 1, f"max_concurrency=1 not respected, peak={max_seen}"

    @pytest.mark.asyncio
    async def test_resume_skips_passed_tasks(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """第一次跑 t1 过、t2 挂；resume 第二次应跳过 t1（不重跑），只重跑 t2。"""
        from kiro_conduit.git_utils import run_git
        from kiro_conduit.run_state import load_state, state_path

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
              - name: B
                type: serial
                tasks: [t2]
            tasks:
              t1: {spec: specs/t1.md}
              t2: {spec: specs/t2.md, depends_on: [t1]}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)

        def real_wt_fake(
            behaviors: dict[str, bool], started: list[str]
        ) -> Callable[..., object]:
            async def fake(self, task_def, wm, lock_manager, sem, base_branch, owner_handles=None):  # type: ignore[no-untyped-def]
                async with sem:
                    started.append(task_def.id)
                    wt = await wm.create(task_def.id, base_branch=base_branch)
                    passed = behaviors.get(task_def.id, True)
                    if passed:
                        (wt.path / f"{task_def.id}.txt").write_text("done\n")
                        await run_git(wt.path, ["add", "-A"])
                        await run_git(wt.path, ["commit", "-m", f"{task_def.id} done"])
                    return make_outcome(task_def.id, passed=passed)

            return fake

        # 第一次跑：t1 过，t2 挂
        started1: list[str] = []
        monkeypatch.setattr(
            ParallelOrchestrator, "_run_one_task",
            real_wt_fake({"t1": True, "t2": False}, started1),
        )
        report1 = await ParallelOrchestrator(ws, real_repo).run()
        assert report1.outcomes["t1"].passed
        assert not report1.outcomes["t2"].passed

        # state 落盘了，且只有 t1 passed
        st = load_state(state_path(real_repo))
        assert st is not None
        assert st.passed_ids() == {"t1"}

        # 第二次跑 resume=True：t1 不重跑（restored），t2 重跑且这次过
        started2: list[str] = []
        monkeypatch.setattr(
            ParallelOrchestrator, "_run_one_task",
            real_wt_fake({"t2": True}, started2),
        )
        report2 = await ParallelOrchestrator(ws, real_repo, resume=True).run()

        assert started2 == ["t2"], f"只应重跑 t2，实际跑了 {started2}"
        assert report2.outcomes["t1"].passed  # restored
        assert report2.outcomes["t2"].passed  # 重跑通过
        assert report2.all_passed
        # t1 的 worktree 被 resume 重建，handle 可供 merge 阶段用
        assert "t1" in report2.handles

    @pytest.mark.asyncio
    async def test_cross_repo_routing(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """t1（默认仓库）落 base_repo，t2（repo: other）落 other 仓库。"""
        other = tmp_path / "other_repo"
        init_git_repo(other)

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
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
            """,
        )
        ws = load_workspace(dag)

        async def fake(self, task_def, wm, lock_manager, sem, base_branch, owner_handles=None):  # type: ignore[no-untyped-def]
            async with sem:
                await wm.create(task_def.id, base_branch=base_branch)
                return make_outcome(task_def.id, passed=True)

        monkeypatch.setattr(ParallelOrchestrator, "_run_one_task", fake)

        report = await ParallelOrchestrator(ws, real_repo).run()

        assert report.all_passed
        assert str(report.handles["t1"].path).startswith(str(real_repo))
        assert str(report.handles["t2"].path).startswith(str(other))

    @pytest.mark.asyncio
    async def test_commit_task_commits_work_to_its_branch(
        self, real_repo: Path, tmp_path: Path
    ) -> None:
        """_commit_task：task 通过后改动被提交到它自己的分支（review/merge 都依赖此）。"""
        from kiro_conduit.worktree import WorktreeManager

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)
        orch = ParallelOrchestrator(ws, real_repo)
        async with WorktreeManager(real_repo) as wm:
            wt = await wm.create("t1")
            (wt.path / "new.py").write_text("x = 1\n")
            await orch._commit_task(wt)
            # 在 cleanup（会删分支）之前断言：分支上已有提交
            out = subprocess.run(
                ["git", "show", "kiro-conduit/t1:new.py"],
                cwd=real_repo, capture_output=True, text=True,
            )
        assert out.returncode == 0
        assert out.stdout == "x = 1\n"

    @pytest.mark.asyncio
    async def test_isolation_env_is_deterministic_and_distinct(
        self, real_repo: Path, tmp_path: Path
    ) -> None:
        """每个 task 拿到确定性、不冲突的隔离 env（端口区间不重叠 + 独立 scratch + task-id）。"""
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: parallel
                tasks: [a, b, c]
            tasks:
              a: {spec: specs/a.md}
              b: {spec: specs/b.md}
              c: {spec: specs/c.md}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)
        orch = ParallelOrchestrator(ws, real_repo, isolation_base_port=5000)

        envs = {tid: orch._isolation_env(tid) for tid in ("a", "b", "c")}
        # task-id 注入
        assert envs["a"]["KIRO_CONDUIT_TASK_ID"] == "a"
        # 端口区间按字母序索引递增、互不重叠
        ports = {tid: int(e["KIRO_CONDUIT_PORT_BASE"]) for tid, e in envs.items()}
        assert ports == {"a": 5000, "b": 5100, "c": 5200}
        # scratch 各自独立且已创建
        scratches = {e["KIRO_CONDUIT_SCRATCH"] for e in envs.values()}
        assert len(scratches) == 3
        assert all(Path(s).is_dir() for s in scratches)
        # 确定性：再算一次结果一致
        assert orch._isolation_env("a") == envs["a"]

    @pytest.mark.asyncio
    async def test_merge_dependencies_brings_in_dep_code(
        self, real_repo: Path, tmp_path: Path
    ) -> None:
        """依赖的产出应被 merge 进 task 的 worktree（task 站在依赖之上工作）。"""
        from kiro_conduit.git_utils import run_git
        from kiro_conduit.worktree import WorktreeManager

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
              - name: B
                type: serial
                tasks: [t2]
            tasks:
              t1: {spec: specs/t1.md}
              t2: {spec: specs/t2.md, depends_on: [t1]}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)
        orch = ParallelOrchestrator(ws, real_repo)
        async with WorktreeManager(real_repo) as wm:
            # t1 产出 depfile.py 并提交到自己分支
            wt1 = await wm.create("t1")
            (wt1.path / "depfile.py").write_text("DEP = 1\n")
            await run_git(wt1.path, ["add", "-A"])
            await run_git(wt1.path, ["commit", "-m", "t1 output"])
            # t2 从 base 起，本身没有 depfile
            wt2 = await wm.create("t2")
            assert not (wt2.path / "depfile.py").exists()
            # merge 依赖后，t2 的 worktree 应能看到 t1 的产出
            await orch._merge_dependencies(wt2, ws.task("t2"), {"t1": wt1})
            assert (wt2.path / "depfile.py").read_text() == "DEP = 1\n"

    @pytest.mark.asyncio
    async def test_commit_task_survives_gitignored_files(
        self, real_repo: Path, tmp_path: Path
    ) -> None:
        """worktree 里有被 .gitignore 忽略的文件（git add 返回非 0）时，
        仍应提交合法文件，而不是放弃（修复 add-failed→空分支 的 bug）。"""
        from kiro_conduit.worktree import WorktreeManager

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)
        orch = ParallelOrchestrator(ws, real_repo)
        async with WorktreeManager(real_repo) as wm:
            wt = await wm.create("t1")
            (wt.path / ".gitignore").write_text("*.log\n")
            (wt.path / "real.py").write_text("x = 1\n")
            (wt.path / "debug.log").write_text("noise\n")  # 被忽略，会让 git add 报非 0
            await orch._commit_task(wt)
            # 合法文件应已提交到分支，noise 不在
            out = subprocess.run(
                ["git", "show", "kiro-conduit/t1:real.py"],
                cwd=real_repo, capture_output=True, text=True,
            )
            assert out.returncode == 0 and out.stdout == "x = 1\n"
            ignored = subprocess.run(
                ["git", "show", "kiro-conduit/t1:debug.log"],
                cwd=real_repo, capture_output=True, text=True,
            )
            assert ignored.returncode != 0  # 被忽略的文件没进提交

    @pytest.mark.asyncio
    async def test_interrupt_keeps_branches_for_resume(
        self, real_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """中断时不清理：已完成 task 的分支应保留，供 --resume 续跑。"""
        import asyncio

        from kiro_conduit.git_utils import run_git

        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
              - name: B
                type: serial
                tasks: [t2]
            tasks:
              t1: {spec: specs/t1.md}
              t2: {spec: specs/t2.md, depends_on: [t1]}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)

        async def fake(self, task_def, wm, lock_manager, sem, base_branch, owner_handles=None):  # type: ignore[no-untyped-def]
            async with sem:
                wt = await wm.create(task_def.id, base_branch=base_branch)
                (wt.path / f"{task_def.id}.txt").write_text("done\n")
                await run_git(wt.path, ["add", "-A"])
                await run_git(wt.path, ["commit", "-m", f"{task_def.id} done"])
                return make_outcome(task_def.id, passed=True)

        monkeypatch.setattr(ParallelOrchestrator, "_run_one_task", fake)

        # 模拟中断：第一波跑完、t1 分支已建后，在落盘处抛中断（等价于 await 点被 Ctrl-C 打断）
        def boom(self, *a, **k):  # type: ignore[no-untyped-def]
            raise asyncio.CancelledError

        monkeypatch.setattr(ParallelOrchestrator, "_persist_state", boom)

        with pytest.raises(asyncio.CancelledError):
            await ParallelOrchestrator(ws, real_repo).run()

        # 中断后 t1 的分支仍在（没被清理）→ resume 可复用
        code, _out, _err = await run_git(
            real_repo, ["show-ref", "--verify", "--quiet", "refs/heads/kiro-conduit/t1"]
        )
        assert code == 0, "interrupt should keep completed task branch for resume"

    @pytest.mark.asyncio
    async def test_constructor_validation(self, real_repo: Path, tmp_path: Path) -> None:
        ws_dir = tmp_path / "ws"
        ws_dir.mkdir()
        dag = write_workspace_with_specs(
            ws_dir,
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        ws = load_workspace(dag)

        with pytest.raises(ValueError, match="absolute"):
            ParallelOrchestrator(ws, Path("relative"))
        with pytest.raises(ValueError, match="max_concurrency"):
            ParallelOrchestrator(ws, real_repo, max_concurrency=0)
