"""Command pattern implementation providing unlimited undo/redo.

Every mutating operation in PDF Studio is expressed as a :class:`Command`.
Commands are pushed onto a per-document :class:`UndoStack` which supports:

* unlimited (or bounded) history with memory accounting,
* command *merging* (typing many characters collapses into one undo step),
* *macros* / transactions grouping several commands atomically,
* named **snapshots** and restore points,
* a history model the UI renders in the History panel.

Commands must be **self-contained**: they capture whatever state they need to
undo themselves at construction/execution time.
"""

from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.logging_setup import get_logger

log = get_logger("undo")
T = TypeVar("T")


class Command(ABC):
    """Base class for all undoable operations."""

    #: Short label shown in the History panel and Undo menu item.
    label: str = "Command"
    #: Commands with the same non-zero merge id issued within
    #: :attr:`merge_window` seconds are merged into one history entry.
    merge_id: int = 0
    merge_window: float = 0.8

    def __init__(self, label: str | None = None) -> None:
        if label:
            self.label = label
        self.id = uuid.uuid4().hex
        self.timestamp = time.time()

    @abstractmethod
    def execute(self) -> None:
        """Apply the change. Called on push and on every redo."""

    @abstractmethod
    def undo(self) -> None:
        """Revert the change performed by :meth:`execute`."""

    def redo(self) -> None:
        """Re-apply. Defaults to :meth:`execute`; override when different."""
        self.execute()

    def merge_with(self, other: Command) -> bool:
        """Try to absorb ``other`` (a newer command). Return ``True`` on success."""
        return False

    def memory_cost(self) -> int:
        """Approximate retained bytes; used to bound history size."""
        return 1024

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.label!r}>"


class FunctionCommand(Command):
    """Adapt two callables into a :class:`Command` (handy for plugins/tests)."""

    def __init__(
        self,
        label: str,
        do: Callable[[], Any],
        undo: Callable[[], Any],
        *,
        cost: int = 1024,
    ) -> None:
        super().__init__(label)
        self._do = do
        self._undo = undo
        self._cost = cost

    def execute(self) -> None:
        self._do()

    def undo(self) -> None:
        self._undo()

    def memory_cost(self) -> int:
        return self._cost


class PropertyCommand(Command, Generic[T]):
    """Set an attribute on an object, remembering the previous value."""

    def __init__(self, target: Any, attribute: str, value: T, label: str | None = None):
        super().__init__(label or f"Change {attribute}")
        self._target = target
        self._attribute = attribute
        self._new = value
        self._old: T = getattr(target, attribute)

    def execute(self) -> None:
        setattr(self._target, self._attribute, self._new)

    def undo(self) -> None:
        setattr(self._target, self._attribute, self._old)

    def merge_with(self, other: Command) -> bool:
        if (
            isinstance(other, PropertyCommand)
            and other._target is self._target
            and other._attribute == self._attribute
            and other.timestamp - self.timestamp < self.merge_window
        ):
            self._new = other._new
            self.timestamp = other.timestamp
            return True
        return False


class MacroCommand(Command):
    """A group of commands treated as a single, atomic undo step."""

    def __init__(self, label: str, commands: Sequence[Command] = ()) -> None:
        super().__init__(label)
        self.commands: list[Command] = list(commands)

    def add(self, command: Command) -> None:
        self.commands.append(command)

    def execute(self) -> None:
        done: list[Command] = []
        try:
            for cmd in self.commands:
                cmd.execute()
                done.append(cmd)
        except Exception:
            for cmd in reversed(done):  # roll back partial application
                try:
                    cmd.undo()
                except Exception:
                    log.exception("Rollback failed for {}", cmd)
            raise

    def undo(self) -> None:
        for cmd in reversed(self.commands):
            cmd.undo()

    def memory_cost(self) -> int:
        return sum(c.memory_cost() for c in self.commands)

    def __len__(self) -> int:
        return len(self.commands)


@dataclass(slots=True)
class HistoryEntry:
    """Row in the History panel."""

    index: int
    label: str
    timestamp: float
    current: bool
    is_snapshot: bool = False


@dataclass(slots=True)
class Snapshot:
    """A named restore point referencing a position in the stack."""

    name: str
    index: int
    created: float = field(default_factory=time.time)


