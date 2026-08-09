"""Typed, observable, JSON-backed settings manager.

Design goals
------------
* **Typed** — settings are declared as dataclasses so IDEs and ``mypy`` catch
  typos; nested sections keep the file readable.
* **Observable** — widgets subscribe to dotted keys (``ui.theme``) and are
  notified on change, which is how live theme switching works.
* **Safe** — writes are atomic (temp file + ``os.replace``); a corrupt file is
  backed up and defaults are restored rather than crashing at start-up.
* **Thread-safe** — a re-entrant lock guards every read/write.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, TypeVar, get_type_hints

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.paths import app_paths

log = get_logger("settings")
T = TypeVar("T")

Listener = Callable[[str, Any], None]


# --------------------------------------------------------------------------- #
# Setting sections
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class UISettings:
    """Appearance and window behaviour."""

    theme: str = "dark"
    accent: str = "#3d7eff"
    toolbar_mode: str = "ribbon"  # ribbon | classic | compact
    icon_size: int = 20
    font_family: str = ""
    font_size: int = 10
    animations: bool = True
    restore_session: bool = True
    show_thumbnails: bool = True
    sidebar_width: int = 260
    remember_window_geometry: bool = True
    hidpi_rounding: str = "PassThrough"
    language: str = "en"
    units: str = "mm"  # mm | cm | in | pt


@dataclass(slots=True)
class ViewerSettings:
    """Default document presentation."""

    layout: str = "continuous"  # single | continuous | facing | book
    zoom_mode: str = "fit-width"  # fit-page | fit-width | fit-height | custom
    zoom: float = 1.0
    min_zoom: float = 0.05
    max_zoom: float = 40.0
    zoom_step: float = 1.15
    page_gap: int = 14
    smooth_scroll: bool = True
    invert_colors: bool = False
    highlight_color: str = "#ffe066"
    show_annotations: bool = True
    default_rotation: int = 0


@dataclass(slots=True)
class PerformanceSettings:
    """Rendering and memory tuning."""

    render_threads: int = 0  # 0 = auto (cpu_count - 1)
    render_dpi: int = 110
    thumbnail_dpi: int = 26
    page_cache_mb: int = 512
    thumbnail_cache_mb: int = 128
    disk_cache: bool = True
    tile_size: int = 1024
    prefetch_pages: int = 3
    gpu_acceleration: bool = True
    lazy_load_threshold: int = 500  # pages before switching to fully lazy mode


@dataclass(slots=True)
class OcrSettings:
    engine: str = "tesseract"  # tesseract | easyocr | auto
    languages: list[str] = field(default_factory=lambda: ["eng"])
    dpi: int = 300
    deskew: bool = True
    denoise: bool = True
    auto_rotate: bool = True
    force: bool = False
    gpu: bool = False
    jobs: int = 0


@dataclass(slots=True)
class AISettings:
    enabled: bool = False
    provider: str = "local"  # local | openai | anthropic | custom
    model: str = "local-extractive"
    endpoint: str = ""
    api_key_env: str = "PDFSTUDIO_AI_API_KEY"
    max_context_chars: int = 60_000
    temperature: float = 0.2


@dataclass(slots=True)
class SecuritySettings:
    default_encryption: str = "AES-256"
    sanitize_on_export: bool = False
    strip_metadata_on_export: bool = False
    warn_on_javascript: bool = True
    allow_remote_content: bool = False


@dataclass(slots=True)
class AutosaveSettings:
    enabled: bool = True
    interval_seconds: int = 120
    keep_versions: int = 8
    crash_recovery: bool = True


@dataclass(slots=True)
class PluginSettings:
    enabled: bool = True
    sandbox: bool = True
    autoload: list[str] = field(default_factory=list)
    disabled: list[str] = field(default_factory=list)
    hot_reload: bool = False


@dataclass(slots=True)
class CloudSettings:
    providers: dict[str, dict[str, Any]] = field(default_factory=dict)
    auto_sync: bool = False
    conflict_policy: str = "ask"  # ask | keep-local | keep-remote | keep-both


@dataclass(slots=True)
class Settings:
    """Root settings object persisted as JSON."""

    version: int = 1
    ui: UISettings = field(default_factory=UISettings)
    viewer: ViewerSettings = field(default_factory=ViewerSettings)
    performance: PerformanceSettings = field(default_factory=PerformanceSettings)
    ocr: OcrSettings = field(default_factory=OcrSettings)
    ai: AISettings = field(default_factory=AISettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)
    autosave: AutosaveSettings = field(default_factory=AutosaveSettings)
    plugins: PluginSettings = field(default_factory=PluginSettings)
    cloud: CloudSettings = field(default_factory=CloudSettings)
    shortcuts: dict[str, str] = field(default_factory=dict)
    recent_files: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Manager
# --------------------------------------------------------------------------- #
def _coerce[T](cls: type[T], data: dict[str, Any]) -> T:
    """Rebuild a (possibly nested) dataclass from ``data``, ignoring unknown keys.

    ``from __future__ import annotations`` turns every field annotation into a
    string, so the real types are resolved with :func:`typing.get_type_hints`
    before deciding whether a value describes a nested section.
    """
    try:
        hints = get_type_hints(cls)
    except Exception:
        hints = {}
    kwargs: dict[str, Any] = {}
    for f in fields(cls):  # type: ignore[arg-type]
        if f.name not in data:
            continue
        value = data[f.name]
        field_type = hints.get(f.name, f.type)
        if is_dataclass(field_type) and isinstance(value, dict):
            kwargs[f.name] = _coerce(field_type, value)  # type: ignore[arg-type]
        else:
            kwargs[f.name] = value
    return cls(**kwargs)  # type: ignore[call-arg]


class SettingsManager:
    """Loads, validates, observes and atomically persists :class:`Settings`."""

    def __init__(self, path: Path | None = None, *, autosave: bool = True) -> None:
        self._path = path or app_paths().ensure().settings_file
        self._lock = threading.RLock()
        self._listeners: dict[str, list[Listener]] = {}
        self._autosave = autosave
        self._settings = self._load()

    # -- persistence -------------------------------------------------------- #
    def _load(self) -> Settings:
        if not self._path.exists():
            log.info("No settings file — creating defaults at {}", self._path)
            settings = Settings()
            self._write(settings)
            return settings
        try:
            raw = json.loads(self._path.read_text("utf-8"))
            settings = _coerce(Settings, raw)
            log.debug("Loaded settings from {}", self._path)
            return settings
        except Exception as exc:
            backup = self._path.with_suffix(".corrupt.json")
            try:
                self._path.replace(backup)
                log.error("Corrupt settings ({}). Backed up to {}", exc, backup)
            except OSError:
                log.error("Corrupt settings ({}) and backup failed", exc)
            settings = Settings()
            self._write(settings)
            return settings

    def _write(self, settings: Settings) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(settings), indent=2, sort_keys=False), "utf-8")
        os.replace(tmp, self._path)

    def save(self) -> None:
        """Force an atomic flush to disk."""
        with self._lock:
            self._write(self._settings)
            log.debug("Settings saved to {}", self._path)

    def reset(self) -> None:
        """Restore factory defaults and notify every listener."""
        with self._lock:
            self._settings = Settings()
            self.save()
        self._notify("", None)

    # -- access ------------------------------------------------------------- #
    @property
    def data(self) -> Settings:
        """The live settings object (mutate through :meth:`set` to get events)."""
        return self._settings

    def get(self, dotted: str, default: Any = None) -> Any:
        """Read ``ui.theme`` style keys."""
        with self._lock:
            node: Any = self._settings
            for part in dotted.split("."):
                if is_dataclass(node):
                    if not hasattr(node, part):
                        return default
                    node = getattr(node, part)
                elif isinstance(node, dict):
                    if part not in node:
                        return default
                    node = node[part]
                else:
                    return default
            return node

    def set(self, dotted: str, value: Any, *, notify: bool = True) -> None:
        """Write a dotted key, persist (if autosave) and notify subscribers."""
        parts = dotted.split(".")
        with self._lock:
            node: Any = self._settings
            for part in parts[:-1]:
                node = getattr(node, part) if is_dataclass(node) else node[part]
            leaf = parts[-1]
            if is_dataclass(node):
                if not hasattr(node, leaf):
                    raise KeyError(f"Unknown setting {dotted!r}")
                setattr(node, leaf, value)
            else:
                node[leaf] = value
            if self._autosave:
                self._write(self._settings)
        if notify:
            self._notify(dotted, value)

    def update(self, values: dict[str, Any]) -> None:
        """Apply many dotted keys at once (single disk write)."""
        previous, self._autosave = self._autosave, False
        try:
            for key, value in values.items():
                self.set(key, value)
        finally:
            self._autosave = previous
        self.save()

    # -- observation -------------------------------------------------------- #
    def subscribe(self, prefix: str, listener: Listener) -> Callable[[], None]:
        """Register ``listener`` for keys starting with ``prefix``.

        Returns a callable that unsubscribes again.
        """
        with self._lock:
            self._listeners.setdefault(prefix, []).append(listener)

        def _unsubscribe() -> None:
            with self._lock:
                if listener in self._listeners.get(prefix, []):
                    self._listeners[prefix].remove(listener)

        return _unsubscribe

    def _notify(self, key: str, value: Any) -> None:
        with self._lock:
            targets = [
                fn
                for prefix, fns in self._listeners.items()
                if key.startswith(prefix) or prefix == ""
                for fn in fns
            ]
        for fn in targets:
            try:
                fn(key, value)
            except Exception:
                log.exception("Settings listener failed for {}", key)

    # -- recent files ------------------------------------------------------- #
    def push_recent(self, path: str | Path, limit: int = 25) -> None:
        text = str(Path(path).resolve())
        with self._lock:
            recents = [r for r in self._settings.recent_files if r != text]
            recents.insert(0, text)
            self._settings.recent_files = recents[:limit]
            if self._autosave:
                self._write(self._settings)
        self._notify("recent_files", self._settings.recent_files)

    def recent_files(self, *, existing_only: bool = True) -> list[Path]:
        paths = [Path(p) for p in self._settings.recent_files]
        return [p for p in paths if p.exists()] if existing_only else paths

    def __iter__(self) -> Iterator[tuple[str, Any]]:  # pragma: no cover
        yield from asdict(self._settings).items()


_GLOBAL: SettingsManager | None = None


def settings() -> SettingsManager:
    """Process-wide singleton used by the UI layer."""
    global _GLOBAL
    if _GLOBAL is None:
        _GLOBAL = SettingsManager()
    return _GLOBAL
