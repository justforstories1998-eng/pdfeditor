"""Page thumbnail panel with drag-and-drop reordering and page operations.

Thumbnails are rendered lazily in the background as items scroll into view and
are cached by the shared :class:`~pdfstudio.render.renderer.PageRenderer`, so
opening a 10 000 page document costs nothing until the user scrolls.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.render.renderer import PageRenderer, RenderedPage, RenderRequest

log = get_logger("ui.thumbnails")


class ThumbnailPanel(QWidget):
    """List of page thumbnails supporting selection, reorder and context menu."""

    page_selected = Signal(int)
    pages_reordered = Signal(list, int)  # moved indices, destination
    pages_action = Signal(str, list)  # action name, page indices

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SidePanel")
        self._document: PdfDocument | None = None
        self._renderer: PageRenderer | None = None
        self._size = 140
        self._pending: set[int] = set()
        self._suppress_signal = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self.list = QListWidget(self)
        self.list.setObjectName("ThumbnailList")
        self.list.setViewMode(QListWidget.ViewMode.IconMode)
        self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.list.setMovement(QListWidget.Movement.Static)
        self.list.setSpacing(8)
        self.list.setUniformItemSizes(True)
        # Without an explicit icon size Qt paints icons at 32px regardless of
        # the pixmap resolution, which made every thumbnail a tiny stamp.
        self.list.setIconSize(QSize(self._size, int(self._size * 1.42)))
        self.list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.setWordWrap(True)
        layout.addWidget(self.list, 1)

        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(8, 4, 8, 6)
        self._count_label = QLabel("No pages", footer)
        self._slider = QSlider(Qt.Orientation.Horizontal, footer)
        self._slider.setRange(70, 320)
        self._slider.setValue(self._size)
        self._slider.setFixedWidth(90)
        self._slider.setToolTip("Thumbnail size")
        footer_layout.addWidget(self._count_label, 1)
        footer_layout.addWidget(self._slider)
        layout.addWidget(footer)

        self.list.itemSelectionChanged.connect(self._on_selection)
        self.list.customContextMenuRequested.connect(self._show_menu)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        self.list.verticalScrollBar().valueChanged.connect(self._schedule_render)
        self._slider.valueChanged.connect(self._on_size_changed)

        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.setInterval(90)
        self._render_timer.timeout.connect(self._render_visible)

    # -- document ------------------------------------------------------------- #
    def set_document(
        self, document: PdfDocument | None, renderer: PageRenderer | None = None
    ) -> None:
        self._document = document
        self._renderer = renderer
        self.rebuild()

    def rebuild(self) -> None:
        """Recreate every item (after open, insert, delete or reorder)."""
        self._suppress_signal = True
        self.list.clear()
        self._pending.clear()
        if self._document is None:
            self._count_label.setText("No pages")
            self._suppress_signal = False
            return
        for index in range(self._document.page_count):
            item = QListWidgetItem(self._document.page_label(index))
            item.setData(Qt.ItemDataRole.UserRole, index)
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            item.setSizeHint(QSize(self._size + 18, int(self._size * 1.45) + 26))
            item.setIcon(self._placeholder(index))
            self.list.addItem(item)
        self._count_label.setText(f"{self._document.page_count} pages")
        self._suppress_signal = False
        self._schedule_render()

    def refresh_page(self, index: int) -> None:
        """Re-render a single thumbnail after an edit."""
        self._pending.discard(index)
        if 0 <= index < self.list.count():
            self._render_one(index)

    def set_current_page(self, index: int) -> None:
        """Highlight the page shown in the main view without emitting signals."""
        if not 0 <= index < self.list.count():
            return
        self._suppress_signal = True
        self.list.setCurrentRow(index)
        self.list.scrollToItem(
            self.list.item(index), QAbstractItemView.ScrollHint.EnsureVisible
        )
        self._suppress_signal = False

    def selected_pages(self) -> list[int]:
        return sorted(item.data(Qt.ItemDataRole.UserRole) for item in self.list.selectedItems())

    # -- rendering -------------------------------------------------------------- #
    def _placeholder(self, index: int) -> QIcon:
        width, height = (595.0, 842.0)
        if self._document is not None:
            width, height = self._document.page_size(index)
        scale = self._size / max(width, height)
        pixmap = QPixmap(max(1, int(width * scale)), max(1, int(height * scale)))
        pixmap.fill(QColor("#ffffff"))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#cfd3da"))
        painter.drawRect(0, 0, pixmap.width() - 1, pixmap.height() - 1)
        painter.end()
        return QIcon(pixmap)

    def _schedule_render(self) -> None:
        self._render_timer.start()

    def _render_visible(self) -> None:
        if self._document is None or self._renderer is None:
            return
        viewport = self.list.viewport().rect().adjusted(0, -260, 0, 260)
        for row in range(self.list.count()):
            item = self.list.item(row)
            rect = self.list.visualItemRect(item)
            if rect.intersects(viewport):
                self._render_one(row)

    def _render_one(self, index: int) -> None:
        if self._renderer is None or index in self._pending:
            return
        self._pending.add(index)
        width, height = self._document.page_size(index) if self._document else (595, 842)
        zoom = self._size / max(width, height)
        request = RenderRequest(
            document_id=self._document.id if self._document else "",
            page=index,
            zoom=zoom,
            generation=self._renderer.generation,
        )

        def done(result: RenderedPage, row: int = index) -> None:
            self._pending.discard(row)
            item = self.list.item(row)
            if item is None:
                return
            image = _to_qimage(result)
            if image is not None:
                item.setIcon(QIcon(QPixmap.fromImage(image)))

        outcome = self._renderer.request(
            request, done, error_callback=lambda exc, row=index: self._pending.discard(row)
        )
        if isinstance(outcome, RenderedPage):
            self._pending.discard(index)

    def _on_size_changed(self, value: int) -> None:
        self._size = value
        self.list.setIconSize(QSize(value, int(value * 1.42)))
        for row in range(self.list.count()):
            self.list.item(row).setSizeHint(QSize(value + 18, int(value * 1.45) + 26))
        self._pending.clear()
        self._schedule_render()

    # -- interaction -------------------------------------------------------------- #
    def _on_selection(self) -> None:
        if self._suppress_signal:
            return
        pages = self.selected_pages()
        if pages:
            self.page_selected.emit(pages[0])

    def _on_rows_moved(self, _parent: Any, start: int, end: int, _dest: Any, row: int) -> None:
        if self._suppress_signal:
            return
        moved = list(range(start, end + 1))
        self.pages_reordered.emit(moved, row)

    def _show_menu(self, position: QPoint) -> None:
        pages = self.selected_pages()
        if not pages:
            return
        menu = QMenu(self)
        label = f"{len(pages)} page(s)" if len(pages) > 1 else f"page {pages[0] + 1}"

        actions: list[tuple[str, str]] = [
            ("rotate-left", f"Rotate {label} left"),
            ("rotate-right", f"Rotate {label} right"),
            ("", ""),
            ("insert-before", "Insert blank page before"),
            ("insert-after", "Insert blank page after"),
            ("duplicate", f"Duplicate {label}"),
            ("", ""),
            ("extract", f"Extract {label}…"),
            ("export-image", f"Export {label} as image…"),
            ("", ""),
            ("crop", "Crop pages…"),
            ("resize", "Resize pages…"),
            ("", ""),
            ("delete", f"Delete {label}"),
        ]
        for key, title in actions:
            if not key:
                menu.addSeparator()
                continue
            action = QAction(title, menu)
            action.triggered.connect(
                lambda _checked=False, k=key: self.pages_action.emit(k, pages)
            )
            menu.addAction(action)
        menu.exec(self.list.viewport().mapToGlobal(position))


def _to_qimage(result: RenderedPage) -> QImage | None:
    fmt = {
        1: QImage.Format.Format_Grayscale8,
        3: QImage.Format.Format_RGB888,
        4: QImage.Format.Format_RGBA8888,
    }.get(result.channels)
    if fmt is None or result.width <= 0:
        return None
    return QImage(result.samples, result.width, result.height, result.stride, fmt).copy()
