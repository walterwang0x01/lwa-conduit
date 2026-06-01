"""单元测试：CLI（kiro_conduit.cli）。

不调真 Kiro：monkeypatch ParallelOrchestrator.run / MergeOrchestrator.merge。
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from textwrap import dedent

import pytest

from kiro_conduit.cli import _resolve_dag, _venv_path_prepend, main
from kiro_conduit.merge import MergeOrchestrator, MergeReport, TaskMergeResult
from kiro_conduit.orchestrator import ParallelOrchestrator, ParallelRunReport
from kiro_conduit.roles.coordinator import CoordinatorOutcome
from kiro_conduit.types import LayerResult, TaskResult, VerifyLayer, VerifyResult


def _write_ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "dag.yaml").write_text(
        dedent(
            """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """
        ).lstrip(),
        encoding="utf-8",
    )
    specs = ws / "specs"
    specs.mkdir()
    (specs / "t1.md").write_text("t1\n")
    # git 仓库化：让 ws 自身可作 base_repo（CLI 预检要求是 git 仓库）
    subprocess.run(["git", "init", "-b", "main"], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=ws, capture_output=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=ws, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=ws, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=ws, check=True, capture_output=True)
    return ws


def _passing(tid: str) -> CoordinatorOutcome:
    tr = TaskResult(task_id=tid, success=True, diff="", files_changed=[])
    vr = VerifyResult(
        task_id=tid,
        passed=True,
        layers=[LayerResult(layer=VerifyLayer.STATIC, passed=True, output="ok")],
        feedback="ok",
    )
    return CoordinatorOutcome(
        task_id=tid, passed=True, attempts=1,
        last_task_result=tr, last_verify_result=vr, history=[(tr, vr)],
    )


class TestPlanCommand:
    def test_plan_generates_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from kiro_conduit import planner as planner_mod
        from kiro_conduit.dag import load_workspace, topological_waves
        from kiro_conduit.planner import TaskPlan

        spec = tmp_path / "spec.md"
        spec.write_text("build a small util lib", encoding="utf-8")
        out = tmp_path / "ws"

        async def fake_generate(self, spec_text, cwd):  # type: ignore[no-untyped-def]
            assert "small util" in spec_text
            return [
                TaskPlan(id="a", prompt="build a", files_owned=["a.py"]),
                TaskPlan(id="b", prompt="build b", depends_on=["a"], files_owned=["b.py"]),
            ]

        monkeypatch.setattr(planner_mod.KiroPlanner, "generate_plan", fake_generate)
        code = main(["plan", "--spec", str(spec), "--out", str(out)])
        assert code == 0
        # 生成了可加载的 dag.yaml + specs
        ws = load_workspace(out / "dag.yaml")
        assert set(ws.tasks) == {"a", "b"}
        assert topological_waves(ws) == [["a"], ["b"]]
        assert (out / "specs" / "a.md").read_text().startswith("build a")

    def test_plan_missing_spec_errors(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="spec file not found"):
            main(["plan", "--spec", str(tmp_path / "nope.md"), "--out", str(tmp_path / "ws")])


class TestRunGuardsAndLog:
    def _prior_state(self, ws: Path) -> None:
        from kiro_conduit.run_state import RunState, TaskRunStatus, save_state, state_path

        st = RunState(base_branch="main")
        st.record("t1", TaskRunStatus.PASSED, branch="kiro-conduit/t1", attempts=1)
        save_state(state_path(ws.resolve()), st)

    def test_bare_rerun_with_prior_state_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)
        self._prior_state(ws)
        called = {"run": False}

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            called["run"] = True
            return ParallelRunReport(outcomes={}, skipped=(), handles={})

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        code = main(["run", "--workspace", str(ws)])  # 无 --resume/--fresh
        assert code == 1
        assert called["run"] is False  # 守卫拦下，没真跑

    def test_fresh_overrides_guard(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)
        self._prior_state(ws)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        assert main(["run", "--workspace", str(ws), "--fresh"]) == 0

    def test_run_writes_log_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        main(["run", "--workspace", str(ws)])
        assert (ws / ".kiro-conduit" / "run.log").is_file()


class TestResolveDag:
    def test_dir_with_dag(self, tmp_path: Path) -> None:
        ws = _write_ws(tmp_path)
        assert _resolve_dag(str(ws)) == (ws / "dag.yaml").resolve()

    def test_direct_file(self, tmp_path: Path) -> None:
        ws = _write_ws(tmp_path)
        dag = ws / "dag.yaml"
        assert _resolve_dag(str(dag)) == dag.resolve()

    def test_dir_without_dag(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="no dag"):
            _resolve_dag(str(tmp_path))

    def test_missing(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="not found"):
            _resolve_dag(str(tmp_path / "nope"))


class TestMain:
    def test_requires_subcommand(self) -> None:
        with pytest.raises(SystemExit):
            main([])

    def test_preflight_rejects_non_git_base_repo(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)
        non_git = tmp_path / "nongit"
        non_git.mkdir()

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            raise AssertionError("should not reach run(): preflight must fail first")

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        with pytest.raises(SystemExit, match="not a git repository"):
            main(["run", "--workspace", str(ws), "--base-repo", str(non_git)])

    def test_run_no_merge_exit0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        # 默认即 review 模式（不合并）
        code = main(["run", "--workspace", str(ws)])
        assert code == 0

    def test_run_full_invokes_merge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)
        merged: dict[str, bool] = {}

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        async def fake_merge(  # type: ignore[no-untyped-def]
            self, handles, successful_task_ids, base_branch="main", commit_messages=None
        ):
            merged["called"] = True
            return MergeReport(
                results={"t1": TaskMergeResult(task_id="t1", merged=True)},
                stopped_at=None,
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        monkeypatch.setattr(MergeOrchestrator, "merge", fake_merge)
        code = main(["run", "--workspace", str(ws), "--merge"])
        assert code == 0
        assert merged.get("called") is True

    def test_base_branch_defaults_to_current_branch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=repo, capture_output=True)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo, capture_output=True)
        (repo / "f").write_text("x")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "checkout", "-b", "feature/x"], cwd=repo, check=True, capture_output=True
        )

        captured: dict[str, str] = {}

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            captured["bb"] = base_branch
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        code = main(["run", "--workspace", str(ws), "--base-repo", str(repo)])
        assert code == 0
        assert captured["bb"] == "feature/x"  # 跟随当前分支，不是 main

    def test_run_failed_tasks_merge_passed_exit1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            # t1 通过、t2 跳过 → all_passed False，但仍应合并 t1
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=("t2",), handles={}
            )

        seen: dict[str, object] = {}

        async def fake_merge(  # type: ignore[no-untyped-def]
            self, handles, successful_task_ids, base_branch="main", commit_messages=None
        ):
            seen["ids"] = set(successful_task_ids)
            return MergeReport(
                results={"t1": TaskMergeResult(task_id="t1", merged=True)},
                stopped_at=None,
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        monkeypatch.setattr(MergeOrchestrator, "merge", fake_merge)
        # 部分失败 + --merge：仍合并通过的（只传 t1），但退出码非 0
        code = main(["run", "--workspace", str(ws), "--merge"])
        assert code == 1
        assert seen["ids"] == {"t1"}  # 只合通过的，不含跳过的 t2


class TestSummaryTable:
    """跑完的 per-task 汇总表。"""

    def test_report_shows_model_and_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        ws = _write_ws(tmp_path)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        main(["run", "--workspace", str(ws)])  # review 模式
        out = capsys.readouterr().out
        assert "model" in out and "att" in out  # 表头
        assert "<default>" in out  # t1 没声明模型 → <default>


class TestReviewFlag:
    """--review：只做合并后的集成级初审，不再把 per-task 语义审接进 orchestrator。"""

    def _spy_reviewer(
        self, monkeypatch: pytest.MonkeyPatch, ws: Path, argv: list[str]
    ) -> object:
        captured: dict[str, object] = {}
        orig_init = ParallelOrchestrator.__init__

        def spy_init(self, *a, **k):  # type: ignore[no-untyped-def]
            captured["reviewer"] = k.get("semantic_reviewer")
            orig_init(self, *a, **k)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "__init__", spy_init)
        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        main(["run", "--workspace", str(ws), *argv])
        return captured["reviewer"]

    def test_review_does_not_wire_per_task_reviewer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # --review 不再给 orchestrator 接 per-task 语义审（避免 180s 超时）；
        # 评审只在合并后的集成级做。
        assert self._spy_reviewer(monkeypatch, _write_ws(tmp_path), ["--review"]) is None

    def test_no_review_by_default(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        assert self._spy_reviewer(monkeypatch, _write_ws(tmp_path), []) is None


class TestIntegrationCheck:
    """_integration_check：在集成结果上跑全量验证命令。"""

    def _repo(self, tmp_path: Path) -> Path:
        repo = tmp_path / "repo"
        repo.mkdir()
        for a in (
            ["init", "-b", "main"], ["config", "user.email", "t@t.com"],
            ["config", "user.name", "t"],
        ):
            subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)
        (repo / "f.py").write_text("x = 1\n")
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True, capture_output=True)
        return repo

    @pytest.mark.asyncio
    async def test_passing_command_returns_true(self, tmp_path: Path) -> None:
        from kiro_conduit.cli import _integration_check
        from kiro_conduit.dag import Workspace

        repo = self._repo(tmp_path)
        ws = Workspace(phases=(), tasks={}, shared_files=(), workspace_root=repo,
                       integration_check="true")
        assert await _integration_check(ws, repo, "main") is True

    @pytest.mark.asyncio
    async def test_failing_command_returns_false(self, tmp_path: Path) -> None:
        from kiro_conduit.cli import _integration_check
        from kiro_conduit.dag import Workspace

        repo = self._repo(tmp_path)
        ws = Workspace(phases=(), tasks={}, shared_files=(), workspace_root=repo,
                       integration_check="false")
        assert await _integration_check(ws, repo, "main") is False

    @pytest.mark.asyncio
    async def test_none_when_unset(self, tmp_path: Path) -> None:
        from kiro_conduit.cli import _integration_check
        from kiro_conduit.dag import Workspace

        repo = self._repo(tmp_path)
        ws = Workspace(phases=(), tasks={}, shared_files=(), workspace_root=repo)
        assert await _integration_check(ws, repo, "main") is None


class TestVenvPathPrepend:
    """--venv：把 venv/bin 前置到 PATH。"""

    def test_prepends_existing_venv_bin(self, tmp_path: Path) -> None:
        (tmp_path / "bin").mkdir()
        out = _venv_path_prepend(tmp_path, "/usr/bin:/bin")
        assert out == f"{(tmp_path / 'bin').resolve()}:/usr/bin:/bin"

    def test_rejects_missing_venv(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit, match="venv"):
            _venv_path_prepend(tmp_path / "nope", "/usr/bin")
