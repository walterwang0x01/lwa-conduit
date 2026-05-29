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

import logging

from kiro_conduit.acp import (
    AcpClient,
    AcpClientConfig,
    AgentMessageChunk,
    AgentThoughtChunk,
    ToolCallEvent,
    TurnEnd,
)
from kiro_conduit.git_utils import collect_diff, list_changed_files
from kiro_conduit.types import Task, TaskResult

logger = logging.getLogger(__name__)


class Implementor:
    """单任务 Implementor。每次调用 run() 起一个独立 Kiro 子进程。"""

    def __init__(
        self,
        kiro_cli_path: str = "kiro-cli",
        prompt_timeout: float = 600.0,
    ) -> None:
        self._kiro_cli_path = kiro_cli_path
        self._prompt_timeout = prompt_timeout

    async def run(self, task: Task) -> TaskResult:
        """执行任务，返回结果。"""
        logger.info("[implementor] start task=%s cwd=%s", task.id, task.cwd)
        config = AcpClientConfig(
            kiro_cli_path=self._kiro_cli_path,
            cwd=task.cwd,
            response_timeout=self._prompt_timeout,
        )
        transcript_parts: list[str] = []
        try:
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
                        transcript_parts.append(
                            f"[tool {event.status}] {event.name}\n"
                        )
                    elif isinstance(event, TurnEnd):
                        logger.info(
                            "[implementor] task=%s turn ended (stop=%s)",
                            task.id,
                            event.stop_reason,
                        )
                        break
        except (TimeoutError, ConnectionError) as exc:
            logger.error("[implementor] task=%s failed: %s", task.id, exc)
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
                error="no files changed (agent may have refused or misunderstood)",
                transcript="".join(transcript_parts),
            )

        return TaskResult(
            task_id=task.id,
            success=True,
            diff=diff,
            files_changed=files_changed,
            error=None,
            transcript="".join(transcript_parts),
        )

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
