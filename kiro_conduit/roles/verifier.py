"""Verifier 角色：跑 task.acceptance 里的命令，分层判断 PASS/FAIL。

CIV 三角色之一。本角色的边界：
- 输入：Task（含 acceptance 命令清单）+ TaskResult（含 cwd / diff）
- 输出：VerifyResult（每层结果 + 反馈）
- 不做：改代码（read-only），不做 LLM 评审（M1 才上）

M0 流水线（短路：前面挂了不走后面）：
- Layer 1 STATIC: acceptance 里非 pytest 命令（lint / type 等）
- Layer 2 DYNAMIC: acceptance 里 pytest / test 命令
- Layer 3 SEMANTIC: skip
- Layer 4 CONTRACT: skip
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kiro_conduit.types import (
    LayerResult,
    Task,
    TaskResult,
    VerifyLayer,
    VerifyResult,
)

logger = logging.getLogger(__name__)


# 简单启发式：命令含这些 token 视为动态测试
_DYNAMIC_HINTS = ("pytest", "unittest", "python -m test", "npm test", "jest")


class Verifier:
    """M0 Verifier：跑 shell 命令清单，分静态/动态两层。"""

    def __init__(self, command_timeout: float = 120.0) -> None:
        self._command_timeout = command_timeout

    async def verify(self, task: Task, result: TaskResult) -> VerifyResult:
        """跑验证流水线。"""
        if not result.success:
            # Implementor 都没成功，没必要往下跑
            return VerifyResult(
                task_id=task.id,
                passed=False,
                layers=[],
                feedback=f"Implementor failed: {result.error}",
            )

        static_cmds, dynamic_cmds = self._classify(task.acceptance)

        layers: list[LayerResult] = []
        feedback_parts: list[str] = []
        all_passed = True

        # Layer 1: STATIC
        if not static_cmds:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.STATIC,
                    passed=True,
                    output="(no static commands)",
                    skipped=True,
                )
            )
        else:
            r1 = await self._run_layer(VerifyLayer.STATIC, static_cmds, task.cwd)
            layers.append(r1)
            if not r1.passed:
                all_passed = False
                feedback_parts.append(f"[static failed]\n{r1.output}")

        # Layer 2: DYNAMIC（短路）
        if not all_passed:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.DYNAMIC,
                    passed=False,
                    output="(skipped because static failed)",
                    skipped=True,
                )
            )
        elif not dynamic_cmds:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.DYNAMIC,
                    passed=True,
                    output="(no dynamic commands)",
                    skipped=True,
                )
            )
        else:
            r2 = await self._run_layer(VerifyLayer.DYNAMIC, dynamic_cmds, task.cwd)
            layers.append(r2)
            if not r2.passed:
                all_passed = False
                feedback_parts.append(f"[dynamic failed]\n{r2.output}")

        # Layer 3 / 4 占位（M0 不做）
        for layer in (VerifyLayer.SEMANTIC, VerifyLayer.CONTRACT):
            layers.append(
                LayerResult(
                    layer=layer,
                    passed=True,
                    output="(not implemented in M0)",
                    skipped=True,
                )
            )

        feedback = "\n\n".join(feedback_parts) if feedback_parts else "all checks passed"
        return VerifyResult(
            task_id=task.id,
            passed=all_passed,
            layers=layers,
            feedback=feedback,
        )

    @staticmethod
    def _classify(acceptance: list[str]) -> tuple[list[str], list[str]]:
        static, dynamic = [], []
        for cmd in acceptance:
            lower = cmd.lower()
            if any(hint in lower for hint in _DYNAMIC_HINTS):
                dynamic.append(cmd)
            else:
                static.append(cmd)
        return static, dynamic

    async def _run_layer(
        self,
        layer: VerifyLayer,
        commands: list[str],
        cwd: Path,
    ) -> LayerResult:
        outputs: list[str] = []
        for cmd in commands:
            logger.info("[verifier %s] $ %s", layer, cmd)
            code, output = await self._run_shell(cmd, cwd)
            outputs.append(f"$ {cmd}\n{output}\n[exit={code}]")
            if code != 0:
                return LayerResult(
                    layer=layer,
                    passed=False,
                    output="\n\n".join(outputs),
                    skipped=False,
                )
        return LayerResult(
            layer=layer,
            passed=True,
            output="\n\n".join(outputs),
            skipped=False,
        )

    async def _run_shell(self, cmd: str, cwd: Path) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._command_timeout
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, f"(timeout after {self._command_timeout}s)"
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode("utf-8", errors="replace"),
        )
