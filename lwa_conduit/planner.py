"""spec → DAG 自动规划器（P-B）。

把一份高层 markdown spec 交给 LLM（Kiro），让它拆成结构化的 task plan，再自动
生成 lwa-conduit 能吃的 `dag.yaml` + 各 task 的 spec 文件（人确认后 run）。

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

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from lwa_conduit.dag import DagError, load_workspace
from lwa_conduit.memory import Memory
from lwa_conduit.runtime.model_router import resolve_runtime_for_prompt
from lwa_conduit.runtime.types import RuntimeConfig

logger = logging.getLogger(__name__)


class PlanError(RuntimeError):
    """plan 解析 / 校验失败。"""


@dataclass(frozen=True, slots=True)
class DimensionResult:
    """单个自评维度的结果。"""

    score: int
    issues: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class PlanEvaluation:
    """plan 自评结果：6 维打分 + 必须修复项 + 建议项。"""

    score: int
    coverage: DimensionResult
    granularity: DimensionResult
    coupling: DimensionResult
    dependencies: DimensionResult
    clarity: DimensionResult
    spec_alignment: DimensionResult
    must_fix: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)

    @property
    def needs_repair(self) -> bool:
        """有 must_fix 项 → 必须修复后才能给人。"""
        return len(self.must_fix) > 0

    def summary(self) -> str:
        """人可读的单行摘要。"""
        dims = (
            f"覆盖={self.coverage.score} 粒度={self.granularity.score} "
            f"耦合={self.coupling.score} 依赖={self.dependencies.score} "
            f"清晰={self.clarity.score} 对齐={self.spec_alignment.score}"
        )
        return f"总分={self.score} [{dims}] must_fix={len(self.must_fix)}"


# plan 自评合格的分数阈值（低于此分且有 must_fix 才触发 repair）
SELF_EVAL_PASS_THRESHOLD = 70


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


def plan_validation_error(tasks: list[TaskPlan]) -> str | None:
    """纯校验：返回人/LLM 可读的错误（环 / 未知依赖 / files_owned 重叠），合法则 None。

    用于 plan 自动修复：把这条错误喂回 Kiro 让它重拆。
    """
    try:
        compute_layers(tasks)
    except PlanError as exc:
        return str(exc)
    owner: dict[str, str] = {}
    for t in tasks:
        for f in t.files_owned:
            if f in owner:
                return f"file {f!r} is owned by both {owner[f]!r} and {t.id!r}"
            owner[f] = t.id
    return None


# ---------------------------------------------------------------- 自评解析

def parse_evaluation(raw: str) -> PlanEvaluation:
    """从 LLM 自评输出解析 PlanEvaluation。容错：字段缺失用默认值。"""
    data = _extract_json(raw)

    def _dim(key: str) -> DimensionResult:
        dims = data.get("dimensions", {})
        d = dims.get(key, {}) if isinstance(dims, dict) else {}
        if not isinstance(d, dict):
            d = {}
        score = d.get("score", 0)
        issues = d.get("issues", [])
        return DimensionResult(
            score=int(score) if isinstance(score, (int, float)) else 0,
            issues=[str(i) for i in issues] if isinstance(issues, list) else [],
        )

    def _str_list(key: str) -> list[str]:
        v = data.get(key, [])
        return [str(i) for i in v] if isinstance(v, list) else []

    raw_score = data.get("score", 0)
    score = int(raw_score) if isinstance(raw_score, (int, float)) else 0

    return PlanEvaluation(
        score=score,
        coverage=_dim("coverage"),
        granularity=_dim("granularity"),
        coupling=_dim("coupling"),
        dependencies=_dim("dependencies"),
        clarity=_dim("clarity"),
        spec_alignment=_dim("spec_alignment"),
        must_fix=_str_list("must_fix"),
        suggestions=_str_list("suggestions"),
    )


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
    # 校验：能加载 + DAG 合法（无环 / 依赖存在 / files_owned 不重叠 / 每个 task 在某 phase）。
    # LLM 拆分常在 files_owned 边界等细节出错——失败时不甩 traceback，转成带指引的
    # PlanError；产物已落盘，留给人按提示修正（这正是 plan 必须人工 review 的一步）。
    try:
        load_workspace(dag_path)
    except DagError as exc:
        raise PlanError(
            f"生成的 dag.yaml 没通过校验：{exc}\n"
            f"  dag.yaml + specs/ 已写到 {out_dir}，请手动修正后再 run "
            f"（plan 的产物本就需要人工 review，不能直接跑）。"
        ) from exc
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


REPAIR_PROMPT = """你刚拆出的 task plan 没通过校验：
{error}

