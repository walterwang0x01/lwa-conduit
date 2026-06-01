"""spec → DAG 自动规划器（P-B）。

把一份高层 markdown spec 交给 LLM（Kiro），让它拆成结构化的 task plan，再自动
生成 kiro-conduit 能吃的 `dag.yaml` + 各 task 的 spec 文件（人确认后 run）。

分两层：
- 纯核心（可单测，不调 LLM）：parse_plan → compute_layers → render_dag_yaml → write_plan
- LLM 后端（KiroPlanner，隔离）：用 ACP 驱动 Kiro 产出 plan JSON

LLM 产出的 plan JSON 约定：
    {
      "tasks": [
        {"id": "...", "prompt": "...", "depends_on": [...],
         "files_owned": [...], "acceptance": [...]},
        ...
      ]
    }
波次（phases）由 depends_on 自动拓扑分层得到，不要求 LLM 产出。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kiro_conduit.dag import load_workspace

logger = logging.getLogger(__name__)


class PlanError(RuntimeError):
    """plan 解析 / 校验失败。"""


@dataclass(frozen=True, slots=True)
class TaskPlan:
    id: str
    prompt: str  # 写进 specs/<id>.md，当 Implementor 的指令
    depends_on: list[str] = field(default_factory=list)
    files_owned: list[str] = field(default_factory=list)
    acceptance: list[str] = field(default_factory=list)


# ---------------------------------------------------------------- 解析

def _extract_json(raw: str) -> dict[str, Any]:
    """从 LLM 输出里抽出 JSON 对象（容忍 ```json 围栏或前后散文）。"""
    s = raw.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*\})\s*```", s, re.DOTALL)
    if fence:
        s = fence.group(1)
    else:
        i, j = s.find("{"), s.rfind("}")
        if i != -1 and j > i:
            s = s[i : j + 1]
    try:
        data = json.loads(s)
    except json.JSONDecodeError as exc:
        raise PlanError(f"could not parse plan JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise PlanError("plan JSON must be an object")
    return data


def parse_plan(raw: str) -> list[TaskPlan]:
    """LLM 文本 → TaskPlan 列表（基本校验）。"""
    data = _extract_json(raw)
    raw_tasks = data.get("tasks")
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise PlanError("plan must contain a non-empty 'tasks' list")

    def _str_list(v: object) -> list[str]:
        return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []

    tasks: list[TaskPlan] = []
    seen: set[str] = set()
    for t in raw_tasks:
        if not isinstance(t, dict):
            raise PlanError("each task must be an object")
        tid = t.get("id")
        if not isinstance(tid, str) or not tid:
            raise PlanError("task missing non-empty 'id'")
        if tid in seen:
            raise PlanError(f"duplicate task id: {tid}")
        seen.add(tid)
        prompt = t.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise PlanError(f"task {tid!r} missing non-empty 'prompt'")
        tasks.append(
            TaskPlan(
                id=tid,
                prompt=prompt.strip(),
                depends_on=_str_list(t.get("depends_on")),
                files_owned=_str_list(t.get("files_owned")),
                acceptance=_str_list(t.get("acceptance")),
            )
        )
    return tasks


# ---------------------------------------------------------------- 分层

def compute_layers(tasks: list[TaskPlan]) -> list[list[str]]:
    """按 depends_on 拓扑分层；每层内互不依赖（可并行）。检测未知依赖 / 环。"""
    ids = {t.id for t in tasks}
    deps = {t.id: list(t.depends_on) for t in tasks}
    for tid, ds in deps.items():
        for d in ds:
            if d not in ids:
                raise PlanError(f"task {tid!r} depends on unknown task {d!r}")

    done: set[str] = set()
    remaining = set(ids)
    layers: list[list[str]] = []
    while remaining:
        layer = sorted(t for t in remaining if all(d in done for d in deps[t]))
        if not layer:
            raise PlanError("dependency cycle detected in plan")
        layers.append(layer)
        done.update(layer)
        remaining.difference_update(layer)
    return layers


# ---------------------------------------------------------------- 生成

def render_dag_yaml(tasks: list[TaskPlan]) -> str:
    """TaskPlan 列表 → dag.yaml 文本（phases 由分层自动生成）。"""
    layers = compute_layers(tasks)
    phases = [
        {
            "name": f"phase{i}",
            "type": "parallel",
            "tasks": layer,
        }
        for i, layer in enumerate(layers, start=1)
    ]
    tasks_doc: dict[str, Any] = {}
    for t in tasks:
        entry: dict[str, Any] = {"spec": f"specs/{t.id}.md"}
        if t.depends_on:
            entry["depends_on"] = t.depends_on
        if t.files_owned:
            entry["files_owned"] = t.files_owned
        if t.acceptance:
            entry["acceptance"] = t.acceptance
        tasks_doc[t.id] = entry
    doc = {"phases": phases, "tasks": tasks_doc, "shared_files": []}
    return yaml.safe_dump(doc, sort_keys=False, allow_unicode=True)


def write_plan(tasks: list[TaskPlan], out_dir: Path) -> Path:
    """写 dag.yaml + specs/<id>.md 到 out_dir，并用 load_workspace 校验。返回 dag.yaml 路径。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    specs_dir = out_dir / "specs"
    specs_dir.mkdir(exist_ok=True)
    for t in tasks:
        (specs_dir / f"{t.id}.md").write_text(t.prompt + "\n", encoding="utf-8")
    dag_path = out_dir / "dag.yaml"
    dag_path.write_text(render_dag_yaml(tasks), encoding="utf-8")
    # 校验：能加载 + DAG 合法（无环 / 依赖存在 / 每个 task 在某 phase）
    load_workspace(dag_path)
    return dag_path


# ---------------------------------------------------------------- LLM 后端

PLAN_PROMPT = """你是一个软件规划器。读下面的 spec，把它拆成可并行执行的 task 列表。

只输出一个 JSON 对象，不要任何多余文字、不要创建任何文件。格式：
{{
  "tasks": [
    {{
      "id": "短横线小写标识",
      "prompt": "给实现者的清晰指令（这个 task 要做什么、建/改哪些文件）",
      "depends_on": ["前置 task 的 id", ...],
      "files_owned": ["这个 task 会创建/修改的文件路径（相对仓库根）", ...],
      "acceptance": ["验收命令，如 pytest -q tests/test_x.py", ...]
    }}
  ]
}}

规则：
- 并行的 task 的 files_owned **不能重叠**（否则用 depends_on 串起来，或合成一个 task）。
- 有依赖关系的用 depends_on 表达（被依赖的在前）。
- **任务粒度按"一个 PR"来切**：每个 task 是一个**自包含、可独立 review** 的变更单元。
  宁可少而粗，不要多而碎——任务太多会让分支数爆炸、review 跟不上。
- **强耦合的工作放进同一个 task，别拆开**：典型如"新建一个模块"和"把它注册/接线进去"
  应是同一个 task；把它们拆成两个会导致后一个无事可做（依赖已做掉）或两者改同一文件冲突。
- acceptance 用可执行的验证命令（优先 pytest / 编译检查）。
- acceptance **必须包含项目自身的 linter/格式检查**，对本 task 改的文件运行（如 Python
  项目用 `ruff check <files>`、JS 用 `eslint <files>`），让代码规范在任务级被验证，
  而不是等合并后才人工发现。
- 每个 task 的 prompt 要自包含、明确。

spec：
---
{spec}
---
"""


class KiroPlanner:
    """用 ACP 驱动 Kiro 把 spec 规划成 TaskPlan 列表。"""

    def __init__(
        self,
        kiro_cli_path: str = "kiro-cli",
        prompt_timeout: float = 300.0,
        model: str | None = None,
    ) -> None:
        self._kiro_cli_path = kiro_cli_path
        self._prompt_timeout = prompt_timeout
        self._model = model

    async def generate_plan(self, spec_text: str, cwd: Path) -> list[TaskPlan]:
        from kiro_conduit.acp import AcpClient, AcpClientConfig, AgentMessageChunk, TurnEnd

        config = AcpClientConfig(
            kiro_cli_path=self._kiro_cli_path,
            cwd=cwd,
            response_timeout=self._prompt_timeout,
            model=self._model,
        )
        parts: list[str] = []
        async with await AcpClient.spawn(config) as client:
            await client.initialize()
            session_id = await client.new_session(cwd=cwd)
            events = await client.prompt(session_id, PLAN_PROMPT.format(spec=spec_text))
            async for event in events:
                if isinstance(event, AgentMessageChunk):
                    parts.append(event.text)
                elif isinstance(event, TurnEnd):
                    break
        return parse_plan("".join(parts))
