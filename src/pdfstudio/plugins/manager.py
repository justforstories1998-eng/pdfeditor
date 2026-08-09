"""Plugin discovery, loading, sandboxing and hot reload.

Plugins are discovered from three places:

1. ``pdfstudio.plugins.builtin`` — shipped with the application.
2. The user plugin directory (``~/.local/share/pdfstudio/plugins`` or platform
   equivalent) — one sub-directory or ``.py`` file per plugin.
3. Installed distributions advertising the ``pdfstudio.plugins`` entry point.

The optional sandbox restricts what a plugin may import (no ``socket``,
``subprocess``, ``ctypes`` … unless the plugin declares the matching permission
in its metadata and the user approves it).  It is a *safety* mechanism against
careless plugins rather than a security boundary against hostile code — the UI
says so before enabling a third-party plugin.
"""

from __future__ import annotations

import builtins
import importlib
import importlib.util
import inspect
import sys
import threading
import traceback
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.exceptions import PluginError
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.paths import app_paths
from pdfstudio.core.settings import SettingsManager
from pdfstudio.plugins.api import (
    PLUGIN_API_VERSION,
    HostApplication,
    Plugin,
    PluginContext,
    PluginMetadata,
)

log = get_logger("plugins")

#: Modules a sandboxed plugin may not import without declaring a permission.
_RESTRICTED_IMPORTS: dict[str, str] = {
    "socket": "network",
    "http": "network",
    "urllib": "network",
    "requests": "network",
    "ftplib": "network",
    "smtplib": "network",
    "subprocess": "process",
    "multiprocessing": "process",
    "ctypes": "native",
    "cffi": "native",
    "shutil": "filesystem",
    "os": "filesystem",
}


@dataclass(slots=True)
class LoadedPlugin:
    """A plugin instance plus its runtime state."""

    metadata: PluginMetadata
    instance: Plugin
    context: PluginContext
    module: ModuleType
    source: Path | None = None
    enabled: bool = False
    error: str = ""

    @property
    def identifier(self) -> str:
        return self.metadata.identifier


class _SandboxImporter:
    """Context manager swapping ``__import__`` for a permission-checking one."""

    def __init__(self, permissions: Iterable[str]) -> None:
        self.permissions = set(permissions)
        self._original = builtins.__import__

    def __enter__(self) -> _SandboxImporter:
        original = self._original
        permissions = self.permissions

        def guarded(name: str, *args: Any, **kwargs: Any) -> ModuleType:
            root = name.split(".", maxsplit=1)[0]
            needed = _RESTRICTED_IMPORTS.get(root)
            if needed and needed not in permissions:
                raise PluginError(
                    f"This plugin tried to import {name!r} without the '{needed}' permission."
                )
            return original(name, *args, **kwargs)

        builtins.__import__ = guarded
        return self

    def __exit__(self, *exc: Any) -> bool:
        builtins.__import__ = self._original
        return False


