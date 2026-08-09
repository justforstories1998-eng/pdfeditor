"""Example plugin: word-frequency report.

Copy this file into your plugin directory (Help ▸ Open plugin folder) and
restart PDF Studio, or use Plugins ▸ Reload plugins for hot reloading.

It demonstrates the four things most plugins need:

* declaring metadata and permissions,
* registering commands that appear in the Plugins menu,
* reading the active document,
* contributing a new export format.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from pdfstudio.plugins.api import Plugin, PluginContext, PluginMetadata, Topic, hook

STOPWORDS = {
    "the", "and", "for", "that", "with", "this", "from", "are", "was", "were",
    "have", "has", "not", "but", "you", "your", "our", "their", "its", "will",
}


class WordFrequencyPlugin(Plugin):
    """Counts words in the active document and can export the counts as CSV."""

    metadata = PluginMetadata(
        identifier="com.example.word-frequency",
        name="Word Frequency",
        version="1.0.0",
        description="Counts word occurrences and exports a CSV report.",
        author="PDF Studio examples",
        license="MIT",
        tags=["analysis", "example"],
        permissions=[],  # no network, no subprocess — fully sandbox-safe
    )

    def activate(self, context: PluginContext) -> None:
        context.register_command(
            "report",
            "Word frequency report",
            self.report,
            menu="Plugins",
            shortcut="Ctrl+Shift+W",
            enabled_when="document",
        )
        context.register_command(
            "export", "Export word counts as CSV…", self.export_csv, menu="Plugins"
        )
        context.register_format(
            "wordcount-csv",
            "Word counts (*.csv)",
            ["csv"],
            self.export_csv,
            direction="export",
        )
        context.log.info("Word Frequency plugin ready")

    def deactivate(self) -> None:
        """Nothing to clean up — the context disposes registrations for us."""

    # -- commands ------------------------------------------------------------- #
    def counts(self, context: PluginContext, limit: int = 25) -> list[tuple[str, int]]:
        document = context.document()
        if document is None:
            return []
        words = re.findall(r"[A-Za-z']{3,}", document.extract_all_text().lower())
        return Counter(w for w in words if w not in STOPWORDS).most_common(limit)

    def report(self, context: PluginContext) -> list[tuple[str, int]]:
        top = self.counts(context)
        if not top:
            context.notify("Open a document with text first.", level="warning")
            return []
        preview = ", ".join(f"{word} ({count})" for word, count in top[:5])
        context.notify(f"Top words: {preview}")
        return top

    def export_csv(self, context: PluginContext, path: str | Path | None = None) -> Path | None:
        rows = self.counts(context, limit=1000)
        if not rows:
            return None
        target = Path(path) if path else context.data_path("word-counts.csv")
        target.write_text(
            "word,count\n" + "\n".join(f"{w},{c}" for w, c in rows), encoding="utf-8"
        )
        context.notify(f"Saved {target}")
        return target

    # -- hooks ------------------------------------------------------------------ #
    @hook(Topic.DOCUMENT_OPENED)
    def on_opened(self, event: Any) -> None:
        """Demonstrates reacting to application events."""
        if self.context:
            self.context.log.debug("Document opened: {}", event.get("path", ""))


PLUGIN = WordFrequencyPlugin
