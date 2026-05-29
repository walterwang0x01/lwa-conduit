"""共享 pytest fixture。

主要提供：
- mock_acp_script: 写一份剧本到 tmp 文件，返回 AcpClientConfig 启动用的命令
- tmp_git_repo: 一个初始化好的临时 git repo（含 1 次 baseline commit）
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from kiro_conduit.acp import AcpClientConfig

REPO_ROOT = Path(__file__).resolve().parent.parent
MOCK_SERVER = REPO_ROOT / "tests" / "fakes" / "mock_acp_server.py"


@pytest.fixture
def mock_acp_config(tmp_path: Path) -> Callable[[dict[str, Any]], AcpClientConfig]:
    """工厂 fixture：传一份剧本 dict，返回一个能启动 mock server 的 AcpClientConfig。

    用法：
        def test_xxx(mock_acp_config):
            cfg = mock_acp_config({
                "initialize_response": {...},
                "session_new_response": {...},
                "prompt_responses": [...],
            })
            client = await AcpClient.spawn(cfg)
    """

    def _make(script: dict[str, Any]) -> AcpClientConfig:
        script_path = tmp_path / "script.json"
        script_path.write_text(json.dumps(script), encoding="utf-8")
        return AcpClientConfig(
            kiro_cli_path=sys.executable,
            extra_args=(str(MOCK_SERVER),),
            cwd=tmp_path,
            extra_env={
                "KIRO_CONDUIT_MOCK_SCRIPT": str(script_path),
            },
            response_timeout=5.0,
        )

    return _make


@pytest.fixture
def default_mock_script() -> dict[str, Any]:
    """常用最小剧本：initialize + session/new + 1 个 prompt 走 happy path。"""
    return {
        "initialize_response": {
            "protocolVersion": 1,
            "agentCapabilities": {
                "loadSession": True,
                "promptCapabilities": {"image": False, "audio": False},
                "mcpCapabilities": {"http": True, "sse": False},
                "sessionCapabilities": {},
            },
            "authMethods": [],
            "agentInfo": {"name": "mock", "title": "mock", "version": "0.0.1"},
        },
        "session_new_response": {"sessionId": "sess_test_1"},
        "prompt_responses": [
            {
                "notifications": [
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "hello "},
                    },
                    {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": "world"},
                    },
                ],
                "result": {"stopReason": "end_turn"},
            }
        ],
    }


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    """初始化一个临时 git repo，含一次 baseline commit。返回 repo 路径。"""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }

    def _git(*args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            env=env,
        )

    _git("init", "-b", "main")
    (tmp_path / "README.md").write_text("# test repo\n")
    _git("add", ".")
    _git("commit", "-m", "initial")
    return tmp_path
