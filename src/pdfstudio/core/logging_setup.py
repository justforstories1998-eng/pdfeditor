"""Centralised logging configuration built on :mod:`loguru`.

Provides three sinks:

* **stderr** — colourised, level configurable through ``PDFSTUDIO_LOG_LEVEL``.
* **rotating file** — ``pdfstudio.log`` (10 MB x 10, zipped) with full context.
* **crash file** — ``crash.log`` receiving ``ERROR`` and above plus tracebacks
  from the global excepthook so post-mortem reports are always available.

The module also installs bridges so that :mod:`logging`-based libraries
(pikepdf, pypdf, urllib3 …) and Qt messages end up in the same stream.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from loguru import logger

from pdfstudio.core.paths import app_paths

_CONFIGURED = threading.Event()

_CONSOLE_FORMAT: Final = (
    "<green>{time:HH:mm:ss.SSS}</green> "
    "<level>{level: <8}</level> "
    "<cyan>{extra[scope]: <14}</cyan> "
    "<level>{message}</level>"
)
_FILE_FORMAT: Final = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {process}:{thread.name} | "
    "{extra[scope]} | {name}:{function}:{line} - {message}"
)


class _InterceptHandler(logging.Handler):
    """Route standard-library logging records into loguru."""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - glue
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame = frame.f_back
            depth += 1
        logger.bind(scope=record.name.split(".")[0]).opt(
            depth=depth, exception=record.exc_info
        ).log(level, record.getMessage())


def setup_logging(
    *,
    level: str | None = None,
    log_dir: Path | None = None,
    console: bool = True,
    retention: str = "30 days",
) -> Path:
    """Configure logging once per process and return the log directory.

    Args:
        level: Minimum console level. Defaults to ``PDFSTUDIO_LOG_LEVEL`` or ``INFO``.
        log_dir: Override the log directory (tests, portable mode).
        console: Emit to stderr as well as to files.
        retention: How long rotated files are kept.
    """
    directory = log_dir or app_paths().ensure().logs
    directory.mkdir(parents=True, exist_ok=True)
    if _CONFIGURED.is_set():
        return directory

    lvl = (level or os.environ.get("PDFSTUDIO_LOG_LEVEL", "INFO")).upper()
    logger.remove()
    logger.configure(extra={"scope": "app"})

    if console:
        logger.add(
            sys.stderr,
            level=lvl,
            format=_CONSOLE_FORMAT,
            backtrace=False,
            diagnose=False,
            enqueue=True,
        )
    logger.add(
        directory / "pdfstudio.log",
        level="DEBUG",
        format=_FILE_FORMAT,
        rotation="10 MB",
        retention=retention,
        compression="zip",
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )
    logger.add(
        directory / "crash.log",
        level="ERROR",
        format=_FILE_FORMAT,
        rotation="2 MB",
        retention=retention,
        enqueue=True,
        backtrace=True,
        diagnose=False,
    )

    logging.basicConfig(handlers=[_InterceptHandler()], level=0, force=True)
    for noisy in ("urllib3", "PIL", "fontTools", "pikepdf", "asyncio", "matplotlib"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    _install_excepthooks()
    _CONFIGURED.set()
    logger.bind(scope="logging").debug("Logging initialised at {} -> {}", lvl, directory)
    return directory


def _install_excepthooks() -> None:
    """Log uncaught exceptions from the main thread, worker threads and asyncio."""

    def _hook(
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):  # pragma: no cover
            sys.__excepthook__(exc_type, exc, tb)
            return
        logger.bind(scope="crash").opt(exception=(exc_type, exc, tb)).critical(
            "Unhandled exception: {}", exc
        )

    sys.excepthook = _hook

    def _thread_hook(args: Any) -> None:  # pragma: no cover - threading glue
        if issubclass(args.exc_type, SystemExit):
            return
        logger.bind(scope="crash").opt(
            exception=(args.exc_type, args.exc_value, args.exc_traceback)
        ).critical("Unhandled exception in thread {}", args.thread.name)

    threading.excepthook = _thread_hook


def get_logger(scope: str) -> logger.__class__:
    """Return a logger bound to ``scope`` (shown as a column in every sink)."""
    return logger.bind(scope=scope)


def crash_report(exc: BaseException) -> str:
    """Render a shareable crash report for the bug-reporting dialog."""
    import platform

    from pdfstudio import __version__

    return "\n".join(
        [
            f"PDF Studio {__version__}",
            f"Python   {sys.version.split()[0]}",
            f"Platform {platform.platform()}",
            f"Machine  {platform.machine()}",
            "",
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        ]
    )
