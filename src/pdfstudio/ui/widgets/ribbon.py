"""Ribbon toolbar (Office/Acrobat style) with a classic-toolbar alternative.

The ribbon is built declaratively from a list of :class:`RibbonTab` →
:class:`RibbonGroup` → :class:`RibbonItem` definitions, so the same
specification drives the ribbon, the classic toolbar, the menu bar and the
command palette — one source of truth for every command in the application.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from PySide6.QtCore import QEvent, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QFont,
    QIcon,
    QKeySequence,
    QPainter,
    QPixmap,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMenu,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger

log = get_logger("ui.ribbon")


@dataclass(slots=True)
class RibbonItem:
    """One command in the ribbon."""

    identifier: str
    title: str
    icon: str = ""
    shortcut: str = ""
    tooltip: str = ""
    checkable: bool = False
    checked: bool = False
    large: bool = False
    separator: bool = False
    menu: list[RibbonItem] = field(default_factory=list)
    widget: str = ""  # "zoom", "page", "font", … for embedded controls
    group_id: str = ""  # radio-style exclusivity (tools)


@dataclass(slots=True)
class RibbonGroup:
    """A labelled cluster of commands."""

    title: str
    items: list[RibbonItem] = field(default_factory=list)
    columns: int = 3


@dataclass(slots=True)
class RibbonTab:
    """One ribbon tab."""

    title: str
    groups: list[RibbonGroup] = field(default_factory=list)


def _text_icon(glyph: str, color: str = "#c8ccd4", size: int = 22) -> QIcon:
    """Render a glyph into an icon.

    PDF Studio ships SVG icons, but rendering the glyph keeps the application
    fully functional when the icon theme is missing (source checkouts, tests).
    """
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    font = QFont()
    font.setPointSizeF(size * 0.62)
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    painter.end()
    return QIcon(pixmap)


#: Glyphs used when no icon theme is installed.
GLYPHS: dict[str, str] = {
    "open": "\u25b1",
    "save": "\u25a3",
    "save-as": "\u25a4",
    "print": "\u2399",
    "new": "\u25a1",
    "undo": "\u21b6",
    "redo": "\u21b7",
    "cut": "\u2702",
    "copy": "\u29c9",
    "paste": "\u25a7",
    "zoom-in": "+",
    "zoom-out": "\u2212",
    "fit-page": "\u25a2",
    "fit-width": "\u2194",
    "rotate-left": "\u21ba",
    "rotate-right": "\u21bb",
    "search": "\u2315",
    "select": "\u2196",
    "pan": "\u2725",
    "text": "T",
    "highlight": "\u25ac",
    "underline": "U",
    "strike": "S",
    "note": "\u25a8",
    "ink": "\u270e",
    "shape": "\u25ad",
    "ellipse": "\u25ef",
    "arrow": "\u2197",
    "stamp": "\u25c8",
    "redact": "\u25ae",
    "measure": "\u2314",
    "sign": "\u270d",
    "form": "\u25a5",
    "ocr": "\u24b6",
    "compress": "\u21f2",
    "protect": "\u26bf",
    "watermark": "\u2591",
    "pages": "\u25f1",
    "merge": "\u29c9",
    "split": "\u2551",
    "export": "\u21d3",
    "import": "\u21d1",
    "ai": "\u2726",
    "compare": "\u21c4",
    "bookmark": "\u2691",
    "layers": "\u2750",
    "attach": "\u26b7",
    "settings": "\u2699",
    "help": "?",
    "plugin": "\u2756",
    "batch": "\u26a1",
    "delete": "\u2715",
    "insert": "+",
    "crop": "\u2337",
    "link": "\u26ad",
    "image": "\u26f0",
    "table": "\u229e",
    "presentation": "\u25b6",
    "fullscreen": "\u2922",
    "theme": "\u25d0",
    "accessibility": "\u26f9",
}


def build_default_tabs() -> list[RibbonTab]:
    """The complete command surface of PDF Studio."""
    return [
        RibbonTab(
            "Home",
            [
                RibbonGroup(
                    "File",
                    [
                        RibbonItem("file.open", "Open", "open", "Ctrl+O", large=True),
                        RibbonItem("file.save", "Save", "save", "Ctrl+S", large=True),
                        RibbonItem("file.save_as", "Save as", "save-as", "Ctrl+Shift+S"),
                        RibbonItem("file.print", "Print", "print", "Ctrl+P"),
                        RibbonItem("file.close", "Close", "delete", "Ctrl+W"),
                    ],
                ),
                RibbonGroup(
                    "Edit",
                    [
                        RibbonItem("edit.undo", "Undo", "undo", "Ctrl+Z", large=True),
                        RibbonItem("edit.redo", "Redo", "redo", "Ctrl+Y", large=True),
                        RibbonItem(
                            "edit.copy",
                            "Copy",
                            "copy",
                            "Ctrl+C",
                            tooltip="Copy the selected object, or the selected text",
                        ),
                        RibbonItem(
                            "edit.cut",
                            "Cut",
                            "delete",
                            "Ctrl+X",
                            tooltip="Cut the selected object",
                        ),
                        RibbonItem(
                            "edit.paste",
                            "Paste",
                            "paste",
                            "Ctrl+V",
                            tooltip="Paste an object, image or text onto the page",
                        ),
                        RibbonItem("edit.find", "Find", "search", "Ctrl+F"),
                    ],
                ),
                RibbonGroup(
                    "Tools",
                    [
                        RibbonItem(
                            "tool.select",
                            "Select",
                            "select",
                            "V",
                            checkable=True,
                            checked=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "tool.pan", "Pan", "pan", "H", checkable=True, group_id="tool"
                        ),
                        RibbonItem(
                            "tool.text_select",
                            "Text",
                            "text",
                            "T",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "tool.zoom",
                            "Marquee zoom",
                            "zoom-in",
                            "Z",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "tool.edit_text",
                            "Edit text",
                            "text",
                            "E",
                            checkable=True,
                            group_id="tool",
                            large=True,
                        ),
                        RibbonItem(
                            "tool.move_object",
                            "Move object",
                            "pages",
                            "M",
                            checkable=True,
                            group_id="tool",
                            large=True,
                            tooltip="Drag any text, image or line; arrow keys nudge",
                        ),
                    ],
                ),
                RibbonGroup(
                    "View",
                    [
                        RibbonItem("view.zoom_out", "Zoom out", "zoom-out", "Ctrl+-"),
                        RibbonItem("view.zoom_widget", "", widget="zoom"),
                        RibbonItem("view.zoom_in", "Zoom in", "zoom-in", "Ctrl++"),
                        RibbonItem("view.fit_page", "Fit page", "fit-page", "Ctrl+0"),
                        RibbonItem("view.fit_width", "Fit width", "fit-width", "Ctrl+1"),
                        RibbonItem("view.rotate_left", "Rotate left", "rotate-left"),
                        RibbonItem("view.rotate_right", "Rotate right", "rotate-right"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Edit",
            [
                RibbonGroup(
                    "Content",
                    [
                        RibbonItem("content.edit_text", "Edit text", "text", large=True),
                        RibbonItem("content.add_text", "Add text", "text"),
                        RibbonItem("content.add_image", "Add image", "image"),
                        RibbonItem("content.edit_image", "Edit image", "image"),
                        RibbonItem("content.draw", "Draw shape", "shape"),
                        RibbonItem("content.link", "Add link", "link"),
                    ],
                ),
                RibbonGroup(
                    "Arrange",
                    [
                        RibbonItem(
                            "object.move",
                            "Move object",
                            "pages",
                            "M",
                            checkable=True,
                            group_id="tool",
                            large=True,
                            tooltip="Select and drag any object on the page",
                        ),
                        RibbonItem(
                            "object.copy",
                            "Copy",
                            "copy",
                            "Ctrl+C",
                            tooltip="Copy the selected object",
                        ),
                        RibbonItem(
                            "object.paste",
                            "Paste",
                            "paste",
                            tooltip="Paste the copied object onto this page",
                        ),
                        RibbonItem(
                            "object.duplicate",
                            "Duplicate",
                            "copy",
                            "Ctrl+D",
                            tooltip="Copy and paste in one step",
                        ),
                        RibbonItem("object.align_left", "Align left", "fit-width"),
                        RibbonItem("object.align_center", "Align centre", "fit-page"),
                        RibbonItem("object.align_right", "Align right", "fit-width"),
                        RibbonItem("object.nudge_left", "Nudge left", "zoom-out"),
                        RibbonItem("object.nudge_right", "Nudge right", "zoom-in"),
                        RibbonItem("object.delete", "Delete object", "delete"),
                    ],
                ),
                RibbonGroup(
                    "Paragraph",
                    [
                        RibbonItem(
                            "text.edit_paragraph",
                            "Edit paragraph",
                            "text",
                            "Ctrl+Shift+P",
                            large=True,
                            tooltip="Select and edit the whole paragraph at once",
                        ),
                        RibbonItem(
                            "text.select_paragraph",
                            "Select paragraph",
                            "text",
                            tooltip="Select all text in the paragraph under the cursor",
                        ),
                        RibbonItem("text.align_left", "Align left", "fit-width"),
                        RibbonItem("text.align_center", "Centre", "fit-page"),
                        RibbonItem("text.align_right", "Align right", "fit-width"),
                        RibbonItem("text.outdent", "Remove indent", "zoom-out"),
                        RibbonItem("text.indent", "Add indent", "zoom-in"),
                        RibbonItem(
                            "text.move_line_up",
                            "Move line up",
                            "zoom-out",
                            "Alt+Up",
                            tooltip="Swap this line with the one above",
                        ),
                        RibbonItem(
                            "text.move_line_down",
                            "Move line down",
                            "zoom-in",
                            "Alt+Down",
                            tooltip="Swap this line with the one below",
                        ),
                    ],
                ),
                RibbonGroup(
                    "Lists & case",
                    [
                        RibbonItem("text.bullets", "Bullets", "text"),
                        RibbonItem("text.numbering", "Numbering", "text"),
                        RibbonItem("text.list_none", "Remove list", "delete"),
                        RibbonItem("text.case_upper", "UPPERCASE", "text"),
                        RibbonItem("text.case_lower", "lowercase", "text"),
                        RibbonItem("text.case_title", "Title Case", "text"),
                        RibbonItem("text.case_sentence", "Sentence case", "text"),
                        RibbonItem("text.spacing_single", "Single spacing", "fit-height"),
                        RibbonItem("text.spacing_15", "1.5 spacing", "fit-height"),
                        RibbonItem("text.spacing_double", "Double spacing", "fit-height"),
                    ],
                ),
                RibbonGroup(
                    "Lines & format",
                    [
                        RibbonItem(
                            "text.duplicate_line",
                            "Duplicate line",
                            "copy",
                            "Ctrl+Shift+D",
                        ),
                        RibbonItem("text.delete_line", "Delete line", "delete", "Ctrl+Shift+K"),
                        RibbonItem(
                            "text.copy_format",
                            "Copy formatting",
                            "copy",
                            tooltip="Remember this paragraph's font, size and colour",
                        ),
                        RibbonItem(
                            "text.paste_format",
                            "Apply formatting",
                            "paste",
                            tooltip="Restyle this paragraph to match the copied one",
                        ),
                        RibbonItem("review.word_count", "Word count", "search"),
                    ],
                ),
                RibbonGroup(
                    "Text style",
                    [
                        RibbonItem("style.font", "", widget="font"),
                        RibbonItem("style.size", "", widget="font_size"),
                        RibbonItem("style.bold", "Bold", "text", "Ctrl+B", checkable=True),
                        RibbonItem("style.italic", "Italic", "text", "Ctrl+I", checkable=True),
                        RibbonItem(
                            "text.underline", "Underline", "text", "Ctrl+U", checkable=True
                        ),
                        RibbonItem("text.strikethrough", "Strikethrough", "text"),
                        RibbonItem("style.color", "Colour", "shape"),
                    ],
                ),
                RibbonGroup(
                    "Find",
                    [
                        RibbonItem("edit.find", "Find", "search", "Ctrl+F", large=True),
                        RibbonItem("edit.replace", "Replace", "search", "Ctrl+H", large=True),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Pages",
            [
                RibbonGroup(
                    "Organise",
                    [
                        RibbonItem("pages.insert", "Insert", "insert", large=True),
                        RibbonItem("pages.delete", "Delete", "delete", large=True),
                        RibbonItem("pages.extract", "Extract", "export"),
                        RibbonItem("pages.replace", "Replace", "pages"),
                        RibbonItem("pages.duplicate", "Duplicate", "copy"),
                        RibbonItem("pages.move", "Move", "pages"),
                    ],
                ),
                RibbonGroup(
                    "Transform",
                    [
                        RibbonItem("pages.rotate_left", "Rotate left", "rotate-left"),
                        RibbonItem("pages.rotate_right", "Rotate right", "rotate-right"),
                        RibbonItem("pages.crop", "Crop", "crop"),
                        RibbonItem("pages.resize", "Resize", "pages"),
                        RibbonItem("pages.labels", "Page labels", "text"),
                    ],
                ),
                RibbonGroup(
                    "Document",
                    [
                        RibbonItem("pages.merge", "Merge", "merge", large=True),
                        RibbonItem("pages.split", "Split", "split", large=True),
                        RibbonItem("pages.nup", "N-up", "pages"),
                        RibbonItem("pages.booklet", "Booklet", "pages"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Comment",
            [
                RibbonGroup(
                    "Markup",
                    [
                        RibbonItem(
                            "annot.highlight",
                            "Highlight",
                            "highlight",
                            "Ctrl+Shift+H",
                            checkable=True,
                            group_id="tool",
                            large=True,
                        ),
                        RibbonItem(
                            "annot.underline",
                            "Underline",
                            "underline",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "annot.strikeout",
                            "Strikeout",
                            "strike",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "annot.note", "Sticky note", "note", checkable=True, group_id="tool"
                        ),
                        RibbonItem(
                            "annot.textbox", "Text box", "text", checkable=True, group_id="tool"
                        ),
                    ],
                ),
                RibbonGroup(
                    "Drawing",
                    [
                        RibbonItem("annot.ink", "Ink", "ink", checkable=True, group_id="tool"),
                        RibbonItem(
                            "annot.rectangle",
                            "Rectangle",
                            "shape",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "annot.ellipse",
                            "Ellipse",
                            "ellipse",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem(
                            "annot.arrow", "Arrow", "arrow", checkable=True, group_id="tool"
                        ),
                        RibbonItem("annot.stamp", "Stamp", "stamp"),
                    ],
                ),
                RibbonGroup(
                    "Review",
                    [
                        RibbonItem(
                            "annot.measure",
                            "Measure",
                            "measure",
                            checkable=True,
                            group_id="tool",
                        ),
                        RibbonItem("annot.export", "Export comments", "export"),
                        RibbonItem("annot.import", "Import comments", "import"),
                        RibbonItem("annot.flatten", "Flatten", "pages"),
                        RibbonItem("annot.delete_all", "Delete all", "delete"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Forms",
            [
                RibbonGroup(
                    "Fields",
                    [
                        RibbonItem("form.text", "Text field", "form", large=True),
                        RibbonItem("form.checkbox", "Check box", "form"),
                        RibbonItem("form.radio", "Radio button", "form"),
                        RibbonItem("form.dropdown", "Dropdown", "form"),
                        RibbonItem("form.listbox", "List box", "form"),
                        RibbonItem("form.date", "Date picker", "form"),
                        RibbonItem("form.signature", "Signature", "sign"),
                        RibbonItem("form.barcode", "QR / barcode", "form"),
                    ],
                ),
                RibbonGroup(
                    "Data",
                    [
                        RibbonItem("form.import", "Import data", "import"),
                        RibbonItem("form.export", "Export data", "export"),
                        RibbonItem("form.reset", "Reset", "undo"),
                        RibbonItem("form.flatten", "Flatten", "pages"),
                        RibbonItem("form.validate", "Validate", "form"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Protect",
            [
                RibbonGroup(
                    "Security",
                    [
                        RibbonItem("secure.encrypt", "Encrypt", "protect", large=True),
                        RibbonItem("secure.decrypt", "Remove security", "protect"),
                        RibbonItem("secure.permissions", "Permissions", "protect"),
                        RibbonItem("secure.sanitize", "Sanitise", "protect"),
                    ],
                ),
                RibbonGroup(
                    "Redaction",
                    [
                        RibbonItem(
                            "secure.mark_redaction",
                            "Mark",
                            "redact",
                            checkable=True,
                            group_id="tool",
                            large=True,
                        ),
                        RibbonItem("secure.find_redact", "Find text to redact", "search"),
                        RibbonItem("secure.apply_redaction", "Apply", "redact", large=True),
                    ],
                ),
                RibbonGroup(
                    "Signatures",
                    [
                        RibbonItem("sign.sign", "Sign document", "sign", large=True),
                        RibbonItem("sign.validate", "Validate", "sign"),
                        RibbonItem("sign.certificates", "Certificates", "protect"),
                    ],
                ),
                RibbonGroup(
                    "Marks",
                    [
                        RibbonItem("mark.watermark", "Watermark", "watermark"),
                        RibbonItem("mark.bates", "Bates numbering", "text"),
                        RibbonItem("mark.header_footer", "Header & footer", "text"),
                        RibbonItem("mark.background", "Background", "shape"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Convert",
            [
                RibbonGroup(
                    "Create",
                    [
                        RibbonItem("convert.from_file", "From file", "import", large=True),
                        RibbonItem("convert.from_images", "From images", "image"),
                        RibbonItem("convert.from_scanner", "From scanner", "image"),
                    ],
                ),
                RibbonGroup(
                    "Export",
                    [
                        RibbonItem("convert.to_docx", "Word", "export", large=True),
                        RibbonItem("convert.to_pptx", "PowerPoint", "export"),
                        RibbonItem("convert.to_images", "Images", "image"),
                        RibbonItem("convert.to_text", "Text", "text"),
                        RibbonItem("convert.to_html", "HTML", "export"),
                        RibbonItem("convert.to_markdown", "Markdown", "export"),
                    ],
                ),
                RibbonGroup(
                    "Archive",
                    [
                        RibbonItem("convert.pdfa", "PDF/A", "export"),
                        RibbonItem("convert.pdfx", "PDF/X", "export"),
                        RibbonItem("convert.pdfua", "PDF/UA", "accessibility"),
                    ],
                ),
                RibbonGroup(
                    "Optimise",
                    [
                        RibbonItem("optimize.compress", "Compress", "compress", large=True),
                        RibbonItem("optimize.analyze", "Analyse size", "compress"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Tools",
            [
                RibbonGroup(
                    "Recognition",
                    [
                        RibbonItem("ocr.run", "OCR", "ocr", large=True),
                        RibbonItem("ocr.settings", "OCR settings", "settings"),
                    ],
                ),
                RibbonGroup(
                    "AI",
                    [
                        RibbonItem("ai.summarize", "Summarise", "ai", large=True),
                        RibbonItem("ai.chat", "Chat with PDF", "ai", large=True),
                        RibbonItem("ai.translate", "Translate", "ai"),
                        RibbonItem("ai.bookmarks", "Generate bookmarks", "bookmark"),
                        RibbonItem("ai.tables", "Extract tables", "table"),
                        RibbonItem("ai.metadata", "Suggest metadata", "ai"),
                    ],
                ),
                RibbonGroup(
                    "Compare",
                    [
                        RibbonItem("compare.documents", "Compare files", "compare", large=True),
                    ],
                ),
                RibbonGroup(
                    "Batch",
                    [
                        RibbonItem("batch.run", "Batch process", "batch", large=True),
                        RibbonItem("batch.merge", "Merge files", "merge"),
                        RibbonItem("batch.split", "Split files", "split"),
                    ],
                ),
                RibbonGroup(
                    "Automation",
                    [
                        RibbonItem("script.console", "Script console", "plugin"),
                        RibbonItem("script.macro", "Record macro", "plugin"),
                        RibbonItem("plugins.manage", "Plugins", "plugin"),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "View",
            [
                RibbonGroup(
                    "Layout",
                    [
                        RibbonItem(
                            "view.single",
                            "Single page",
                            "pages",
                            checkable=True,
                            group_id="layout",
                        ),
                        RibbonItem(
                            "view.continuous",
                            "Continuous",
                            "pages",
                            checkable=True,
                            checked=True,
                            group_id="layout",
                        ),
                        RibbonItem(
                            "view.facing", "Facing", "pages", checkable=True, group_id="layout"
                        ),
                        RibbonItem(
                            "view.book", "Book", "pages", checkable=True, group_id="layout"
                        ),
                    ],
                ),
                RibbonGroup(
                    "Display",
                    [
                        RibbonItem(
                            "view.presentation",
                            "Presentation",
                            "presentation",
                            "F5",
                            large=True,
                        ),
                        RibbonItem("view.fullscreen", "Full screen", "fullscreen", "F11"),
                        RibbonItem("view.invert", "Night mode", "theme", checkable=True),
                        RibbonItem(
                            "view.annotations",
                            "Show comments",
                            "note",
                            checkable=True,
                            checked=True,
                        ),
                    ],
                ),
                RibbonGroup(
                    "Panels",
                    [
                        RibbonItem(
                            "panel.thumbnails",
                            "Thumbnails",
                            "pages",
                            checkable=True,
                            checked=True,
                        ),
                        RibbonItem("panel.bookmarks", "Bookmarks", "bookmark", checkable=True),
                        RibbonItem("panel.comments", "Comments", "note", checkable=True),
                        RibbonItem("panel.layers", "Layers", "layers", checkable=True),
                        RibbonItem(
                            "panel.attachments", "Attachments", "attach", checkable=True
                        ),
                        RibbonItem(
                            "panel.properties", "Properties", "settings", checkable=True
                        ),
                    ],
                ),
                RibbonGroup(
                    "Appearance",
                    [
                        RibbonItem("view.theme", "Theme", "theme"),
                        RibbonItem(
                            "view.toolbar_mode", "Classic toolbar", "settings", checkable=True
                        ),
                    ],
                ),
            ],
        ),
        RibbonTab(
            "Accessibility",
            [
                RibbonGroup(
                    "Check",
                    [
                        RibbonItem("a11y.check", "Full check", "accessibility", large=True),
                        RibbonItem(
                            "a11y.autofix", "Fix automatically", "accessibility", large=True
                        ),
                    ],
                ),
                RibbonGroup(
                    "Structure",
                    [
                        RibbonItem("a11y.tags", "Tag editor", "table"),
                        RibbonItem("a11y.reading_order", "Reading order", "pages"),
                        RibbonItem("a11y.alt_text", "Alternative text", "image"),
                        RibbonItem("a11y.language", "Set language", "text"),
                    ],
                ),
                RibbonGroup(
                    "Reading",
                    [
                        RibbonItem("a11y.read_aloud", "Read aloud", "presentation"),
                        RibbonItem(
                            "a11y.high_contrast", "High contrast", "theme", checkable=True
                        ),
                    ],
                ),
            ],
        ),
    ]


class _LargeToolButton(QToolButton):
    """A tall ribbon button that always fits its own caption.

    ``ToolButtonTextUnderIcon`` elides the caption when the button is narrower
    than the text ("Edit text" → "Edi...ext"). A minimum width measured once at
    construction is not enough, because a theme is applied *after* the ribbon
    is built and can change the font. Overriding the size hint means the width
    is recomputed from the current font whenever it matters.
    """

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        hint = super().sizeHint()
        needed = self.fontMetrics().horizontalAdvance(self.text()) + 20
        return QSize(max(hint.width(), needed, 64), max(hint.height(), 58))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.sizeHint()

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt API
        # A font change invalidates the cached width, so ask the layout to
        # measure again rather than keeping a stale (too small) geometry.
        if event.type() in (QEvent.Type.FontChange, QEvent.Type.StyleChange):
            self.updateGeometry()
        super().changeEvent(event)


class RibbonBar(QWidget):
    """Tabbed ribbon rendering a :func:`build_default_tabs` specification."""

    command = Signal(str)  # identifier
    toggled = Signal(str, bool)  # identifier, state
    zoom_requested = Signal(float)
    page_requested = Signal(int)

    def __init__(
        self, tabs: Sequence[RibbonTab] | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setObjectName("RibbonBar")
        self._actions: dict[str, QAction] = {}
        self._buttons: dict[str, QToolButton] = {}
        self._groups: dict[str, QActionGroup] = {}
        # QActionGroup only manages QActions; the ribbon's buttons need their
        # own exclusive group or several tools appear active at once.
        self._button_groups: dict[str, QButtonGroup] = {}
        self.zoom_combo: QComboBox | None = None
        self.page_spin: QSpinBox | None = None
        self.font_combo: QComboBox | None = None
        self.font_size_combo: QComboBox | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)

        for tab in tabs or build_default_tabs():
            self.tabs.addTab(self._build_tab(tab), tab.title)
        # Tall enough for the deepest tab. Dense groups stack three rows, so a
        # height fixed at the two-row figure clipped their bottom row.
        self.setFixedHeight(self._natural_height())

    def _natural_height(self) -> int:
        """Height that shows every tab's tallest row without clipping.

        Measured from the built pages rather than hard-coded, so adding a
        command to a group can never silently cut off its bottom row again.
        """
        tallest = 0
        bar = 0
        for index in range(self.tabs.count()):
            scroller = self.tabs.widget(index)
            page = scroller.widget() if hasattr(scroller, "widget") else scroller
            if page is not None:
                tallest = max(tallest, page.sizeHint().height())
            if isinstance(scroller, QScrollArea):
                # A tab wider than the window shows a horizontal scrollbar,
                # which eats into the viewport. Not reserving that space left
                # the deepest tab a dozen pixels short of its bottom row.
                bar = max(bar, scroller.horizontalScrollBar().sizeHint().height())
        # Room for the tab strip itself and the page frame.
        return tallest + bar + self.tabs.tabBar().sizeHint().height() + 12

    # -- construction ---------------------------------------------------------- #
    def _build_tab(self, tab: RibbonTab) -> QWidget:
        """One ribbon tab, wrapped in a horizontal scroller.

        Without the scroll area a tab's full width becomes its
        ``minimumSizeHint``, and ``QTabWidget`` reports the *widest* tab as the
        minimum for the whole bar — which then becomes a hard floor on the
        window. The Edit tab needs ~1765 px, so on any display narrower than
        that the window could not shrink to fit and Qt simply clipped the right
        edge: "Cut" and "Move object" vanished with no scrollbar to reveal
        them. Scrolling keeps every command reachable at any window size.
        """
        page = QWidget()
        layout = QHBoxLayout(page)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(2)
        for group in tab.groups:
            layout.addWidget(self._build_group(group))
        layout.addStretch(1)

        scroller = QScrollArea(self)
        scroller.setObjectName("RibbonScroll")
        scroller.setWidget(page)
        scroller.setWidgetResizable(True)
        scroller.setFrameShape(QFrame.Shape.NoFrame)
        scroller.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroller.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # The scroll area must not inherit the page's width as a minimum, or
        # the floor it was introduced to remove would come straight back.
        scroller.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        scroller.setMinimumWidth(0)
        return scroller

    def _build_group(self, group: RibbonGroup) -> QWidget:
        frame = QFrame(self)
        frame.setObjectName("RibbonGroup")
        outer = QVBoxLayout(frame)
        outer.setContentsMargins(6, 2, 6, 2)
        outer.setSpacing(2)

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(2)

        # Dense groups stack three deep instead of two. Arrange carries ten
        # commands: at two per column that is five columns wide and pushed the
        # Edit tab past 1700 px, which is wider than many laptop screens.
        small = sum(1 for item in group.items if not item.large and not item.widget)
        rows = 3 if small > 6 else 2

        row = column = 0
        for item in group.items:
            widget = self._build_item(item)
            if widget is None:
                continue
            if item.large:
                # A tall button always spans the group's full height, so it
                # stays the same size whether the group is two or three deep.
                if row:  # don't drop it into a half-filled column
                    column += 1
                    row = 0
                grid.addWidget(widget, 0, column, rows, 1)
                column += 1
            else:
                grid.addWidget(widget, row, column)
                row += 1
                if row >= rows:
                    row = 0
                    column += 1
        outer.addLayout(grid, 1)

        label = QLabel(group.title, frame)
        label.setObjectName("RibbonGroupLabel")
        label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        outer.addWidget(label)
        return frame

    def _build_item(self, item: RibbonItem) -> QWidget | None:
        if item.widget:
            return self._build_embedded(item)

        button = _LargeToolButton(self) if item.large else QToolButton(self)
        button.setText(item.title)
        button.setIcon(_text_icon(GLYPHS.get(item.icon, "•"), size=24 if item.large else 18))
        button.setIconSize(QSize(24, 24) if item.large else QSize(16, 16))
        button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextUnderIcon
            if item.large
            else Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        button.setAutoRaise(True)
        button.setCheckable(item.checkable)
        button.setChecked(item.checked)
        tip = item.tooltip or item.title
        if item.shortcut:
            tip = f"{tip} ({item.shortcut})"
        button.setToolTip(tip)
        if item.large:
            # Wide enough for the caption; ToolButtonTextUnderIcon elides
            # otherwise (e.g. "Edit text" became "Ed...xt").
            #
            # The width cannot be frozen here: a theme is applied *after* the
            # ribbon is built and may set a larger font, which re-elided the
            # caption ("Edi...ext"). ``_LargeToolButton`` re-measures on every
            # font change instead.
            button.setMinimumHeight(58)
        else:
            button.setMinimumHeight(26)

        action = QAction(item.title, self)
        action.setCheckable(item.checkable)
        action.setChecked(item.checked)
        if item.shortcut:
            action.setShortcut(QKeySequence(item.shortcut))
        action.setToolTip(tip)
        self._actions[item.identifier] = action
        self._buttons[item.identifier] = button

        if item.group_id:
            group = self._groups.setdefault(item.group_id, QActionGroup(self))
            group.setExclusive(True)
            group.addAction(action)
            buttons = self._button_groups.get(item.group_id)
            if buttons is None:
                buttons = QButtonGroup(self)
                buttons.setExclusive(True)
                self._button_groups[item.group_id] = buttons
            buttons.addButton(button)

        if item.checkable:
            button.toggled.connect(
                lambda state, key=item.identifier: self._on_toggled(key, state)
            )
        else:
            button.clicked.connect(lambda _=False, key=item.identifier: self.command.emit(key))

        if item.menu:
            menu = QMenu(button)
            for entry in item.menu:
                sub = QAction(entry.title, menu)
                sub.triggered.connect(
                    lambda _=False, key=entry.identifier: self.command.emit(key)
                )
                menu.addAction(sub)
            button.setMenu(menu)
            button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        return button

    def _build_embedded(self, item: RibbonItem) -> QWidget:
        match item.widget:
            case "zoom":
                combo = QComboBox(self)
                combo.setEditable(True)
                combo.addItems(
                    ["10%", "25%", "50%", "75%", "100%", "125%", "150%", "200%", "400%", "800%"]
                )
                combo.setCurrentText("100%")
                combo.setFixedWidth(78)
                combo.setToolTip("Zoom level")
                combo.activated.connect(lambda _i: self._emit_zoom(combo.currentText()))
                combo.lineEdit().returnPressed.connect(
                    lambda: self._emit_zoom(combo.currentText())
                )
                self.zoom_combo = combo
                return combo
            case "page":
                spin = QSpinBox(self)
                spin.setRange(1, 1)
                spin.setFixedWidth(64)
                spin.setToolTip("Current page")
                spin.valueChanged.connect(lambda value: self.page_requested.emit(value - 1))
                self.page_spin = spin
                return spin
            case "font":
                combo = QComboBox(self)
                combo.setEditable(True)
                combo.addItems(
                    ["Helvetica", "Times-Roman", "Courier", "Arial", "Georgia", "Verdana"]
                )
                combo.setFixedWidth(120)
                self.font_combo = combo
                return combo
            case "font_size":
                combo = QComboBox(self)
                combo.setEditable(True)
                combo.addItems(
                    [str(s) for s in (6, 8, 9, 10, 11, 12, 14, 16, 18, 24, 32, 48, 72)]
                )
                combo.setCurrentText("11")
                combo.setFixedWidth(56)
                self.font_size_combo = combo
                return combo
        return QWidget(self)

    # -- interaction ------------------------------------------------------------ #
    def _emit_zoom(self, text: str) -> None:
        try:
            value = float(text.strip().rstrip("%")) / 100
        except ValueError:
            return
        self.zoom_requested.emit(max(0.05, min(40.0, value)))

    def _on_toggled(self, identifier: str, state: bool) -> None:
        action = self._actions.get(identifier)
        if action is not None:
            action.setChecked(state)
        self.toggled.emit(identifier, state)
        if state:
            self.command.emit(identifier)

    # -- API used by the main window --------------------------------------------- #
    def action(self, identifier: str) -> QAction | None:
        return self._actions.get(identifier)

    def actions_map(self) -> dict[str, QAction]:
        return dict(self._actions)

    def set_enabled(self, identifier: str, enabled: bool) -> None:
        if button := self._buttons.get(identifier):
            button.setEnabled(enabled)
        if action := self._actions.get(identifier):
            action.setEnabled(enabled)

    def set_checked(self, identifier: str, checked: bool) -> None:
        """Set a button's checked state without emitting its signals.

        Exclusive :class:`QButtonGroup` membership normally forbids clearing
        the last checked button and unchecks siblings on its own, so
        exclusivity is suspended for the update. Two buttons may legitimately
        map to the same tool (Home and Edit both offer "Move object") and both
        must be able to show as active.
        """
        button = self._buttons.get(identifier)
        if button is not None and button.isChecked() != checked:
            group = button.group()
            if group is not None:
                group.setExclusive(False)
            button.blockSignals(True)
            button.setChecked(checked)
            button.blockSignals(False)
            if group is not None:
                group.setExclusive(True)
        if action := self._actions.get(identifier):
            action.setChecked(checked)

    def set_zoom_display(self, zoom: float) -> None:
        if self.zoom_combo is not None:
            self.zoom_combo.blockSignals(True)
            self.zoom_combo.setCurrentText(f"{zoom * 100:.0f}%")
            self.zoom_combo.blockSignals(False)

    def set_page_display(self, page: int, total: int) -> None:
        if self.page_spin is not None:
            self.page_spin.blockSignals(True)
            self.page_spin.setRange(1, max(1, total))
            self.page_spin.setValue(page + 1)
            self.page_spin.blockSignals(False)

    def add_plugin_commands(self, commands: dict[str, Any]) -> None:
        """Append plugin-contributed commands to a dedicated ribbon tab."""
        if not commands:
            return
        index = self.tabs.count()
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i) == "Plugins":
                self.tabs.removeTab(i)
                index = i
                break
        group = RibbonGroup(
            "Plugin commands",
            [
                RibbonItem(identifier, spec.title, spec.icon or "plugin")
                for identifier, spec in commands.items()
            ],
        )
        self.tabs.insertTab(index, self._build_tab(RibbonTab("Plugins", [group])), "Plugins")


class ClassicToolBar(QToolBar):
    """Compact single-row toolbar for users who dislike ribbons."""

    command = Signal(str)

    #: Commands shown, in order (``""`` inserts a separator).
    LAYOUT: tuple[str, ...] = (
        "file.open",
        "file.save",
        "file.print",
        "",
        "edit.undo",
        "edit.redo",
        "",
        "tool.select",
        "tool.pan",
        "tool.text_select",
        "",
        "view.zoom_out",
        "view.zoom_in",
        "view.fit_page",
        "view.fit_width",
        "",
        "view.rotate_left",
        "view.rotate_right",
        "",
        "annot.highlight",
        "annot.note",
        "annot.ink",
        "",
        "edit.find",
        "ocr.run",
        "ai.summarize",
        "secure.encrypt",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("Main toolbar", parent)
        self.setMovable(True)
        self.setIconSize(QSize(18, 18))
        titles = {
            item.identifier: item
            for tab in build_default_tabs()
            for group in tab.groups
            for item in group.items
        }
        for identifier in self.LAYOUT:
            if not identifier:
                self.addSeparator()
                continue
            spec = titles.get(identifier)
            if spec is None:
                continue
            action = QAction(_text_icon(GLYPHS.get(spec.icon, "•"), size=18), spec.title, self)
            action.setToolTip(spec.tooltip or spec.title)
            if spec.shortcut:
                action.setShortcut(QKeySequence(spec.shortcut))
            action.setCheckable(spec.checkable)
            action.triggered.connect(
                lambda _checked=False, key=identifier: self.command.emit(key)
            )
            self.addAction(action)
