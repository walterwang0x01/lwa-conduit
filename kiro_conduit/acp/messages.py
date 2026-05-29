"""ACP（Agent Client Protocol）消息类型定义。

参考：https://agentclientprotocol.com/protocol/overview
传输：JSON-RPC 2.0 over stdio。

本模块只定义在 kiro-conduit M0/M1 阶段会用到的子集。完整 schema 等用到时再补。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# ---------------------------------------------------------------------------
# JSON-RPC 基础
# ---------------------------------------------------------------------------

# JSON-RPC 协议版本，所有消息固定 "2.0"
JSONRPC_VERSION: Literal["2.0"] = "2.0"

# ACP 协议版本（2026-05 跑 Kiro 2.4.2 实测得到，是数字 1 不是日期串）
ACP_PROTOCOL_VERSION: int = 1


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """JSON-RPC 2.0 请求。"""

    method: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int | str | None = None  # None = notification（无需响应）

    def to_wire(self) -> dict[str, Any]:
        """转成线上传输的 dict。"""
        out: dict[str, Any] = {
            "jsonrpc": JSONRPC_VERSION,
            "method": self.method,
            "params": self.params,
        }
        if self.id is not None:
            out["id"] = self.id
        return out


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """JSON-RPC 2.0 响应。result 和 error 互斥。"""

    id: int | str | None
    result: Any | None = None
    error: dict[str, Any] | None = None

    @classmethod
    def from_wire(cls, msg: dict[str, Any]) -> JsonRpcResponse:
        return cls(id=msg.get("id"), result=msg.get("result"), error=msg.get("error"))

    @property
    def is_error(self) -> bool:
        return self.error is not None


@dataclass(frozen=True, slots=True)
class JsonRpcNotification:
    """JSON-RPC 2.0 通知（无 id，不需响应）。"""

    method: str
    params: dict[str, Any]

    @classmethod
    def from_wire(cls, msg: dict[str, Any]) -> JsonRpcNotification:
        return cls(method=msg["method"], params=msg.get("params", {}))


# ---------------------------------------------------------------------------
# ACP 方法名常量
# ---------------------------------------------------------------------------


class Method:
    """ACP 方法名常量集合。避免散落字符串。"""

    # 客户端 → 代理
    INITIALIZE = "initialize"
    SESSION_NEW = "session/new"
    SESSION_LOAD = "session/load"
    SESSION_PROMPT = "session/prompt"
    SESSION_CANCEL = "session/cancel"

    # 代理 → 客户端（通知）
    SESSION_UPDATE = "session/update"
    SESSION_NOTIFICATION = "session/notification"  # Kiro 早期文档里的名字，做兼容


# ---------------------------------------------------------------------------
# session/update 通知里的 sessionUpdate 类型枚举
# ---------------------------------------------------------------------------

# 这些字符串值来自 ACP 规范 / Kiro 实测
# https://agentclientprotocol.com/protocol/prompt-turn
SESSION_UPDATE_AGENT_MESSAGE_CHUNK = "agent_message_chunk"
SESSION_UPDATE_USER_MESSAGE_CHUNK = "user_message_chunk"
SESSION_UPDATE_AGENT_THOUGHT_CHUNK = "agent_thought_chunk"
SESSION_UPDATE_TOOL_CALL = "tool_call"
SESSION_UPDATE_TOOL_CALL_UPDATE = "tool_call_update"
SESSION_UPDATE_PLAN = "plan"


# ---------------------------------------------------------------------------
# 代理 → 客户端通知的高层封装（解析后的事件，更易用）
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentMessageChunk:
    """代理流式输出的一段文本。"""

    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class AgentThoughtChunk:
    """代理思考过程的一段文本。"""

    session_id: str
    text: str


@dataclass(frozen=True, slots=True)
class ToolCallEvent:
    """代理调用工具事件（开始 / 进度 / 完成）。"""

    session_id: str
    tool_call_id: str
    name: str
    status: str  # "pending" | "in_progress" | "completed" | "failed" 等
    raw: dict[str, Any]  # 保留原始 update 内容方便后续扩展


@dataclass(frozen=True, slots=True)
class TurnEnd:
    """一轮 prompt 处理完成。"""

    session_id: str
    stop_reason: str | None = None  # "end_turn" / "max_tokens" / "tool_use" 等


# 所有可能的 session 事件联合
SessionEvent = AgentMessageChunk | AgentThoughtChunk | ToolCallEvent | TurnEnd


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class AcpError(Exception):
    """ACP 调用返回了 error 对象。"""

    def __init__(self, code: int, message: str, data: Any | None = None) -> None:
        super().__init__(f"ACP error {code}: {message}")
        self.code = code
        self.message = message
        self.data = data

    @classmethod
    def from_wire(cls, error: dict[str, Any]) -> AcpError:
        return cls(
            code=int(error.get("code", -1)),
            message=str(error.get("message", "unknown")),
            data=error.get("data"),
        )


class AcpProtocolError(Exception):
    """协议层面的异常（非合法 JSON / 非合法 JSON-RPC 等）。"""
