"""Cross-cutting infrastructure: paths, logging, settings, events, undo, jobs."""

from __future__ import annotations

from pdfstudio.core.events import Event, EventBus, Topic, bus
from pdfstudio.core.exceptions import (
    DependencyMissingError,
    DocumentError,
    OcrError,
    PasswordRequiredError,
    PdfStudioError,
    PermissionDeniedError,
    PluginError,
    RenderError,
    ValidationError,
)
from pdfstudio.core.jobs import Job, JobContext, JobManager, JobState, jobs
from pdfstudio.core.logging_setup import get_logger, setup_logging
from pdfstudio.core.paths import AppPaths, app_paths, resources_dir
from pdfstudio.core.settings import Settings, SettingsManager, settings
from pdfstudio.core.undo import Command, FunctionCommand, MacroCommand, UndoStack

__all__ = [
    "AppPaths",
    "Command",
    "DependencyMissingError",
    "DocumentError",
    "Event",
    "EventBus",
    "FunctionCommand",
    "Job",
    "JobContext",
    "JobManager",
    "JobState",
    "MacroCommand",
    "OcrError",
    "PasswordRequiredError",
    "PdfStudioError",
    "PermissionDeniedError",
    "PluginError",
    "RenderError",
    "Settings",
    "SettingsManager",
    "Topic",
    "UndoStack",
    "ValidationError",
    "app_paths",
    "bus",
    "get_logger",
    "jobs",
    "resources_dir",
    "settings",
    "setup_logging",
]
