"""集成测试：AcpClient 起真子进程（mock_acp_server.py），不调真 Kiro。

覆盖：
- spawn → initialize → new_session → prompt → close 全链路
- 流式事件正确分发（agent_message_chunk / tool_call）
- 反向 session/request_permission 自动响应
- 干净关闭（无僵尸进程）
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from kiro_conduit.acp import (
    AcpClient,
    AcpClientConfig,
    AgentMessageChunk,
    ToolCallEvent,
    TurnEnd,
)


@pytest.mark.asyncio
async def test_initialize_returns_capabilities(
    mock_acp_config: Callable[[dict[str, Any]], AcpClientConfig],
    default_mock_script: dict[str, Any],
) -> None:
    cfg = mock_acp_config(default_mock_script)
    async with await AcpClient.spawn(cfg) as client:
        info = await client.initialize()
        assert info["protocolVersion"] == 1
        assert info["agentInfo"]["name"] == "mock"


@pytest.mark.asyncio
async def test_new_session_returns_session_id(
    mock_acp_config: Callable[[dict[str, Any]], AcpClientConfig],
    default_mock_script: dict[str, Any],
    tmp_path: Any,
) -> None:
    cfg = mock_acp_config(default_mock_script)
    async with await AcpClient.spawn(cfg) as client:
        await client.initialize()
        sid = await client.new_session(cwd=tmp_path)
        assert sid == "sess_test_1"


@pytest.mark.asyncio
async def test_prompt_streams_message_chunks_and_ends_with_turn_end(
    mock_acp_config: Callable[[dict[str, Any]], AcpClientConfig],
    default_mock_script: dict[str, Any],
    tmp_path: Any,
) -> None:
    cfg = mock_acp_config(default_mock_script)
    chunks: list[str] = []
    saw_turn_end = False

    async with await AcpClient.spawn(cfg) as client:
        await client.initialize()
        sid = await client.new_session(cwd=tmp_path)
        events = await client.prompt(sid, "say hi")
        async for event in events:
            if isinstance(event, AgentMessageChunk):
                chunks.append(event.text)
            elif isinstance(event, TurnEnd):
                saw_turn_end = True
                assert event.stop_reason == "end_turn"
                break

    assert chunks == ["hello ", "world"]
    assert saw_turn_end


@pytest.mark.asyncio
async def test_tool_call_events_propagate(
    mock_acp_config: Callable[[dict[str, Any]], AcpClientConfig],
    tmp_path: Any,
) -> None:
    script: dict[str, Any] = {
        "initialize_response": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "authMethods": [],
            "agentInfo": {"name": "mock", "title": "mock", "version": "0"},
        },
        "session_new_response": {"sessionId": "s1"},
        "prompt_responses": [
            {
                "notifications": [
                    {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "call_1",
                        "name": "fs_write",
                        "status": "pending",
                    },
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": "call_1",
                        "status": "completed",
                    },
                ],
                "result": {"stopReason": "end_turn"},
            }
        ],
    }
    cfg = mock_acp_config(script)
    statuses: list[str] = []
    async with await AcpClient.spawn(cfg) as client:
        await client.initialize()
        sid = await client.new_session(cwd=tmp_path)
        events = await client.prompt(sid, "do work")
        async for event in events:
            if isinstance(event, ToolCallEvent):
                statuses.append(event.status)
            elif isinstance(event, TurnEnd):
                break
    assert statuses == ["pending", "completed"]


@pytest.mark.asyncio
async def test_permission_request_is_auto_allowed(
    mock_acp_config: Callable[[dict[str, Any]], AcpClientConfig],
    tmp_path: Any,
) -> None:
    """模拟 Kiro 反向请求权限：客户端必须自动响应，不能阻塞。"""
    script: dict[str, Any] = {
        "initialize_response": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True},
            "authMethods": [],
            "agentInfo": {"name": "mock", "title": "mock", "version": "0"},
        },
        "session_new_response": {"sessionId": "s1"},
        "prompt_responses": [
            {
                # 先发一条反向请求权限，再发流式输出
                "reverse_requests": [
                    {
                        "method": "session/request_permission",
                        "params": {
                            "sessionId": "s1",
                            "toolCall": {"toolCallId": "call_1"},
                            "options": [
                                {
                                    "optionId": "opt_yes",
                                    "name": "Allow once",
                                    "kind": "allow_once",
                                },
                                {
                                    "optionId": "opt_no",
                                    "name": "Reject",
                                    "kind": "reject_once",
                                },
                            ],
                        },
                    }
                ],
                "notifications": [
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "done"},
                    }
                ],
                "result": {"stopReason": "end_turn"},
            }
        ],
    }
    cfg = mock_acp_config(script)
    chunks: list[str] = []
    async with await AcpClient.spawn(cfg) as client:
        await client.initialize()
        sid = await client.new_session(cwd=tmp_path)
        events = await client.prompt(sid, "do work")
        async for event in events:
            if isinstance(event, AgentMessageChunk):
                chunks.append(event.text)
            elif isinstance(event, TurnEnd):
                break

    # 关键断言：能正常走完流程（说明权限请求被自动响应了，没卡死）
    assert chunks == ["done"]


@pytest.mark.asyncio
async def test_close_terminates_subprocess(
    mock_acp_config: Callable[[dict[str, Any]], AcpClientConfig],
    default_mock_script: dict[str, Any],
) -> None:
    cfg = mock_acp_config(default_mock_script)
    client = await AcpClient.spawn(cfg)
    try:
        await client.initialize()
    finally:
        await client.close()
    # close 之后子进程应该退了
    assert client._proc.returncode is not None