class UndoStack:
    """Thread-safe undo/redo stack with merging, macros and snapshots."""

    def __init__(
        self,
        *,
        limit: int = 0,
        memory_limit_mb: int = 256,
        document_id: str = "",
    ) -> None:
        """
        Args:
            limit: Maximum number of entries (``0`` = unlimited).
            memory_limit_mb: Soft cap; oldest entries are dropped when exceeded.
            document_id: Included in emitted events so the UI can route updates.
        """
        self._lock = threading.RLock()
        self._commands: list[Command] = []
        self._index = 0  # number of applied commands
        self._limit = limit
        self._memory_limit = memory_limit_mb * 1024 * 1024
        self._clean_index = 0
        self._macro: MacroCommand | None = None
        self._macro_depth = 0
        self._snapshots: list[Snapshot] = []
        self.document_id = document_id

    # -- state -------------------------------------------------------------- #
    @property
    def can_undo(self) -> bool:
        with self._lock:
            return self._index > 0

    @property
    def can_redo(self) -> bool:
        with self._lock:
            return self._index < len(self._commands)

    @property
    def undo_label(self) -> str:
        with self._lock:
            return self._commands[self._index - 1].label if self._index else ""

    @property
    def redo_label(self) -> str:
        with self._lock:
            return (
                self._commands[self._index].label if self._index < len(self._commands) else ""
            )

    @property
    def is_clean(self) -> bool:
        """``True`` when the document matches its last saved state."""
        with self._lock:
            return self._index == self._clean_index

    def set_clean(self) -> None:
        """Mark the current position as saved."""
        with self._lock:
            self._clean_index = self._index
        self._emit()

    # -- mutation ----------------------------------------------------------- #
    def push(self, command: Command, *, execute: bool = True) -> None:
        """Execute (optionally) and record ``command``."""
        if self._macro is not None:
            if execute:
                command.execute()
            self._macro.add(command)
            return
        if execute:
            command.execute()
        with self._lock:
            del self._commands[self._index :]  # drop redo branch
            if (
                self._commands
                and command.merge_id
                and self._commands[-1].merge_id == command.merge_id
                and self._commands[-1].merge_with(command)
            ):
                log.debug("Merged command {}", command.label)
            else:
                self._commands.append(command)
                self._index += 1
                self._enforce_limits()
        self._emit()

    def undo(self) -> bool:
        with self._lock:
            if self._index == 0:
                return False
            cmd = self._commands[self._index - 1]
            self._index -= 1
        try:
            cmd.undo()
        except Exception:
            log.exception("Undo failed for {}", cmd)
            with self._lock:
                self._index += 1
            raise
        log.debug("Undo: {}", cmd.label)
        self._emit()
        return True

    def redo(self) -> bool:
        with self._lock:
            if self._index >= len(self._commands):
                return False
            cmd = self._commands[self._index]
            self._index += 1
        try:
            cmd.redo()
        except Exception:
            log.exception("Redo failed for {}", cmd)
            with self._lock:
                self._index -= 1
            raise
        log.debug("Redo: {}", cmd.label)
        self._emit()
        return True

    def goto(self, index: int) -> None:
        """Undo/redo until exactly ``index`` commands are applied."""
        index = max(0, min(index, len(self._commands)))
        while self._index > index:
            self.undo()
        while self._index < index:
            self.redo()

    def clear(self) -> None:
        with self._lock:
            self._commands.clear()
            self._index = 0
            self._clean_index = 0
            self._snapshots.clear()
        self._emit()

    # -- macros ------------------------------------------------------------- #
    def begin_macro(self, label: str) -> None:
        """Start grouping commands; nestable."""
        with self._lock:
            if self._macro is None:
                self._macro = MacroCommand(label)
            self._macro_depth += 1

    def end_macro(self) -> None:
        """Close the innermost group and push it as one undo step."""
        with self._lock:
            self._macro_depth -= 1
            if self._macro_depth > 0:
                return
            macro, self._macro = self._macro, None
        if macro and len(macro):
            self.push(macro, execute=False)

    def macro(self, label: str) -> _MacroContext:
        """Context manager form:  ``with stack.macro("Delete pages"): ...``"""
        return _MacroContext(self, label)

    # -- snapshots ---------------------------------------------------------- #
    def create_snapshot(self, name: str) -> Snapshot:
        with self._lock:
            snap = Snapshot(name=name, index=self._index)
            self._snapshots.append(snap)
        log.info("Snapshot {!r} at index {}", name, snap.index)
        self._emit()
        return snap

    def restore_snapshot(self, name: str) -> bool:
        with self._lock:
            match = next((s for s in self._snapshots if s.name == name), None)
        if match is None:
            return False
        self.goto(match.index)
        return True

    @property
    def snapshots(self) -> list[Snapshot]:
        with self._lock:
            return list(self._snapshots)

    # -- introspection ------------------------------------------------------ #
    def history(self) -> list[HistoryEntry]:
        with self._lock:
            snap_idx = {s.index for s in self._snapshots}
            return [
                HistoryEntry(
                    index=i + 1,
                    label=c.label,
                    timestamp=c.timestamp,
                    current=(i + 1) == self._index,
                    is_snapshot=(i + 1) in snap_idx,
                )
                for i, c in enumerate(self._commands)
            ]

    def memory_usage(self) -> int:
        with self._lock:
            return sum(c.memory_cost() for c in self._commands)

    def _enforce_limits(self) -> None:
        """Drop oldest entries when count or memory limits are exceeded."""
        if self._limit and len(self._commands) > self._limit:
            drop = len(self._commands) - self._limit
            del self._commands[:drop]
            self._index -= drop
            self._clean_index = max(-1, self._clean_index - drop)
        while (
            len(self._commands) > 1
            and sum(c.memory_cost() for c in self._commands) > self._memory_limit
        ):
            self._commands.pop(0)
            self._index -= 1
            self._clean_index = max(-1, self._clean_index - 1)

    def _emit(self) -> None:
        bus().publish(
            Topic.UNDO_STACK_CHANGED,
            {
                "document_id": self.document_id,
                "can_undo": self.can_undo,
                "can_redo": self.can_redo,
                "undo_label": self.undo_label,
                "redo_label": self.redo_label,
                "clean": self.is_clean,
                "index": self._index,
                "count": len(self._commands),
            },
            source="undo",
        )

    def __len__(self) -> int:
        with self._lock:
            return len(self._commands)

    def __iter__(self) -> Iterator[Command]:
        with self._lock:
            return iter(list(self._commands))


class _MacroContext:
    """Context manager returned by :meth:`UndoStack.macro`."""

    def __init__(self, stack: UndoStack, label: str) -> None:
        self._stack = stack
        self._label = label

    def __enter__(self) -> UndoStack:
        self._stack.begin_macro(self._label)
        return self._stack

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        self._stack.end_macro()
        return False
