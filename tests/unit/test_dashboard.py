"""单元测试：Dashboard 状态机。

不真起 rich.Live 渲染（CI 没 TTY），只测内存状态更新和 render() 不崩。
"""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

from kiro_conduit.dag import load_workspace
from kiro_conduit.dashboard import Dashboard
from kiro_conduit.events import (
    EventBus,
    LockEvent,
    MergeFinished,
    MergeStarted,
    RunCompleted,
    TaskFinished,
    TaskStarted,
    WaveStarted,
)


def make_workspace(tmp_path: Path):
    body = dedent(
        """
        phases:
          - name: A
            type: parallel
            tasks: [t1, t2]
        tasks:
          t1:
            spec: s
            shared_files_to_modify: ["src/x.py"]
          t2:
            spec: s
            shared_files_to_modify: ["src/x.py"]
        shared_files:
          - path: src/x.py
            policy: single-writer
        """
    ).lstrip()
    p = tmp_path / "dag.yaml"
    p.write_text(body, encoding="utf-8")
    return load_workspace(p)


class TestDashboardState:
    def test_initial_state(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        assert "t1" in db._state.tasks
        assert "t2" in db._state.tasks
        assert "src/x.py" in db._state.locks
        for tstate in db._state.tasks.values():
            assert tstate.status == "pending"

    def test_wave_started_updates_counter(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(WaveStarted(wave_index=1, total_waves=3, task_ids=("t1",)))
        assert db._state.current_wave == 1
        assert db._state.total_waves == 3

    def test_task_started_finished(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(TaskStarted(task_id="t1", attempt=1, max_attempts=3))
        assert db._state.tasks["t1"].status == "running"
        bus.publish(
            TaskFinished(task_id="t1", attempt=2, passed=False, failed_layer="dynamic")
        )
        assert db._state.tasks["t1"].status == "failed"
        assert db._state.tasks["t1"].attempts == 2
        assert db._state.tasks["t1"].failed_layer == "dynamic"

    def test_lock_acquire_release(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(
            LockEvent(
                file_path="src/x.py",
                task_id="t1",
                action="acquired",
                policy="single-writer",
            )
        )
        assert db._state.locks["src/x.py"].holder == "t1"
        bus.publish(
            LockEvent(
                file_path="src/x.py",
                task_id="t1",
                action="released",
                policy="single-writer",
            )
        )
        assert db._state.locks["src/x.py"].holder is None
        assert db._state.locks["src/x.py"].last_action == "released"

    def test_lock_rejected_doesnt_change_holder(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(
            LockEvent(
                file_path="src/x.py",
                task_id="t1",
                action="rejected",
                policy="coordinator-only",
            )
        )
        assert db._state.locks["src/x.py"].holder is None
        assert db._state.locks["src/x.py"].last_action == "rejected"

    def test_merge_events(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(MergeStarted(task_id="t1"))
        assert db._state.merges["t1"].state == "running"
        bus.publish(MergeFinished(task_id="t1", merged=True, error=None))
        assert db._state.merges["t1"].state == "merged"

    def test_run_completed(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(RunCompleted(passed_count=2, failed_count=1, skipped_count=0))
        assert db._state.run_completed is not None
        assert db._state.run_completed.passed_count == 2


class TestTaskDuration:
    """M2.0.2: 每个 task 的开始/结束时间戳 + duration 计算。"""

    def test_pending_task_no_duration(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        ts = db._state.tasks["t1"]
        assert ts.started_at is None
        assert ts.finished_at is None
        assert ts.duration() is None

    def test_running_task_has_running_duration(self, tmp_path: Path) -> None:
        import time

        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(TaskStarted(task_id="t1", attempt=1, max_attempts=3))
        ts = db._state.tasks["t1"]
        assert ts.started_at is not None
        assert ts.finished_at is None
        # 等一小会儿确保 monotonic 推进
        time.sleep(0.02)
        d = ts.duration()
        assert d is not None
        assert d >= 0.02

    def test_finished_task_has_total_duration(self, tmp_path: Path) -> None:
        import time

        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(TaskStarted(task_id="t1", attempt=1, max_attempts=3))
        time.sleep(0.02)
        bus.publish(TaskFinished(task_id="t1", attempt=1, passed=True))
        ts = db._state.tasks["t1"]
        assert ts.finished_at is not None
        d1 = ts.duration()
        # 完成后再等，duration 不应继续增长
        time.sleep(0.02)
        d2 = ts.duration()
        assert d1 is not None and d2 is not None
        assert d1 == d2

    def test_retry_does_not_reset_started_at(self, tmp_path: Path) -> None:
        """重试时第二次 TaskStarted 不应重置 started_at——保留含重试的总耗时语义。"""
        import time

        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        bus.publish(TaskStarted(task_id="t1", attempt=1, max_attempts=3))
        ts = db._state.tasks["t1"]
        first_started = ts.started_at
        time.sleep(0.02)
        bus.publish(TaskStarted(task_id="t1", attempt=2, max_attempts=3))
        assert ts.started_at == first_started

    def test_render_includes_duration_column(self, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        ws = make_workspace(tmp_path)
        console = Console(file=StringIO(), force_terminal=False, width=120)
        db = Dashboard(workspace=ws, console=console)
        bus = EventBus()
        db.attach(bus)
        bus.publish(TaskStarted(task_id="t1", attempt=1, max_attempts=3))
        bus.publish(TaskFinished(task_id="t1", attempt=1, passed=True))
        console.print(db.render())
        out = console.file.getvalue()
        assert "duration" in out


class TestDashboardRender:
    """render() 不该崩，且包含期望的字符串。"""

    def test_render_returns_renderable(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        rendered = db.render()
        # 不深入断言 rich 内部，只确认 render 返回了东西
        assert rendered is not None

    def test_render_after_events(self, tmp_path: Path) -> None:
        from io import StringIO

        from rich.console import Console

        ws = make_workspace(tmp_path)
        # 用一个不连接终端的 Console，把渲染结果写到 StringIO
        console = Console(file=StringIO(), force_terminal=False, width=120)
        db = Dashboard(workspace=ws, console=console)
        bus = EventBus()
        db.attach(bus)
        bus.publish(WaveStarted(wave_index=1, total_waves=2, task_ids=("t1",)))
        bus.publish(TaskStarted(task_id="t1", attempt=1, max_attempts=3))
        bus.publish(
            LockEvent(
                file_path="src/x.py",
                task_id="t1",
                action="acquired",
                policy="single-writer",
            )
        )
        # 把当前 render 写进 console，验证含关键字
        console.print(db.render())
        out = console.file.getvalue()
        assert "kiro-conduit dashboard" in out
        assert "t1" in out
        assert "src/x.py" in out

    def test_detach_all_unsubscribes(self, tmp_path: Path) -> None:
        ws = make_workspace(tmp_path)
        db = Dashboard(workspace=ws)
        bus = EventBus()
        db.attach(bus)
        assert bus.subscriber_count() == 1
        db.detach_all()
        assert bus.subscriber_count() == 0
