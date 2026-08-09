"""Public plugin API — the only module third-party plugins should import.

A plugin is a Python package or module exposing a subclass of :class:`Plugin`
and a module-level ``PLUGIN`` attribute (or a ``create_plugin()`` factory)::

    from pdfstudio.plugins.api import Plugin, PluginMetadata, hook

    class HelloPlugin(Plugin):
        metadata = PluginMetadata(
            identifier="com.example.hello",
            name="Hello",
            version="1.0.0",
            description="Adds a greeting command",
            author="Example Ltd",
            api_version=1,
        )

        def activate(self, context):
            context.register_command("hello", "Say hello", self.say_hello)

        def say_hello(self, ctx):
            ctx.notify("Hello from a plugin!")

    PLUGIN = HelloPlugin

The :class:`PluginContext` handed to ``activate`` is the plugin's entire view of
the application: it can register commands, menu items, tools, file-format
handlers, event listeners and background jobs — all of which are automatically
unregistered when the plugin is disabled or hot-reloaded.
"""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pdfstudio.core.events import Event, EventBus, Topic, bus
from pdfstudio.core.jobs import Job, JobManager, jobs
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.undo import Command
from pdfstudio.pdfengine.document import PdfDocument

#: Version of the plugin API. Plugins declaring a different major version are
#: refused by the loader.
PLUGIN_API_VERSION = 1

log = get_logger("plugins")


@dataclass(slots=True)
class PluginMetadata:
    """Identity and requirements of a plugin."""

    identifier: str
    name: str
    version: str = "1.0.0"
    description: str = ""
    author: str = ""
    homepage: str = ""
    license: str = ""
    api_version: int = PLUGIN_API_VERSION
    min_app_version: str = "1.0.0"
    requires: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    icon: str = ""
    tags: list[str] = field(default_factory=list)

    def is_compatible(self) -> bool:
        return self.api_version == PLUGIN_API_VERSION


@dataclass(slots=True)
class CommandSpec:
    """A command a plugin contributes to menus, the ribbon and the palette."""

    identifier: str
    title: str
    handler: Callable[[PluginContext], Any]
    shortcut: str = ""
    icon: str = ""
    menu: str = "Plugins"
    toolbar: bool = False
    tooltip: str = ""
    checkable: bool = False
    enabled_when: str = "always"  # always | document | selection


@dataclass(slots=True)
class ToolSpec:
    """A canvas tool (like the highlight or shape tools)."""

    identifier: str
    title: str
    cursor: str = "arrow"
    icon: str = ""
    on_press: Callable[..., Any] | None = None
    on_move: Callable[..., Any] | None = None
    on_release: Callable[..., Any] | None = None


@dataclass(slots=True)
class FormatSpec:
    """An import/export format contributed by a plugin."""

    identifier: str
    title: str
    extensions: list[str]
    direction: str = "export"  # import | export | both
    handler: Callable[..., Any] | None = None


@runtime_checkable
class HostApplication(Protocol):
    """The subset of the application a plugin may touch."""

    def active_document(self) -> PdfDocument | None: ...
    def open_document(self, path: str | Path) -> PdfDocument: ...
    def notify(self, message: str, *, level: str = "info") -> None: ...
    def ask(self, question: str, options: list[str]) -> str | None: ...


