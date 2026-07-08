"""Implementor 角色：在指定工作目录里跑 Kiro CLI，让它实施一个任务。

CIV 三角色之一。本角色的边界：
- 输入：Task（含 prompt + cwd）
- 输出：TaskResult（git diff + files_changed + transcript）
- 不做：DAG 调度、验证、merge —— 那是 Coordinator / Verifier / MergeOrchestrator 的事

M0 实现策略：
- 每个任务起一个独立 ACP 子进程（短生命周期）
- prompt 流式收 AgentMessageChunk 拼成 transcript
- TurnEnd 后跑 `git status --porcelain` + `git diff` 收集变更
"""

from __future__ import annotations

import asyncio
import logging

from kiro_conduit.acp import (
    AcpClient,
    AcpClientConfig,
    AcpError,
    AgentMessageChunk,
    AgentThoughtChunk,
    ToolCallEvent,
    TurnEnd,
)
from kiro_conduit.git_utils import collect_diff, list_changed_files
from kiro_conduit.runtime.cursor_cli import cursor_prompt_stream
from kiro_conduit.runtime.types import RuntimeConfig
from kiro_conduit.types import Task, TaskResult

logger = logging.getLogger(__name__)


class Implementor:
    """单任务 Implementor。每次调用 run() 起一个独立 Kiro 子进程。"""

    def __init__(
        self,
        runtime: RuntimeConfig | None = None,
        *,
        kiro_cli_path: str = "kiro-cli",
        prompt_timeout: float = 600.0,
        model: str | None = None,
        max_retries: int = 2,
        retry_base_delay: float = 1.0,
        sandbox: bool = False,
        idle_timeout: float = 300.0,
    ) -> None:
        self._runtime = runtime or RuntimeConfig.from_cli(
            kiro_cli=kiro_cli_path, model=model, timeout=prompt_timeout
        )
        self._prompt_timeout = prompt_timeout
        self._idle_timeout = idle_timeout
        self._model = model
        self._sandbox = sandbox
        # 瞬时基础设施错误（超时 / 连接）时的退避重试。max_retries=2 → 最多跑 3 次。
        self._max_retries = max_retries
        self._retry_base_delay = retry_base_delay

    async def run(self, task: Task) -> TaskResult:
        """执行任务，返回结果。瞬时 ACP 错误会退避重试。"""
        logger.info(
            "[implementor] start task=%s model=%s cwd=%s",
            task.id,
            self._model or "<default>",
            task.cwd,
        )
        transcript_parts: list[str] = []

        for attempt in range(1, self._max_retries + 2):
            try:
                transcript_parts = await self._run_acp(task)
                break
            except (TimeoutError, ConnectionError, AcpError) as exc:
                # AcpError：内部错误(-32603)与服务端错误区间(-32000~-32099)视为瞬时、退避重试；
                # 其它确定性协议错(如 -32601)不重试，但落到下面优雅判失败，而不是崩(attempts=0)。
                retryable = not isinstance(exc, AcpError) or (
                    exc.code == -32603 or -32099 <= exc.code <= -32000
                )
                if retryable and attempt <= self._max_retries:
                    delay = self._retry_base_delay * (2 ** (attempt - 1))
                    logger.warning(
                        "[implementor] task=%s ACP attempt %d/%d failed: %s; "
                        "retrying in %.1fs",
                        task.id,
                        attempt,
                        self._max_retries + 1,
                        exc,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                logger.error(
                    "[implementor] task=%s ACP failed (no more retries): %s", task.id, exc
                )
                return TaskResult(
                    task_id=task.id,
                    success=False,
                    diff="",
                    files_changed=[],
                    error=f"{type(exc).__name__}: {exc}",
                    transcript="".join(transcript_parts),
                )

        # 收集 git 改动
        try:
            files_changed = await list_changed_files(task.cwd)
            diff = await collect_diff(task.cwd)
        except RuntimeError as exc:
            logger.warning("[implementor] task=%s git collect failed: %s", task.id, exc)
            return TaskResult(
                task_id=task.id,
                success=False,
                diff="",
                files_changed=[],
                error=f"git collect failed: {exc}",
                transcript="".join(transcript_parts),
            )

        if not files_changed:
            logger.warning("[implementor] task=%s no files changed", task.id)
            return TaskResult(
                task_id=task.id,
                success=False,
                diff="",
                files_changed=[],
                error="no files changed",
                transcript="".join(transcript_parts),
                no_changes=True,
            )

        return TaskResult(
            task_id=task.id,
            success=True,
            diff=diff,
            files_changed=files_changed,
            error=None,
            transcript="".join(transcript_parts),
        )

    async def _run_acp(self, task: Task) -> list[str]:
        """跑一次完整 agent 交互，返回 transcript 片段。"""
        if self._runtime.kind == "cursor-cli":
            return await self._run_cursor(task)
        return await self._run_kiro_acp(task)

    async def _run_cursor(self, task: Task) -> list[str]:
        transcript_parts: list[str] = []
        full_prompt = self._render_prompt(task)
        async for chunk in cursor_prompt_stream(
            self._runtime,
            cwd=task.cwd,
            prompt=full_prompt,
        ):
            transcript_parts.append(chunk)
        return transcript_parts

    async def _run_kiro_acp(self, task: Task) -> list[str]:
        """Kiro ACP 路径（原实现）。"""
        config = AcpClientConfig(
            kiro_cli_path=self._runtime.bin,
            cwd=task.cwd,
            response_timeout=self._prompt_timeout,
            idle_timeout=self._idle_timeout,
            model=self._model or self._runtime.model,
            sandbox_writable=(task.cwd,) if self._sandbox else None,
        )
        transcript_parts: list[str] = []
        async with await AcpClient.spawn(config) as client:
            await client.initialize()
            session_id = await client.new_session(cwd=task.cwd)

            full_prompt = self._render_prompt(task)
            logger.debug("[implementor] prompt:\n%s", full_prompt)

            events = await client.prompt(session_id, full_prompt)
            async for event in events:
                if isinstance(event, AgentMessageChunk):
                    transcript_parts.append(event.text)
                elif isinstance(event, AgentThoughtChunk):
                    # 思考内容也记录进 transcript，方便排查
                    transcript_parts.append(f"[thought] {event.text}\n")
                elif isinstance(event, ToolCallEvent):
                    transcript_parts.append(f"[tool {event.status}] {event.name}\n")
                elif isinstance(event, TurnEnd):
                    logger.info(
                        "[implementor] task=%s turn ended (stop=%s)",
                        task.id,
                        event.stop_reason,
                    )
                    break
        return transcript_parts

    @staticmethod
    def _render_prompt(task: Task) -> str:
        """拼装给 Kiro 的实际指令。"""
        parts = [
            "你是一个被自动编排器派发任务的 Implementor 角色。",
            "请严格按下面的任务说明实施，不要跑测试，不要 commit，",
            "只修改 / 创建必要的文件。完成后简短总结改了哪些文件即可。",
            "",
            f"任务 ID: {task.id}",
            "",
            "任务说明：",
            task.prompt,
        ]
        if task.acceptance:
            parts.extend(
                [
                    "",
                    "完成后将由独立的 Verifier 跑下面的命令做验收（你不需要自己跑）：",
                    *(f"  - {cmd}" for cmd in task.acceptance),
                ]
            )
        return "\n".join(parts)
