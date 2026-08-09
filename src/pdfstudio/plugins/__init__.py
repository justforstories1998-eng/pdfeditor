"""Plugin API, manager and built-in plugins."""

from pdfstudio.plugins.api import (
    PLUGIN_API_VERSION,
    CommandSpec,
    FormatSpec,
    Plugin,
    PluginContext,
    PluginMetadata,
    ToolSpec,
    hook,
)
from pdfstudio.plugins.manager import LoadedPlugin, PluginManager

__all__ = [
    "PLUGIN_API_VERSION",
    "CommandSpec",
    "FormatSpec",
    "LoadedPlugin",
    "Plugin",
    "PluginContext",
    "PluginManager",
    "PluginMetadata",
    "ToolSpec",
    "hook",
]
