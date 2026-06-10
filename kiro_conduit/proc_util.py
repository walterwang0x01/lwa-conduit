"""子进程工具：超时时连根杀掉整个进程组。

背景：`create_subprocess_shell("npm install ...")` 跑的是 `sh -c`，npm 是它的子进程。
只 `proc.kill()` 杀掉 sh，npm 会变孤儿继续跑，还占着 stdout 管道，导致随后的
`proc.wait()` 永久挂住（实测 setup 超时后挂死 40 分钟）。

正确做法：用 `start_new_session=True` 让 sh 自成进程组组长，超时时 `os.killpg`
杀整个组（sh + npm + 其后代），管道随之关闭，wait 立刻返回。
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal


def kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """连根杀掉 proc 所在进程组（best-effort；要求 proc 以 start_new_session=True 启动）。"""
    if proc.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)


async def reap(proc: asyncio.subprocess.Process, timeout: float = 5.0) -> None:
    """杀掉进程组后等它真正退出（有界等待，绝不无限挂）。"""
    kill_process_group(proc)
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=timeout)
