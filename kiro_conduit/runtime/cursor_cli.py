"""Cursor Agent CLI 适配：agent --print --output-format stream-json。"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from kiro_conduit.runtime.types import RuntimeConfig

logger = logging.getLogger(__name__)


async def cursor_prompt_stream(
    runtime: RuntimeConfig,
    *,
    cwd: Path,
    prompt: str,
    resume_id: str | None = None,
) -> AsyncIterator[str]:
    """流式产出 assistant 文本片段。"""
    args = ["--print", "--output-format", "stream-json"]
    if runtime.force:
        args.append("-f")
    if runtime.model:
        args.extend(["--model", runtime.model])
    if resume_id:
        args.extend(["--resume", resume_id])
    args.extend(["-p", prompt])

    proc = await asyncio.create_subprocess_exec(
        runtime.bin,
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None
    session_id = resume_id or ""
    async for raw_line in proc.stdout:
        line = raw_line.decode("utf-8", errors="replace").strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = obj.get("type")
        if typ == "system" and obj.get("subtype") == "init":
            session_id = str(obj.get("session_id") or session_id)
            continue
        if typ == "assistant":
            message = obj.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    yield str(block["text"])
    code = await proc.wait()
    if code != 0:
        stderr = (await proc.stderr.read()).decode("utf-8", errors="replace") if proc.stderr else ""
        logger.warning("cursor agent exit %s: %s", code, stderr[:300])


async def cursor_prompt_text(
    runtime: RuntimeConfig,
    *,
    cwd: Path,
    prompt: str,
    resume_id: str | None = None,
) -> str:
    parts: list[str] = []
    async for chunk in cursor_prompt_stream(runtime, cwd=cwd, prompt=prompt, resume_id=resume_id):
        parts.append(chunk)
    return "".join(parts).strip()