下面是你上次的输出。请**修正这个问题**后，重新只输出修正后的完整 JSON 对象
（同样格式、不要多余文字、不要创建文件）：
{plan}
"""


SELF_EVAL_PROMPT = """你是一个规划审查器。下面是原始 spec 和据此拆出的 task plan。
请从以下 6 个维度对 plan 打分（0-100）并列出具体缺陷。

维度：
1. **需求覆盖**：plan 是否覆盖了 spec 的所有要求？有没有漏需求？
2. **粒度合理性**：任务粒度是否"一个 PR"大小？有没有过碎（>10 task 做一个中等功能）或过粗？
3. **耦合切割**：强耦合的工作有没有被拆开？有没有两个 task 实际必须一起改同一模块？
4. **依赖正确性**：depends_on 关系是否充分？有没有实际有依赖但没声明的？
5. **prompt 清晰度**：每个 task 的 prompt 是否自包含、明确，让实现者不需猜？
6. **spec 对齐度**：plan 有没有偏离 spec 的约束或意图？有没有 task 做了 spec 没要求的事？\
有没有 task 的 prompt 和 spec 矛盾？

只输出一个 JSON 对象，不要任何多余文字、不要创建任何文件。格式：
{{
  "score": 整数总分(0-100，6 维平均),
  "dimensions": {{
    "coverage": {{"score": 0-100, "issues": ["具体问题..."]}},
    "granularity": {{"score": 0-100, "issues": ["具体问题..."]}},
    "coupling": {{"score": 0-100, "issues": ["具体问题..."]}},
    "dependencies": {{"score": 0-100, "issues": ["具体问题..."]}},
    "clarity": {{"score": 0-100, "issues": ["具体问题..."]}},
    "spec_alignment": {{"score": 0-100, "issues": ["具体问题..."]}}
  }},
  "must_fix": ["必须修复的问题（阻塞性缺陷）"],
  "suggestions": ["建议改进（非阻塞）"]
}}

spec：
---
{spec}
---

plan：
---
{plan}
---
"""


SELF_EVAL_REPAIR_PROMPT = """你刚拆出的 task plan 经过自评发现以下**必须修复的缺陷**：
{must_fix}

完整的自评结果（供参考）：
{eval_summary}

