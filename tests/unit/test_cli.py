"""单元测试：CLI（kiro_conduit.cli）。

不调真 Kiro：monkeypatch ParallelOrchestrator.run / MergeOrchestrator.merge。
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from kiro_conduit.cli import _resolve_dag, main
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

    def test_run_no_merge_exit0(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=(), handles={}
            )

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        code = main(["run", "--workspace", str(ws), "--base-repo", str(tmp_path), "--no-merge"])
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
        code = main(["run", "--workspace", str(ws), "--base-repo", str(tmp_path)])
        assert code == 0
        assert merged.get("called") is True

    def test_run_failed_tasks_skip_merge_exit1(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ws = _write_ws(tmp_path)

        async def fake_run(self, base_branch: str = "main") -> ParallelRunReport:  # type: ignore[no-untyped-def]
            # 有 skipped → all_passed False
            return ParallelRunReport(
                outcomes={"t1": _passing("t1")}, skipped=("t2",), handles={}
            )

        called = {"merge": False}

        async def fake_merge(self, *a, **k):  # type: ignore[no-untyped-def]
            called["merge"] = True
            return MergeReport(results={}, stopped_at=None)

        monkeypatch.setattr(ParallelOrchestrator, "run", fake_run)
        monkeypatch.setattr(MergeOrchestrator, "merge", fake_merge)
        code = main(["run", "--workspace", str(ws), "--base-repo", str(tmp_path)])
        assert code == 1
        assert called["merge"] is False  # 失败时不该进 merge
