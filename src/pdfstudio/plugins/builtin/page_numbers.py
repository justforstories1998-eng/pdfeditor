"""Built-in plugin: insert page numbers with configurable position and format."""

from __future__ import annotations

from typing import Any

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.security import SecurityService
from pdfstudio.plugins.api import Plugin, PluginContext, PluginMetadata

log = get_logger("plugin:page-numbers")


class PageNumbersPlugin(Plugin):
    """Adds "Insert page numbers" to the Plugins menu."""

    metadata = PluginMetadata(
        identifier="org.pdfstudio.page-numbers",
        name="Page Numbers",
        version="1.0.0",
        description="Insert page numbers in any corner, with custom formats.",
        author="PDF Studio",
        license="MIT",
        tags=["pages", "headers"],
    )

    def activate(self, context: PluginContext) -> None:
        context.register_command(
            "insert",
            "Insert page numbers…",
            self.insert_numbers,
            menu="Plugins",
            icon="number",
            tooltip="Stamp page numbers onto every page",
            enabled_when="document",
        )
        context.register_command(
            "insert-roman",
            "Insert Roman numerals",
            self.insert_roman,
            menu="Plugins",
            enabled_when="document",
        )

    def insert_numbers(
        self,
        context: PluginContext,
        *,
        template: str = "Page {page} of {pages}",
        position: str = "bottom-center",
    ) -> Any:
        doc = context.document()
        if doc is None:
            context.notify("Open a document first.", level="warning")
            return None
        SecurityService(doc).header_footer(
            footer_center=template if "bottom" in position else "",
            header_center=template if "top" in position else "",
        )
        context.notify(f"Numbered {doc.page_count} page(s).")
        return doc.page_count

    def insert_roman(self, context: PluginContext) -> Any:
        """Apply lower-case Roman page labels to the front matter."""
        doc = context.document()
        if doc is None:
            context.notify("Open a document first.", level="warning")
            return None
        doc.set_page_labels([{"startpage": 0, "prefix": "", "style": "r", "firstpagenum": 1}])
        context.notify("Applied Roman numeral page labels.")
        return True


PLUGIN = PageNumbersPlugin
