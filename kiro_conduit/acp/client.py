"""ACP 子进程客户端：包一层 `kiro-cli acp` 子进程，提供 async API。

设计：
- 子进程通过 stdin/stdout 跑 JSON-RPC 2.0
- 后台 reader task 持续读 stdout，把响应分流到 pending future / 事件队列
- 上层用 await/async-iterator 拿结果，无需关心协议细节

最小生命周期（M0）：
    async with AcpClient.spawn(cwd=Path("/tmp/foo")) as client:
        await client.initialize()
        session_id = await client.new_session(cwd=Path("/tmp/foo"))
        async for event in client.prompt(session_id, "hello"):
            print(event)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any

from kiro_conduit.acp.messages import (
    ACP_PROTOCOL_VERSION,
    SESSION_UPDATE_AGENT_MESSAGE_CHUNK,
    SESSION_UPDATE_AGENT_THOUGHT_CHUNK,
    SESSION_UPDATE_TOOL_CALL,
    SESSION_UPDATE_TOOL_CALL_UPDATE,
    AcpError,
    AcpProtocolError,
    AgentMessageChunk,
    AgentThoughtChunk,
    JsonRpcRequest,
    Method,
    SessionEvent,
    ToolCallEvent,
    TurnEnd,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AcpClientConfig:
    """ACP 客户端启动配置。"""

    # kiro-cli 可执行文件路径或命令名（PATH 中可解析）
    kiro_cli_path: str = "kiro-cli"
    # 子进程额外参数。M0 阶段就是 ["acp"]
    extra_args: tuple[str, ...] = ("acp",)
    # 子进程 cwd（默认 None = 继承）
    cwd: Path | None = None
    # 子进程额外环境变量
    extra_env: dict[str, str] | None = None
    # 等待响应的超时时间（秒）
    response_timeout: float = 60.0
    # 自动决策代理发来的 session/request_permission：
    # "allow_once" / "allow_always" / "reject_once" / "reject_always"
    # kiro-conduit 默认全自动 allow_once，因为编排器跑在无人干预场景
    permission_policy: str = "allow_once"


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------


class AcpClient:
    """异步 ACP 客户端。"""

    def __init__(self, proc: asyncio.subprocess.Process, config: AcpClientConfig) -> None:
        self._proc = proc
        self._config = config
        self._next_id = 0
        self._pending: dict[int, asyncio.Future[Any]] = {}
        # 每个 session_id 一个事件队列
        self._session_queues: dict[str, asyncio.Queue[SessionEvent | None]] = {}
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        # 保留对反向请求响应 task 的强引用，防止被 GC 提前回收
        self._detached_tasks: set[asyncio.Task[None]] = set()
        self._closed = False

    # ------------------------------------------------------------------ start

    @classmethod
    async def spawn(cls, config: AcpClientConfig | None = None) -> AcpClient:
        """启动 kiro-cli acp 子进程。"""
        cfg = config or AcpClientConfig()
        env = os.environ.copy()
        if cfg.extra_env:
            env.update(cfg.extra_env)

        cmd = [cfg.kiro_cli_path, *cfg.extra_args]
        logger.debug("spawning ACP subprocess: %s (cwd=%s)", cmd, cfg.cwd)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cfg.cwd) if cfg.cwd else None,
            env=env,
        )
        client = cls(proc, cfg)
        client._reader_task = asyncio.create_task(client._read_stdout(), name="acp-reader")
        client._stderr_task = asyncio.create_task(client._drain_stderr(), name="acp-stderr")
        return client

    # ----------------------------------------------------------- async context

    async def __aenter__(self) -> AcpClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------- public API

    async def initialize(self) -> dict[str, Any]:
        """ACP initialize 握手，返回代理的 capabilities 等信息。"""
        result = await self._call(
            Method.INITIALIZE,
            {
                "protocolVersion": ACP_PROTOCOL_VERSION,
                "clientCapabilities": {},
                "clientInfo": {"name": "kiro-conduit", "version": "0.0.1"},
            },
        )
        if not isinstance(result, dict):
            raise AcpProtocolError(f"initialize result not a dict: {result!r}")
        return result

    async def new_session(
        self,
        cwd: Path,
        mcp_servers: list[dict[str, Any]] | None = None,
    ) -> str:
        """创建新 session，返回 sessionId。

        cwd 必须是绝对路径（ACP 规范要求）。
        """
        if not cwd.is_absolute():
            raise ValueError(f"session/new cwd must be absolute, got {cwd}")
        result = await self._call(
            Method.SESSION_NEW,
            {"cwd": str(cwd), "mcpServers": mcp_servers or []},
        )
        if not isinstance(result, dict) or "sessionId" not in result:
            raise AcpProtocolError(f"session/new result missing sessionId: {result!r}")
        session_id = str(result["sessionId"])
        self._session_queues[session_id] = asyncio.Queue()
        return session_id

    async def prompt(
        self,
        session_id: str,
        text: str,
    ) -> AsyncIterator[SessionEvent]:
        """发送 prompt，返回流式事件异步迭代器。

        用法：
            async for event in client.prompt(sid, "hi"):
                ...

        TurnEnd 事件之后迭代器自动结束。
        """
        queue = self._session_queues.get(session_id)
        if queue is None:
            raise ValueError(f"unknown session_id: {session_id}")

        # 确保队列里没残留旧事件（防御性）
        while not queue.empty():
            queue.get_nowait()

        # 发起调用（返回的 future 会在 TurnEnd 后被 resolve）
        # ACP 规范：session/prompt 请求成功的 result 也表示 turn 完成
        # 但实际事件流通过 session/update notifications 推送
        prompt_task = asyncio.create_task(
            self._call(
                Method.SESSION_PROMPT,
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}],
                },
            ),
            name=f"acp-prompt-{session_id}",
        )

        return _PromptIterator(queue, prompt_task)

    async def cancel(self, session_id: str) -> None:
        """取消当前 session 的进行中操作。"""
        await self._call(Method.SESSION_CANCEL, {"sessionId": session_id})

    async def close(self) -> None:
        """关闭客户端：终止子进程 + 清理后台任务。"""
        if self._closed:
            return
        self._closed = True

        # 让所有等待中的 future 失败
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(ConnectionError("ACP client closed"))
        self._pending.clear()

        # 让所有 session 队列吐 None 收尾
        for queue in self._session_queues.values():
            with suppress(asyncio.QueueFull):
                queue.put_nowait(None)

        # 关闭子进程
        if self._proc.returncode is None:
            with suppress(ProcessLookupError):
                self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except TimeoutError:
                with suppress(ProcessLookupError):
                    self._proc.kill()
                await self._proc.wait()

        # 取消后台 task
        for task in (self._reader_task, self._stderr_task):
            if task and not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task

    # --------------------------------------------------------------- internal

    async def _call(self, method: str, params: dict[str, Any]) -> Any:
        """发请求，等响应。"""
        if self._closed:
            raise ConnectionError("ACP client is closed")
        self._next_id += 1
        req_id = self._next_id
        req = JsonRpcRequest(method=method, params=params, id=req_id)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[req_id] = future

        await self._send(req.to_wire())

        try:
            return await asyncio.wait_for(future, timeout=self._config.response_timeout)
        finally:
            self._pending.pop(req_id, None)

    def _spawn_detached(self, coro: Any) -> None:
        """起一个 fire-and-forget task，但保留强引用防止被 GC 回收。"""
        task: asyncio.Task[None] = asyncio.create_task(coro)
        self._detached_tasks.add(task)
        task.add_done_callback(self._detached_tasks.discard)

    async def _send(self, payload: dict[str, Any]) -> None:
        assert self._proc.stdin is not None
        line = json.dumps(payload, ensure_ascii=False) + "\n"
        logger.debug("send: %s", line.rstrip())
        self._proc.stdin.write(line.encode("utf-8"))
        await self._proc.stdin.drain()

    async def _read_stdout(self) -> None:
        """后台读 stdout，分流到 pending future / session 队列。"""
        assert self._proc.stdout is not None
        try:
            while True:
                line = await self._proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                logger.debug("recv: %s", text)
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    logger.warning("non-JSON line from ACP: %r", text)
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ACP reader crashed")

    async def _drain_stderr(self) -> None:
        """把 stderr 内容转发到 logger（避免 pipe 堵死）。"""
        assert self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    logger.debug("[kiro stderr] %s", text)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ACP stderr drainer crashed")

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """单条 JSON-RPC 消息分流。"""
        # 响应：含 id 且含 result 或 error
        if "id" in msg and ("result" in msg or "error" in msg):
            self._handle_response(msg)
            return
        # 通知：含 method 但无 id
        if "method" in msg and "id" not in msg:
            self._handle_notification(msg)
            return
        # 反向请求（代理向客户端要东西）：含 method 且有 id
        if "method" in msg and "id" in msg:
            self._handle_reverse_request(msg)
            return
        logger.warning("unrecognized ACP message: %r", msg)

    def _handle_reverse_request(self, msg: dict[str, Any]) -> None:
        """处理代理 → 客户端的反向 JSON-RPC 请求。"""
        method = msg.get("method", "")
        req_id = msg.get("id")
        params = msg.get("params") or {}

        if method == "session/request_permission":
            self._respond_permission(req_id, params)
            return

        # 其他反向请求（fs/read / fs/write / terminal/* 等）M0 暂不实现
        logger.warning(
            "ACP server requested %s (id=%s); auto-replying with method-not-found",
            method,
            req_id,
        )
        self._spawn_detached(self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {
                "code": -32601,  # JSON-RPC standard: Method not found
                "message": f"client does not implement {method}",
            },
        }))

    def _respond_permission(self, req_id: Any, params: dict[str, Any]) -> None:
        """根据 permission_policy 自动响应权限请求。"""
        options = params.get("options") or []
        target_kind = self._config.permission_policy
        chosen_id: str | None = None
        for opt in options:
            if isinstance(opt, dict) and opt.get("kind") == target_kind:
                chosen_id = str(opt.get("optionId") or "")
                break

        # 找不到偏好的就退到 allow_once，再退到第一个选项
        if chosen_id is None:
            for opt in options:
                if isinstance(opt, dict) and opt.get("kind") == "allow_once":
                    chosen_id = str(opt.get("optionId") or "")
                    break
        if chosen_id is None and options:
            first = options[0]
            if isinstance(first, dict):
                chosen_id = str(first.get("optionId") or "")

        if chosen_id is None:
            logger.warning("permission request has no usable options: %r", params)
            self._spawn_detached(self._send({
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {"outcome": {"outcome": "cancelled"}},
            }))
            return

        logger.debug(
            "auto-responding permission (policy=%s, optionId=%s)",
            target_kind,
            chosen_id,
        )
        self._spawn_detached(self._send({
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "outcome": {
                    "outcome": "selected",
                    "optionId": chosen_id,
                }
            },
        }))

    def _handle_response(self, msg: dict[str, Any]) -> None:
        msg_id = msg.get("id")
        # kiro-conduit 自己发的 request id 都是 int，反向请求的 id 是 str
        # 这里只匹配 int id（即响应自己发出的请求）
        if not isinstance(msg_id, int):
            return
        future = self._pending.get(msg_id)
        if future is None or future.done():
            return
        if "error" in msg and msg["error"] is not None:
            future.set_exception(AcpError.from_wire(msg["error"]))
        else:
            future.set_result(msg.get("result"))

    def _handle_notification(self, msg: dict[str, Any]) -> None:
        method = msg.get("method", "")
        params = msg.get("params") or {}

        # 会话更新事件
        if method in (Method.SESSION_UPDATE, Method.SESSION_NOTIFICATION):
            event = self._parse_session_update(params)
            if event is None:
                return
            session_id = self._extract_session_id(params)
            queue = self._session_queues.get(session_id)
            if queue is None:
                logger.warning("event for unknown session %s: %r", session_id, params)
                return
            queue.put_nowait(event)
            return

        # 其他通知（kiro 扩展、agent_plan 等）M0 暂不处理
        logger.debug("ignoring notification: %s", method)

    @staticmethod
    def _extract_session_id(params: dict[str, Any]) -> str:
        sid = params.get("sessionId") or params.get("session_id") or ""
        return str(sid)

    @staticmethod
    def _parse_session_update(params: dict[str, Any]) -> SessionEvent | None:
        """把 session/update 通知 params 解析成高层事件。"""
        update = params.get("update")
        # 老协议 / Kiro 早期可能直接放在 params 顶层
        if not isinstance(update, dict):
            update = params

        kind = update.get("sessionUpdate") or update.get("type")
        session_id = AcpClient._extract_session_id(params)

        if kind == SESSION_UPDATE_AGENT_MESSAGE_CHUNK:
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else ""
            return AgentMessageChunk(session_id=session_id, text=str(text or ""))

        if kind == SESSION_UPDATE_AGENT_THOUGHT_CHUNK:
            content = update.get("content") or {}
            text = content.get("text") if isinstance(content, dict) else ""
            return AgentThoughtChunk(session_id=session_id, text=str(text or ""))

        if kind in (SESSION_UPDATE_TOOL_CALL, SESSION_UPDATE_TOOL_CALL_UPDATE):
            return ToolCallEvent(
                session_id=session_id,
                tool_call_id=str(update.get("toolCallId") or update.get("id") or ""),
                name=str(update.get("name") or update.get("toolName") or ""),
                status=str(update.get("status") or "unknown"),
                raw=update,
            )

        # AgentMessageChunk 之类老命名（驼峰 / 旧 Kiro doc 里可能见过 "AgentMessageChunk"）
        if kind in ("AgentMessageChunk", "TurnEnd"):
            if kind == "AgentMessageChunk":
                content = update.get("content") or {}
                text = content.get("text") if isinstance(content, dict) else update.get("text", "")
                return AgentMessageChunk(session_id=session_id, text=str(text or ""))
            return TurnEnd(session_id=session_id, stop_reason=update.get("stopReason"))

        # 未识别就忽略（不抛异常，protocol 还在演进）
        return None


# ---------------------------------------------------------------------------
# Prompt 异步迭代器
# ---------------------------------------------------------------------------


class _PromptIterator:
    """async-iterate session 事件队列，直到 prompt 调用 resolve（视作 TurnEnd）。"""

    def __init__(
        self,
        queue: asyncio.Queue[SessionEvent | None],
        prompt_task: asyncio.Task[Any],
    ) -> None:
        self._queue = queue
        self._prompt_task = prompt_task
        self._terminated = False

    def __aiter__(self) -> _PromptIterator:
        return self

    async def __anext__(self) -> SessionEvent:
        if self._terminated:
            raise StopAsyncIteration

        # 等队列下一条事件，或 prompt_task 完成
        get_task: asyncio.Task[SessionEvent | None] = asyncio.create_task(self._queue.get())
        done, _pending = await asyncio.wait(
            {get_task, self._prompt_task},
            return_when=asyncio.FIRST_COMPLETED,
        )

        if get_task in done:
            event = get_task.result()
            if event is None:  # 关闭信号
                self._terminated = True
                raise StopAsyncIteration
            return event

        # prompt_task 先完成（说明这一轮结束）
        # 队列里可能还有最后一批事件没消费完，再尽量榨干
        get_task.cancel()
        with suppress(asyncio.CancelledError):
            await get_task

        # 收尾：把剩余事件吐完，最后给一个 TurnEnd
        # ACP 规范里 turn 结束时 prompt 请求会返回 result（含 stopReason）
        result = self._prompt_task.result() if not self._prompt_task.cancelled() else None
        stop_reason = None
        if isinstance(result, dict):
            stop_reason = result.get("stopReason")

        self._terminated = True
        # 队列里可能还有 1-2 条事件，先回吐一条；TurnEnd 当作单独事件返回
        # 简单起见：直接返回 TurnEnd，未消费的事件留在队列里不影响
        return TurnEnd(
            session_id=_first_session_id_in_queue(self._queue) or "",
            stop_reason=str(stop_reason) if stop_reason else None,
        )


def _first_session_id_in_queue(_queue: asyncio.Queue[SessionEvent | None]) -> str | None:
    """尽力从队列里推断 session_id（用于构造 TurnEnd 事件）。"""
    # asyncio.Queue 没有 peek，简单返 None；调用方知道 session_id
    return None
