"""Platform aware application directories.

Follows the XDG spec on Linux, ``~/Library/Application Support`` on macOS and
``%APPDATA%`` on Windows.  All directories are created lazily on first access
so importing this module has no filesystem side effects beyond path building.
"""

from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pdfstudio import APP_NAME

_SLUG = "pdfstudio"


def _home() -> Path:
    return Path.home()


def _base_config() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", _home() / "AppData/Roaming"))
    if sys.platform == "darwin":
        return _home() / "Library/Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME", _home() / ".config"))


def _base_data() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", _home() / "AppData/Local"))
    if sys.platform == "darwin":
        return _home() / "Library/Application Support"
    return Path(os.environ.get("XDG_DATA_HOME", _home() / ".local/share"))


def _base_cache() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", _home() / "AppData/Local")) / "Cache"
    if sys.platform == "darwin":
        return _home() / "Library/Caches"
    return Path(os.environ.get("XDG_CACHE_HOME", _home() / ".cache"))


@dataclass(frozen=True, slots=True)
class AppPaths:
    """Resolved set of application directories."""

    config: Path
    data: Path
    cache: Path
    logs: Path
    plugins: Path
    themes: Path
    autosave: Path
    temp: Path

    def ensure(self) -> AppPaths:
        """Create every directory (idempotent) and return ``self``."""
        for p in (
            self.config,
            self.data,
            self.cache,
            self.logs,
            self.plugins,
            self.themes,
            self.autosave,
            self.temp,
        ):
            p.mkdir(parents=True, exist_ok=True)
        return self

    @property
    def settings_file(self) -> Path:
        return self.config / "settings.json"

    @property
    def database_file(self) -> Path:
        return self.data / "pdfstudio.sqlite3"

    @property
    def session_file(self) -> Path:
        return self.data / "session.json"


@lru_cache(maxsize=1)
def app_paths() -> AppPaths:
    """Return the (cached) :class:`AppPaths` for this machine and user.

    Set ``PDFSTUDIO_HOME`` to override every location — useful for tests and
    portable installations that keep all state next to the executable.
    """
    override = os.environ.get("PDFSTUDIO_HOME")
    if override:
        root = Path(override).expanduser().resolve()
        return AppPaths(
            config=root / "config",
            data=root / "data",
            cache=root / "cache",
            logs=root / "logs",
            plugins=root / "plugins",
            themes=root / "themes",
            autosave=root / "autosave",
            temp=root / "tmp",
        )
    cfg = _base_config() / _SLUG
    data = _base_data() / _SLUG
    cache = _base_cache() / _SLUG
    return AppPaths(
        config=cfg,
        data=data,
        cache=cache,
        logs=cache / "logs",
        plugins=data / "plugins",
        themes=data / "themes",
        autosave=data / "autosave",
        temp=Path(tempfile.gettempdir()) / _SLUG,
    )


def resources_dir() -> Path:
    """Directory holding bundled read-only assets (themes, icons, samples)."""
    return Path(__file__).resolve().parent.parent / "resources"


def describe() -> str:  # pragma: no cover - diagnostics helper
    p = app_paths()
    return "\n".join(
        [
            f"{APP_NAME} directories:",
            f"  config   {p.config}",
            f"  data     {p.data}",
            f"  cache    {p.cache}",
            f"  logs     {p.logs}",
            f"  plugins  {p.plugins}",
            f"  autosave {p.autosave}",
            f"  bundled  {resources_dir()}",
        ]
    )
