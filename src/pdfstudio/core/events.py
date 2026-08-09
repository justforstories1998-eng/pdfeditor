"""A tiny, thread-safe, Qt-independent publish/subscribe event bus.

Head-less layers (PDF engine, OCR, plugins) publish domain events here; the UI
adapts them onto Qt signals.  This keeps the core importable without Qt while
still allowing rich, decoupled communication.
"""

from __future__ import annotations

import threading
import weakref
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum, auto
from typing import Any

from pdfstudio.core.logging_setup import get_logger

log = get_logger("events")


class Topic(StrEnum):
    """Well-known event topics."""

    DOCUMENT_OPENED = auto()
    DOCUMENT_CLOSED = auto()
    DOCUMENT_SAVED = auto()
    DOCUMENT_MODIFIED = auto()
    PAGE_CHANGED = auto()
    PAGES_MUTATED = auto()
    SELECTION_CHANGED = auto()
    ANNOTATION_ADDED = auto()
    ANNOTATION_REMOVED = auto()
    ANNOTATION_UPDATED = auto()
    UNDO_STACK_CHANGED = auto()
    RENDER_COMPLETED = auto()
    JOB_STARTED = auto()
    JOB_PROGRESS = auto()
    JOB_FINISHED = auto()
    JOB_FAILED = auto()
    THEME_CHANGED = auto()
    PLUGIN_LOADED = auto()
    PLUGIN_UNLOADED = auto()
    SEARCH_RESULTS = auto()
    STATUS_MESSAGE = auto()
    ERROR = auto()


@dataclass(slots=True)
class Event:
    """An immutable-ish event payload."""

    topic: Topic | str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = ""

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.payload.get(key, default)


Handler = Callable[[Event], None]


class EventBus:
    """Synchronous pub/sub with weak references to bound methods.

    Weak references mean a subscriber that is garbage collected (a closed tab,
    for example) is unsubscribed automatically — no dangling-callback leaks.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: dict[str, list[Any]] = {}

    def subscribe(self, topic: Topic | str, handler: Handler) -> Callable[[], None]:
        """Subscribe ``handler`` to ``topic``; returns an unsubscribe callable."""
        key = str(topic)
        # Bound methods are held weakly so a destroyed subscriber unsubscribes
        # itself; plain functions and lambdas must be kept alive strongly.
        ref: Any = (
            weakref.WeakMethod(handler)  # type: ignore[arg-type]
            if hasattr(handler, "__self__")
            else handler
        )
        with self._lock:
            self._subs.setdefault(key, []).append(ref)

        def _unsubscribe() -> None:
            with self._lock:
                if ref in self._subs.get(key, []):
                    self._subs[key].remove(ref)

        return _unsubscribe

    def publish(
        self,
        topic: Topic | str,
        payload: dict[str, Any] | None = None,
        *,
        source: str = "",
    ) -> None:
        """Dispatch an event to every live subscriber of ``topic``."""
        event = Event(topic=topic, payload=payload or {}, source=source)
        key = str(topic)
        with self._lock:
            refs = list(self._subs.get(key, []))
            dead: list[Any] = []
        for ref in refs:
            fn = ref() if isinstance(ref, weakref.WeakMethod) else ref
            if fn is None:
                dead.append(ref)
                continue
            try:
                fn(event)
            except Exception:
                log.exception("Event handler failed for topic {}", key)
        if dead:
            with self._lock:
                self._subs[key] = [r for r in self._subs.get(key, []) if r not in dead]

    def clear(self, topic: Topic | str | None = None) -> None:
        with self._lock:
            if topic is None:
                self._subs.clear()
            else:
                self._subs.pop(str(topic), None)

    def subscriber_count(self, topic: Topic | str) -> int:
        with self._lock:
            return len(self._subs.get(str(topic), []))


_BUS = EventBus()


def bus() -> EventBus:
    """Process-wide event bus."""
    return _BUS
