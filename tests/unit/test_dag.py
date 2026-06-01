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

    def test_m1_1_accepts_all_policies(self, tmp_path: Path) -> None:
        """M1.1 step 3 起，append-only / coordinator-only / single-writer 三种都接受。"""
        from kiro_conduit.dag import SharedFilePolicy

        body = """
            phases:
              - name: A
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: s, shared_files_to_modify: [src/a.py, src/b.py, src/c.py]}
            shared_files:
              - path: src/a.py
                policy: append-only
              - path: src/b.py
                policy: coordinator-only
              - path: src/c.py
                policy: single-writer
        """
        ws = load_workspace(write_dag(tmp_path, body))
        policies = {sf.path: sf.policy for sf in ws.shared_files}
        assert policies["src/a.py"] == SharedFilePolicy.APPEND_ONLY
        assert policies["src/b.py"] == SharedFilePolicy.COORDINATOR_ONLY
        assert policies["src/c.py"] == SharedFilePolicy.SINGLE_WRITER

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


class TestInterfaceLock:
    """M1.1 stub-first 接口锁定。"""

    def test_basic_parse(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: B
                type: parallel
                tasks: [stub, impl-a, impl-b]
                interface_lock:
                  - file: src/lib.py
                    owner: stub
                    consumers: [impl-a, impl-b]
            tasks:
              stub: {spec: s}
              impl-a: {spec: s}
              impl-b: {spec: s}
            shared_files: []
        """
        ws = load_workspace(write_dag(tmp_path, body))
        assert len(ws.phases[0].interface_locks) == 1
        lock = ws.phases[0].interface_locks[0]
        assert lock.file == "src/lib.py"
        assert lock.owner == "stub"
        assert lock.consumers == ("impl-a", "impl-b")
        assert lock.mode == "stub-first"

    def test_only_stub_first_supported_in_m1_1(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: B
                type: parallel
                tasks: [stub, impl]
                interface_lock:
                  - file: src/lib.py
                    owner: stub
                    consumers: [impl]
                    mode: somethingelse
            tasks:
              stub: {spec: s}
              impl: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match=r"not supported in M1\.1"):
            load_workspace(write_dag(tmp_path, body))

    def test_owner_in_consumers_rejected(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: B
                type: parallel
                tasks: [stub, impl]
                interface_lock:
                  - file: src/lib.py
                    owner: stub
                    consumers: [stub, impl]
            tasks:
              stub: {spec: s}
              impl: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="cannot also be in consumers"):
            load_workspace(write_dag(tmp_path, body))

    def test_owner_must_be_in_phase(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: parallel
                tasks: [t1]
              - name: B
                type: parallel
                tasks: [stub, impl]
                interface_lock:
                  - file: src/lib.py
                    owner: t1
                    consumers: [impl]
            tasks:
              t1: {spec: s}
              stub: {spec: s}
              impl: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="owner 't1' is not a task in this phase"):
            load_workspace(write_dag(tmp_path, body))

    def test_serial_phase_with_interface_lock_rejected(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: A
                type: serial
                tasks: [stub, impl]
                interface_lock:
                  - file: src/lib.py
                    owner: stub
                    consumers: [impl]
            tasks:
              stub: {spec: s}
              impl: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="stub-first only makes sense for parallel phases"):
            load_workspace(write_dag(tmp_path, body))

    def test_interface_lock_makes_consumer_wait_for_owner(self, tmp_path: Path) -> None:
        body = """
            phases:
              - name: B
                type: parallel
                tasks: [stub, impl-a, impl-b]
                interface_lock:
                  - file: src/lib.py
                    owner: stub
                    consumers: [impl-a, impl-b]
            tasks:
              stub: {spec: s}
              impl-a: {spec: s}
              impl-b: {spec: s}
            shared_files: []
        """
        ws = load_workspace(write_dag(tmp_path, body))
        # stub 应在自己的子波次先跑，impl-a 和 impl-b 在第二波并行
        assert topological_waves(ws) == [["stub"], ["impl-a", "impl-b"]]

    def test_owner_consumer_cycle_rejected(self, tmp_path: Path) -> None:
        """同一 task 既是一个 lock 的 owner 又是另一个的 consumer → 死锁。"""
        body = """
            phases:
              - name: B
                type: parallel
                tasks: [a, b, c]
                interface_lock:
                  - file: x.py
                    owner: a
                    consumers: [b]
                  - file: y.py
                    owner: b
                    consumers: [a]
            tasks:
              a: {spec: s}
              b: {spec: s}
              c: {spec: s}
            shared_files: []
        """
        with pytest.raises(DagError, match="both owner and consumer"):
            load_workspace(write_dag(tmp_path, body))


class TestCrossRepoSchema:
    def test_default_no_repos(self, tmp_path: Path) -> None:
        """不声明 repos 时：repos 为空、task.repo 为 None（行为不变）。"""
        ws = load_workspace(write_dag(tmp_path, minimal_yaml()))
        assert ws.repos == {}
        assert ws.tasks["t1"].repo is None

    def test_repos_and_task_repo_parsed(self, tmp_path: Path) -> None:
        ws = load_workspace(
            write_dag(
                tmp_path,
                """
                repos:
                  api: ../api-repo
                  web: ../web-repo
                phases:
                  - name: only
                    type: parallel
                    tasks: [t1, t2]
                tasks:
                  t1: {spec: specs/t1.md, repo: api}
                  t2: {spec: specs/t2.md, repo: web}
                shared_files: []
                """,
            )
        )
        assert ws.repos == {"api": "../api-repo", "web": "../web-repo"}
        assert ws.tasks["t1"].repo == "api"
        assert ws.tasks["t2"].repo == "web"

    def test_undeclared_repo_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(DagError, match="undeclared repo"):
            load_workspace(
                write_dag(
                    tmp_path,
                    """
                    repos:
                      api: ../api-repo
                    phases:
                      - name: only
                        type: serial
                        tasks: [t1]
                    tasks:
                      t1: {spec: specs/t1.md, repo: nope}
                    shared_files: []
                    """,
                )
            )

    def test_repos_path_must_be_string(self, tmp_path: Path) -> None:
        with pytest.raises(DagError, match="must be a non-empty path string"):
            load_workspace(
                write_dag(
                    tmp_path,
                    """
                    repos:
                      api: 123
                    phases:
                      - name: only
                        type: serial
                        tasks: [t1]
                    tasks:
                      t1: {spec: specs/t1.md}
                    shared_files: []
                    """,
                )
            )

    def test_example_cross_repo_dag_loads(self) -> None:
        """examples/dags/cross-repo.yaml 能加载，repos/repo 与波次符合预期。"""
        from kiro_conduit.dag import topological_waves

        example = (
            Path(__file__).resolve().parents[2]
            / "examples" / "dags" / "cross-repo.yaml"
        )
        ws = load_workspace(example)
        assert ws.repos == {"api": "../api-repo", "web": "../web-repo"}
        assert ws.tasks["api-schema"].repo == "api"
        assert ws.tasks["api-handler"].repo == "api"
        assert ws.tasks["web-client"].repo == "web"
        waves = topological_waves(ws)
        assert waves[0] == ["api-schema"]
        assert set(waves[1]) == {"api-handler", "web-client"}


class TestPerTaskModel:
    """per-task 模型路由：dag.yaml 的 task 可声明 model。"""

    def test_model_parsed(self, tmp_path: Path) -> None:
        p = write_dag(
            tmp_path,
            """
            phases:
              - name: only
                type: serial
                tasks: [t1, t2]
            tasks:
              t1: {spec: specs/t1.md, model: claude-haiku-4.5}
              t2: {spec: specs/t2.md}
            shared_files: []
            """,
        )
        ws = load_workspace(p)
        assert ws.tasks["t1"].model == "claude-haiku-4.5"
        assert ws.tasks["t2"].model is None  # 缺省 = 用 role 路由/Kiro 默认

    def test_empty_model_rejected(self, tmp_path: Path) -> None:
        p = write_dag(
            tmp_path,
            """
            phases:
              - name: only
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md, model: ""}
            shared_files: []
            """,
        )
        with pytest.raises(DagError, match="model"):
            load_workspace(p)


class TestSetupScript:
    """workspace 级 setup 命令：每个 worktree 创建后执行。"""

    def test_setup_parsed(self, tmp_path: Path) -> None:
        p = write_dag(
            tmp_path,
            """
            setup: uv sync
            phases:
              - name: only
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        ws = load_workspace(p)
        assert ws.setup == "uv sync"

    def test_setup_defaults_none(self, tmp_path: Path) -> None:
        assert load_workspace(write_dag(tmp_path, minimal_yaml())).setup is None

    def test_blank_setup_rejected(self, tmp_path: Path) -> None:
        p = write_dag(
            tmp_path,
            """
            setup: "   "
            phases:
              - name: only
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        with pytest.raises(DagError, match="setup"):
            load_workspace(p)


class TestCopyFiles:
    """workspace 级 copy_files：把本地（gitignored）文件拷进每个 worktree。"""

    def test_copy_files_parsed(self, tmp_path: Path) -> None:
        p = write_dag(
            tmp_path,
            """
            copy_files: [".env", "config/local.yaml"]
            phases:
              - name: only
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        assert load_workspace(p).copy_files == (".env", "config/local.yaml")

    def test_copy_files_defaults_empty(self, tmp_path: Path) -> None:
        assert load_workspace(write_dag(tmp_path, minimal_yaml())).copy_files == ()

    def test_copy_files_must_be_string_list(self, tmp_path: Path) -> None:
        p = write_dag(
            tmp_path,
            """
            copy_files: [1, 2]
            phases:
              - name: only
                type: serial
                tasks: [t1]
            tasks:
              t1: {spec: specs/t1.md}
            shared_files: []
            """,
        )
        with pytest.raises(DagError, match="copy_files"):
            load_workspace(p)

