"""Sidebar panels: bookmarks, comments, attachments, layers, search, properties.

Each panel is a self-contained ``QWidget`` that takes a
:class:`~pdfstudio.pdfengine.document.PdfDocument`, renders its model and emits
high-level signals; the main window connects those to controller methods.  No
panel mutates the document directly, which keeps undo handling in one place.
"""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QAction, QBrush, QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.annotations import AnnotationService
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import (
    Annotation,
    Bookmark,
    Point,
    SearchHit,
    SearchQuery,
)

log = get_logger("ui.panels")


# --------------------------------------------------------------------------- #
# Bookmarks
# --------------------------------------------------------------------------- #
class BookmarkPanel(QWidget):
    """Outline tree with add/rename/delete and auto-generation."""

    navigate = Signal(int, object)  # page, Point | None
    bookmarks_changed = Signal(list)
    generate_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._document: PdfDocument | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        toolbar = QHBoxLayout()
        self._add_button = QToolButton(self)
        self._add_button.setText("+")
        self._add_button.setToolTip("Add bookmark at the current page")
        self._delete_button = QToolButton(self)
        self._delete_button.setText("−")
        self._delete_button.setToolTip("Delete the selected bookmark")
        self._generate_button = QToolButton(self)
        self._generate_button.setText("Auto")
        self._generate_button.setToolTip("Generate bookmarks from headings")
        self._filter = QLineEdit(self)
        self._filter.setPlaceholderText("Filter bookmarks…")
        self._filter.setClearButtonEnabled(True)
        for widget in (self._add_button, self._delete_button, self._generate_button):
            toolbar.addWidget(widget)
        toolbar.addWidget(self._filter, 1)
        layout.addLayout(toolbar)

        self.tree = QTreeWidget(self)
        self.tree.setHeaderHidden(True)
        self.tree.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.tree, 1)

        self.tree.itemClicked.connect(self._on_clicked)
        self.tree.itemChanged.connect(lambda *_: self._emit_changed())
        self.tree.customContextMenuRequested.connect(self._menu)
        self._filter.textChanged.connect(self._apply_filter)
        self._generate_button.clicked.connect(self.generate_requested)
        self._add_button.clicked.connect(self._add_current)
        self._delete_button.clicked.connect(self._delete_selected)

    def set_document(self, document: PdfDocument | None) -> None:
        self._document = document
        self.reload()

    def reload(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        if self._document is not None:
            for bookmark in self._document.bookmarks():
                self.tree.addTopLevelItem(self._make_item(bookmark))
            self.tree.expandAll()
        self.tree.blockSignals(False)

    def _make_item(self, bookmark: Bookmark) -> QTreeWidgetItem:
        item = QTreeWidgetItem([bookmark.title])
        item.setData(0, Qt.ItemDataRole.UserRole, bookmark)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        item.setToolTip(0, f"{bookmark.title} — page {bookmark.page + 1}")
        if bookmark.bold or bookmark.italic:
            font = item.font(0)
            font.setBold(bookmark.bold)
            font.setItalic(bookmark.italic)
            item.setFont(0, font)
        if bookmark.color:
            item.setForeground(0, QBrush(QColor(bookmark.color.to_hex())))
        for child in bookmark.children:
            item.addChild(self._make_item(child))
        return item

    def current_page_hint(self) -> int:
        return 0

    def _on_clicked(self, item: QTreeWidgetItem, _column: int) -> None:
        bookmark: Bookmark = item.data(0, Qt.ItemDataRole.UserRole)
        if bookmark is not None:
            self.navigate.emit(bookmark.page, Point(0, bookmark.y))

    def _add_current(self) -> None:
        if self._document is None:
            return
        page = self.current_page_hint()
        item = QTreeWidgetItem([f"Page {page + 1}"])
        item.setData(0, Qt.ItemDataRole.UserRole, Bookmark(f"Page {page + 1}", page))
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
        self.tree.addTopLevelItem(item)
        self.tree.editItem(item)
        self._emit_changed()

    def _delete_selected(self) -> None:
        for item in self.tree.selectedItems():
            parent = item.parent()
            if parent:
                parent.removeChild(item)
            else:
                self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))
        self._emit_changed()

    def _menu(self, position: QPoint) -> None:
        menu = QMenu(self)
        menu.addAction("Rename", lambda: self.tree.editItem(self.tree.currentItem()))
        menu.addAction("Delete", self._delete_selected)
        menu.addSeparator()
        menu.addAction("Expand all", self.tree.expandAll)
        menu.addAction("Collapse all", self.tree.collapseAll)
        menu.addSeparator()
        menu.addAction("Generate from headings", self.generate_requested.emit)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _apply_filter(self, text: str) -> None:
        needle = text.lower().strip()

        def walk(item: QTreeWidgetItem) -> bool:
            visible = needle in item.text(0).lower() if needle else True
            for i in range(item.childCount()):
                visible = walk(item.child(i)) or visible
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            walk(self.tree.topLevelItem(i))

    def collect(self) -> list[Bookmark]:
        """Rebuild the bookmark tree from the widget (after edits/reorder)."""

        def walk(item: QTreeWidgetItem, level: int) -> Bookmark:
            original: Bookmark = item.data(0, Qt.ItemDataRole.UserRole) or Bookmark("", 0)
            bookmark = Bookmark(
                title=item.text(0),
                page=original.page,
                level=level,
                y=original.y,
                children=[walk(item.child(i), level + 1) for i in range(item.childCount())],
            )
            return bookmark

        return [
            walk(self.tree.topLevelItem(i), 1) for i in range(self.tree.topLevelItemCount())
        ]

    def _emit_changed(self) -> None:
        self.bookmarks_changed.emit(self.collect())


