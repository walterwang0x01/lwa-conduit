"""单元测试：Coordinator 重试与反馈逻辑。

用 mock Implementor / Verifier 单测，不起子进程。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from kiro_conduit.roles.coordinator import Coordinator
from kiro_conduit.types import (
    LayerResult,
    Task,
    TaskResult,
    VerifyLayer,
    VerifyResult,
)

# ------------------------------------------------------------------ mocks ---


@dataclass
class FakeImplementor:
    """按 task.prompt 决定返回什么的可编程 fake。"""

    behavior: Callable[[Task, int], TaskResult]
    calls: list[Task] = field(default_factory=list)

    async def run(self, task: Task) -> TaskResult:
        self.calls.append(task)
        return self.behavior(task, len(self.calls))


@dataclass
class FakeVerifier:
    """根据调用次数返回不同结果的可编程 fake。"""

    behavior: Callable[[Task, TaskResult, int], VerifyResult]
    calls: list[tuple[Task, TaskResult]] = field(default_factory=list)

    async def verify(self, task: Task, result: TaskResult) -> VerifyResult:
        self.calls.append((task, result))
        return self.behavior(task, result, len(self.calls))


def _make_task(cwd: Path) -> Task:
    return Task(id="t1", prompt="original prompt", cwd=cwd, acceptance=["true"])


def _success_task_result() -> TaskResult:
    return TaskResult(
        task_id="t1", success=True, diff="x", files_changed=["a.py"]
    )


def _pass_verify(task_id: str = "t1") -> VerifyResult:
    return VerifyResult(
        task_id=task_id,
        passed=True,
        layers=[
            LayerResult(layer=VerifyLayer.STATIC, passed=True, output="ok"),
        ],
        feedback="ok",
    )


def _fail_verify(task_id: str = "t1", layer: VerifyLayer = VerifyLayer.STATIC) -> VerifyResult:
    return VerifyResult(
        task_id=task_id,
        passed=False,
        layers=[
            LayerResult(layer=layer, passed=False, output="boom"),
        ],
        feedback=f"{layer} failed: boom",
    )


# ----------------------------------------------------------------- tests ---


class TestRunTask:
    @pytest.mark.asyncio
    async def test_pass_on_first_attempt(self, tmp_path: Path) -> None:
        impl = FakeImplementor(behavior=lambda task, _n: _success_task_result())
        ver = FakeVerifier(behavior=lambda *_: _pass_verify())
        coord = Coordinator(impl, ver, max_attempts=3)  # type: ignore[arg-type]

        outcome = await coord.run_task(_make_task(tmp_path))

        assert outcome.passed
        assert outcome.attempts == 1
        assert len(impl.calls) == 1
        assert len(ver.calls) == 1

    @pytest.mark.asyncio
    async def test_pass_on_second_attempt(self, tmp_path: Path) -> None:
        impl = FakeImplementor(behavior=lambda task, _n: _success_task_result())

        def verify_behavior(_t: Task, _r: TaskResult, n: int) -> VerifyResult:
            return _fail_verify() if n == 1 else _pass_verify()

        ver = FakeVerifier(behavior=verify_behavior)
        coord = Coordinator(impl, ver, max_attempts=3)  # type: ignore[arg-type]

        outcome = await coord.run_task(_make_task(tmp_path))

        assert outcome.passed
        assert outcome.attempts == 2
        assert len(outcome.history) == 2

    @pytest.mark.asyncio
    async def test_retry_prompt_contains_feedback(self, tmp_path: Path) -> None:
        """第二次调用 Implementor 时，prompt 应该包含上一次的 feedback。"""
        impl = FakeImplementor(behavior=lambda task, _n: _success_task_result())

        def verify_behavior(_t: Task, _r: TaskResult, n: int) -> VerifyResult:
            return _fail_verify() if n == 1 else _pass_verify()

        ver = FakeVerifier(behavior=verify_behavior)
        coord = Coordinator(impl, ver, max_attempts=3)  # type: ignore[arg-type]

        await coord.run_task(_make_task(tmp_path))

        assert len(impl.calls) == 2
        first_prompt = impl.calls[0].prompt
        second_prompt = impl.calls[1].prompt
        assert first_prompt == "original prompt"
        # 重试 prompt 应该携带原始任务 + 反馈
        assert "original prompt" in second_prompt
        assert "boom" in second_prompt
        assert "static" in second_prompt.lower()

    @pytest.mark.asyncio
    async def test_exhausts_max_attempts(self, tmp_path: Path) -> None:
        impl = FakeImplementor(behavior=lambda task, _n: _success_task_result())
        ver = FakeVerifier(behavior=lambda *_: _fail_verify())
        coord = Coordinator(impl, ver, max_attempts=3)  # type: ignore[arg-type]

        outcome = await coord.run_task(_make_task(tmp_path))

        assert not outcome.passed
        assert outcome.attempts == 3
        assert len(outcome.history) == 3

    @pytest.mark.asyncio
    async def test_implementor_failure_propagates_to_verifier(
        self, tmp_path: Path
    ) -> None:
        """Implementor 失败时仍然走 Verifier（由 Verifier 决定怎么处理）。"""
        impl = FakeImplementor(
            behavior=lambda task, _n: TaskResult(
                task_id="t1", success=False, diff="", files_changed=[], error="impl fail"
            )
        )
        ver = FakeVerifier(behavior=lambda *_: _fail_verify())
        coord = Coordinator(impl, ver, max_attempts=2)  # type: ignore[arg-type]

        outcome = await coord.run_task(_make_task(tmp_path))

        assert not outcome.passed
        # Verifier 仍被调用了
        assert len(ver.calls) == 2

    @pytest.mark.asyncio
    async def test_history_records_all_attempts(self, tmp_path: Path) -> None:
        impl = FakeImplementor(behavior=lambda task, _n: _success_task_result())
        ver = FakeVerifier(behavior=lambda *_: _fail_verify())
        coord = Coordinator(impl, ver, max_attempts=3)  # type: ignore[arg-type]

        outcome = await coord.run_task(_make_task(tmp_path))

        assert len(outcome.history) == 3
        for tr, vr in outcome.history:
            assert tr.task_id == "t1"
            assert vr.task_id == "t1"
            assert not vr.passed


class TestConstructor:
    def test_max_attempts_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            Coordinator(implementor=None, verifier=None, max_attempts=0)  # type: ignore[arg-type]
