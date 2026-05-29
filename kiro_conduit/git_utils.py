"""Git 操作工具：异步跑 git 命令，收集 diff / 改动文件。

M0 阶段只用到几个只读命令；worktree 创建留到 M1。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


async def run_git(
    cwd: Path,
    args: list[str],
    timeout: float = 30.0,
) -> tuple[int, str, str]:
    """异步跑一条 git 命令，返回 (returncode, stdout, stderr)。"""
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return (
        proc.returncode if proc.returncode is not None else -1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def list_changed_files(cwd: Path) -> list[str]:
    """列出工作区有改动的文件（含未追踪），相对路径。"""
    code, stdout, stderr = await run_git(cwd, ["status", "--porcelain"])
    if code != 0:
        raise RuntimeError(f"git status failed: {stderr.strip()}")

    files: list[str] = []
    for line in stdout.splitlines():
        # porcelain v1 格式：XY <path>，X 和 Y 是状态字符
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # 处理重命名 "old -> new" 的情况
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        files.append(path)
    return files


async def collect_diff(cwd: Path, include_untracked: bool = True) -> str:
    """收集工作区的全部改动（含 staged / unstaged，可选含未追踪）。

    未追踪文件的内容会被通过 `git diff --no-index /dev/null <file>` 模拟成 diff 形式。
    """
    code, tracked_diff, stderr = await run_git(cwd, ["diff", "HEAD", "--no-color"])
    if code not in (0, 1):  # git diff 有差异时返回 1，正常
        raise RuntimeError(f"git diff failed: {stderr.strip()}")

    untracked_diff = ""
    if include_untracked:
        code, stdout, stderr = await run_git(
            cwd, ["ls-files", "--others", "--exclude-standard"]
        )
        if code != 0:
            raise RuntimeError(f"git ls-files failed: {stderr.strip()}")
        untracked = [f for f in stdout.splitlines() if f.strip()]
        if untracked:
            chunks: list[str] = []
            for path in untracked:
                # 用 diff --no-index 把未追踪文件变成 diff
                code, file_diff, _stderr = await run_git(
                    cwd,
                    [
                        "--no-pager",
                        "diff",
                        "--no-color",
                        "--no-index",
                        "/dev/null",
                        path,
                    ],
                )
                # --no-index 有差异时返回 1
                if code in (0, 1) and file_diff:
                    chunks.append(file_diff)
            untracked_diff = "\n".join(chunks)

    if tracked_diff and untracked_diff:
        return tracked_diff + "\n" + untracked_diff
    return tracked_diff or untracked_diff
