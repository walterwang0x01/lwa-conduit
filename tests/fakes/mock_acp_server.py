#!/usr/bin/env python3
"""Mock ACP server：扮演 kiro-cli acp，让集成测试不依赖真 Kiro。

工作方式：
- 真子进程，通过 stdin/stdout 跑 JSON-RPC 2.0
- 收到请求后，按"剧本"（环境变量 KIRO_CONDUIT_MOCK_SCRIPT 指向的 JSON 文件）回响应
- 不调任何 LLM，零 token 消耗

剧本格式（JSON 文件）：
{
  "initialize_response": {...},          # initialize 的 result
  "session_new_response": {...},          # session/new 的 result（必须含 sessionId）
  "prompt_responses": [                   # 每次 session/prompt 调一次
    {
      "notifications": [                  # turn 内推送的 session/update 通知列表
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "hi"}},
        ...
      ],
      "reverse_requests": [               # 可选：turn 内发起的反向请求
        {"method": "session/request_permission", "params": {...}}
      ],
      "result": {"stopReason": "end_turn"}
    },
    ...
  ],
  "delays": {                             # 可选：人为延迟（秒）
    "before_initialize": 0,
    "before_notification": 0,
    "before_response": 0
  },
  "behaviors": {                          # 可选：异常注入
    "exit_on_request_count": 0,           # 收到第 N 个请求就退出（0 = 不主动退）
    "garbage_before_response": false,     # 在合法响应前发一行非 JSON
    "stderr_lines": []                    # 启动时先写到 stderr 的内容
  }
}

环境变量：
- KIRO_CONDUIT_MOCK_SCRIPT: 指向剧本 JSON 文件（必填）
- KIRO_CONDUIT_MOCK_LOG: 调试日志路径（可选）
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any


def _log(msg: str, log_file: Path | None) -> None:
    """写到调试日志文件，避免污染 stdout。"""
    if log_file is None:
        return
    try:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(f"[{time.time():.3f}] {msg}\n")
    except OSError:
        pass


def _send(msg: dict[str, Any]) -> None:
    """把消息发到 stdout。"""
    sys.stdout.write(json.dumps(msg, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _maybe_send_garbage(behaviors: dict[str, Any]) -> None:
    if behaviors.get("garbage_before_response"):
        sys.stdout.write("not-json garbage\n")
        sys.stdout.flush()


def _maybe_delay(seconds: float) -> None:
    if seconds and seconds > 0:
        time.sleep(seconds)


def main() -> int:
    script_path = os.environ.get("KIRO_CONDUIT_MOCK_SCRIPT")
    if not script_path:
        sys.stderr.write("KIRO_CONDUIT_MOCK_SCRIPT not set\n")
        return 2

    log_file: Path | None = None
    if log_path := os.environ.get("KIRO_CONDUIT_MOCK_LOG"):
        log_file = Path(log_path)

    script: dict[str, Any] = json.loads(Path(script_path).read_text(encoding="utf-8"))
    delays: dict[str, Any] = script.get("delays", {})
    behaviors: dict[str, Any] = script.get("behaviors", {})

    # 启动时的 stderr 内容
    for line in behaviors.get("stderr_lines", []):
        sys.stderr.write(str(line) + "\n")
    sys.stderr.flush()

    prompt_idx = 0
    request_count = 0
    session_id_holder: dict[str, str] = {}

    _log("mock server up", log_file)

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        request_count += 1
        _log(f"recv: {line}", log_file)

        # 行为注入：到指定计数就退出
        exit_on = int(behaviors.get("exit_on_request_count") or 0)
        if exit_on and request_count >= exit_on:
            _log(f"exit_on_request_count={exit_on} reached, exiting", log_file)
            return 0

        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            _log("invalid json from client, ignoring", log_file)
            continue

        method = msg.get("method")
        msg_id = msg.get("id")

        # 收到反向请求的响应（msg 没有 method，但有 id + result/error）
        if method is None and msg_id is not None:
            _log(f"got reverse-request response id={msg_id}", log_file)
            continue

        if method == "initialize":
            _maybe_delay(delays.get("before_initialize", 0))
            _maybe_send_garbage(behaviors)
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": script["initialize_response"],
                }
            )
            continue

        if method == "session/new":
            session = script.get("session_new_response", {})
            sid = str(session.get("sessionId") or f"sess_{uuid.uuid4().hex[:8]}")
            session_id_holder["id"] = sid
            _maybe_delay(delays.get("before_response", 0))
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {**session, "sessionId": sid},
                }
            )
            continue

        if method == "session/prompt":
            sid = session_id_holder.get("id", "sess_unknown")
            scenarios = script.get("prompt_responses", [])
            if prompt_idx >= len(scenarios):
                # 没剧本了，直接 stopReason=end_turn
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": msg_id,
                        "result": {"stopReason": "end_turn"},
                    }
                )
                continue
            scenario = scenarios[prompt_idx]
            prompt_idx += 1

            # 反向请求（在 turn 中可能发起，需要客户端响应）
            for reverse_req in scenario.get("reverse_requests", []):
                rid = str(uuid.uuid4())
                payload = {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "method": reverse_req["method"],
                    "params": reverse_req.get("params", {}),
                }
                _send(payload)
                # 这里简单起见不等响应（响应在主循环里被识别但忽略）

            # 流式通知
            for notif in scenario.get("notifications", []):
                _maybe_delay(delays.get("before_notification", 0))
                _send(
                    {
                        "jsonrpc": "2.0",
                        "method": "session/update",
                        "params": {"sessionId": sid, "update": notif},
                    }
                )

            _maybe_delay(delays.get("before_response", 0))
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": scenario.get("result", {"stopReason": "end_turn"}),
                }
            )
            continue

        if method == "session/cancel":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": None})
            continue

        # 未识别的方法
        _send(
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"unknown method {method}"},
            }
        )

    _log("stdin closed, exiting", log_file)
    return 0


if __name__ == "__main__":
    # 让线程里的 stdout 也及时刷
    threading.current_thread().name = "mock-acp-main"
    sys.exit(main())
