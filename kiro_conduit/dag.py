"""DAG schema：dag.yaml 的解析、验证、拓扑排序。

M1.0 范围（最小可用）：
- 数据类型：TaskDef / PhaseDef / SharedFileSpec / Workspace
- 从 yaml 文件加载并做严格 schema 校验
- 拓扑序 + 并行波次（同波次任务无依赖，可并行执行）
- 共享文件 policy：M1.0 只支持 single-writer，append-only / coordinator-only 留给 M1.1

dag.yaml 示例（最小）：

    phases:
      - name: setup
        type: serial
        tasks: [pkg-base]
      - name: features
        type: parallel
        tasks: [pkg-a, pkg-b]

    tasks:
      pkg-base:
        spec: specs/pkg-base.md
        depends_on: []
        files_owned: ["src/lib/*"]
        shared_files_to_modify: []
        max_lines: 800
        max_files: 12
        acceptance:
          - "ruff check ."
      pkg-a:
        spec: specs/pkg-a.md
        depends_on: [pkg-base]
        files_owned: ["src/feature_a/*"]
        shared_files_to_modify: ["src/constants.py"]
        max_lines: 800
        max_files: 12
        acceptance:
          - "pytest -q tests/feature_a"

    shared_files:
      - path: src/constants.py
        policy: single-writer
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class DagError(Exception):
    """DAG 加载 / 验证错误。"""


# ---------------------------------------------------------------------------
# 数据类型
# ---------------------------------------------------------------------------


class PhaseType(StrEnum):
    """phase 执行类型。"""

    SERIAL = "serial"      # phase 内 task 一次跑一个（按 yaml 出现顺序）
    PARALLEL = "parallel"  # phase 内 task 全部可并行


class SharedFilePolicy(StrEnum):
    """共享文件写入策略。"""

    SINGLE_WRITER = "single-writer"  # 同时只能一个 task 持锁
    APPEND_ONLY = "append-only"      # M1.1：只允许追加
    COORDINATOR_ONLY = "coordinator-only"  # M1.1：只 Coordinator 能改


@dataclass(frozen=True, slots=True)
class TaskDef:
    """任务定义（来自 dag.yaml tasks 部分）。"""

    id: str
    spec: str  # spec 文件路径（相对 workspace 根，运行时由调度器加载）
    depends_on: tuple[str, ...] = ()
    files_owned: tuple[str, ...] = ()
    shared_files_to_modify: tuple[str, ...] = ()
    max_lines: int = 800
    max_files: int = 12
    acceptance: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PhaseDef:
    """phase 定义。"""

    name: str
    type: PhaseType
    task_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SharedFileSpec:
    """共享文件规范。"""

    path: str
    policy: SharedFilePolicy


@dataclass(frozen=True, slots=True)
class Workspace:
    """完整 dag.yaml 加载后的内存模型。"""

    phases: tuple[PhaseDef, ...]
    tasks: dict[str, TaskDef]
    shared_files: tuple[SharedFileSpec, ...]
    workspace_root: Path  # dag.yaml 所在目录，用于解析相对路径

    def task(self, task_id: str) -> TaskDef:
        if task_id not in self.tasks:
            raise KeyError(f"task not in workspace: {task_id}")
        return self.tasks[task_id]

    def shared_file(self, path: str) -> SharedFileSpec | None:
        for sf in self.shared_files:
            if sf.path == path:
                return sf
        return None


# ---------------------------------------------------------------------------
# 加载（YAML → 数据类型，含基本格式校验）
# ---------------------------------------------------------------------------


def load_workspace(dag_yaml_path: Path) -> Workspace:
    """从文件加载并解析 dag.yaml。校验失败抛 DagError。"""
    if not dag_yaml_path.is_file():
        raise DagError(f"dag.yaml not found: {dag_yaml_path}")

    try:
        raw = yaml.safe_load(dag_yaml_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise DagError(f"invalid YAML in {dag_yaml_path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise DagError(f"dag.yaml top-level must be a mapping, got {type(raw).__name__}")

    workspace = _parse_workspace(raw, dag_yaml_path.parent.resolve())
    validate(workspace)
    return workspace


def _parse_workspace(raw: dict[str, Any], workspace_root: Path) -> Workspace:
    phases = _parse_phases(raw.get("phases", []))
    tasks = _parse_tasks(raw.get("tasks", {}))
    shared_files = _parse_shared_files(raw.get("shared_files", []))
    return Workspace(
        phases=phases,
        tasks=tasks,
        shared_files=shared_files,
        workspace_root=workspace_root,
    )


def _parse_phases(raw_phases: Any) -> tuple[PhaseDef, ...]:
    if not isinstance(raw_phases, list):
        raise DagError(f"phases must be a list, got {type(raw_phases).__name__}")
    if not raw_phases:
        raise DagError("phases must not be empty")

    out: list[PhaseDef] = []
    seen_names: set[str] = set()
    for idx, item in enumerate(raw_phases):
        if not isinstance(item, dict):
            raise DagError(f"phases[{idx}] must be a mapping, got {type(item).__name__}")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise DagError(f"phases[{idx}].name must be a non-empty string")
        if name in seen_names:
            raise DagError(f"duplicate phase name: {name}")
        seen_names.add(name)

        type_raw = item.get("type", "serial")
        try:
            phase_type = PhaseType(type_raw)
        except ValueError as exc:
            raise DagError(
                f"phases[{idx}].type must be one of {[e.value for e in PhaseType]}, "
                f"got {type_raw!r}"
            ) from exc

        task_ids = item.get("tasks", [])
        if not isinstance(task_ids, list) or not all(isinstance(t, str) for t in task_ids):
            raise DagError(f"phases[{idx}].tasks must be a list of strings")
        if not task_ids:
            raise DagError(f"phase {name!r} has no tasks")
        if len(set(task_ids)) != len(task_ids):
            raise DagError(f"phase {name!r} has duplicate task ids: {task_ids}")

        out.append(PhaseDef(name=name, type=phase_type, task_ids=tuple(task_ids)))
    return tuple(out)


def _parse_tasks(raw_tasks: Any) -> dict[str, TaskDef]:
    if not isinstance(raw_tasks, dict):
        raise DagError(f"tasks must be a mapping, got {type(raw_tasks).__name__}")
    if not raw_tasks:
        raise DagError("tasks must not be empty")

    out: dict[str, TaskDef] = {}
    for tid, body in raw_tasks.items():
        if not isinstance(tid, str) or not tid:
            raise DagError(f"task id must be non-empty string, got {tid!r}")
        if not isinstance(body, dict):
            raise DagError(f"task {tid!r} body must be a mapping")
        out[tid] = _parse_task(tid, body)
    return out


def _parse_task(tid: str, body: dict[str, Any]) -> TaskDef:
    spec = body.get("spec")
    if not isinstance(spec, str) or not spec:
        raise DagError(f"task {tid!r} missing spec (string)")

    depends_on = body.get("depends_on", [])
    if not isinstance(depends_on, list) or not all(isinstance(d, str) for d in depends_on):
        raise DagError(f"task {tid!r} depends_on must be a list of strings")

    files_owned = body.get("files_owned", [])
    if not isinstance(files_owned, list) or not all(isinstance(f, str) for f in files_owned):
        raise DagError(f"task {tid!r} files_owned must be a list of strings")

    shared = body.get("shared_files_to_modify", [])
    if not isinstance(shared, list) or not all(isinstance(f, str) for f in shared):
        raise DagError(f"task {tid!r} shared_files_to_modify must be a list of strings")

    max_lines = body.get("max_lines", 800)
    if not isinstance(max_lines, int) or max_lines <= 0:
        raise DagError(f"task {tid!r} max_lines must be positive int")

    max_files = body.get("max_files", 12)
    if not isinstance(max_files, int) or max_files <= 0:
        raise DagError(f"task {tid!r} max_files must be positive int")

    acceptance = body.get("acceptance", [])
    if not isinstance(acceptance, list) or not all(isinstance(a, str) for a in acceptance):
        raise DagError(f"task {tid!r} acceptance must be a list of strings")

    return TaskDef(
        id=tid,
        spec=spec,
        depends_on=tuple(depends_on),
        files_owned=tuple(files_owned),
        shared_files_to_modify=tuple(shared),
        max_lines=max_lines,
        max_files=max_files,
        acceptance=tuple(acceptance),
    )


def _parse_shared_files(raw_sf: Any) -> tuple[SharedFileSpec, ...]:
    if not isinstance(raw_sf, list):
        raise DagError(f"shared_files must be a list, got {type(raw_sf).__name__}")
    out: list[SharedFileSpec] = []
    seen_paths: set[str] = set()
    for idx, item in enumerate(raw_sf):
        if not isinstance(item, dict):
            raise DagError(f"shared_files[{idx}] must be a mapping")
        path = item.get("path")
        if not isinstance(path, str) or not path:
            raise DagError(f"shared_files[{idx}].path must be non-empty string")
        if path in seen_paths:
            raise DagError(f"duplicate shared_file path: {path}")
        seen_paths.add(path)
        policy_raw = item.get("policy", "single-writer")
        try:
            policy = SharedFilePolicy(policy_raw)
        except ValueError as exc:
            raise DagError(
                f"shared_files[{idx}].policy must be one of "
                f"{[p.value for p in SharedFilePolicy]}, got {policy_raw!r}"
            ) from exc
        # M1.0 限制
        if policy != SharedFilePolicy.SINGLE_WRITER:
            raise DagError(
                f"shared_files[{idx}].policy={policy.value!r} not supported in M1.0; "
                "only 'single-writer' is implemented"
            )
        out.append(SharedFileSpec(path=path, policy=policy))
    return tuple(out)


# ---------------------------------------------------------------------------
# 验证（语义校验）
# ---------------------------------------------------------------------------


def validate(workspace: Workspace) -> None:
    """对解析后的 Workspace 做语义校验。失败抛 DagError。"""
    _check_phase_tasks_exist(workspace)
    _check_every_task_in_some_phase(workspace)
    _check_depends_on_targets_exist(workspace)
    _check_no_dependency_cycle(workspace)
    _check_files_owned_no_overlap(workspace)
    _check_shared_files_declared(workspace)


def _check_phase_tasks_exist(workspace: Workspace) -> None:
    for phase in workspace.phases:
        for tid in phase.task_ids:
            if tid not in workspace.tasks:
                raise DagError(
                    f"phase {phase.name!r} references unknown task {tid!r}"
                )


def _check_every_task_in_some_phase(workspace: Workspace) -> None:
    in_phase: set[str] = set()
    seen_twice: set[str] = set()
    for phase in workspace.phases:
        for tid in phase.task_ids:
            if tid in in_phase:
                seen_twice.add(tid)
            in_phase.add(tid)
    if seen_twice:
        raise DagError(f"task(s) appear in multiple phases: {sorted(seen_twice)}")
    orphans = set(workspace.tasks) - in_phase
    if orphans:
        raise DagError(f"task(s) defined but not in any phase: {sorted(orphans)}")


def _check_depends_on_targets_exist(workspace: Workspace) -> None:
    for tid, t in workspace.tasks.items():
        for dep in t.depends_on:
            if dep not in workspace.tasks:
                raise DagError(f"task {tid!r} depends on unknown task {dep!r}")
            if dep == tid:
                raise DagError(f"task {tid!r} depends on itself")


def _check_no_dependency_cycle(workspace: Workspace) -> None:
    # 用 Kahn 算法检测：能不能拓扑排掉所有节点
    indeg: dict[str, int] = {tid: 0 for tid in workspace.tasks}
    for tid, t in workspace.tasks.items():
        for _ in t.depends_on:
            indeg[tid] += 1
    queue: deque[str] = deque(tid for tid, d in indeg.items() if d == 0)
    visited = 0
    while queue:
        cur = queue.popleft()
        visited += 1
        # 找出所有 depends_on 含 cur 的 task
        for tid, t in workspace.tasks.items():
            if cur in t.depends_on:
                indeg[tid] -= 1
                if indeg[tid] == 0:
                    queue.append(tid)
    if visited != len(workspace.tasks):
        unvisited = [tid for tid, d in indeg.items() if d > 0]
        raise DagError(f"dependency cycle detected involving: {sorted(unvisited)}")


def _check_files_owned_no_overlap(workspace: Workspace) -> None:
    """两个 task 不能 own 同一个 path 字面值（M1.0 不做 glob 模式匹配）。"""
    owner: dict[str, str] = {}
    for tid, t in workspace.tasks.items():
        for path in t.files_owned:
            if path in owner:
                raise DagError(
                    f"file {path!r} is owned by both {owner[path]!r} and {tid!r}"
                )
            owner[path] = tid


def _check_shared_files_declared(workspace: Workspace) -> None:
    """task.shared_files_to_modify 里出现的 path 必须在顶层 shared_files 声明过。"""
    declared = {sf.path for sf in workspace.shared_files}
    for tid, t in workspace.tasks.items():
        for path in t.shared_files_to_modify:
            if path not in declared:
                raise DagError(
                    f"task {tid!r} touches shared file {path!r} not declared "
                    f"in top-level shared_files"
                )


# ---------------------------------------------------------------------------
# 拓扑波次：每个波次内的 task 可以并行执行
# ---------------------------------------------------------------------------


def topological_waves(workspace: Workspace) -> list[list[str]]:
    """把 workspace 切成可执行的波次列表。

    每个波次是一个 task_id 列表，同波次内 task 互不依赖，可并行。
    波次按依赖关系全局排序：第 N 波只在第 N-1 波全部完成后启动。

    顺序约束：
    - 严格遵守 task.depends_on（跨 phase 也要遵守）
    - 同时遵守 phase 顺序：phase B 的 task 至少要等到 phase A 所有 task 完成才能开始
      （这是为了支持 phase 间隐式的"屏障"语义，比如 phase A 是基础设施）
    - phase.type=serial 时，phase 内 task 按 yaml 顺序串行（即使没有显式 depends_on）

    实现：先按上面三条规则计算"实际依赖集"，再做层次化拓扑排序。
    """
    # 计算每个 task 的实际依赖（含 phase serial 推导出的依赖）
    effective_deps: dict[str, set[str]] = {
        tid: set(t.depends_on) for tid, t in workspace.tasks.items()
    }

    # phase 间屏障：phase B 的所有 task depends on phase A 的所有 task
    prior_phase_tasks: list[str] = []
    for phase in workspace.phases:
        if prior_phase_tasks:
            for tid in phase.task_ids:
                effective_deps[tid].update(prior_phase_tasks)
        # phase 内 serial：第 i 个 task depends on 前 i 个
        if phase.type == PhaseType.SERIAL:
            for i, tid in enumerate(phase.task_ids):
                for prev in phase.task_ids[:i]:
                    effective_deps[tid].add(prev)
        prior_phase_tasks.extend(phase.task_ids)

    # 层次化拓扑排序：每一轮取出所有 indeg=0 的，作为一个波次
    indeg: dict[str, int] = {tid: len(deps) for tid, deps in effective_deps.items()}
    rev: dict[str, list[str]] = defaultdict(list)
    for tid, deps in effective_deps.items():
        for d in deps:
            rev[d].append(tid)

    waves: list[list[str]] = []
    remaining = set(workspace.tasks)
    while remaining:
        ready = sorted(tid for tid in remaining if indeg[tid] == 0)
        if not ready:
            # 不应该发生（validate 已查过环），但留个 sanity check
            raise DagError(f"cannot wave-schedule: stuck on {sorted(remaining)}")
        waves.append(ready)
        for tid in ready:
            remaining.discard(tid)
            for nxt in rev[tid]:
                indeg[nxt] -= 1
    return waves
