"""Autosave, versioned backups and crash recovery.

Every open, modified document is periodically written to a private autosave
directory as ``<document-id>-<timestamp>.pdf`` together with a JSON manifest
recording the original path.  On start-up :class:`RecoveryManager` reports any
autosaves that were not cleaned up by a normal exit so the user can restore
their work.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.paths import app_paths
from pdfstudio.core.settings import AutosaveSettings
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions

log = get_logger("autosave")


@dataclass(slots=True)
class AutosaveRecord:
    """Manifest describing one autosaved document."""

    document_id: str
    origin: str
    autosave_path: Path
    saved_at: float
    page_count: int
    display_name: str

    @property
    def age_minutes(self) -> float:
        return (time.time() - self.saved_at) / 60

    def as_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "origin": self.origin,
            "autosave_path": str(self.autosave_path),
            "saved_at": self.saved_at,
            "page_count": self.page_count,
            "display_name": self.display_name,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutosaveRecord:
        return cls(
            document_id=data["document_id"],
            origin=data.get("origin", ""),
            autosave_path=Path(data["autosave_path"]),
            saved_at=float(data.get("saved_at", 0)),
            page_count=int(data.get("page_count", 0)),
            display_name=data.get("display_name", "Untitled"),
        )


class AutosaveService:
    """Timer-driven autosave for a set of open documents."""

    def __init__(
        self,
        settings: AutosaveSettings | None = None,
        *,
        directory: Path | None = None,
    ) -> None:
        self.settings = settings or AutosaveSettings()
        self.directory = directory or app_paths().ensure().autosave
        self.directory.mkdir(parents=True, exist_ok=True)
        self._documents: dict[str, PdfDocument] = {}
        self._lock = threading.RLock()
        self._timer: threading.Timer | None = None
        self._running = False
        self.on_saved: Callable[[AutosaveRecord], None] | None = None

    # -- registration ---------------------------------------------------------- #
    def track(self, document: PdfDocument) -> None:
        """Start autosaving ``document``."""
        with self._lock:
            self._documents[document.id] = document
        log.debug("Tracking {} for autosave", document.display_name)

    def untrack(self, document: PdfDocument, *, discard: bool = True) -> None:
        """Stop autosaving and (by default) delete its autosave files."""
        with self._lock:
            self._documents.pop(document.id, None)
        if discard:
            self.discard(document.id)

    # -- lifecycle -------------------------------------------------------------- #
    def start(self) -> None:
        if not self.settings.enabled or self._running:
            return
        self._running = True
        self._schedule()
        log.info("Autosave every {}s → {}", self.settings.interval_seconds, self.directory)

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None

    def _schedule(self) -> None:
        if not self._running:
            return
        timer = threading.Timer(self.settings.interval_seconds, self._tick)
        timer.daemon = True
        timer.name = "pdfstudio-autosave"
        with self._lock:
            self._timer = timer
        timer.start()

    def _tick(self) -> None:
        try:
            self.save_all()
        except Exception:
            log.exception("Autosave cycle failed")
        finally:
            self._schedule()

    # -- saving -------------------------------------------------------------------- #
    def save_all(self) -> list[AutosaveRecord]:
        """Autosave every tracked document that has unsaved changes."""
        with self._lock:
            documents = list(self._documents.values())
        records: list[AutosaveRecord] = []
        for document in documents:
            if document.is_closed or not document.is_modified:
                continue
            record = self.save(document)
            if record:
                records.append(record)
        return records

    def save(self, document: PdfDocument) -> AutosaveRecord | None:
        """Write one autosave snapshot plus its manifest."""
        if document.is_closed:
            return None
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.directory / f"{document.id}-{stamp}.pdf"
        try:
            data = document.to_bytes(SaveOptions.fast())
            target.write_bytes(data)
        except Exception as exc:
            log.error("Autosave failed for {}: {}", document.display_name, exc)
            return None

        record = AutosaveRecord(
            document_id=document.id,
            origin=str(document.path or ""),
            autosave_path=target,
            saved_at=time.time(),
            page_count=document.page_count,
            display_name=document.display_name,
        )
        manifest = self.directory / f"{document.id}.json"
        manifest.write_text(json.dumps(record.as_dict(), indent=2), "utf-8")
        self._prune(document.id)
        log.debug("Autosaved {} ({} KB)", document.display_name, len(data) // 1024)
        bus().publish(
            Topic.STATUS_MESSAGE,
            {"message": f"Autosaved {document.display_name}", "timeout": 2000},
            source="autosave",
        )
        if self.on_saved:
            self.on_saved(record)
        return record

    def _prune(self, document_id: str) -> None:
        """Keep only the newest ``keep_versions`` snapshots per document."""
        versions = sorted(
            self.directory.glob(f"{document_id}-*.pdf"), key=lambda p: p.stat().st_mtime
        )
        for stale in versions[: max(0, len(versions) - self.settings.keep_versions)]:
            stale.unlink(missing_ok=True)

    def versions(self, document_id: str) -> list[Path]:
        """All retained snapshots for a document, newest first."""
        return sorted(
            self.directory.glob(f"{document_id}-*.pdf"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def discard(self, document_id: str) -> None:
        """Delete every autosave artefact for a document (after a real save)."""
        for path in self.directory.glob(f"{document_id}-*.pdf"):
            path.unlink(missing_ok=True)
        (self.directory / f"{document_id}.json").unlink(missing_ok=True)


class RecoveryManager:
    """Finds and restores autosaves left behind by a crash."""

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = directory or app_paths().ensure().autosave

    def pending(self) -> list[AutosaveRecord]:
        """Autosaves that were never cleaned up (i.e. the app did not exit cleanly)."""
        records: list[AutosaveRecord] = []
        for manifest in self.directory.glob("*.json"):
            try:
                record = AutosaveRecord.from_dict(json.loads(manifest.read_text("utf-8")))
            except (OSError, ValueError, KeyError):
                manifest.unlink(missing_ok=True)
                continue
            if record.autosave_path.exists():
                records.append(record)
            else:
                latest = sorted(
                    self.directory.glob(f"{record.document_id}-*.pdf"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                if latest:
                    record.autosave_path = latest[0]
                    records.append(record)
                else:
                    manifest.unlink(missing_ok=True)
        records.sort(key=lambda r: r.saved_at, reverse=True)
        if records:
            log.warning("{} document(s) available for recovery", len(records))
        return records

    def restore(self, record: AutosaveRecord) -> PdfDocument:
        """Open a recovered document (unsaved, pointing at the original path)."""
        document = PdfDocument.from_bytes(
            record.autosave_path.read_bytes(),
            name=record.display_name,
        )
        if record.origin:
            document.path = Path(record.origin)
            document.display_name = Path(record.origin).name
        document.mark_modified("recovered")
        log.info("Recovered {}", record.display_name)
        return document

    def discard(self, record: AutosaveRecord) -> None:
        record.autosave_path.unlink(missing_ok=True)
        (self.directory / f"{record.document_id}.json").unlink(missing_ok=True)

    def discard_all(self) -> int:
        count = 0
        for record in self.pending():
            self.discard(record)
            count += 1
        return count

    def cleanup_old(self, days: int = 14) -> int:
        """Remove autosave files older than ``days``."""
        cutoff = time.time() - days * 86400
        removed = 0
        for path in self.directory.glob("*"):
            try:
                if path.is_file() and path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed
