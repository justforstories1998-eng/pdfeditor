"""Application services: history, autosave/recovery and batch processing."""

from __future__ import annotations

from pdfstudio.services.autosave import AutosaveRecord, AutosaveService, RecoveryManager
from pdfstudio.services.batch import BatchProcessor, BatchResult, RenameRule
from pdfstudio.services.history import HistoryStore, RecentFile

__all__ = [
    "AutosaveRecord",
    "AutosaveService",
    "BatchProcessor",
    "BatchResult",
    "HistoryStore",
    "RecentFile",
    "RecoveryManager",
    "RenameRule",
]
