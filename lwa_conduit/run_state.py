"""Run 状态持久化：支持中断后 resume。

跑到一半崩溃时，已 passed 的 task 的分支已经 commit 在那里。把每个 task 的
状态 + 分支名增量写进 `<base_repo>/.lwa-conduit/run-state.json`，下次
resume 时读出来跳过已 passed 的 task，从未完成处续跑。

设计要点：
- 原子写（写临时文件再 replace），避免崩在写一半留下损坏文件
- load 容错：文件不存在 / 损坏 / 版本不符都返回 None（当作没有历史，全新跑）
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from lwa_conduit.paths import CONDUIT_DIR_NAME as CONDUIT_DIR_NAME  # 重新导出给 memory.py 用
from lwa_conduit.paths import conduit_dir

RUN_STATE_FILENAME = "run-state.json"
_SCHEMA_VERSION = 1


class TaskRunStatus(StrEnum):
    """task 在一次 run 里的最终落点。"""

    PENDING = "pending"
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class TaskState:
    status: TaskRunStatus
    branch: str | None = None
    attempts: int = 0
    # M2 扩展：最后一次失败的摘要信息，让 resume 时 Coordinator 能恢复上下文
    last_failure_feedback: str | None = None
    last_failed_layer: str | None = None


@dataclass(slots=True)
class RunState:
    base_branch: str
    tasks: dict[str, TaskState] = field(default_factory=dict)

    def record(
        self,
        task_id: str,
        status: TaskRunStatus,
        branch: str | None = None,
        attempts: int = 0,
        last_failure_feedback: str | None = None,
        last_failed_layer: str | None = None,
    ) -> None:
        self.tasks[task_id] = TaskState(
            status=status,
            branch=branch,
            attempts=attempts,
            last_failure_feedback=last_failure_feedback,
            last_failed_layer=last_failed_layer,
        )

    def passed_ids(self) -> set[str]:
        return {
            tid
            for tid, s in self.tasks.items()
            if s.status is TaskRunStatus.PASSED
        }

    def failed_summary(self) -> dict[str, tuple[str | None, str | None]]:
        """返回 failed tasks 的 {task_id: (feedback, layer)}，用于 resume 时恢复上下文。"""
        return {
            tid: (s.last_failure_feedback, s.last_failed_layer)
            for tid, s in self.tasks.items()
            if s.status is TaskRunStatus.FAILED
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "version": _SCHEMA_VERSION,
            "base_branch": self.base_branch,
            "tasks": {
                tid: {
                    "status": s.status.value,
                    "branch": s.branch,
                    "attempts": s.attempts,
                    "last_failure_feedback": s.last_failure_feedback,
                    "last_failed_layer": s.last_failed_layer,
                }
                for tid, s in self.tasks.items()
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> RunState:
        if data.get("version") != _SCHEMA_VERSION:
            raise ValueError(f"unsupported run-state version: {data.get('version')!r}")
        base_branch = data["base_branch"]
        if not isinstance(base_branch, str):
            raise ValueError("base_branch must be a string")
        raw_tasks = data.get("tasks", {})
        if not isinstance(raw_tasks, dict):
            raise ValueError("tasks must be a mapping")
        tasks: dict[str, TaskState] = {}
        for tid, t in raw_tasks.items():
            tasks[tid] = TaskState(
                status=TaskRunStatus(t["status"]),
                branch=t.get("branch"),
                attempts=int(t.get("attempts", 0)),
                last_failure_feedback=t.get("last_failure_feedback"),
                last_failed_layer=t.get("last_failed_layer"),
            )
        return cls(base_branch=base_branch, tasks=tasks)


def state_path(base_repo: Path) -> Path:
    """run-state.json 的标准路径。"""
    return conduit_dir(base_repo) / RUN_STATE_FILENAME


def save_state(path: Path, state: RunState) -> None:
    """原子写：先写 .tmp 再 replace。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(
        json.dumps(state.to_dict(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(path)


def load_state(path: Path) -> RunState | None:
    """读 run-state。文件不存在 / 损坏 / 版本不符都返回 None（当作全新跑）。"""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return RunState.from_dict(data)
    except (json.JSONDecodeError, KeyError, ValueError, TypeError, OSError):
        return None
