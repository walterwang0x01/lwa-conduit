"""单元测试：AcpClient 的纯解析逻辑（不起子进程）。

测的是静态方法 _parse_session_update / _extract_session_id：
- agent_message_chunk
- agent_thought_chunk
- tool_call / tool_call_update
- 老命名（AgentMessageChunk / TurnEnd）
- 未知类型返回 None
"""

from __future__ import annotations

from kiro_conduit.acp.client import AcpClient
from kiro_conduit.acp.messages import (
    AgentMessageChunk,
    AgentThoughtChunk,
    ToolCallEvent,
    TurnEnd,
)


class TestExtractSessionId:
    def test_camel_case(self) -> None:
        assert AcpClient._extract_session_id({"sessionId": "s1"}) == "s1"

    def test_snake_case_fallback(self) -> None:
        assert AcpClient._extract_session_id({"session_id": "s2"}) == "s2"

    def test_missing_returns_empty(self) -> None:
        assert AcpClient._extract_session_id({}) == ""


class TestParseSessionUpdate:
    def test_agent_message_chunk(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": "hello"},
            },
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, AgentMessageChunk)
        assert evt.session_id == "s1"
        assert evt.text == "hello"

    def test_agent_thought_chunk(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "thinking..."},
            },
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, AgentThoughtChunk)
        assert evt.text == "thinking..."

    def test_tool_call(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "call_1",
                "name": "fs_read",
                "status": "pending",
            },
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, ToolCallEvent)
        assert evt.tool_call_id == "call_1"
        assert evt.name == "fs_read"
        assert evt.status == "pending"
        assert evt.raw["toolCallId"] == "call_1"

    def test_tool_call_update(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "call_1",
                "status": "completed",
            },
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, ToolCallEvent)
        assert evt.status == "completed"

    def test_legacy_agent_message_chunk_camelcase(self) -> None:
        """兼容旧 Kiro 文档里的驼峰命名 'AgentMessageChunk'。"""
        params = {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "AgentMessageChunk",
                "content": {"type": "text", "text": "old style"},
            },
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, AgentMessageChunk)
        assert evt.text == "old style"

    def test_legacy_turn_end(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {"sessionUpdate": "TurnEnd", "stopReason": "end_turn"},
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, TurnEnd)
        assert evt.stop_reason == "end_turn"

    def test_unknown_kind_returns_none(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {"sessionUpdate": "future_thing", "data": "..."},
        }
        assert AcpClient._parse_session_update(params) is None

    def test_update_at_top_level_old_protocol(self) -> None:
        """有些老消息把 update 字段直接放在 params 顶层。"""
        params = {
            "sessionId": "s1",
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "flat"},
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, AgentMessageChunk)
        assert evt.text == "flat"

    def test_missing_content_text_falls_back_to_empty(self) -> None:
        params = {
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                # content 整个缺失
            },
        }
        evt = AcpClient._parse_session_update(params)
        assert isinstance(evt, AgentMessageChunk)
        assert evt.text == ""
