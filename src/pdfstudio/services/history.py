"""SQLite-backed persistence: recent files, sessions, bookmarks and stamps.

A single small database holds everything that must survive a restart but does
not belong in the JSON settings file: the recent-file list with thumbnails and
last-read position, saved sessions, custom stamps, search history and the
crash-recovery journal.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.paths import app_paths

log = get_logger("history")

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS recent_files (
    path          TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    opened_at     REAL NOT NULL,
    last_page     INTEGER DEFAULT 0,
    zoom          REAL DEFAULT 1.0,
    page_count    INTEGER DEFAULT 0,
    size_bytes    INTEGER DEFAULT 0,
    fingerprint   TEXT DEFAULT '',
    thumbnail     BLOB,
    pinned        INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    name       TEXT PRIMARY KEY,
    saved_at   REAL NOT NULL,
    payload    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS search_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    query      TEXT NOT NULL,
    used_at    REAL NOT NULL,
    hits       INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stamps (
    name       TEXT PRIMARY KEY,
    kind       TEXT NOT NULL,
    payload    BLOB,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery (
    document_id TEXT PRIMARY KEY,
    origin      TEXT,
    snapshot    TEXT NOT NULL,
    saved_at    REAL NOT NULL,
    page_count  INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_recent_opened ON recent_files(opened_at DESC);
CREATE INDEX IF NOT EXISTS idx_search_used  ON search_history(used_at DESC);
"""


@dataclass(slots=True)
class RecentFile:
    """One entry in the recent-files list / start page."""

    path: Path
    name: str
    opened_at: float
    last_page: int = 0
    zoom: float = 1.0
    page_count: int = 0
    size_bytes: int = 0
    fingerprint: str = ""
    thumbnail: bytes | None = None
    pinned: bool = False

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def age_days(self) -> float:
        return (time.time() - self.opened_at) / 86400