class PluginContext:
    """Everything a plugin can do, with automatic clean-up on unload."""

    def __init__(
        self,
        metadata: PluginMetadata,
        *,
        host: HostApplication | None = None,
        event_bus: EventBus | None = None,
        job_manager: JobManager | None = None,
        storage_dir: Path | None = None,
    ) -> None:
        self.metadata = metadata
        self.host = host
        self.bus = event_bus or bus()
        self.jobs = job_manager or jobs()
        self.storage_dir = storage_dir
        self.commands: dict[str, CommandSpec] = {}
        self.tools: dict[str, ToolSpec] = {}
        self.formats: dict[str, FormatSpec] = {}
        self.menu_items: list[tuple[str, str]] = []
        self._unsubscribers: list[Callable[[], None]] = []
        self.log = get_logger(f"plugin:{metadata.identifier}")

    # -- contributions -------------------------------------------------------- #
    def register_command(
        self,
        identifier: str,
        title: str,
        handler: Callable[[PluginContext], Any],
        **kwargs: Any,
    ) -> CommandSpec:
        """Add a command to the Plugins menu / command palette."""
        spec = CommandSpec(
            identifier=f"{self.metadata.identifier}.{identifier}",
            title=title,
            handler=handler,
            **kwargs,
        )
        self.commands[spec.identifier] = spec
        self.log.debug("Registered command {}", spec.identifier)
        return spec

    def register_tool(self, identifier: str, title: str, **kwargs: Any) -> ToolSpec:
        spec = ToolSpec(
            identifier=f"{self.metadata.identifier}.{identifier}", title=title, **kwargs
        )
        self.tools[spec.identifier] = spec
        return spec

    def register_format(
        self,
        identifier: str,
        title: str,
        extensions: Iterable[str],
        handler: Callable[..., Any],
        *,
        direction: str = "export",
    ) -> FormatSpec:
        """Contribute a new import or export format."""
        spec = FormatSpec(
            identifier=f"{self.metadata.identifier}.{identifier}",
            title=title,
            extensions=[e.lower().lstrip(".") for e in extensions],
            direction=direction,
            handler=handler,
        )
        self.formats[spec.identifier] = spec
        return spec

    def subscribe(self, topic: Topic | str, handler: Callable[[Event], None]) -> None:
        """Listen to an application event (auto-unsubscribed on unload)."""
        self._unsubscribers.append(self.bus.subscribe(topic, handler))

    def publish(self, topic: Topic | str, payload: dict[str, Any] | None = None) -> None:
        self.bus.publish(topic, payload or {}, source=self.metadata.identifier)

    # -- convenience ----------------------------------------------------------- #
    def document(self) -> PdfDocument | None:
        """The document the user is currently working on."""
        return self.host.active_document() if self.host else None

    def notify(self, message: str, *, level: str = "info") -> None:
        """Show a status-bar/toast message."""
        if self.host:
            self.host.notify(message, level=level)
        else:
            self.log.info(message)

    def run_in_background(
        self, name: str, fn: Callable[..., Any], *args: Any, **kwargs: Any
    ) -> Job[Any]:
        """Run work off the UI thread."""
        return self.jobs.submit(f"[{self.metadata.name}] {name}", fn, *args, **kwargs)

    def push_undo(self, document: PdfDocument, command: Command) -> None:
        """Register an undoable change so the user can reverse the plugin."""
        document.undo_stack.push(command)

    def data_path(self, *parts: str) -> Path:
        """A private, writable directory for this plugin."""
        base = self.storage_dir or Path.cwd()
        target = base.joinpath(*parts) if parts else base
        target.parent.mkdir(parents=True, exist_ok=True)
        return target

    def dispose(self) -> None:
        """Remove every contribution (called on disable/reload)."""
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                self.log.debug("Failed to unsubscribe a listener")
        self._unsubscribers.clear()
        self.commands.clear()
        self.tools.clear()
        self.formats.clear()
        self.menu_items.clear()


class Plugin(ABC):
    """Base class for all plugins."""

    #: Must be overridden by every plugin.
    metadata: PluginMetadata

    def __init__(self) -> None:
        self.context: PluginContext | None = None
        self.enabled = False

    @abstractmethod
    def activate(self, context: PluginContext) -> None:
        """Register contributions. Called when the plugin is enabled."""

    def deactivate(self) -> None:
        """Release resources. Called on disable, reload and shutdown."""

    def configure(self) -> dict[str, Any]:
        """Return a settings schema so the host can render a preferences page."""
        return {}

    def on_document_opened(self, document: PdfDocument) -> None:
        """Optional hook."""

    def on_document_saved(self, document: PdfDocument) -> None:
        """Optional hook."""

    def on_document_closed(self, document: PdfDocument) -> None:
        """Optional hook."""


def hook(topic: Topic | str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator marking a method as an event handler.

    The loader connects every decorated method when the plugin activates::

        @hook(Topic.DOCUMENT_OPENED)
        def on_open(self, event): ...
    """

    def decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return fn(*args, **kwargs)

        wrapper.__pdfstudio_hook__ = str(topic)  # type: ignore[attr-defined]
        return wrapper

    return decorator


__all__ = [
    "PLUGIN_API_VERSION",
    "CommandSpec",
    "FormatSpec",
    "HostApplication",
    "Plugin",
    "PluginContext",
    "PluginMetadata",
    "ToolSpec",
    "Topic",
    "hook",
]
