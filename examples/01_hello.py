#!/usr/bin/env python3
"""最小 demo：起 1 个 kiro-cli acp 子进程，发一条 prompt，看到流式响应。

这是 M0 PoC 的第一个里程碑——证明 kiro-conduit 的 ACP 通信骨架能跑通。

跑法：
    cd ~/PycharmProjects/kiro-conduit
    python examples/01_hello.py

预期：
    - 看到 AgentInfo（initialize 的返回）
    - 看到一段流式输出（agent 的回复）
    - 看到 TurnEnd
    - 程序干净退出，没有僵尸 kiro-cli 进程
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

# 让脚本能直接运行：把仓库根目录加进 sys.path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiro_conduit.acp import (  # noqa: E402
    AcpClient,
    AcpClientConfig,
    AgentMessageChunk,
    AgentThoughtChunk,
    ToolCallEvent,
    TurnEnd,
)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = AcpClientConfig(cwd=ROOT)
    client = await AcpClient.spawn(config)

    try:
        # 1. initialize 握手
        info = await client.initialize()
        agent_info = info.get("agentInfo", {})
        print(f"✓ Connected to: {agent_info.get('name')} v{agent_info.get('version')}")
        print(f"  Protocol version: {info.get('protocolVersion')}")
        print(f"  Capabilities: {info.get('agentCapabilities')}")
        print()

        # 2. 创建 session
        session_id = await client.new_session(cwd=ROOT)
        print(f"✓ Session created: {session_id}")
        print()

        # 3. 发 prompt，流式打印响应
        prompt_text = "用一句话介绍你自己。"
        print(f"→ Prompt: {prompt_text}")
        print("← Agent:")

        events_iter = await client.prompt(session_id, prompt_text)
        async for event in events_iter:
            if isinstance(event, AgentMessageChunk):
                # 流式追加，不换行
                print(event.text, end="", flush=True)
            elif isinstance(event, AgentThoughtChunk):
                print(f"\n  [thought] {event.text}", flush=True)
            elif isinstance(event, ToolCallEvent):
                print(
                    f"\n  [tool {event.status}] {event.name} (id={event.tool_call_id})",
                    flush=True,
                )
            elif isinstance(event, TurnEnd):
                print(f"\n\n✓ Turn ended (stop_reason={event.stop_reason})")
                break

        return 0
    finally:
        await client.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