# --------------------------------------------------------------------------- #
# Comments
# --------------------------------------------------------------------------- #
class CommentsPanel(QWidget):
    """Threaded comment list with filtering, replies and review states."""

    navigate = Signal(int, object)
    comment_action = Signal(str, object)  # action, annotation

    ALL_AUTHORS = "All authors"
    ALL_TYPES = "All types"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._document: PdfDocument | None = None
        self._annotations: list[Annotation] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        filters = QHBoxLayout()
        self._search = QLineEdit(self)
        self._search.setPlaceholderText("Search comments…")
        self._search.setClearButtonEnabled(True)
        self._author_filter = QComboBox(self)
        self._author_filter.setObjectName("AuthorFilter")
        self._author_filter.addItem(self.ALL_AUTHORS)
        self._type_filter = QComboBox(self)
        self._type_filter.setObjectName("TypeFilter")
        self._type_filter.addItem(self.ALL_TYPES)
        filters.addWidget(self._search, 1)
        layout.addLayout(filters)
        second = QHBoxLayout()
        second.addWidget(self._author_filter, 1)
        second.addWidget(self._type_filter, 1)
        layout.addLayout(second)

        self.list = QListWidget(self)
        self.list.setWordWrap(True)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.setSpacing(2)
        layout.addWidget(self.list, 1)

        self._summary = QLabel("No comments", self)
        self._summary.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._summary)

        self.list.itemClicked.connect(self._on_clicked)
        self.list.customContextMenuRequested.connect(self._menu)
        self._search.textChanged.connect(self.reload)
        self._author_filter.currentIndexChanged.connect(self.reload)
        self._type_filter.currentIndexChanged.connect(self.reload)

    def set_document(self, document: PdfDocument | None) -> None:
        self._document = document
        self.reload()

    def reload(self) -> None:
        self.list.clear()
        if self._document is None:
            self._summary.setText("No comments")
            return
        service = AnnotationService(self._document)
        self._annotations = service.all()

        self._refresh_combo(
            self._author_filter,
            sorted({a.author for a in self._annotations if a.author}),
            self.ALL_AUTHORS,
        )
        self._refresh_combo(
            self._type_filter,
            sorted({a.type.value for a in self._annotations}),
            self.ALL_TYPES,
        )

        needle = self._search.text().lower().strip()
        author = self._author_filter.currentText() or self.ALL_AUTHORS
        kind = self._type_filter.currentText() or self.ALL_TYPES
        shown = 0
        for annotation in self._annotations:
            if needle and needle not in (annotation.contents + annotation.author).lower():
                continue
            if author not in (self.ALL_AUTHORS, annotation.author):
                continue
            if kind not in (self.ALL_TYPES, annotation.type.value):
                continue
            item = QListWidgetItem(self._format(annotation))
            item.setData(Qt.ItemDataRole.UserRole, annotation)
            if annotation.color:
                item.setForeground(QBrush(QColor(annotation.color.to_hex())))
            self.list.addItem(item)
            shown += 1
        self._summary.setText(
            f"{shown} of {len(self._annotations)} comment(s)"
            if self._annotations
            else "No comments"
        )

    @staticmethod
    def _refresh_combo(combo: QComboBox, values: Sequence[str], all_label: str) -> None:
        """Repopulate a filter combo, keeping the current choice if still valid."""
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItem(all_label)
        combo.addItems(values)
        index = combo.findText(current)
        combo.setCurrentIndex(max(0, index))
        combo.blockSignals(False)

    @staticmethod
    def _format(annotation: Annotation) -> str:
        contents = (annotation.contents or "").strip().replace("\n", " ")
        if len(contents) > 110:
            contents = contents[:107] + "…"
        author = annotation.author or "Unknown"
        return (
            f"[p{annotation.page + 1}] {annotation.type.value} — {author}\n"
            f"{contents or '(no text)'}"
        )

    def _on_clicked(self, item: QListWidgetItem) -> None:
        annotation: Annotation = item.data(Qt.ItemDataRole.UserRole)
        self.navigate.emit(annotation.page, Point(annotation.rect.x0, annotation.rect.y0))

    def _menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None:
            return
        annotation: Annotation = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        for key, title in (
            ("goto", "Go to comment"),
            ("reply", "Reply…"),
            ("edit", "Edit text…"),
            ("resolve", "Mark as resolved"),
            ("unresolve", "Reopen"),
            ("", ""),
            ("copy", "Copy text"),
            ("delete", "Delete comment"),
        ):
            if not key:
                menu.addSeparator()
                continue
            action = QAction(title, menu)
            action.triggered.connect(
                lambda _c=False, k=key, a=annotation: self.comment_action.emit(k, a)
            )
            menu.addAction(action)
        menu.exec(self.list.viewport().mapToGlobal(position))


