"""单元测试：Verifier 流水线。

跑真 shell（echo / false / sleep）但不调 kiro-cli。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kiro_conduit.roles.verifier import Verifier
from kiro_conduit.types import Task, TaskResult, VerifyLayer


def _make_task(cwd: Path, acceptance: list[str]) -> Task:
    return Task(id="t1", prompt="dummy", cwd=cwd, acceptance=acceptance)


def _make_success_result() -> TaskResult:
    return TaskResult(
        task_id="t1",
        success=True,
        diff="x",
        files_changed=["a.py"],
    )


class TestClassify:
    def test_pytest_goes_to_dynamic(self) -> None:
        static, dynamic = Verifier._classify(["ruff check .", "pytest -q"])
        assert static == ["ruff check ."]
        assert dynamic == ["pytest -q"]

    def test_unittest_goes_to_dynamic(self) -> None:
        _static, dynamic = Verifier._classify(["python -m unittest"])
        assert dynamic == ["python -m unittest"]

    def test_npm_test_goes_to_dynamic(self) -> None:
        _static, dynamic = Verifier._classify(["npm test"])
        assert dynamic == ["npm test"]

    def test_other_commands_default_to_static(self) -> None:
        static, dynamic = Verifier._classify(["echo hi", "ls"])
        assert static == ["echo hi", "ls"]
        assert dynamic == []


class TestVerifyHappyPath:
    @pytest.mark.asyncio
    async def test_no_acceptance_all_skipped(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, [])
        result = await Verifier().verify(task, _make_success_result())
        assert result.passed
        assert all(layer.skipped for layer in result.layers)

    @pytest.mark.asyncio
    async def test_passing_static_only(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, ["true"])
        result = await Verifier().verify(task, _make_success_result())
        assert result.passed
        static = next(layer for layer in result.layers if layer.layer == VerifyLayer.STATIC)
        assert static.passed and not static.skipped
        dynamic = next(layer for layer in result.layers if layer.layer == VerifyLayer.DYNAMIC)
        assert dynamic.skipped

    @pytest.mark.asyncio
    async def test_passing_static_and_dynamic(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, ["true", "pytest --version"])
        result = await Verifier().verify(task, _make_success_result())
        assert result.passed


class TestVerifyShortCircuit:
    @pytest.mark.asyncio
    async def test_static_failure_skips_dynamic(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, ["false", "pytest --version"])
        result = await Verifier().verify(task, _make_success_result())
        assert not result.passed
        assert result.failed_layer == VerifyLayer.STATIC
        dynamic = next(layer for layer in result.layers if layer.layer == VerifyLayer.DYNAMIC)
        assert dynamic.skipped, "dynamic layer must be skipped when static fails"

    @pytest.mark.asyncio
    async def test_dynamic_failure(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, ["true", "pytest --invalid-arg-xxx"])
        result = await Verifier().verify(task, _make_success_result())
        assert not result.passed
        assert result.failed_layer == VerifyLayer.DYNAMIC


class TestVerifyImplementorFailure:
    @pytest.mark.asyncio
    async def test_skips_when_implementor_failed(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, ["true"])
        bad_result = TaskResult(
            task_id="t1",
            success=False,
            diff="",
            files_changed=[],
            error="boom",
        )
        result = await Verifier().verify(task, bad_result)
        assert not result.passed
        assert "boom" in result.feedback
        # 没跑任何层
        assert result.layers == []


class TestVerifyTimeout:
    @pytest.mark.asyncio
    async def test_long_command_times_out(self, tmp_path: Path) -> None:
        task = _make_task(tmp_path, [f"{sys.executable} -c 'import time; time.sleep(10)'"])
        verifier = Verifier(command_timeout=0.5)
        result = await verifier.verify(task, _make_success_result())
        assert not result.passed
        # 超时被归为 STATIC 失败（命令不含 pytest 等关键字）
        assert result.failed_layer == VerifyLayer.STATIC