class PluginManager:
    """Discovers, loads, enables, disables and reloads plugins."""

    def __init__(
        self,
        *,
        host: HostApplication | None = None,
        settings: SettingsManager | None = None,
        directories: Iterable[Path] | None = None,
    ) -> None:
        self.host = host
        self.settings = settings
        self._lock = threading.RLock()
        self._plugins: dict[str, LoadedPlugin] = {}
        paths = app_paths().ensure()
        self.directories = list(directories) if directories else [paths.plugins]
        self.storage_root = paths.data / "plugin-data"
        self.sandbox = bool(settings.get("plugins.sandbox", True) if settings else True)

    # -- discovery ------------------------------------------------------------ #
    def discover(self) -> list[Path]:
        """Find candidate plugin files/packages on disk."""
        found: list[Path] = []
        for directory in self.directories:
            if not directory.exists():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.name.startswith((".", "_")):
                    continue
                if (
                    entry.is_dir() and (entry / "__init__.py").exists()
                ) or entry.suffix == ".py":
                    found.append(entry)
        log.debug("Discovered {} plugin candidate(s)", len(found))
        return found

    def discover_entry_points(self) -> list[Any]:
        """Plugins installed as distributions with a ``pdfstudio.plugins`` entry point."""
        try:
            from importlib.metadata import entry_points

            return list(entry_points(group="pdfstudio.plugins"))
        except Exception:
            return []

    # -- loading --------------------------------------------------------------- #
    def load_all(self, *, enable: bool = True) -> list[LoadedPlugin]:
        """Load built-ins, entry points and user plugins."""
        loaded: list[LoadedPlugin] = []
        for module_name in self._builtin_modules():
            try:
                loaded.append(self.load_module(module_name, enable=enable))
            except PluginError as exc:
                log.warning("Built-in plugin {} failed: {}", module_name, exc)
        for entry in self.discover_entry_points():
            try:
                module = entry.load()
                plugin = self._instantiate_from_module(
                    module if isinstance(module, ModuleType) else sys.modules[module.__module__]
                )
                loaded.append(self._register(plugin[0], plugin[1], None, enable=enable))
            except Exception as exc:
                log.warning("Entry-point plugin {} failed: {}", entry.name, exc)
        for path in self.discover():
            try:
                loaded.append(self.load_path(path, enable=enable))
            except PluginError as exc:
                log.warning("Plugin at {} failed: {}", path, exc)
        return loaded

    def _builtin_modules(self) -> list[str]:
        package = "pdfstudio.plugins.builtin"
        try:
            module = importlib.import_module(package)
        except ImportError:
            return []
        directory = Path(module.__file__ or "").parent
        return [
            f"{package}.{p.stem}"
            for p in sorted(directory.glob("*.py"))
            if not p.name.startswith("_")
        ]

    def load_module(self, module_name: str, *, enable: bool = True) -> LoadedPlugin:
        """Load a plugin from an importable module path."""
        try:
            module = importlib.import_module(module_name)
        except Exception as exc:
            raise PluginError(f"Cannot import {module_name}", detail=str(exc)) from exc
        cls, metadata = self._instantiate_from_module(module)
        return self._register(cls, metadata, None, module=module, enable=enable)

    def load_path(self, path: Path, *, enable: bool = True) -> LoadedPlugin:
        """Load a plugin from a ``.py`` file or package directory."""
        target = path / "__init__.py" if path.is_dir() else path
        module_name = f"pdfstudio_plugin_{path.stem}"
        spec = importlib.util.spec_from_file_location(module_name, target)
        if spec is None or spec.loader is None:
            raise PluginError(f"Cannot load plugin from {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as exc:
            sys.modules.pop(module_name, None)
            raise PluginError(
                f"Error while loading {path.name}",
                detail="".join(traceback.format_exception_only(type(exc), exc)).strip(),
            ) from exc
        cls, metadata = self._instantiate_from_module(module)
        return self._register(cls, metadata, path, module=module, enable=enable)

    def _instantiate_from_module(
        self, module: ModuleType
    ) -> tuple[type[Plugin], PluginMetadata]:
        """Find the plugin class in ``module`` and validate its metadata."""
        candidate: type[Plugin] | None = None
        if hasattr(module, "PLUGIN"):
            attribute = module.PLUGIN
            candidate = attribute if inspect.isclass(attribute) else type(attribute)
        elif hasattr(module, "create_plugin"):
            instance = module.create_plugin()
            candidate = type(instance)
        else:
            for _, member in inspect.getmembers(module, inspect.isclass):
                if (
                    issubclass(member, Plugin)
                    and member is not Plugin
                    and member.__module__ == module.__name__
                ):
                    candidate = member
                    break
        if candidate is None:
            raise PluginError(
                f"{module.__name__} does not define a Plugin subclass or PLUGIN attribute."
            )
        metadata = getattr(candidate, "metadata", None)
        if not isinstance(metadata, PluginMetadata):
            raise PluginError(f"{candidate.__name__} has no valid PluginMetadata.")
        if not metadata.is_compatible():
            raise PluginError(
                f"{metadata.name} targets plugin API v{metadata.api_version}; "
                f"this build provides v{PLUGIN_API_VERSION}."
            )
        return candidate, metadata

    def _register(
        self,
        cls: type[Plugin],
        metadata: PluginMetadata,
        source: Path | None,
        *,
        module: ModuleType | None = None,
        enable: bool = True,
    ) -> LoadedPlugin:
        with self._lock:
            if metadata.identifier in self._plugins:
                self.unload(metadata.identifier)
            context = PluginContext(
                metadata,
                host=self.host,
                storage_dir=self.storage_root / metadata.identifier,
            )
            record = LoadedPlugin(
                metadata=metadata,
                instance=cls(),
                context=context,
                module=module or sys.modules.get(cls.__module__, ModuleType("plugin")),
                source=source,
            )
            self._plugins[metadata.identifier] = record
        disabled = set(self.settings.get("plugins.disabled", []) if self.settings else [])
        if enable and metadata.identifier not in disabled:
            self.enable(metadata.identifier)
        log.info("Loaded plugin {} v{}", metadata.name, metadata.version)
        return record

    # -- lifecycle -------------------------------------------------------------- #
    def enable(self, identifier: str) -> bool:
        """Activate a plugin, registering its contributions."""
        record = self._plugins.get(identifier)
        if record is None:
            raise PluginError(f"Unknown plugin {identifier!r}")
        if record.enabled:
            return True
        try:
            if self.sandbox:
                with _SandboxImporter(record.metadata.permissions):
                    record.instance.activate(record.context)
            else:
                record.instance.activate(record.context)
            self._connect_hooks(record)
            record.instance.enabled = True
            record.instance.context = record.context
            record.enabled = True
            record.error = ""
            bus().publish(
                Topic.PLUGIN_LOADED,
                {"identifier": identifier, "name": record.metadata.name},
                source="plugins",
            )
            return True
        except Exception as exc:
            record.error = str(exc)
            record.enabled = False
            log.opt(exception=exc).error("Plugin {} failed to activate", identifier)
            return False

    def disable(self, identifier: str) -> bool:
        record = self._plugins.get(identifier)
        if record is None or not record.enabled:
            return False
        try:
            record.instance.deactivate()
        except Exception:
            log.exception("Plugin {} raised during deactivate()", identifier)
        record.context.dispose()
        record.enabled = False
        record.instance.enabled = False
        bus().publish(Topic.PLUGIN_UNLOADED, {"identifier": identifier}, source="plugins")
        log.info("Disabled plugin {}", identifier)
        return True

    def unload(self, identifier: str) -> bool:
        """Disable and forget a plugin entirely."""
        self.disable(identifier)
        with self._lock:
            record = self._plugins.pop(identifier, None)
        if record and record.module.__name__ in sys.modules:
            sys.modules.pop(record.module.__name__, None)
        return record is not None

    def reload(self, identifier: str) -> LoadedPlugin:
        """Hot-reload a plugin from disk, preserving its enabled state."""
        record = self._plugins.get(identifier)
        if record is None:
            raise PluginError(f"Unknown plugin {identifier!r}")
        was_enabled = record.enabled
        source = record.source
        module_name = record.module.__name__
        self.unload(identifier)
        if source is not None:
            reloaded = self.load_path(source, enable=was_enabled)
        else:
            importlib.invalidate_caches()
            module = importlib.reload(importlib.import_module(module_name))
            cls, metadata = self._instantiate_from_module(module)
            reloaded = self._register(cls, metadata, None, module=module, enable=was_enabled)
        log.info("Reloaded plugin {}", identifier)
        return reloaded

    def _connect_hooks(self, record: LoadedPlugin) -> None:
        """Wire ``@hook``-decorated methods to the event bus."""
        for _, member in inspect.getmembers(record.instance, inspect.ismethod):
            topic = getattr(member, "__pdfstudio_hook__", None)
            if topic:
                record.context.subscribe(topic, member)

    def shutdown(self) -> None:
        for identifier in list(self._plugins):
            self.disable(identifier)

    # -- queries ------------------------------------------------------------------ #
    def plugins(self) -> list[LoadedPlugin]:
        with self._lock:
            return list(self._plugins.values())

    def get(self, identifier: str) -> LoadedPlugin | None:
        return self._plugins.get(identifier)

    def commands(self) -> dict[str, Any]:
        """Every command contributed by enabled plugins."""
        out: dict[str, Any] = {}
        for record in self.plugins():
            if record.enabled:
                out.update(record.context.commands)
        return out

    def formats(self, direction: str = "export") -> list[Any]:
        return [
            spec
            for record in self.plugins()
            if record.enabled
            for spec in record.context.formats.values()
            if spec.direction in (direction, "both")
        ]

    def tools(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for record in self.plugins():
            if record.enabled:
                out.update(record.context.tools)
        return out

    def execute(self, command_id: str) -> Any:
        """Run a plugin command by identifier."""
        spec = self.commands().get(command_id)
        if spec is None:
            raise PluginError(f"Unknown command {command_id!r}")
        record = next((r for r in self.plugins() if command_id in r.context.commands), None)
        if record is None:
            raise PluginError(f"No plugin owns command {command_id!r}")
        try:
            return spec.handler(record.context)
        except Exception as exc:
            log.opt(exception=exc).error("Command {} failed", command_id)
            raise PluginError(f"Command {spec.title!r} failed", detail=str(exc)) from exc

    def status(self) -> list[dict[str, Any]]:
        """Table shown in the Plugin Manager dialog."""
        return [
            {
                "identifier": r.identifier,
                "name": r.metadata.name,
                "version": r.metadata.version,
                "author": r.metadata.author,
                "description": r.metadata.description,
                "enabled": r.enabled,
                "commands": len(r.context.commands),
                "source": str(r.source) if r.source else "built-in",
                "error": r.error,
                "permissions": r.metadata.permissions,
            }
            for r in self.plugins()
        ]

    def __iter__(self) -> Iterator[LoadedPlugin]:
        return iter(self.plugins())

    def __len__(self) -> int:
        return len(self._plugins)
