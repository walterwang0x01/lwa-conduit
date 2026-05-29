"""TUI dashboard：用 rich.live 渲染 DAG 进度 + worker 状态 + 锁状态 + merge 状态。

订阅 EventBus，每收到事件就更新内部状态，rich Live 自动重绘。

用法：
    bus = EventBus()
    dashboard = Dashboard(workspace=ws)
    dashboard.attach(bus)

    with dashboard.live():  # rich.live context；什么都不显示就退出
        await orchestrator.run()
        # ... merge ...

设计：
- 状态全在内存里（dict / 计数器），订阅回调只更新这些
- render() 每次重新生成 Layout，rich Live 按 refresh_per_second 刷新
- 不并发——回调和 render 都在同一线程（rich 自己起渲染线程，但把回调串行化）
- M1.1 step 4 简版：4 行内容（waves / tasks / locks / merge），不做 keyboard 交互
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rich.console import Console, ConsoleRenderable, Group, RichCast
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from kiro_conduit.events import (
    LockEvent,
    MergeFinished,
    MergeStarted,
    RunCompleted,
    TaskFinished,
    TaskStarted,
    WaveStarted,
)

if TYPE_CHECKING:
    from kiro_conduit.dag import Workspace
    from kiro_conduit.events import Event, EventBus


@dataclass
class _TaskState:
    status: str = "pending"  # pending / running / passed / failed
    attempts: int = 0
    failed_layer: str | None = None
    started_at: float | None = None  # monotonic timestamp
    finished_at: float | None = None  # monotonic timestamp

    def duration(self) -> float | None:
        """耗时（秒）。pending 返回 None；running 返回到现在的累积；finished 返回总耗时。"""
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at


@dataclass
class _LockState:
    holder: str | None = None
    last_action: str = ""  # acquired / released / rejected


@dataclass
class _MergeState:
    state: str = "pending"  # pending / running / merged / failed
    error: str | None = None


@dataclass
class _DashboardState:
    """所有 dashboard 关心的状态。"""

    started_at: float = field(default_factory=time.monotonic)
    current_wave: int = 0
    total_waves: int = 0
    tasks: dict[str, _TaskState] = field(default_factory=dict)
    locks: dict[str, _LockState] = field(default_factory=dict)
    merges: dict[str, _MergeState] = field(default_factory=dict)
    run_completed: RunCompleted | None = None


class Dashboard:
    """rich Live TUI dashboard。"""

    def __init__(
        self,
        workspace: Workspace,
        refresh_per_second: float = 4.0,
        console: Console | None = None,
    ) -> None:
        self._workspace = workspace
        self._refresh = refresh_per_second
        self._console = console or Console()
        self._state = _DashboardState()
        # 预填 task 状态
        for tid in workspace.tasks:
            self._state.tasks[tid] = _TaskState()
        for sf in workspace.shared_files:
            self._state.locks[sf.path] = _LockState()
        self._unsubscribe_bus: list[Callable[[], None]] = []

    # ---------------------------------------------------------------- attach

    def attach(self, bus: EventBus) -> None:
        """订阅一个 event bus；可以多次 attach 多个 bus。"""
        unsub = bus.subscribe(self._on_event)
        self._unsubscribe_bus.append(unsub)

    def detach_all(self) -> None:
        for unsub in self._unsubscribe_bus:
            unsub()
        self._unsubscribe_bus.clear()

    # ---------------------------------------------------------------- live

    def live(self) -> Live:
        """返回一个 rich.live.Live context manager。"""
        return Live(
            self.render(),
            console=self._console,
            refresh_per_second=self._refresh,
            get_renderable=self.render,
            transient=False,
        )

    # ---------------------------------------------------------------- render

    def render(self) -> Group:
        elapsed = time.monotonic() - self._state.started_at
        title = (
            f"[bold cyan]kiro-conduit dashboard[/]   "
            f"wave {self._state.current_wave}/{self._state.total_waves}   "
            f"elapsed {elapsed:.0f}s"
        )

        sections: list[ConsoleRenderable | RichCast | str] = [Text.from_markup(title)]
        sections.append(self._render_tasks())
        if self._state.locks:
            sections.append(self._render_locks())
        if self._state.merges:
            sections.append(self._render_merges())
        if self._state.run_completed is not None:
            sections.append(self._render_summary())
        return Group(*sections)

    def _render_tasks(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column()
        table.add_row(
            "[bold]task[/]",
            "[bold]status[/]",
            "[bold]attempts[/]",
            "[bold]duration[/]",
            "[bold]failed layer[/]",
        )
        for tid in sorted(self._state.tasks):
            ts = self._state.tasks[tid]
            color = {
                "pending": "dim",
                "running": "yellow",
                "passed": "green",
                "failed": "red",
            }.get(ts.status, "white")
            duration = ts.duration()
            duration_text = f"{duration:.1f}s" if duration is not None else "-"
            table.add_row(
                tid,
                f"[{color}]{ts.status}[/]",
                str(ts.attempts) if ts.attempts else "-",
                duration_text,
                ts.failed_layer or "-",
            )
        return Panel(table, title="Tasks", border_style="cyan")

    def _render_locks(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column()
        table.add_row("[bold]file[/]", "[bold]holder[/]", "[bold]last action[/]")
        for path in sorted(self._state.locks):
            ls = self._state.locks[path]
            holder = ls.holder or "-"
            action_color = {
                "acquired": "yellow",
                "released": "green",
                "rejected": "red",
            }.get(ls.last_action, "white")
            table.add_row(
                path,
                holder,
                f"[{action_color}]{ls.last_action or '-'}[/]",
            )
        return Panel(table, title="Shared file locks", border_style="magenta")

    def _render_merges(self) -> Panel:
        table = Table.grid(padding=(0, 2))
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True)
        table.add_column()
        table.add_row("[bold]task[/]", "[bold]state[/]", "[bold]error[/]")
        for tid in sorted(self._state.merges):
            ms = self._state.merges[tid]
            color = {
                "pending": "dim",
                "running": "yellow",
                "merged": "green",
                "failed": "red",
            }.get(ms.state, "white")
            table.add_row(
                tid,
                f"[{color}]{ms.state}[/]",
                (ms.error or "-")[:80],
            )
        return Panel(table, title="Merge", border_style="green")

    def _render_summary(self) -> Panel:
        rc = self._state.run_completed
        assert rc is not None
        text = (
            f"[bold]Run complete[/]  "
            f"[green]passed={rc.passed_count}[/]  "
            f"[red]failed={rc.failed_count}[/]  "
            f"[yellow]skipped={rc.skipped_count}[/]"
        )
        return Panel(Text.from_markup(text), border_style="bold blue")

    # ------------------------------------------------------------ on_event

    def _on_event(self, event: Event) -> None:
        """订阅回调。同步更新内存状态。rich Live 自己会按 refresh 频率重绘。"""
        if isinstance(event, WaveStarted):
            self._state.current_wave = event.wave_index
            self._state.total_waves = event.total_waves
        elif isinstance(event, TaskStarted):
            ts = self._state.tasks.setdefault(event.task_id, _TaskState())
            ts.status = "running"
            ts.attempts = max(ts.attempts, event.attempt)
            # 仅第一次 started 记开始时间（重试不重置——总耗时含重试）
            if ts.started_at is None:
                ts.started_at = time.monotonic()
        elif isinstance(event, TaskFinished):
            ts = self._state.tasks.setdefault(event.task_id, _TaskState())
            ts.status = "passed" if event.passed else "failed"
            ts.attempts = max(ts.attempts, event.attempt)
            ts.failed_layer = event.failed_layer
            ts.finished_at = time.monotonic()
        elif isinstance(event, LockEvent):
            ls = self._state.locks.setdefault(event.file_path, _LockState())
            ls.last_action = event.action
            if event.action == "acquired":
                ls.holder = event.task_id
            elif event.action == "released":
                ls.holder = None
            # rejected 不改 holder
        elif isinstance(event, MergeStarted):
            ms = self._state.merges.setdefault(event.task_id, _MergeState())
            ms.state = "running"
        elif isinstance(event, MergeFinished):
            ms = self._state.merges.setdefault(event.task_id, _MergeState())
            ms.state = "merged" if event.merged else "failed"
            ms.error = event.error
        elif isinstance(event, RunCompleted):
            self._state.run_completed = event