下面是你上次的输出。请**修正上述必须修复的问题**后，重新只输出修正后的完整 JSON 对象
（同样格式、不要多余文字、不要创建文件）：
{plan}
"""


def _tasks_to_json(tasks: list[TaskPlan]) -> str:
    """TaskPlan 列表 → 可读的 JSON 文本（用于喂给自评 prompt）。"""
    task_dicts: list[dict[str, Any]] = []
    for t in tasks:
        entry: dict[str, Any] = {"id": t.id, "prompt": t.prompt}
        if t.depends_on:
            entry["depends_on"] = t.depends_on
        if t.files_owned:
            entry["files_owned"] = t.files_owned
        if t.acceptance:
            entry["acceptance"] = t.acceptance
        task_dicts.append(entry)
    return json.dumps({"tasks": task_dicts}, indent=2, ensure_ascii=False)


class KiroPlanner:
    """用 ACP 驱动 Kiro 把 spec 规划成 TaskPlan 列表。"""

    def __init__(
        self,
        runtime: RuntimeConfig | None = None,
        *,
        kiro_cli_path: str = "kiro-cli",
        prompt_timeout: float = 300.0,
        model: str | None = None,
        max_repairs: int = 2,
        self_eval: bool = True,
        max_eval_repairs: int = 1,
        memory: Memory | None = None,
        max_ask_retries: int = 2,
        ask_retry_base_delay: float = 1.0,
    ) -> None:
        self._runtime = runtime or RuntimeConfig.from_cli(
            kiro_cli=kiro_cli_path,
            runtime_kind="kiro-cli-acp",
            model=model,
            timeout=prompt_timeout,
        )
        self._prompt_timeout = prompt_timeout
        self._model = model
        self._max_repairs = max_repairs
        self._self_eval = self_eval
        self._max_eval_repairs = max_eval_repairs
        self._memory = memory
        # 瞬时基础设施错误（ACP -32603 内部错误 / -32000~-32099 服务端错误
        # 区间）时的退避重试，跟 Implementor 同一套判定标准。用户真实反馈：
        # kiro-cli ACP 协议模式偶发不稳定，原样重跑同一条命令常能成功
        # （见 lwa-bridge 项目 PROGRESS.md 的调查记录），不该让用户手动重试。
        # max_ask_retries=2 → 最多跑 3 次。
        self._max_ask_retries = max_ask_retries
        self._ask_retry_base_delay = ask_retry_base_delay
        # 最近一次自评结果，供调用方查看 / 日志
        self.last_evaluation: PlanEvaluation | None = None

    async def generate_plan(self, spec_text: str, cwd: Path) -> list[TaskPlan]:
        prompt = self._build_plan_prompt(spec_text)
        raw = await self._ask(prompt, cwd)
        tasks = parse_plan(raw)
        # 第一轮：机械校验 repair（环 / 未知依赖 / files_owned 重叠）
        for _ in range(self._max_repairs):
            err = plan_validation_error(tasks)
            if err is None:
                break
            logger.warning("[planner] plan invalid, asking Kiro to fix: %s", err)
            raw = await self._ask(REPAIR_PROMPT.format(error=err, plan=raw), cwd)
            tasks = parse_plan(raw)

        # 第二轮：LLM 自评 pass（语义质量检查）
        if self._self_eval:
            tasks, raw = await self._run_self_eval(spec_text, tasks, raw, cwd)

        return tasks

    def _build_plan_prompt(self, spec_text: str) -> str:
        """构造 plan prompt，注入记忆上下文（如果有）。"""
        base = PLAN_PROMPT.format(spec=spec_text)
        if not self._memory:
            return base

        context_parts: list[str] = []
        failures = self._memory.get_failure_patterns_text(limit=5)
        if failures:
            context_parts.append(
                "以下是此仓库历史 run 中总结的**失败模式**，规划时请注意避免：\n"
                + failures
            )
        examples = self._memory.get_plan_examples_text(limit=3)
        if examples:
            context_parts.append(
                "以下是此仓库被验证有效的**拆分示例**，可作为参考：\n" + examples
            )

        if not context_parts:
            return base

        memory_block = (
            "\n\n---\n历史记忆（只读参考，不要修改记忆本身）：\n"
            + "\n\n".join(context_parts)
            + "\n---\n\n"
        )
        return memory_block + base

    async def _run_self_eval(
        self,
        spec_text: str,
        tasks: list[TaskPlan],
        raw_plan: str,
        cwd: Path,
    ) -> tuple[list[TaskPlan], str]:
        """对 plan 做 LLM 自评，有 must_fix 项则触发修复。返回最终 tasks 和 raw。"""
        plan_json = _tasks_to_json(tasks)
        eval_raw = await self._ask(
            SELF_EVAL_PROMPT.format(spec=spec_text, plan=plan_json), cwd
        )
        try:
            evaluation = parse_evaluation(eval_raw)
        except PlanError:
            # 自评输出解析失败，不阻塞主流程，当作跳过
            logger.warning("[planner] self-eval output unparseable, skipping")
            self.last_evaluation = None
            return tasks, raw_plan

        self.last_evaluation = evaluation
        logger.info("[planner] self-eval: %s", evaluation.summary())

        # 分数合格或没有 must_fix → 不修
        if not evaluation.needs_repair or evaluation.score >= SELF_EVAL_PASS_THRESHOLD:
            return tasks, raw_plan

        # 有 must_fix 且分数低 → 触发修复
        for i in range(self._max_eval_repairs):
            logger.warning(
                "[planner] self-eval repair %d/%d: must_fix=%s",
                i + 1,
                self._max_eval_repairs,
                evaluation.must_fix,
            )
            must_fix_text = "\n".join(f"- {item}" for item in evaluation.must_fix)
            raw_plan = await self._ask(
                SELF_EVAL_REPAIR_PROMPT.format(
                    must_fix=must_fix_text,
                    eval_summary=evaluation.summary(),
                    plan=plan_json,
                ),
                cwd,
            )
            tasks = parse_plan(raw_plan)
            # 修完再做一次机械校验（修复可能引入新的结构错误）
            mech_err = plan_validation_error(tasks)
            if mech_err is not None:
                logger.warning("[planner] post-eval repair structural error: %s", mech_err)
                raw_plan = await self._ask(
                    REPAIR_PROMPT.format(error=mech_err, plan=raw_plan), cwd
                )
                tasks = parse_plan(raw_plan)
            # 更新 plan_json 供下次循环用
            plan_json = _tasks_to_json(tasks)

        return tasks, raw_plan

    async def _run_acp_once(self, prompt: str, cwd: Path, runtime: RuntimeConfig) -> str:
        """单次 ACP 调用，不含重试。拆出来方便单测直接 monkeypatch。"""
        from lwa_conduit.acp import AcpClient, AcpClientConfig, AgentMessageChunk, TurnEnd

        config = AcpClientConfig(
            kiro_cli_path=runtime.bin,
            cwd=cwd,
            response_timeout=self._prompt_timeout,
            model=self._model or runtime.model,
        )
        parts: list[str] = []
        async with await AcpClient.spawn(config) as client:
            await client.initialize()
            session_id = await client.new_session(cwd=cwd)
            events = await client.prompt(session_id, prompt)
            async for event in events:
                if isinstance(event, AgentMessageChunk):
                    parts.append(event.text)
                elif isinstance(event, TurnEnd):
                    break
        return "".join(parts)

    async def _ask(self, prompt: str, cwd: Path) -> str:
        runtime = resolve_runtime_for_prompt(self._runtime, prompt, role="planner")
        if runtime.kind == "cursor-agent-cli":
            from lwa_conduit.runtime.cursor_cli import cursor_prompt_text

            return await cursor_prompt_text(runtime, cwd=cwd, prompt=prompt)
        if runtime.kind == "gemini-cli":
            from lwa_conduit.runtime.gemini_cli import gemini_prompt_text

            return await gemini_prompt_text(runtime, cwd=cwd, prompt=prompt)

        from lwa_conduit.acp.messages import AcpError

        # 瞬时基础设施错误（ACP -32603 内部错误 / -32000~-32099 服务端错误
        # 区间）时的退避重试，判定标准跟 Implementor._run_acp 一致。用户
        # 真实反馈：kiro-cli ACP 协议模式偶发不稳定，原样重跑同一条命令
        # 常能成功，不该让用户手动重试（见 lwa-bridge 项目 PROGRESS.md）。
        for attempt in range(1, self._max_ask_retries + 2):
            try:
                return await self._run_acp_once(prompt, cwd, runtime)
            except (TimeoutError, ConnectionError, AcpError) as exc:
                retryable = not isinstance(exc, AcpError) or (
                    exc.code == -32603 or -32099 <= exc.code <= -32000
                )
                if retryable and attempt <= self._max_ask_retries:
                    delay = self._ask_retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "[planner] ACP attempt %d/%d failed: %s; retrying in %.1fs",
                        attempt,
                        self._max_ask_retries + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
        # 循环设计上总会在最后一次尝试内 return 或 raise，不会自然落到这里；
        # 加一行保证类型检查器满意，同时给出明确的失败信息而不是隐式 None。
        raise RuntimeError("planner._ask exhausted retries without returning or raising")
