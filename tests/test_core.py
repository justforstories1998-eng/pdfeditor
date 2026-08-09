"""Tests for the core infrastructure: settings, events, undo and jobs."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from pdfstudio.core.events import EventBus, Topic
from pdfstudio.core.jobs import JobCancelled, JobManager, JobState
from pdfstudio.core.settings import SettingsManager
from pdfstudio.core.undo import (
    FunctionCommand,
    MacroCommand,
    PropertyCommand,
    UndoStack,
)


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
class TestSettings:
    def test_defaults_are_created(self, tmp_path: Path) -> None:
        manager = SettingsManager(tmp_path / "settings.json")
        assert manager.data.ui.theme == "dark"
        assert (tmp_path / "settings.json").exists()

    def test_nested_sections_survive_a_round_trip(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        first = SettingsManager(path)
        first.set("performance.render_dpi", 240)
        first.set("ocr.languages", ["eng", "fra"])
        second = SettingsManager(path)
        assert second.data.performance.render_dpi == 240
        assert second.data.ocr.languages == ["eng", "fra"]
        # nested values must be real dataclasses, not dicts
        assert hasattr(second.data.performance, "render_dpi")

    def test_dotted_get_and_set(self, tmp_path: Path) -> None:
        manager = SettingsManager(tmp_path / "s.json")
        manager.set("ui.accent", "#ff0000")
        assert manager.get("ui.accent") == "#ff0000"
        assert manager.get("ui.missing", "fallback") == "fallback"
        with pytest.raises(KeyError):
            manager.set("ui.does_not_exist", 1)

    def test_subscribers_are_notified(self, tmp_path: Path) -> None:
        manager = SettingsManager(tmp_path / "s.json")
        seen: list[tuple[str, object]] = []
        unsubscribe = manager.subscribe("ui.", lambda key, value: seen.append((key, value)))
        manager.set("ui.theme", "light")
        assert seen == [("ui.theme", "light")]
        unsubscribe()
        manager.set("ui.theme", "dark")
        assert len(seen) == 1

    def test_corrupt_file_is_backed_up(self, tmp_path: Path) -> None:
        path = tmp_path / "settings.json"
        path.write_text("{not valid json", "utf-8")
        manager = SettingsManager(path)
        assert manager.data.ui.theme == "dark"
        assert path.with_suffix(".corrupt.json").exists()

    def test_recent_files_are_deduplicated(self, tmp_path: Path) -> None:
        manager = SettingsManager(tmp_path / "s.json")
        target = tmp_path / "a.pdf"
        target.write_bytes(b"%PDF-1.7\n")
        manager.push_recent(target)
        manager.push_recent(target)
        assert manager.data.recent_files.count(str(target.resolve())) == 1


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
class TestEventBus:
    def test_publish_and_subscribe(self) -> None:
        bus = EventBus()
        received: list[dict] = []
        bus.subscribe(Topic.DOCUMENT_OPENED, lambda e: received.append(e.payload))
        bus.publish(Topic.DOCUMENT_OPENED, {"path": "a.pdf"})
        assert received == [{"path": "a.pdf"}]

    def test_unsubscribe(self) -> None:
        bus = EventBus()
        received: list[int] = []
        unsubscribe = bus.subscribe("x", lambda e: received.append(1))
        bus.publish("x")
        unsubscribe()
        bus.publish("x")
        assert len(received) == 1

    def test_failing_handler_does_not_break_others(self) -> None:
        bus = EventBus()
        received: list[int] = []

        def bad(_event: object) -> None:
            raise RuntimeError("boom")

        bus.subscribe("t", bad)
        bus.subscribe("t", lambda e: received.append(1))
        bus.publish("t")
        assert received == [1]

    def test_bound_methods_are_weakly_referenced(self) -> None:
        bus = EventBus()

        class Subscriber:
            def __init__(self) -> None:
                self.count = 0

            def handle(self, _event: object) -> None:
                self.count += 1

        subscriber = Subscriber()
        bus.subscribe("t", subscriber.handle)
        bus.publish("t")
        assert subscriber.count == 1
        del subscriber
        bus.publish("t")  # must not raise


# --------------------------------------------------------------------------- #
# Undo
# --------------------------------------------------------------------------- #
class _Counter:
    def __init__(self) -> None:
        self.value = 0


class TestUndoStack:
    def test_push_undo_redo(self) -> None:
        stack = UndoStack()
        box: list[int] = []
        stack.push(FunctionCommand("add", lambda: box.append(1), lambda: box.pop()))
        assert box == [1] and stack.can_undo
        stack.undo()
        assert box == [] and stack.can_redo
        stack.redo()
        assert box == [1]

    def test_new_command_clears_the_redo_branch(self) -> None:
        stack = UndoStack()
        box: list[int] = []
        for value in (1, 2):
            stack.push(
                FunctionCommand(f"add{value}", lambda v=value: box.append(v), lambda: box.pop())
            )
        stack.undo()
        stack.push(FunctionCommand("add3", lambda: box.append(3), lambda: box.pop()))
        assert not stack.can_redo
        assert box == [1, 3]

    def test_property_commands_merge(self) -> None:
        stack = UndoStack()
        target = _Counter()
        first = PropertyCommand(target, "value", 1)
        first.merge_id = 7
        stack.push(first)
        second = PropertyCommand(target, "value", 2)
        second.merge_id = 7
        stack.push(second)
        assert len(stack) == 1
        assert target.value == 2
        stack.undo()
        assert target.value == 0

    def test_macro_groups_commands(self) -> None:
        stack = UndoStack()
        box: list[int] = []
        with stack.macro("batch"):
            for value in range(3):
                stack.push(
                    FunctionCommand("add", lambda v=value: box.append(v), lambda: box.pop())
                )
        assert len(stack) == 1
        assert box == [0, 1, 2]
        stack.undo()
        assert box == []

    def test_macro_rolls_back_on_failure(self) -> None:
        box: list[int] = []
        macro = MacroCommand("bad")
        macro.add(FunctionCommand("ok", lambda: box.append(1), lambda: box.pop()))
        macro.add(
            FunctionCommand("fail", lambda: (_ for _ in ()).throw(RuntimeError()), lambda: None)
        )
        with pytest.raises(RuntimeError):
            macro.execute()
        assert box == []

    def test_clean_state_tracking(self) -> None:
        stack = UndoStack()
        assert stack.is_clean
        stack.push(FunctionCommand("x", lambda: None, lambda: None))
        assert not stack.is_clean
        stack.set_clean()
        assert stack.is_clean
        stack.undo()
        assert not stack.is_clean

    def test_snapshots(self) -> None:
        stack = UndoStack()
        box: list[int] = []
        for value in range(4):
            stack.push(FunctionCommand("add", lambda v=value: box.append(v), lambda: box.pop()))
            if value == 1:
                stack.create_snapshot("halfway")
        assert stack.restore_snapshot("halfway")
        assert box == [0, 1]
        assert not stack.restore_snapshot("nope")

    def test_history_and_goto(self) -> None:
        stack = UndoStack()
        box: list[int] = []
        for value in range(5):
            stack.push(
                FunctionCommand(f"c{value}", lambda v=value: box.append(v), lambda: box.pop())
            )
        assert len(stack.history()) == 5
        stack.goto(2)
        assert box == [0, 1]
        stack.goto(5)
        assert box == [0, 1, 2, 3, 4]

    def test_limit_drops_oldest(self) -> None:
        stack = UndoStack(limit=3)
        for value in range(6):
            stack.push(FunctionCommand(f"c{value}", lambda: None, lambda: None))
        assert len(stack) == 3


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class TestJobManager:
    def test_submit_returns_a_result(self) -> None:
        with JobManager(thread_workers=2) as manager:
            job = manager.submit("add", lambda a, b: a + b, 2, 3)
            assert job.result(timeout=10) == 5
            assert job.state is JobState.SUCCEEDED

    def test_progress_is_published(self) -> None:
        from pdfstudio.core.events import bus

        events: list[int] = []
        unsubscribe = bus().subscribe(
            Topic.JOB_PROGRESS, lambda e: events.append(e["progress"].current)
        )

        def work(ctx: object) -> str:
            ctx.set_total(3)  # type: ignore[attr-defined]
            for i in range(1, 4):
                ctx.progress(i, f"step {i}")  # type: ignore[attr-defined]
            return "done"

        with JobManager(thread_workers=1) as manager:
            job = manager.submit("work", work, with_context=True)
            assert job.result(timeout=10) == "done"
        unsubscribe()
        assert events == [1, 2, 3]

    def test_cancellation(self) -> None:
        started = threading.Event()

        def work(ctx: object) -> None:
            started.set()
            for _ in range(200):
                ctx.raise_if_cancelled()  # type: ignore[attr-defined]
                time.sleep(0.01)

        with JobManager(thread_workers=1) as manager:
            job = manager.submit("slow", work, with_context=True)
            assert started.wait(5)
            job.cancel()
            with pytest.raises((JobCancelled, Exception)):
                job.result(timeout=10)

    def test_exceptions_propagate(self) -> None:
        with JobManager(thread_workers=1) as manager:
            job = manager.submit("bad", lambda: 1 / 0)
            with pytest.raises(ZeroDivisionError):
                job.result(timeout=10)
            assert job.state is JobState.FAILED