# --------------------------------------------------------------------------- #
# Attachments
# --------------------------------------------------------------------------- #
class AttachmentsPanel(QWidget):
    """Embedded files: list, add, extract, delete."""

    attachment_action = Signal(str, str)  # action, name

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._document: PdfDocument | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        buttons = QHBoxLayout()
        for key, title in (("add", "Add…"), ("extract", "Extract…"), ("delete", "Delete")):
            button = QPushButton(title, self)
            button.clicked.connect(
                lambda _c=False, k=key: self.attachment_action.emit(k, self.current_name())
            )
            buttons.addWidget(button)
        layout.addLayout(buttons)

        self.table = QTableWidget(0, 3, self)
        self.table.setHorizontalHeaderLabels(["Name", "Size", "Description"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        layout.addWidget(self.table, 1)

    def set_document(self, document: PdfDocument | None) -> None:
        self._document = document
        self.reload()

    def reload(self) -> None:
        self.table.setRowCount(0)
        if self._document is None:
            return
        for attachment in self._document.attachments():
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(attachment.name))
            self.table.setItem(row, 1, QTableWidgetItem(_human_size(attachment.size)))
            self.table.setItem(row, 2, QTableWidgetItem(attachment.description))

    def current_name(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.text() if item else ""


# --------------------------------------------------------------------------- #
# Layers
# --------------------------------------------------------------------------- #
class LayersPanel(QWidget):
    """Optional content groups with visibility toggles."""

    layer_toggled = Signal(int, bool)
    layer_action = Signal(str, int)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._document: PdfDocument | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.list = QListWidget(self)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        layout.addWidget(self.list, 1)
        self._empty = QLabel("This document has no layers.", self)
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color: palette(mid);")
        layout.addWidget(self._empty)

        self.list.itemChanged.connect(self._on_item_changed)
        self.list.customContextMenuRequested.connect(self._menu)

    def set_document(self, document: PdfDocument | None) -> None:
        self._document = document
        self.reload()

    def reload(self) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        layers = self._document.layers() if self._document else []
        for layer in layers:
            item = QListWidgetItem(layer.name)
            item.setData(Qt.ItemDataRole.UserRole, layer.xref)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(
                Qt.CheckState.Checked if layer.visible else Qt.CheckState.Unchecked
            )
            self.list.addItem(item)
        self.list.blockSignals(False)
        self._empty.setVisible(not layers)
        self.list.setVisible(bool(layers))

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        self.layer_toggled.emit(
            item.data(Qt.ItemDataRole.UserRole),
            item.checkState() == Qt.CheckState.Checked,
        )

    def _menu(self, position: QPoint) -> None:
        item = self.list.itemAt(position)
        if item is None:
            return
        xref = item.data(Qt.ItemDataRole.UserRole)
        menu = QMenu(self)
        for key, title in (
            ("rename", "Rename…"),
            ("merge", "Merge into…"),
            ("delete", "Delete layer"),
        ):
            action = QAction(title, menu)
            action.triggered.connect(
                lambda _c=False, k=key, x=xref: self.layer_action.emit(k, x)
            )
            menu.addAction(action)
        menu.exec(self.list.viewport().mapToGlobal(position))


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
class SearchPanel(QWidget):
    """Advanced search with regex, boolean and scope options."""

    search_requested = Signal(object)  # SearchQuery
    hit_selected = Signal(int)  # index into the last result list
    highlight_all_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._hits: list[SearchHit] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)

        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Search (Enter to run)…")
        self.input.setClearButtonEnabled(True)
        layout.addWidget(self.input)

        options = QVBoxLayout()
        options.setSpacing(2)
        self.case_box = QCheckBox("Match case", self)
        self.word_box = QCheckBox("Whole words", self)
        self.regex_box = QCheckBox("Regular expression", self)
        self.boolean_box = QCheckBox("Boolean (AND / OR / NOT)", self)
        self.annotations_box = QCheckBox("Include comments", self)
        self.bookmarks_box = QCheckBox("Include bookmarks", self)
        self.forms_box = QCheckBox("Include form fields", self)
        for box in (
            self.case_box,
            self.word_box,
            self.regex_box,
            self.boolean_box,
            self.annotations_box,
            self.bookmarks_box,
            self.forms_box,
        ):
            options.addWidget(box)
        layout.addLayout(options)

        buttons = QHBoxLayout()
        self.search_button = QPushButton("Search", self)
        self.search_button.setProperty("primary", True)
        self.highlight_button = QPushButton("Highlight all", self)
        buttons.addWidget(self.search_button)
        buttons.addWidget(self.highlight_button)
        layout.addLayout(buttons)

        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)

        self.results = QListWidget(self)
        self.results.setWordWrap(True)
        layout.addWidget(self.results, 1)

        self.status = QLabel("", self)
        self.status.setStyleSheet("color: palette(mid);")
        layout.addWidget(self.status)

        self.input.returnPressed.connect(self._emit_search)
        self.search_button.clicked.connect(self._emit_search)
        self.highlight_button.clicked.connect(self.highlight_all_requested)
        self.results.currentRowChanged.connect(self._on_row)
        self.regex_box.toggled.connect(lambda checked: self.boolean_box.setEnabled(not checked))
        self.boolean_box.toggled.connect(lambda checked: self.regex_box.setEnabled(not checked))

    def build_query(self) -> SearchQuery:
        return SearchQuery(
            text=self.input.text(),
            case_sensitive=self.case_box.isChecked(),
            whole_words=self.word_box.isChecked(),
            regex=self.regex_box.isChecked(),
            boolean=self.boolean_box.isChecked(),
            include_annotations=self.annotations_box.isChecked(),
            include_bookmarks=self.bookmarks_box.isChecked(),
            include_forms=self.forms_box.isChecked(),
        )

    def _emit_search(self) -> None:
        if self.input.text().strip():
            self.search_requested.emit(self.build_query())

    def set_results(self, hits: Sequence[SearchHit], *, elapsed: float = 0.0) -> None:
        self._hits = list(hits)
        self.results.clear()
        for hit in self._hits:
            source = "" if hit.source == "text" else f" ({hit.source})"
            item = QListWidgetItem(f"p{hit.page + 1}{source}: {hit.context or hit.text}")
            item.setToolTip(hit.context or hit.text)
            self.results.addItem(item)
        suffix = f" in {elapsed * 1000:.0f} ms" if elapsed else ""
        self.status.setText(f"{len(self._hits)} result(s){suffix}")
        self.progress.setVisible(False)

    def show_progress(self, value: int = -1) -> None:
        self.progress.setVisible(True)
        self.progress.setRange(0, 0 if value < 0 else 100)
        if value >= 0:
            self.progress.setValue(value)

    def focus_input(self) -> None:
        self.input.setFocus()
        self.input.selectAll()

    def _on_row(self, row: int) -> None:
        if 0 <= row < len(self._hits):
            self.hit_selected.emit(row)


