"""单元测试：planner 纯核心（不调 LLM）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lwa_conduit.dag import load_workspace, topological_waves
from lwa_conduit.planner import (
    PLAN_PROMPT,
    PlanError,
    TaskPlan,
    compute_layers,
    parse_plan,
    render_dag_yaml,
    write_plan,
)

_PLAN_JSON = """
some preamble...
```json
{
  "tasks": [
    {
      "id": "a", "prompt": "build a",
      "files_owned": ["a.py"], "acceptance": ["pytest -q test_a.py"]
    },
    {"id": "b", "prompt": "build b", "depends_on": ["a"], "files_owned": ["b.py"]}
  ]
}
```
trailing text
"""


class TestParsePlan:
    def test_parses_fenced_json_with_noise(self) -> None:
        tasks = parse_plan(_PLAN_JSON)
        assert [t.id for t in tasks] == ["a", "b"]
        assert tasks[0].files_owned == ["a.py"]
        assert tasks[1].depends_on == ["a"]

    def test_parses_bare_json(self) -> None:
        tasks = parse_plan('{"tasks":[{"id":"x","prompt":"do x"}]}')
        assert tasks[0].id == "x"

    def test_rejects_non_json(self) -> None:
        with pytest.raises(PlanError, match="parse plan JSON"):
            parse_plan("no json here")

    def test_rejects_empty_tasks(self) -> None:
        with pytest.raises(PlanError, match="non-empty 'tasks'"):
            parse_plan('{"tasks": []}')

    def test_rejects_missing_id(self) -> None:
        with pytest.raises(PlanError, match="missing non-empty 'id'"):
            parse_plan('{"tasks":[{"prompt":"x"}]}')

    def test_rejects_duplicate_id(self) -> None:
        with pytest.raises(PlanError, match="duplicate task id"):
            parse_plan('{"tasks":[{"id":"a","prompt":"x"},{"id":"a","prompt":"y"}]}')

    def test_rejects_missing_prompt(self) -> None:
        with pytest.raises(PlanError, match="missing non-empty 'prompt'"):
            parse_plan('{"tasks":[{"id":"a"}]}')


class TestComputeLayers:
    def test_linear_chain(self) -> None:
        tasks = [
            TaskPlan(id="a", prompt="a"),
            TaskPlan(id="b", prompt="b", depends_on=["a"]),
            TaskPlan(id="c", prompt="c", depends_on=["b"]),
        ]
        assert compute_layers(tasks) == [["a"], ["b"], ["c"]]

    def test_parallel_layer(self) -> None:
        tasks = [
            TaskPlan(id="root", prompt="r"),
            TaskPlan(id="x", prompt="x", depends_on=["root"]),
            TaskPlan(id="y", prompt="y", depends_on=["root"]),
        ]
        assert compute_layers(tasks) == [["root"], ["x", "y"]]

    def test_unknown_dependency(self) -> None:
        with pytest.raises(PlanError, match="unknown task"):
            compute_layers([TaskPlan(id="a", prompt="a", depends_on=["ghost"])])

    def test_cycle(self) -> None:
        tasks = [
            TaskPlan(id="a", prompt="a", depends_on=["b"]),
            TaskPlan(id="b", prompt="b", depends_on=["a"]),
        ]
        with pytest.raises(PlanError, match="cycle"):
            compute_layers(tasks)


class TestRenderAndWrite:
    def test_render_is_loadable(self, tmp_path: Path) -> None:
        tasks = parse_plan(_PLAN_JSON)
        dag = render_dag_yaml(tasks)
        p = tmp_path / "dag.yaml"
        p.write_text(dag, encoding="utf-8")
        # 必须能被 dag 加载器解析且校验通过
        (tmp_path / "specs").mkdir()
        (tmp_path / "specs" / "a.md").write_text("a")
        (tmp_path / "specs" / "b.md").write_text("b")
        ws = load_workspace(p)
        assert set(ws.tasks) == {"a", "b"}
        assert topological_waves(ws) == [["a"], ["b"]]

    def test_write_plan_creates_files_and_validates(self, tmp_path: Path) -> None:
        tasks = parse_plan(_PLAN_JSON)
        out = tmp_path / "ws"
        dag_path = write_plan(tasks, out)
        assert dag_path == out / "dag.yaml"
        assert (out / "specs" / "a.md").read_text().startswith("build a")
        assert (out / "specs" / "b.md").read_text().startswith("build b")
        # 已通过 write_plan 内部 load_workspace 校验；再确认波次
        ws = load_workspace(dag_path)
        assert topological_waves(ws) == [["a"], ["b"]]

    def test_write_plan_rejects_cycle(self, tmp_path: Path) -> None:
        tasks = [
            TaskPlan(id="a", prompt="a", depends_on=["b"]),
            TaskPlan(id="b", prompt="b", depends_on=["a"]),
        ]
        with pytest.raises(PlanError, match="cycle"):
            write_plan(tasks, tmp_path / "ws")

    def test_write_plan_overlap_raises_planerror_but_keeps_files(
        self, tmp_path: Path
    ) -> None:
        """files_owned 重叠 → 校验失败应转成 PlanError（不是裸 DagError），
        且 dag.yaml 仍落盘，供人按提示修正。"""
        tasks = [
            TaskPlan(id="a", prompt="a", files_owned=["src/x.py"]),
            TaskPlan(id="b", prompt="b", files_owned=["src/x.py"]),
        ]
        out = tmp_path / "ws"
        with pytest.raises(PlanError, match="校验"):
            write_plan(tasks, out)
        assert (out / "dag.yaml").is_file()  # 产物保留，供手动修正


class TestPlanPrompt:
    """PLAN_PROMPT 应指示把项目 linter 纳入每个 task 的 acceptance。"""

    def test_prompt_requires_linter_in_acceptance(self) -> None:
        assert "linter" in PLAN_PROMPT and "ruff check" in PLAN_PROMPT

    def test_prompt_biases_toward_pr_sized_tasks(self) -> None:
        # 粒度启发：PR 大小 + 别拆强耦合
        assert "PR" in PLAN_PROMPT
        assert "强耦合" in PLAN_PROMPT


class TestPlanValidationAndRepair:
    """plan_validation_error 纯校验 + KiroPlanner 自动修复重试。"""

    def test_validation_detects_overlap(self) -> None:
        from lwa_conduit.planner import plan_validation_error
        tasks = [
            TaskPlan(id="a", prompt="a", files_owned=["src/x.py"]),
            TaskPlan(id="b", prompt="b", files_owned=["src/x.py"]),
        ]
        err = plan_validation_error(tasks)
        assert err is not None and "src/x.py" in err

    def test_validation_detects_cycle(self) -> None:
        from lwa_conduit.planner import plan_validation_error
        tasks = [
            TaskPlan(id="a", prompt="a", depends_on=["b"]),
            TaskPlan(id="b", prompt="b", depends_on=["a"]),
        ]
        assert plan_validation_error(tasks) is not None

    def test_validation_passes_clean(self) -> None:
        from lwa_conduit.planner import plan_validation_error
        tasks = [
            TaskPlan(id="a", prompt="a", files_owned=["src/a.py"]),
            TaskPlan(id="b", prompt="b", files_owned=["src/b.py"], depends_on=["a"]),
        ]
        assert plan_validation_error(tasks) is None

    @pytest.mark.asyncio
    async def test_generate_plan_auto_repairs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """首拆 files_owned 重叠 → 自动把错误喂回、第二次拆干净 → 返回修好的。"""
        from lwa_conduit.planner import KiroPlanner

        bad = '{"tasks":[{"id":"a","prompt":"a","files_owned":["src/x.py"]},' \
              '{"id":"b","prompt":"b","files_owned":["src/x.py"]}]}'
        good = '{"tasks":[{"id":"a","prompt":"a","files_owned":["src/a.py"]},' \
               '{"id":"b","prompt":"b","files_owned":["src/b.py"]}]}'
        calls: list[str] = []

        async def fake_ask(self, prompt: str, cwd: Path) -> str:  # type: ignore[no-untyped-def]
            calls.append(prompt)
            return bad if len(calls) == 1 else good

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner(self_eval=False)
        tasks = await planner.generate_plan("spec", tmp_path)
        assert {t.id for t in tasks} == {"a", "b"}
        assert len(calls) == 2  # 修复了一次
        assert "校验" in calls[1] or "没通过" in calls[1] or "src/x.py" in calls[1]


class TestAskRetriesOnTransientAcpError:
    """
    _ask() 内部重试逻辑：用户真实反馈 ACP -32603 是偶发的、非确定性错误，
    原样重跑常能成功，不该让用户手动重试（见 lwa-bridge PROGRESS.md）。
    判定标准跟 Implementor._run_acp 保持一致：-32603 与 -32000~-32099
    服务端错误区间视为瞬时重试；其它协议错（如 -32601）不重试。
    """

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch: pytest.MonkeyPatch) -> list[float]:
        """patch asyncio.sleep：不真等，记录每次退避时长。"""
        import lwa_conduit.planner as planner_mod

        slept: list[float] = []

        async def fake_sleep(delay: float) -> None:
            slept.append(delay)

        monkeypatch.setattr(planner_mod.asyncio, "sleep", fake_sleep)
        return slept

    async def test_retries_on_acp_internal_error_and_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
    ) -> None:
        """第一、二次报 -32603，第三次成功：应该拿到结果，不抛异常。"""
        from lwa_conduit.acp.messages import AcpError
        from lwa_conduit.planner import KiroPlanner

        calls = {"n": 0}

        async def flaky(self, prompt, cwd, runtime):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] < 3:
                raise AcpError(code=-32603, message="Internal error")
            return "ok result"

        monkeypatch.setattr(KiroPlanner, "_run_acp_once", flaky)
        planner = KiroPlanner(max_ask_retries=2, model="claude-sonnet-4.6")
        result = await planner._ask("prompt", tmp_path)
        assert result == "ok result"
        assert calls["n"] == 3  # 重试了两次
        assert _no_sleep == [1.0, 2.0]  # 指数退避：1.0 → 2.0

    async def test_deterministic_acp_error_does_not_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
    ) -> None:
        """-32601（方法不存在）是确定性协议错：不重试，直接把异常抛给调用方。"""
        from lwa_conduit.acp.messages import AcpError
        from lwa_conduit.planner import KiroPlanner

        calls = {"n": 0}

        async def always(self, prompt, cwd, runtime):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise AcpError(code=-32601, message="Method not found")

        monkeypatch.setattr(KiroPlanner, "_run_acp_once", always)
        planner = KiroPlanner(max_ask_retries=2, model="claude-sonnet-4.6")
        with pytest.raises(AcpError) as exc_info:
            await planner._ask("prompt", tmp_path)
        assert exc_info.value.code == -32601
        assert calls["n"] == 1  # 没重试
        assert _no_sleep == []

    async def test_exhausts_retries_then_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
    ) -> None:
        """一直报可重试错误：用完 max_ask_retries 次重试后，最终把异常抛出去。"""
        from lwa_conduit.acp.messages import AcpError
        from lwa_conduit.planner import KiroPlanner

        calls = {"n": 0}

        async def always_fails(self, prompt, cwd, runtime):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            raise AcpError(code=-32603, message="Internal error")

        monkeypatch.setattr(KiroPlanner, "_run_acp_once", always_fails)
        planner = KiroPlanner(max_ask_retries=2, model="claude-sonnet-4.6")
        with pytest.raises(AcpError) as exc_info:
            await planner._ask("prompt", tmp_path)
        assert exc_info.value.code == -32603
        assert calls["n"] == 3  # 首次 + 2 次重试
        assert _no_sleep == [1.0, 2.0]

    async def test_server_error_range_is_retryable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
    ) -> None:
        """-32050 落在服务端错误区间(-32000~-32099)内，同样应该重试。"""
        from lwa_conduit.acp.messages import AcpError
        from lwa_conduit.planner import KiroPlanner

        calls = {"n": 0}

        async def flaky(self, prompt, cwd, runtime):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            if calls["n"] < 2:
                raise AcpError(code=-32050, message="Server error")
            return "ok"

        monkeypatch.setattr(KiroPlanner, "_run_acp_once", flaky)
        planner = KiroPlanner(max_ask_retries=2, model="claude-sonnet-4.6")
        result = await planner._ask("prompt", tmp_path)
        assert result == "ok"
        assert calls["n"] == 2

    async def test_no_retries_when_first_attempt_succeeds(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _no_sleep: list[float]
    ) -> None:
        """正常路径：第一次就成功，不应该有任何 sleep/重试开销。"""
        from lwa_conduit.planner import KiroPlanner

        calls = {"n": 0}

        async def ok(self, prompt, cwd, runtime):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return "ok"

        monkeypatch.setattr(KiroPlanner, "_run_acp_once", ok)
        planner = KiroPlanner(max_ask_retries=2, model="claude-sonnet-4.6")
        result = await planner._ask("prompt", tmp_path)
        assert result == "ok"
        assert calls["n"] == 1
        assert _no_sleep == []


class TestParseEvaluation:
    """parse_evaluation 纯解析（不调 LLM）。"""

    def test_parses_full_evaluation(self) -> None:
        from lwa_conduit.planner import parse_evaluation

        raw = """```json
{
  "score": 72,
  "dimensions": {
    "coverage": {"score": 80, "issues": ["漏了认证模块"]},
    "granularity": {"score": 70, "issues": []},
    "coupling": {"score": 65, "issues": ["task-a 和 task-b 改了同一个 router"]},
    "dependencies": {"score": 75, "issues": []},
    "clarity": {"score": 70, "issues": ["task-c prompt 太模糊"]}
  },
  "must_fix": ["task-a 和 task-b 都改 src/router.py，需要合并或加 depends_on"],
  "suggestions": ["task-c 的 prompt 可以更具体"]
}
```"""
        ev = parse_evaluation(raw)
        assert ev.score == 72
        assert ev.coverage.score == 80
        assert ev.coverage.issues == ["漏了认证模块"]
        assert ev.coupling.score == 65
        assert len(ev.must_fix) == 1
        assert ev.needs_repair is True
        assert "72" in ev.summary()

    def test_parses_minimal_evaluation(self) -> None:
        from lwa_conduit.planner import parse_evaluation

        raw = '{"score": 85, "dimensions": {}, "must_fix": [], "suggestions": []}'
        ev = parse_evaluation(raw)
        assert ev.score == 85
        assert ev.needs_repair is False
        # 未提供维度 → 默认 0 分
        assert ev.coverage.score == 0

    def test_handles_missing_fields_gracefully(self) -> None:
        from lwa_conduit.planner import parse_evaluation

        raw = '{"score": 60}'
        ev = parse_evaluation(raw)
        assert ev.score == 60
        assert ev.must_fix == []
        assert ev.needs_repair is False

    def test_rejects_non_json(self) -> None:
        from lwa_conduit.planner import parse_evaluation

        with pytest.raises(PlanError, match="parse plan JSON"):
            parse_evaluation("this is not json")


class TestSelfEval:
    """KiroPlanner 自评 pass 集成测试（mock _ask）。"""

    @pytest.mark.asyncio
    async def test_self_eval_pass_no_must_fix(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自评通过（高分 + 无 must_fix）→ 不触发额外修复。"""
        from lwa_conduit.planner import KiroPlanner

        good_plan = '{"tasks":[{"id":"a","prompt":"build a","files_owned":["src/a.py"]}]}'
        good_eval = '{"score":85,"dimensions":{},"must_fix":[],"suggestions":["可改进"]}'
        calls: list[str] = []

        async def fake_ask(self: object, prompt: str, cwd: Path) -> str:
            calls.append(prompt[:30])
            if "规划审查器" in prompt:
                return good_eval
            return good_plan

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner()
        tasks = await planner.generate_plan("my spec", tmp_path)
        assert len(tasks) == 1
        # 2 次调用：1 次 plan + 1 次 self-eval（无 repair）
        assert len(calls) == 2
        assert planner.last_evaluation is not None
        assert planner.last_evaluation.score == 85

    @pytest.mark.asyncio
    async def test_self_eval_triggers_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自评发现 must_fix + 低分 → 触发一次修复。"""
        from lwa_conduit.planner import KiroPlanner

        plan_v1 = (
            '{"tasks":['
            '{"id":"a","prompt":"build a","files_owned":["src/a.py"]},'
            '{"id":"b","prompt":"build b","files_owned":["src/b.py"]}'
            ']}'
        )
        plan_v2 = (
            '{"tasks":['
            '{"id":"ab","prompt":"build a and b together","files_owned":["src/a.py","src/b.py"]}'
            ']}'
        )
        eval_bad = (
            '{"score":50,"dimensions":{},'
            '"must_fix":["task-a 和 task-b 强耦合应合并"],'
            '"suggestions":[]}'
        )
        call_count = 0

        async def fake_ask(self: object, prompt: str, cwd: Path) -> str:
            nonlocal call_count
            call_count += 1
            if "规划审查器" in prompt:
                return eval_bad
            if "必须修复" in prompt:
                return plan_v2
            return plan_v1

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner()
        tasks = await planner.generate_plan("my spec", tmp_path)
        # 修复后合成了一个 task
        assert len(tasks) == 1
        assert tasks[0].id == "ab"
        # 3 次调用：plan + eval + repair
        assert call_count == 3
        assert planner.last_evaluation is not None
        assert planner.last_evaluation.needs_repair is True

    @pytest.mark.asyncio
    async def test_self_eval_skipped_when_disabled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """self_eval=False → 跳过自评。"""
        from lwa_conduit.planner import KiroPlanner

        good_plan = '{"tasks":[{"id":"a","prompt":"build a","files_owned":["src/a.py"]}]}'
        calls: list[str] = []

        async def fake_ask(self: object, prompt: str, cwd: Path) -> str:
            calls.append(prompt[:30])
            return good_plan

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner(self_eval=False)
        tasks = await planner.generate_plan("spec", tmp_path)
        assert len(tasks) == 1
        assert len(calls) == 1  # 只有 plan，没有 eval
        assert planner.last_evaluation is None

    @pytest.mark.asyncio
    async def test_self_eval_unparseable_graceful(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """自评输出不可解析 → 不崩溃，跳过自评继续返回。"""
        from lwa_conduit.planner import KiroPlanner

        good_plan = '{"tasks":[{"id":"a","prompt":"build a","files_owned":["src/a.py"]}]}'

        async def fake_ask(self: object, prompt: str, cwd: Path) -> str:
            if "规划审查器" in prompt:
                return "sorry I cannot evaluate this"
            return good_plan

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner()
        tasks = await planner.generate_plan("spec", tmp_path)
        assert len(tasks) == 1
        assert planner.last_evaluation is None


class TestTasksToJson:
    """_tasks_to_json 辅助函数。"""

    def test_round_trips(self) -> None:
        from lwa_conduit.planner import _tasks_to_json

        tasks = [
            TaskPlan(id="a", prompt="do a", files_owned=["x.py"], depends_on=["root"]),
            TaskPlan(id="b", prompt="do b"),
        ]
        raw = _tasks_to_json(tasks)
        parsed = json.loads(raw)
        assert len(parsed["tasks"]) == 2
        assert parsed["tasks"][0]["id"] == "a"
        assert parsed["tasks"][0]["depends_on"] == ["root"]
        # b 没有 depends_on/files_owned，不应出现在 JSON 里
        assert "depends_on" not in parsed["tasks"][1]


class TestPlannerMemoryInjection:
    """KiroPlanner 注入 memory 上下文到 plan prompt。"""

    @pytest.mark.asyncio
    async def test_memory_injected_into_prompt(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """有记忆时 plan prompt 应包含失败模式和示例。"""
        from lwa_conduit.memory import Memory
        from lwa_conduit.planner import KiroPlanner

        mem = Memory()
        mem.add_failure_pattern(
            pattern="consumer 改了接口文件",
            root_cause="contract 校验拒绝",
            resolution="spec 里明确说不要改",
        )
        mem.add_plan_example("支付模块", ["pay-base", "pay-hook"], score=88)

        good_plan = '{"tasks":[{"id":"a","prompt":"build a","files_owned":["src/a.py"]}]}'
        captured_prompts: list[str] = []

        async def fake_ask(self: object, prompt: str, cwd: Path) -> str:
            captured_prompts.append(prompt)
            return good_plan

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner(self_eval=False, memory=mem)
        await planner.generate_plan("my spec", tmp_path)

        # plan prompt（第一次调用）应包含记忆内容
        plan_prompt = captured_prompts[0]
        assert "失败模式" in plan_prompt
        assert "consumer 改了接口文件" in plan_prompt
        assert "拆分示例" in plan_prompt
        assert "支付模块" in plan_prompt

    @pytest.mark.asyncio
    async def test_no_memory_no_injection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """无记忆时 prompt 不含历史记忆块。"""
        from lwa_conduit.planner import KiroPlanner

        good_plan = '{"tasks":[{"id":"a","prompt":"build a","files_owned":["src/a.py"]}]}'
        captured_prompts: list[str] = []

        async def fake_ask(self: object, prompt: str, cwd: Path) -> str:
            captured_prompts.append(prompt)
            return good_plan

        monkeypatch.setattr(KiroPlanner, "_ask", fake_ask)
        planner = KiroPlanner(self_eval=False, memory=None)
        await planner.generate_plan("my spec", tmp_path)

        plan_prompt = captured_prompts[0]
        assert "历史记忆" not in plan_prompt
