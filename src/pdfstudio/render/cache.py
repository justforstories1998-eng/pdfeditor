"""Bounded, thread-safe LRU caches for rendered pages and thumbnails.

Rendering is by far the most expensive operation in a PDF viewer, so PDF Studio
keeps three tiers:

1. **Memory LRU** — decoded RGB(A) buffers, bounded by *bytes* not entries.
2. **Disk cache** — PNG files under the user cache directory, keyed by a hash
   of (document fingerprint, page, zoom, rotation, flags); survives restarts.
3. **Generation counters** — bumping a document's generation invalidates every
   cached tile for it after an edit, without scanning the cache.
"""

from __future__ import annotations

import hashlib
import secrets
import shutil
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, TypeVar

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.paths import app_paths

log = get_logger("cache")

K = TypeVar("K")
V = TypeVar("V")


@dataclass(slots=True)
class CacheStats:
    hits: int = 0
    misses: int = 0
    evictions: int = 0
    bytes_used: int = 0
    entries: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def as_dict(self) -> dict[str, float | int]:
        return {
            "hits": self.hits,
            "misses": self.misses,
            "evictions": self.evictions,
            "entries": self.entries,
            "mb_used": round(self.bytes_used / (1024 * 1024), 2),
            "hit_rate": round(self.hit_rate, 3),
        }


class MemoryCache(Generic[K, V]):
    """Byte-bounded LRU cache safe for concurrent use."""

    def __init__(self, max_bytes: int, *, name: str = "cache") -> None:
        self._max = max(1, max_bytes)
        self._name = name
        self._lock = threading.RLock()
        self._data: OrderedDict[K, tuple[V, int]] = OrderedDict()
        self._used = 0
        self.stats = CacheStats()

    def get(self, key: K) -> V | None:
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                self.stats.misses += 1
                return None
            self._data.move_to_end(key)
            self.stats.hits += 1
            return entry[0]

    def put(self, key: K, value: V, size: int) -> None:
        """Insert ``value``; ``size`` is its cost in bytes."""
        with self._lock:
            if key in self._data:
                self._used -= self._data[key][1]
                del self._data[key]
            if size > self._max:  # never cache an item bigger than the budget
                return
            self._data[key] = (value, size)
            self._used += size
            self._evict()
            self.stats.entries = len(self._data)
            self.stats.bytes_used = self._used

    def _evict(self) -> None:
        while self._used > self._max and self._data:
            _, (_, size) = self._data.popitem(last=False)
            self._used -= size
            self.stats.evictions += 1

    def invalidate(self, predicate: callable[[K], bool]) -> int:
        """Drop every entry whose key satisfies ``predicate``."""
        with self._lock:
            doomed = [k for k in self._data if predicate(k)]
            for key in doomed:
                self._used -= self._data[key][1]
                del self._data[key]
            self.stats.entries = len(self._data)
            self.stats.bytes_used = self._used
            return len(doomed)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._used = 0
            self.stats = CacheStats()

    def resize(self, max_bytes: int) -> None:
        with self._lock:
            self._max = max(1, max_bytes)
            self._evict()

    def __contains__(self, key: K) -> bool:
        with self._lock:
            return key in self._data

    def __len__(self) -> int:
        with self._lock:
            return len(self._data)

    def __bool__(self) -> bool:
        """A cache object is always truthy, even when empty.

        Without this, ``cache or default`` would replace a valid but empty
        cache with a new one — a subtle bug when caches are injected.
        """
        return True

    @property
    def bytes_used(self) -> int:
        return self._used


class DiskCache:
    """Persistent PNG cache with a size budget and LRU trimming."""

    def __init__(self, directory: Path | None = None, *, max_mb: int = 512) -> None:
        self.directory = directory or (app_paths().ensure().cache / "render")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_mb * 1024 * 1024
        self._lock = threading.Lock()

    def _path(self, key: str) -> Path:
        digest = hashlib.blake2b(key.encode(), digest_size=16).hexdigest()
        return self.directory / digest[:2] / f"{digest}.png"

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        try:
            data = path.read_bytes()
            path.touch()  # refresh atime for LRU trimming
            return data
        except OSError:
            return None

    def put(self, key: str, data: bytes) -> None:
        path = self._path(key)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        except OSError as exc:  # pragma: no cover - disk full etc.
            log.warning("Disk cache write failed: {}", exc)

    def trim(self) -> int:
        """Delete least-recently-used files until under budget. Returns bytes freed."""
        with self._lock:
            files = [
                (p.stat().st_mtime, p.stat().st_size, p)
                for p in self.directory.rglob("*.png")
                if p.is_file()
            ]
            total = sum(size for _, size, _ in files)
            freed = 0
            for _, size, path in sorted(files):
                if total - freed <= self.max_bytes:
                    break
                try:
                    path.unlink()
                    freed += size
                except OSError:
                    continue
            if freed:
                log.debug("Trimmed {} KB from the disk cache", freed // 1024)
            return freed

    def clear(self) -> None:
        with self._lock:
            shutil.rmtree(self.directory, ignore_errors=True)
            self.directory.mkdir(parents=True, exist_ok=True)

    def size_bytes(self) -> int:
        return sum(p.stat().st_size for p in self.directory.rglob("*.png") if p.is_file())


class GenerationTracker:
    """Per-document generation counters used to invalidate stale renders.

    Counters start from a random base rather than zero. The *disk* cache
    outlives the process while an in-memory counter does not, so a document
    edited in two successive sessions produced identical cache keys
    (``…|g1``) for two different page states — and the second session
    happily served the first session's pixels. A random base makes a
    collision between sessions vanishingly unlikely.
    """

    #: Room for many edits per session without running into the next base.
    _SESSION_SPAN = 1 << 20

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._generations: dict[str, int] = {}

    def _seed(self, document_id: str) -> int:
        """Random starting generation for a document, assigned once."""
        value = self._generations.get(document_id)
        if value is None:
            value = secrets.randbelow(1 << 42) * self._SESSION_SPAN
            self._generations[document_id] = value
        return value

    def current(self, document_id: str) -> int:
        with self._lock:
            return self._seed(document_id)

    def bump(self, document_id: str) -> int:
        with self._lock:
            value = self._seed(document_id) + 1
            self._generations[document_id] = value
            return value

    def forget(self, document_id: str) -> None:
        with self._lock:
            self._generations.pop(document_id, None)
