"""Built-in plugin: one-click redaction of common sensitive patterns.

Detects e-mail addresses, phone numbers, credit-card numbers (Luhn-validated),
UK National Insurance numbers, IBANs, US social-security numbers and IP
addresses, then marks them for redaction so the user can review before applying.
"""

from __future__ import annotations

import re
from typing import Any

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.annotations import AnnotationService
from pdfstudio.plugins.api import Plugin, PluginContext, PluginMetadata

log = get_logger("plugin:quick-redact")

#: ``name -> (pattern, needs_luhn)``
PATTERNS: dict[str, tuple[str, bool]] = {
    "e-mail address": (r"[\w.+-]+@[\w-]+\.[\w.-]+", False),
    "phone number": (r"(?:\+\d{1,3}[\s-]?)?(?:\(?\d{2,5}\)?[\s-]?){2,4}\d{2,4}", False),
    "credit card": (r"\b(?:\d[ -]*?){13,19}\b", True),
    "UK National Insurance": (
        r"\b[A-CEGHJ-PR-TW-Z]{2}\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b",
        False,
    ),
    "IBAN": (r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b", False),
    "US SSN": (r"\b\d{3}-\d{2}-\d{4}\b", False),
    "IP address": (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", False),
}


def _luhn(number: str) -> bool:
    """Validate a card number with the Luhn checksum to avoid false positives."""
    digits = [int(c) for c in re.sub(r"\D", "", number)]
    if not 13 <= len(digits) <= 19:
        return False
    checksum = 0
    for position, digit in enumerate(reversed(digits)):
        doubled = digit * 2 if position % 2 else digit
        checksum += doubled - 9 if doubled > 9 else doubled
    return checksum % 10 == 0


class QuickRedactPlugin(Plugin):
    """Scan for personal data and mark it for redaction."""

    metadata = PluginMetadata(
        identifier="org.pdfstudio.quick-redact",
        name="Quick Redact",
        version="1.0.0",
        description="Find and mark e-mails, phone numbers, cards and IDs for redaction.",
        author="PDF Studio",
        license="MIT",
        tags=["security", "privacy", "redaction"],
    )

    def activate(self, context: PluginContext) -> None:
        context.register_command(
            "scan",
            "Find sensitive data…",
            self.scan,
            menu="Plugins",
            icon="shield",
            enabled_when="document",
        )
        context.register_command(
            "mark-all",
            "Mark all sensitive data for redaction",
            self.mark_all,
            menu="Plugins",
            enabled_when="document",
        )

    def find(self, context: PluginContext) -> list[dict[str, Any]]:
        """Return every match without changing the document."""
        doc = context.document()
        if doc is None:
            return []
        findings: list[dict[str, Any]] = []
        for page in range(doc.page_count):
            text = doc.extract_text(page)
            for label, (pattern, needs_luhn) in PATTERNS.items():
                for match in re.finditer(pattern, text):
                    value = match.group(0).strip()
                    if needs_luhn and not _luhn(value):
                        continue
                    if label == "phone number" and len(re.sub(r"\D", "", value)) < 9:
                        continue
                    findings.append({"page": page, "kind": label, "text": value})
        return findings

    def scan(self, context: PluginContext) -> list[dict[str, Any]]:
        findings = self.find(context)
        summary: dict[str, int] = {}
        for item in findings:
            summary[item["kind"]] = summary.get(item["kind"], 0) + 1
        context.notify(
            "No sensitive data found."
            if not findings
            else "Found " + ", ".join(f"{n} {k}(s)" for k, n in summary.items())
        )
        return findings

    def mark_all(self, context: PluginContext) -> int:
        """Mark every finding with a redaction annotation (review before applying)."""
        doc = context.document()
        if doc is None:
            context.notify("Open a document first.", level="warning")
            return 0
        findings = self.find(context)
        service = AnnotationService(doc, author="Quick Redact")
        marked = 0
        with doc.undo_stack.macro("Mark sensitive data"):
            for item in findings:
                with doc.locked() as handle:
                    quads = handle[item["page"]].search_for(item["text"])
                for quad in quads:
                    from pdfstudio.pdfengine.types import Rect

                    service.mark_redaction(
                        item["page"], Rect(quad.x0, quad.y0, quad.x1, quad.y1)
                    )
                    marked += 1
        context.notify(f"Marked {marked} item(s). Review, then apply redactions.")
        return marked


PLUGIN = QuickRedactPlugin
