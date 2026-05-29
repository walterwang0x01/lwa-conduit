#!/usr/bin/env python3
"""一次性稳定性 runner：连跑 N 次 examples/02_civ_hello.py，汇总成功率 / 耗时 / 重试。

不入版本（放在 /tmp 才对，但放本仓库根方便我们看），跑完就删。

用法：
    .venv/bin/python tools/stability_run.py 5
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from kiro_conduit.roles import Coordinator, Implementor, Verifier  # noqa: E402
from kiro_conduit.types import Task  # noqa: E402


@dataclass
class RunStat:
    idx: int
    passed: bool
    attempts: int
    duration_s: float
    files_changed: list[str]
    failed_layer: str | None


def setup_test_repo() -> Path:
    workdir = Path(tempfile.mkdtemp(prefix="kiro-conduit-stab-"))
    subprocess.run(
        ["git", "init", "-b", "main"], cwd=workdir, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "demo@kiro-conduit.local"],
        cwd=workdir, check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "stability"],
        cwd=workdir, check=True, capture_output=True,
    )
    (workdir / "README.md").write_text("# stab\n")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"],
        cwd=workdir, check=True, capture_output=True,
    )
    return workdir


def make_task(workdir: Path) -> Task:
    return Task(
        id=f"add-function-{workdir.name}",
        prompt=(
            "在当前目录下创建两个文件：\n"
            "1. `calc.py`，定义函数 `def add(a: int, b: int) -> int`，返回两数之和。\n"
            "2. `test_calc.py`，用 pytest 写至少 2 个测试用例覆盖 add 函数。\n"
            "\n"
            "要求：\n"
            "- 代码风格要能通过 `python3 -m py_compile` 编译\n"
            "- 测试要能通过 `pytest -q` 执行\n"
        ),
        cwd=workdir,
        # 直接用 python3，避免上次发现的 'python: command not found' 重试
        acceptance=[
            "python3 -m py_compile calc.py test_calc.py",
            "pytest -q test_calc.py",
        ],
    )


async def run_one(idx: int) -> RunStat:
    workdir = setup_test_repo()
    coord = Coordinator(
        implementor=Implementor(),
        verifier=Verifier(),
        max_attempts=3,
    )
    t0 = time.monotonic()
    try:
        outcome = await coord.run_task(make_task(workdir))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    duration = time.monotonic() - t0
    return RunStat(
        idx=idx,
        passed=outcome.passed,
        attempts=outcome.attempts,
        duration_s=duration,
        files_changed=outcome.last_task_result.files_changed,
        failed_layer=str(outcome.last_verify_result.failed_layer)
        if outcome.last_verify_result.failed_layer
        else None,
    )


async def main() -> int:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    print(f"跑 {n} 次稳定性测试...")
    print("=" * 70)
    stats: list[RunStat] = []
    for i in range(1, n + 1):
        print(f"\n--- run {i}/{n} 开始 ---")
        stat = await run_one(i)
        stats.append(stat)
        print(
            f"--- run {i} 结束: "
            f"{'PASS' if stat.passed else 'FAIL'} | "
            f"attempts={stat.attempts} | "
            f"dur={stat.duration_s:.1f}s | "
            f"files={len(stat.files_changed)} | "
            f"failed_layer={stat.failed_layer}"
        )

    print()
    print("=" * 70)
    print("汇总：")
    pass_count = sum(s.passed for s in stats)
    durations = [s.duration_s for s in stats]
    attempts_total = sum(s.attempts for s in stats)
    print(f"  成功率：{pass_count}/{n} ({pass_count / n * 100:.0f}%)")
    print(f"  总尝试次数：{attempts_total}（理想 {n}，每次 1 次）")
    print(
        f"  耗时：min={min(durations):.1f}s "
        f"avg={sum(durations) / n:.1f}s max={max(durations):.1f}s"
    )
    print()
    print("逐次明细：")
    print(f"  {'#':<3} {'pass':<5} {'attempts':<10} {'dur(s)':<8} {'failed_layer'}")
    for s in stats:
        print(
            f"  {s.idx:<3} {'✓' if s.passed else '✗':<5} "
            f"{s.attempts:<10} {s.duration_s:<8.1f} {s.failed_layer or '-'}"
        )

    return 0 if pass_count >= int(n * 0.8) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