# --------------------------------------------------------------------------- #
# Properties
# --------------------------------------------------------------------------- #
class PropertiesPanel(QWidget):
    """Live document/page/selection properties."""

    metadata_edit_requested = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._document: PdfDocument | None = None

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        outer.addWidget(scroll, 1)

        content = QWidget(scroll)
        self._form = QFormLayout(content)
        self._form.setContentsMargins(10, 10, 10, 10)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        self._form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        scroll.setWidget(content)

        edit = QPushButton("Edit metadata…", self)
        edit.clicked.connect(self.metadata_edit_requested)
        outer.addWidget(edit)

    def set_document(self, document: PdfDocument | None) -> None:
        self._document = document
        self.reload()

    def reload(self, page: int = 0) -> None:
        while self._form.rowCount():
            self._form.removeRow(0)
        if self._document is None:
            self._form.addRow(QLabel("No document open"))
            return

        stats = self._document.statistics()
        meta = self._document.metadata()
        info = self._document.page_info(min(page, self._document.page_count - 1))

        def section(title: str) -> None:
            label = QLabel(title)
            font = QFont(label.font())
            font.setBold(True)
            label.setFont(font)
            self._form.addRow(label)

        section("Document")
        for key, value in (
            ("File", stats["name"]),
            ("Pages", str(stats["pages"])),
            ("Size", _human_size(stats["size_bytes"])),
            ("Version", stats["version"]),
            ("Encrypted", "Yes" if stats["encrypted"] else "No"),
            ("Tagged", "Yes" if stats["tagged"] else "No"),
            ("Forms", "Yes" if stats["form"] else "No"),
            ("JavaScript", "Yes" if stats["javascript"] else "No"),
        ):
            self._form.addRow(f"{key}:", QLabel(str(value)))

        section("Metadata")
        for key, value in (
            ("Title", meta.title),
            ("Author", meta.author),
            ("Subject", meta.subject),
            ("Keywords", meta.keywords),
            ("Creator", meta.creator),
            ("Producer", meta.producer),
            ("Created", _pdf_date(meta.creation_date)),
            ("Modified", _pdf_date(meta.modification_date)),
        ):
            label = QLabel(value or "—")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self._form.addRow(f"{key}:", label)

        section(f"Page {info.index + 1}")
        for key, value in (
            ("Size", f"{info.width:.0f} × {info.height:.0f} pt"),
            ("Millimetres", f"{info.width * 25.4 / 72:.0f} × {info.height * 25.4 / 72:.0f} mm"),
            ("Rotation", f"{info.rotation}°"),
            ("Label", info.display_label),
            ("Comments", str(info.annotation_count)),
            ("Images", str(info.image_count)),
            ("Has text", "Yes" if info.has_text else "No (scan?)"),
        ):
            self._form.addRow(f"{key}:", QLabel(value))

        section("Security")
        for key, allowed in stats["permissions"].items():
            self._form.addRow(
                f"{key.replace('_', ' ').title()}:",
                QLabel("Allowed" if allowed else "Not allowed"),
            )


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _pdf_date(value: str) -> str:
    """Format a ``D:YYYYMMDDHHmmSS`` PDF date for display."""
    if not value or not value.startswith("D:"):
        return value or "—"
    raw = value[2:]
    try:
        return (
            f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}"
            if len(raw) >= 12
            else raw
        )
    except Exception:
        return value