class HistoryStore:
    """Thread-safe SQLite wrapper for persistent application state."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or app_paths().ensure().database_file
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
        log.debug("History store ready at {}", self.path)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # -- recent files ---------------------------------------------------------- #
    def touch_recent(
        self,
        path: str | Path,
        *,
        page: int = 0,
        zoom: float = 1.0,
        page_count: int = 0,
        thumbnail: bytes | None = None,
        fingerprint: str = "",
    ) -> None:
        """Insert or update a recent-file entry."""
        target = Path(path).expanduser().resolve()
        size = target.stat().st_size if target.exists() else 0
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO recent_files
                    (path, name, opened_at, last_page, zoom, page_count,
                     size_bytes, fingerprint, thumbnail)
                VALUES (?,?,?,?,?,?,?,?,?)
                ON CONFLICT(path) DO UPDATE SET
                    opened_at=excluded.opened_at,
                    last_page=excluded.last_page,
                    zoom=excluded.zoom,
                    page_count=MAX(excluded.page_count, recent_files.page_count),
                    size_bytes=excluded.size_bytes,
                    fingerprint=CASE WHEN excluded.fingerprint <> ''
                                     THEN excluded.fingerprint
                                     ELSE recent_files.fingerprint END,
                    thumbnail=COALESCE(excluded.thumbnail, recent_files.thumbnail)
                """,
                (
                    str(target),
                    target.name,
                    time.time(),
                    page,
                    zoom,
                    page_count,
                    size,
                    fingerprint,
                    thumbnail,
                ),
            )

    def recent(self, limit: int = 25, *, existing_only: bool = True) -> list[RecentFile]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM recent_files ORDER BY pinned DESC, opened_at DESC LIMIT ?",
                (limit * 2,),
            ).fetchall()
        items = [
            RecentFile(
                path=Path(r["path"]),
                name=r["name"],
                opened_at=r["opened_at"],
                last_page=r["last_page"],
                zoom=r["zoom"],
                page_count=r["page_count"],
                size_bytes=r["size_bytes"],
                fingerprint=r["fingerprint"] or "",
                thumbnail=r["thumbnail"],
                pinned=bool(r["pinned"]),
            )
            for r in rows
        ]
        if existing_only:
            items = [i for i in items if i.exists]
        return items[:limit]

    def position_of(self, path: str | Path) -> tuple[int, float] | None:
        """Last read page and zoom, so documents reopen where the user left off."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT last_page, zoom FROM recent_files WHERE path=?",
                (str(Path(path).expanduser().resolve()),),
            ).fetchone()
        return (row["last_page"], row["zoom"]) if row else None

    def pin(self, path: str | Path, pinned: bool = True) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE recent_files SET pinned=? WHERE path=?",
                (int(pinned), str(Path(path).resolve())),
            )

    def remove_recent(self, path: str | Path) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM recent_files WHERE path=?", (str(Path(path).resolve()),))

    def clear_recent(self, *, keep_pinned: bool = True) -> int:
        """Empty the recent list. Returns the number of rows removed."""
        with self._connect() as conn:
            cursor = (
                conn.execute("DELETE FROM recent_files WHERE pinned=0")
                if keep_pinned
                else conn.execute("DELETE FROM recent_files")
            )
            return cursor.rowcount

    def prune_missing(self) -> int:
        removed = 0
        for item in self.recent(limit=1000, existing_only=False):
            if not item.exists:
                self.remove_recent(item.path)
                removed += 1
        return removed

    # -- sessions ---------------------------------------------------------------- #
    def save_session(self, name: str, payload: dict[str, Any]) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO sessions (name, saved_at, payload) VALUES (?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET saved_at=excluded.saved_at, "
                "payload=excluded.payload",
                (name, time.time(), json.dumps(payload)),
            )
        log.debug("Saved session {!r}", name)

    def load_session(self, name: str = "last") -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT payload FROM sessions WHERE name=?", (name,)).fetchone()
        return json.loads(row["payload"]) if row else None

    def sessions(self) -> list[tuple[str, float]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, saved_at FROM sessions ORDER BY saved_at DESC"
            ).fetchall()
        return [(r["name"], r["saved_at"]) for r in rows]

    def delete_session(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM sessions WHERE name=?", (name,))

    # -- search history ------------------------------------------------------------ #
    def add_search(self, query: str, hits: int = 0) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO search_history (query, used_at, hits) VALUES (?,?,?)",
                (query, time.time(), hits),
            )
            conn.execute(
                "DELETE FROM search_history WHERE id NOT IN "
                "(SELECT id FROM search_history ORDER BY used_at DESC LIMIT 100)"
            )

    def recent_searches(self, limit: int = 15) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT query FROM search_history ORDER BY used_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [r["query"] for r in rows]

    # -- custom stamps ---------------------------------------------------------------- #
    def save_stamp(self, name: str, kind: str, payload: bytes) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO stamps (name, kind, payload, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                (name, kind, payload, time.time()),
            )

    def stamps(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT name, kind, payload FROM stamps ORDER BY name"
            ).fetchall()
        return [{"name": r["name"], "kind": r["kind"], "payload": r["payload"]} for r in rows]

    def delete_stamp(self, name: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM stamps WHERE name=?", (name,))

    # -- crash recovery -------------------------------------------------------------- #
    def record_recovery(
        self, document_id: str, origin: str, snapshot: str, page_count: int
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO recovery (document_id, origin, snapshot, saved_at, page_count) "
                "VALUES (?,?,?,?,?) ON CONFLICT(document_id) DO UPDATE SET "
                "snapshot=excluded.snapshot, saved_at=excluded.saved_at",
                (document_id, origin, snapshot, time.time(), page_count),
            )

    def pending_recovery(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM recovery ORDER BY saved_at DESC").fetchall()
        return [dict(r) for r in rows]

    def clear_recovery(self, document_id: str | None = None) -> None:
        with self._connect() as conn:
            if document_id:
                conn.execute("DELETE FROM recovery WHERE document_id=?", (document_id,))
            else:
                conn.execute("DELETE FROM recovery")

    # -- housekeeping ------------------------------------------------------------------ #
    def vacuum(self) -> None:
        with self._connect() as conn:
            conn.execute("VACUUM")

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None
