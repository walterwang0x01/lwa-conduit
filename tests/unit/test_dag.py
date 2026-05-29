"""单元测试：dag.py 的 parser / validator / topological_waves。"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from kiro_conduit.dag import (
    DagError,
    PhaseType,
    SharedFilePolicy,
    load_workspace,
    topological_waves,
)

# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------


def write_dag(tmp_path: Path, body: str) -> Path:
    """把 yaml 字符串写到临时目录的 dag.yaml，返回路径。"""
    p = tmp_path / "dag.yaml"
    p.write_text(dedent(body).lstrip(), encoding="utf-8")
    return p


def minimal_yaml() -> str:
    """最小可解析的合法 yaml。"""
    return """
        phases:
          - name: only
            type: serial
            tasks: [t1]
        tasks:
          t1:
            spec: specs/t1.md
            depends_on: []
            files_owned: []
            shared_files_to_modify: []
            acceptance: []
        shared_files: []
    """


# ---------------------------------------------------------------------------
# 加载 + parse
# ---------------------------------------------------------------------------


class TestLoadWorkspace:
    def test_minimal(self, tmp_path: Path) -> None:
        p = write_dag(tmp_path, minimal_yaml())
        ws = load_workspace(p)
        assert len(ws.phases) == 1
        assert ws.phases[0].name == "only"
        assert ws.phases[0].type == PhaseType.SERIAL
        assert ws.phases[0].task_ids == ("t1",)
        assert "t1" in ws.tasks
        assert ws.tasks["t1"].spec == "specs/t1.md"
        assert ws.workspace_root == tmp_path.resolve()

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(DagError, match="not found"):
            load_workspace(tmp_path / "nope.yaml")

    def test_invalid_yaml(self, tmp_path: Path) -> None:
        p = tmp_path / "dag.yaml"
        p.write_text("phases: [unclosed", encoding="utf-8")
        with pytest.raises(DagError, match="invalid YAML"):
            load_workspace(p)

    def test_top_level_must_be_mapping(self, tmp_path: Path) -> None:
        p = write_dag(tmp_path, "- just a list\n- of items\n")
        with pytest.raises(DagError, match="top-level must be a mapping"):
            load_workspace(p)


class TestParsePhases:
    def test_phases_required(self, tmp_path: Path) -> None:
        p = write_dag(tmp_path, "tasks:\n  t1:\n    spec: x\nshared_files: []\n")
        with pytest.raises(DagError, match="phases must not be empty"):
            load_workspace(p)

    def test_phase_type_invalid(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: bad
                type: weird
                tasks: [t1]
            tasks:
              t1:
                spec: s
            shared_files: []
        """
        with pytest.raises(DagError, match="type must be one of"):
            load_workspace(write_dag(tmp_path, body))

    def test_duplicate_phase_name(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
              - name: A
                type: serial
                tasks: [t2]
            tasks:
              t1: {spec: s1}
              t2: {spec: s2}
            shared_files: []
        """
        with pytest.raises(DagError, match="duplicate phase name"):
            load_workspace(write_dag(tmp_path, body))


class TestParseTasks:
    def test_task_missing_spec(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {}
            shared_files: []
        """
        with pytest.raises(DagError, match="missing spec"):
            load_workspace(write_dag(tmp_path, body))

    def test_task_max_lines_validation(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1:
                spec: s
                max_lines: -1
            shared_files: []
        """
        with pytest.raises(DagError, match="max_lines"):
            load_workspace(write_dag(tmp_path, body))


class TestParseSharedFiles:
    def test_unknown_policy(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: s}
            shared_files:
              - path: src/x.py
                policy: bogus
        """
        with pytest.raises(DagError, match="policy must be one of"):
            load_workspace(write_dag(tmp_path, body))

    def test_m1_0_only_single_writer(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: s}
            shared_files:
              - path: src/x.py
                policy: append-only
        """
        with pytest.raises(DagError, match=r"not supported in M1\.0"):
            load_workspace(write_dag(tmp_path, body))

    def test_duplicate_shared_path(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: s}
            shared_files:
              - path: src/x.py
                policy: single-writer
              - path: src/x.py
                policy: single-writer
        """
        with pytest.raises(DagError, match="duplicate shared_file path"):
            load_workspace(write_dag(tmp_path, body))


# ---------------------------------------------------------------------------
# validate（语义校验）
# ---------------------------------------------------------------------------


class TestValidate:
    def test_phase_references_unknown_task(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [ghost]
            tasks:
              t1: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="unknown task 'ghost'"):
            load_workspace(write_dag(tmp_path, body))

    def test_task_in_two_phases(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
              - name: B
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="multiple phases"):
            load_workspace(write_dag(tmp_path, body))

    def test_orphan_task(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: s}
              t2: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="not in any phase"):
            load_workspace(write_dag(tmp_path, body))

    def test_depends_on_unknown(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1:
                spec: s
                depends_on: [ghost]
            shared_files: []
        """
        with pytest.raises(DagError, match="depends on unknown task"):
            load_workspace(write_dag(tmp_path, body))

    def test_depends_on_self(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1:
                spec: s
                depends_on: [t1]
            shared_files: []
        """
        with pytest.raises(DagError, match="depends on itself"):
            load_workspace(write_dag(tmp_path, body))

    def test_dependency_cycle(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: parallel
                tasks: [t1, t2]
            tasks:
              t1:
                spec: s
                depends_on: [t2]
              t2:
                spec: s
                depends_on: [t1]
            shared_files: []
        """
        with pytest.raises(DagError, match="cycle detected"):
            load_workspace(write_dag(tmp_path, body))

    def test_files_owned_overlap(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: parallel
                tasks: [t1, t2]
            tasks:
              t1:
                spec: s
                files_owned: ["src/x.py"]
              t2:
                spec: s
                files_owned: ["src/x.py"]
            shared_files: []
        """
        with pytest.raises(DagError, match="owned by both"):
            load_workspace(write_dag(tmp_path, body))

    def test_shared_file_not_declared(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1:
                spec: s
                shared_files_to_modify: ["src/constants.py"]
            shared_files: []
        """
        with pytest.raises(DagError, match="not declared"):
            load_workspace(write_dag(tmp_path, body))


# ---------------------------------------------------------------------------
# 拓扑波次
# ---------------------------------------------------------------------------


class TestTopologicalWaves:
    def test_single_task(self, tmp_path: Path) -> None:
        ws = load_workspace(write_dag(tmp_path, minimal_yaml()))
        assert topological_waves(ws) == [["t1"]]

    def test_serial_phase_forces_order(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1, t2, t3]
            tasks:
              t1: {spec: s}
              t2: {spec: s}
              t3: {spec: s}
            shared_files: []
        """
        ws = load_workspace(write_dag(tmp_path, body))
        # serial phase 必须串行：每个波次只 1 个
        assert topological_waves(ws) == [["t1"], ["t2"], ["t3"]]

    def test_parallel_phase_one_wave(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: parallel
                tasks: [t1, t2, t3]
            tasks:
              t1: {spec: s}
              t2: {spec: s}
              t3: {spec: s}
            shared_files: []
        """
        ws = load_workspace(write_dag(tmp_path, body))
        # parallel phase 全部一波出
        assert topological_waves(ws) == [["t1", "t2", "t3"]]

    def test_phase_barrier(self, tmp_path: Path) -> None:
        """phase B 不能在 phase A 没全部完成前启动。"""
        body = """
            phases:
              - name: A
                type: parallel
                tasks: [a1, a2]
              - name: B
                type: parallel
                tasks: [b1, b2]
            tasks:
              a1: {spec: s}
              a2: {spec: s}
              b1: {spec: s}
              b2: {spec: s}
            shared_files: []
        """
        ws = load_workspace(write_dag(tmp_path, body))
        waves = topological_waves(ws)
        assert waves == [["a1", "a2"], ["b1", "b2"]]

    def test_explicit_depends_on(self, tmp_path: Path) -> None:
        """显式 depends_on 跨 phase 也成立（虽然 phase 屏障已经隐含）。"""
        body = """
            phases:
              - name: A
                type: serial
                tasks: [base]
              - name: B
                type: parallel
                tasks: [feat1, feat2]
            tasks:
              base: {spec: s}
              feat1:
                spec: s
                depends_on: [base]
              feat2:
                spec: s
                depends_on: [base]
            shared_files: []
        """
        ws = load_workspace(write_dag(tmp_path, body))
        waves = topological_waves(ws)
        assert waves == [["base"], ["feat1", "feat2"]]

    def test_real_world_example(self, tmp_path: Path) -> None:
        """对应 examples/dags/m1-hello.yaml 期望的波次。"""
        body = """
            phases:
              - name: A
                type: serial
                tasks: [pkg-base]
              - name: B
                type: parallel
                tasks: [pkg-mul, pkg-sub]
            tasks:
              pkg-base:
                spec: specs/pkg-base.md
                files_owned: ["src/calc/add.py"]
              pkg-mul:
                spec: specs/pkg-mul.md
                depends_on: [pkg-base]
                files_owned: ["src/calc/mul.py"]
                shared_files_to_modify: ["src/calc/__init__.py"]
              pkg-sub:
                spec: specs/pkg-sub.md
                depends_on: [pkg-base]
                files_owned: ["src/calc/sub.py"]
                shared_files_to_modify: ["src/calc/__init__.py"]
            shared_files:
              - path: "src/calc/__init__.py"
                policy: single-writer
        """
        ws = load_workspace(write_dag(tmp_path, body))
        assert topological_waves(ws) == [["pkg-base"], ["pkg-mul", "pkg-sub"]]


class TestExampleDagFile:
    """对真实 examples/dags/m1-hello.yaml 跑一遍，证明它合法。"""

    def test_m1_hello_loads(self) -> None:
        repo_root = Path(__file__).resolve().parents[2]
        dag_file = repo_root / "examples" / "dags" / "m1-hello.yaml"
        ws = load_workspace(dag_file)
        assert {"pkg-base", "pkg-mul", "pkg-sub"} == set(ws.tasks)
        assert topological_waves(ws) == [["pkg-base"], ["pkg-mul", "pkg-sub"]]
        assert len(ws.shared_files) == 1
        assert ws.shared_files[0].policy == SharedFilePolicy.SINGLE_WRITER
