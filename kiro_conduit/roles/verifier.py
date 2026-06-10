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
import os
from pathlib import Path
from typing import TYPE_CHECKING

from kiro_conduit.types import (
    LayerResult,
    Task,
    TaskResult,
    VerifyLayer,
    VerifyResult,
)

if TYPE_CHECKING:
    from kiro_conduit.semantic import SemanticReviewer

logger = logging.getLogger(__name__)


# 简单启发式：命令含这些 token 视为动态测试
_DYNAMIC_HINTS = ("pytest", "unittest", "python -m test", "npm test", "jest")


class Verifier:
    """M0 Verifier：跑 shell 命令清单，分静态/动态两层。"""

    def __init__(
        self,
        command_timeout: float = 120.0,
        *,
        contract_baselines: dict[str, str] | None = None,
        semantic_reviewer: SemanticReviewer | None = None,
        review_timeout: float = 180.0,
    ) -> None:
        """contract_baselines: file_path -> baseline_source。
        给 Layer 4 用：consumer 完成后，对比 worktree 里 file 的签名 vs baseline 的签名，
        要求**完全一致**（consumer 不能修改 owner 冻结的接口）。

        semantic_reviewer: 可插拔的 Layer 3 后端。None 表示不跑 Layer 3（skipped）。
        review_timeout: Layer 3 reviewer 的超时（秒），超时按 fail-open 处理。
        """
        # 局部 import 避免循环依赖（roles/verifier <-> semantic）
        from kiro_conduit.semantic import NoOpSemanticReviewer, SemanticReviewer  # noqa: F401

        self._command_timeout = command_timeout
        self._contract_baselines = contract_baselines or {}
        # None 显式区别于 NoOp：None 时 Layer 3 完全 skipped，给个 NoOp 时也 skipped 但有 feedback
        self._semantic_reviewer = semantic_reviewer
        self._review_timeout = review_timeout

    async def verify(self, task: Task, result: TaskResult) -> VerifyResult:
        """跑验证流水线。"""
        if not result.success and not result.no_changes:
            # Implementor 真出错（ACP/ git 失败），没必要往下跑
            return VerifyResult(
                task_id=task.id,
                passed=False,
                layers=[],
                feedback=f"Implementor failed: {result.error}",
            )
        # no_changes：可能是依赖已把活干了（幂等/wiring 任务）。不直接判失败，
        # 照常跑 acceptance —— 过了就算 PASS，没过才是真失败。

        static_cmds, dynamic_cmds = self._classify(task.acceptance)

        # no_changes 且没有任何 acceptance 命令 → 无从验证目标是否达成，判失败
        # （避免"agent 啥也没干、又没东西可验"被误当 PASS）。
        if result.no_changes and not static_cmds and not dynamic_cmds:
            return VerifyResult(
                task_id=task.id,
                passed=False,
                layers=[],
                feedback="no files changed and no acceptance commands to verify",
            )

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
            r1 = await self._run_layer(VerifyLayer.STATIC, static_cmds, task.cwd, task.env)
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
            r2 = await self._run_layer(VerifyLayer.DYNAMIC, dynamic_cmds, task.cwd, task.env)
            layers.append(r2)
            if not r2.passed:
                all_passed = False
                feedback_parts.append(f"[dynamic failed]\n{r2.output}")

        # Layer 3 SEMANTIC：跑可插拔的 reviewer
        if not all_passed:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.SEMANTIC,
                    passed=False,
                    output="(skipped because earlier layer failed)",
                    skipped=True,
                )
            )
        elif self._semantic_reviewer is None:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.SEMANTIC,
                    passed=True,
                    output="(no semantic reviewer configured)",
                    skipped=True,
                )
            )
        else:
            r3 = await self._run_semantic_layer(task, result)
            layers.append(r3)
            if not r3.passed:
                all_passed = False
                feedback_parts.append(f"[semantic failed]\n{r3.output}")

        # Layer 4 CONTRACT：检查 consumer 没改 owner 冻结的接口
        if not all_passed:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.CONTRACT,
                    passed=False,
                    output="(skipped because earlier layer failed)",
                    skipped=True,
                )
            )
        elif not self._contract_baselines:
            layers.append(
                LayerResult(
                    layer=VerifyLayer.CONTRACT,
                    passed=True,
                    output="(no interface contracts to check)",
                    skipped=True,
                )
            )
        else:
            r4 = self._run_contract_layer(task.cwd)
            layers.append(r4)
            if not r4.passed:
                all_passed = False
                feedback_parts.append(f"[contract failed]\n{r4.output}")

        feedback = "\n\n".join(feedback_parts) if feedback_parts else "all checks passed"
        return VerifyResult(
            task_id=task.id,
            passed=all_passed,
            layers=layers,
            feedback=feedback,
        )

    async def _run_semantic_layer(
        self, task: Task, result: TaskResult
    ) -> LayerResult:
        """跑 self._semantic_reviewer 一次。reviewer 不能为 None（调用方保证）。"""
        from kiro_conduit.semantic import ReviewContext, run_with_timeout

        assert self._semantic_reviewer is not None
        ctx = ReviewContext(
            task_id=task.id,
            task_prompt=task.prompt,
            diff=result.diff,
            cwd=task.cwd,
        )
        review = await run_with_timeout(
            self._semantic_reviewer, ctx, timeout=self._review_timeout
        )
        return LayerResult(
            layer=VerifyLayer.SEMANTIC,
            passed=review.passed,
            output=review.feedback,
            skipped=False,
        )

    def _run_contract_layer(self, cwd: Path) -> LayerResult:
        """对每个 baseline file 跑签名对比。任意一个不一致就 fail。"""
        from kiro_conduit.contracts import diff_signatures, extract_signatures

        violations: list[str] = []
        for file_path, baseline_source in self._contract_baselines.items():
            full = cwd / file_path
            if not full.is_file():
                violations.append(
                    f"{file_path}: file missing in worktree (was the stub deleted?)"
                )
                continue
            try:
                current_source = full.read_text(encoding="utf-8")
            except OSError as exc:
                violations.append(f"{file_path}: read failed: {exc}")
                continue
            old_sigs = extract_signatures(baseline_source)
            new_sigs = extract_signatures(current_source)
            d = diff_signatures(old_sigs, new_sigs)
            if not d.is_empty:
                violations.append(f"{file_path}:\n{d.to_message()}")

        if violations:
            return LayerResult(
                layer=VerifyLayer.CONTRACT,
                passed=False,
                output="\n\n".join(violations),
                skipped=False,
            )
        return LayerResult(
            layer=VerifyLayer.CONTRACT,
            passed=True,
            output=f"(checked {len(self._contract_baselines)} interface file(s))",
            skipped=False,
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
        env: dict[str, str] | None = None,
    ) -> LayerResult:
        outputs: list[str] = []
        for cmd in commands:
            logger.info("[verifier %s] $ %s", layer, cmd)
            code, output = await self._run_shell(cmd, cwd, env)
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

    async def _run_shell(
        self, cmd: str, cwd: Path, env: dict[str, str] | None = None
    ) -> tuple[int, str]:
        # 注入的隔离 env 覆盖到继承的环境之上（端口区间 / scratch / task-id 等）
        proc_env = {**os.environ, **env} if env else None
        proc = await asyncio.create_subprocess_shell(
            cmd,
            cwd=str(cwd),
            env=proc_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            stdout_b, _ = await asyncio.wait_for(
                proc.communicate(), timeout=self._command_timeout
            )
        except TimeoutError:
            from kiro_conduit.proc_util import reap

            await reap(proc)  # 连根杀（pytest/npm 等子进程不留孤儿）
            return 124, f"(timeout after {self._command_timeout}s)"
        return (
            proc.returncode if proc.returncode is not None else -1,
            stdout_b.decode("utf-8", errors="replace"),
        )
