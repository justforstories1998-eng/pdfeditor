"""The page canvas: scrolling, zooming, selection and annotation tools.

Implemented on ``QAbstractScrollArea`` rather than ``QGraphicsView`` so the
scroll model can be virtualised: only the pages intersecting the viewport are
laid out and rasterised, which is what makes a 100 000-page document scroll
smoothly.  Renders arrive asynchronously from
:class:`~pdfstudio.render.renderer.PageRenderer`; until a page is ready a
placeholder with the correct aspect ratio is drawn, so layout never jumps.

Supported interactions
----------------------
* smooth wheel/trackpad scrolling with kinetic easing,
* ``Ctrl`` + wheel and pinch-to-zoom about the cursor,
* single / continuous / facing / book layouts, presentation mode,
* text selection with word and line snapping, copy to clipboard,
* marquee zoom, pan (space or middle mouse), rotation,
* annotation tools (highlight, ink, shapes, notes, redaction),
* search-hit highlighting and animated "scroll to hit".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum, auto
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QGuiApplication,
    QImage,
    QKeyEvent,
    QMouseEvent,
    QNativeGestureEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
    QResizeEvent,
    QWheelEvent,
)
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QRubberBand,
    QScrollBar,
    QSizePolicy,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.settings import settings
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import (
    AnnotationType,
    PageLayout,
    Point,
    Rect,
    SearchHit,
    ZoomMode,
)
from pdfstudio.render.renderer import PageRenderer, RenderedPage, RenderRequest

log = get_logger("pageview")

ZOOM_PRESETS: tuple[float, ...] = (
    0.10,
    0.25,
    0.33,
    0.50,
    0.67,
    0.75,
    1.00,
    1.25,
    1.50,
    2.00,
    3.00,
    4.00,
    6.00,
    8.00,
    16.00,
)


class Tool(StrEnum):
    """Active canvas tool."""

    SELECT = auto()
    PAN = auto()
    TEXT_SELECT = auto()
    ZOOM = auto()
    HIGHLIGHT = auto()
    UNDERLINE = auto()
    STRIKEOUT = auto()
    INK = auto()
    RECTANGLE = auto()
    ELLIPSE = auto()
    ARROW = auto()
    LINE = auto()
    NOTE = auto()
    TEXT_BOX = auto()
    STAMP = auto()
    REDACT = auto()
    MEASURE = auto()
    CROP = auto()
    EDIT_TEXT = auto()
    MOVE_OBJECT = auto()


@dataclass(slots=True)
class PageGeometry:
    """Where a page sits in the virtual canvas (unscaled document points)."""

    index: int
    x: float
    y: float
    width: float
    height: float

    @property
    def rect(self) -> QRectF:
        return QRectF(self.x, self.y, self.width, self.height)


class PageView(QAbstractScrollArea):
    """Scrollable, zoomable, interactive page canvas."""

    # -- signals ------------------------------------------------------------- #
    page_changed = Signal(int)
    zoom_changed = Signal(float)
    selection_changed = Signal(str)
    annotation_requested = Signal(object)  # dict describing the new annotation
    text_edit_requested = Signal(int, object, object, bool)
    """page, Rect|None, click Point, whole_paragraph.

    ``Rect`` is ``None`` for a click on empty space (insert new text).
    """
    context_menu_requested = Signal(QPoint, int, object)
    object_moved = Signal(object, float, float)  # PageObject, dx, dy
    object_selected = Signal(object)  # PageObject | None
    object_delete_requested = Signal(object)  # PageObject
    #: "copy" | "cut" | "paste" | "duplicate", target PageObject or None.
    object_clipboard_requested = Signal(str, object)
    #: A ribbon command identifier the canvas wants run (keyboard shortcuts).
    command_requested = Signal(str)
    link_activated = Signal(object)
    status_message = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("PageCanvas")
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, True)
        self.grabGesture(Qt.GestureType.PinchGesture)

        cfg = settings().data.viewer
        self._document: PdfDocument | None = None
        self._renderer: PageRenderer | None = None
        self._geometry: list[PageGeometry] = []
        self._images: dict[int, QImage] = {}
        self._pending: set[int] = set()
        self._zoom: float = cfg.zoom
        self._zoom_mode = ZoomMode(cfg.zoom_mode)
        self._layout = PageLayout(cfg.layout)
        self._rotation = 0
        self._gap = cfg.page_gap
        self._margin = 18
        self._current_page = 0
        self._tool = Tool.SELECT
        self._presentation = False
        self._invert = cfg.invert_colors
        self._show_annotations = cfg.show_annotations

        # interaction state
        self._panning = False
        self._pan_origin = QPoint()
        self._scroll_origin = (0, 0)
        self._drag_start: QPointF | None = None
        self._drag_current: QPointF | None = None
        self._drag_page = -1
        self._ink_strokes: list[list[Point]] = []
        self._selection_rects: list[tuple[int, Rect]] = []
        self._selected_text = ""
        self._search_hits: list[SearchHit] = []
        self._active_hit = -1
        self._inline_editor: Any = None
        self._inline_page: int | None = None
        #: Page and point of the last text click, so ribbon paragraph
        #: commands have somewhere to act without a fresh click.
        self._last_text_point: tuple[int, Point] | None = None
        #: (page, rect, mask_existing_text) for the open inline editor.
        self._edit_mask: tuple[int, Rect, bool] | None = None
        self._hover_line: tuple[int, Rect] | None = None
        self._selected_object: Any = None
        self._hover_object: Any = None
        self._object_drag_origin: Point | None = None
        self._object_drag_delta: tuple[float, float] = (0.0, 0.0)
        self._rubber = QRubberBand(QRubberBand.Shape.Rectangle, self.viewport())

        # smooth scrolling
        self._scroll_animation = QPropertyAnimation(self.verticalScrollBar(), b"value", self)
        self._scroll_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._scroll_animation.setDuration(220 if cfg.smooth_scroll else 0)
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setSingleShot(True)
        self._prefetch_timer.setInterval(140)
        self._prefetch_timer.timeout.connect(self._prefetch_visible)

        self.verticalScrollBar().valueChanged.connect(self._on_scrolled)
        self.horizontalScrollBar().valueChanged.connect(self._on_scrolled)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    # ------------------------------------------------------------------ #
    # Document
    # ------------------------------------------------------------------ #
    def set_document(
        self, document: PdfDocument | None, renderer: PageRenderer | None = None
    ) -> None:
        """Attach a document (or ``None`` to clear the view)."""
        self._document = document
        self._renderer = renderer or (PageRenderer(document) if document else None)
        self._images.clear()
        self._pending.clear()
        self._selection_rects.clear()
        self._search_hits.clear()
        self._current_page = 0
        if document is not None:
            self.relayout()
            self.verticalScrollBar().setValue(0)
        self.viewport().update()

    @property
    def document(self) -> PdfDocument | None:
        return self._document

    @property
    def renderer(self) -> PageRenderer | None:
        return self._renderer

    def refresh(self, page: int | None = None) -> None:
        """Discard cached images after an edit and repaint."""
        if self._renderer is not None:
            self._renderer.invalidate(page)
        if page is None:
            self._images.clear()
        else:
            self._images.pop(page, None)
        self._pending.discard(page if page is not None else -1)
        self.relayout()
        self.viewport().update()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def relayout(self) -> None:
        """Recompute page positions for the current layout and zoom."""
        if self._document is None:
            self._geometry = []
            self.verticalScrollBar().setRange(0, 0)
            return

        sizes = [self._page_size(i) for i in range(self._document.page_count)]
        geometry: list[PageGeometry] = []
        gap = self._gap
        y = float(self._margin)

        if self._layout in (PageLayout.SINGLE, PageLayout.CONTINUOUS):
            widest = max((w for w, _ in sizes), default=1.0)
            for index, (width, height) in enumerate(sizes):
                x = (widest - width) / 2
                geometry.append(PageGeometry(index, x, y, width, height))
                y += height + gap
            content_width = widest
        else:
            # Facing / book: two pages per row, book view leaves page 1 alone.
            offset = 1 if self._layout is PageLayout.BOOK else 0
            index = 0
            content_width = 0.0
            while index < len(sizes):
                if index == 0 and offset:
                    width, height = sizes[0]
                    geometry.append(PageGeometry(0, width + gap, y, width, height))
                    content_width = max(content_width, width * 2 + gap)
                    y += height + gap
                    index = 1
                    continue
                left = sizes[index]
                right = sizes[index + 1] if index + 1 < len(sizes) else None
                row_height = max(left[1], right[1] if right else 0)
                geometry.append(PageGeometry(index, 0.0, y, left[0], left[1]))
                if right:
                    geometry.append(
                        PageGeometry(index + 1, left[0] + gap, y, right[0], right[1])
                    )
                content_width = max(content_width, left[0] + gap + (right[0] if right else 0))
                y += row_height + gap
                index += 2

        self._geometry = geometry
        total_height = (y - gap + self._margin) * self._zoom
        total_width = content_width * self._zoom + self._margin * 2
        viewport = self.viewport().size()
        self.verticalScrollBar().setRange(0, max(0, int(total_height - viewport.height())))
        self.verticalScrollBar().setPageStep(viewport.height())
        self.verticalScrollBar().setSingleStep(48)
        self.horizontalScrollBar().setRange(0, max(0, int(total_width - viewport.width())))
        self.horizontalScrollBar().setPageStep(viewport.width())
        self.horizontalScrollBar().setSingleStep(48)

    def _page_size(self, index: int) -> tuple[float, float]:
        """Page size in points including the view rotation."""
        assert self._document is not None
        width, height = self._document.page_size(index)
        total = (self._document.page_rotation(index) + self._rotation) % 360
        return (height, width) if total in (90, 270) else (width, height)

    def _content_offset(self) -> QPointF:
        """Top-left of the content in viewport coordinates."""
        total_width = 0.0
        if self._geometry:
            total_width = max(g.x + g.width for g in self._geometry) * self._zoom
        extra = max(0.0, (self.viewport().width() - total_width) / 2)
        return QPointF(
            extra - self.horizontalScrollBar().value(),
            float(-self.verticalScrollBar().value()),
        )

    def page_rect_in_view(self, index: int) -> QRectF | None:
        """Viewport rectangle occupied by ``index`` (``None`` if not laid out)."""
        geo = next((g for g in self._geometry if g.index == index), None)
        if geo is None:
            return None
        offset = self._content_offset()
        return QRectF(
            offset.x() + geo.x * self._zoom,
            offset.y() + geo.y * self._zoom,
            geo.width * self._zoom,
            geo.height * self._zoom,
        )

    def visible_pages(self) -> list[int]:
        """Indices of pages intersecting the viewport (plus a small margin)."""
        viewport = QRectF(
            -240, -240, self.viewport().width() + 480, self.viewport().height() + 480
        )
        out: list[int] = []
        for geo in self._geometry:
            rect = self.page_rect_in_view(geo.index)
            if rect is not None and rect.intersects(viewport):
                out.append(geo.index)
        return out

    # ------------------------------------------------------------------ #
    # Zoom & navigation
    # ------------------------------------------------------------------ #
    @property
    def zoom(self) -> float:
        return self._zoom

    def set_zoom(self, value: float, *, anchor: QPointF | None = None) -> None:
        """Zoom about ``anchor`` (viewport coords) or the viewport centre."""
        cfg = settings().data.viewer
        value = max(cfg.min_zoom, min(cfg.max_zoom, value))
        if abs(value - self._zoom) < 1e-6:
            return
        focus = anchor or QPointF(self.viewport().width() / 2, self.viewport().height() / 2)
        before = self._viewport_to_document(focus)
        old = self._zoom
        self._zoom = value
        self._zoom_mode = ZoomMode.CUSTOM
        self.relayout()
        if before is not None:
            page, doc_point = before
            after = self.page_rect_in_view(page)
            if after is not None:
                target = QPointF(
                    after.x() + doc_point.x * self._zoom,
                    after.y() + doc_point.y * self._zoom,
                )
                delta = target - focus
                self.horizontalScrollBar().setValue(
                    int(self.horizontalScrollBar().value() + delta.x())
                )
                self.verticalScrollBar().setValue(
                    int(self.verticalScrollBar().value() + delta.y())
                )
        self._images.clear()  # re-render at the new scale
        self._sync_inline_editor()
        self.zoom_changed.emit(self._zoom)
        self.viewport().update()
        self._prefetch_timer.start()
        log.debug("Zoom {:.0f}% → {:.0f}%", old * 100, value * 100)

    def zoom_in(self) -> None:
        step = settings().data.viewer.zoom_step
        self.set_zoom(self._zoom * step)

    def zoom_out(self) -> None:
        step = settings().data.viewer.zoom_step
        self.set_zoom(self._zoom / step)

    def zoom_to_preset(self, value: float) -> None:
        self.set_zoom(value)

    def next_zoom_preset(self, *, forward: bool = True) -> None:
        presets = ZOOM_PRESETS if forward else tuple(reversed(ZOOM_PRESETS))
        for preset in presets:
            if (forward and preset > self._zoom * 1.001) or (
                not forward and preset < self._zoom * 0.999
            ):
                self.set_zoom(preset)
                return

    def set_zoom_mode(self, mode: ZoomMode) -> None:
        """Fit page / width / height, or actual size."""
        self._zoom_mode = mode
        if self._document is None or not self._geometry:
            return
        page = self._current_page
        width, height = self._page_size(page)
        viewport = self.viewport().size()
        available_w = viewport.width() - 2 * self._margin - 16
        available_h = viewport.height() - 2 * self._margin
        if self._layout in (PageLayout.FACING, PageLayout.BOOK):
            available_w = (available_w - self._gap) / 2
        match mode:
            case ZoomMode.FIT_WIDTH:
                value = available_w / width
            case ZoomMode.FIT_HEIGHT:
                value = available_h / height
            case ZoomMode.FIT_PAGE:
                value = min(available_w / width, available_h / height)
            case ZoomMode.ACTUAL:
                value = 1.0
            case _:
                return
        self._zoom = max(0.05, min(40.0, value))
        self._zoom_mode = mode
        self.relayout()
        self._images.clear()
        self.zoom_changed.emit(self._zoom)
        self.viewport().update()

    def set_layout(self, layout: PageLayout) -> None:
        self._layout = layout
        self.relayout()
        self.go_to_page(self._current_page, animate=False)
        self.viewport().update()

    def set_rotation(self, degrees: int) -> None:
        """Rotate the *view* (does not modify the document)."""
        self._rotation = degrees % 360
        self._images.clear()
        self.relayout()
        self.viewport().update()

    def rotate_view(self, delta: int = 90) -> None:
        self.set_rotation(self._rotation + delta)

    def set_invert(self, enabled: bool) -> None:
        """Night-reading mode: invert page colours."""
        self._invert = enabled
        self._images.clear()
        self.viewport().update()

    def set_show_annotations(self, enabled: bool) -> None:
        self._show_annotations = enabled
        self._images.clear()
        self.viewport().update()

    @property
    def current_page(self) -> int:
        return self._current_page

    def go_to_page(self, index: int, *, animate: bool = True, top: bool = True) -> None:
        """Scroll so ``index`` is visible."""
        if self._document is None:
            return
        index = max(0, min(index, self._document.page_count - 1))
        geo = next((g for g in self._geometry if g.index == index), None)
        if geo is None:
            return
        target = int(geo.y * self._zoom - (self._margin if top else 0))
        self._animate_scroll(target, animate)
        self._set_current_page(index)

    def go_to_point(self, index: int, point: Point, *, animate: bool = True) -> None:
        """Scroll to a specific location on a page (bookmarks, search hits)."""
        geo = next((g for g in self._geometry if g.index == index), None)
        if geo is None:
            return
        y = (geo.y + max(0.0, point.y - 80)) * self._zoom
        self._animate_scroll(int(y), animate)
        self._set_current_page(index)

    def _animate_scroll(self, value: int, animate: bool) -> None:
        bar = self.verticalScrollBar()
        value = max(bar.minimum(), min(bar.maximum(), value))
        if animate and settings().data.viewer.smooth_scroll and settings().data.ui.animations:
            self._scroll_animation.stop()
            self._scroll_animation.setStartValue(bar.value())
            self._scroll_animation.setEndValue(value)
            self._scroll_animation.start()
        else:
            bar.setValue(value)

    def next_page(self) -> None:
        step = 2 if self._layout in (PageLayout.FACING, PageLayout.BOOK) else 1
        self.go_to_page(self._current_page + step)

    def previous_page(self) -> None:
        step = 2 if self._layout in (PageLayout.FACING, PageLayout.BOOK) else 1
        self.go_to_page(self._current_page - step)

    def first_page(self) -> None:
        self.go_to_page(0)

    def last_page(self) -> None:
        if self._document:
            self.go_to_page(self._document.page_count - 1)

    def _set_current_page(self, index: int) -> None:
        if index != self._current_page:
            self._current_page = index
            self.page_changed.emit(index)

    # ------------------------------------------------------------------ #
    # Search integration
    # ------------------------------------------------------------------ #
    def set_search_hits(self, hits: Sequence[SearchHit]) -> None:
        self._search_hits = list(hits)
        self._active_hit = 0 if hits else -1
        self.viewport().update()

    def clear_search_hits(self) -> None:
        self._search_hits.clear()
        self._active_hit = -1
        self.viewport().update()

    def show_hit(self, index: int) -> None:
        """Scroll to and emphasise search hit ``index``."""
        if not self._search_hits:
            return
        self._active_hit = index % len(self._search_hits)
        hit = self._search_hits[self._active_hit]
        self.go_to_point(hit.page, Point(hit.rect.x0, hit.rect.y0))
        self.viewport().update()

    # ------------------------------------------------------------------ #
    # Tools & selection
    # ------------------------------------------------------------------ #
    def set_tool(self, tool: Tool) -> None:
        self._tool = tool
        cursors = {
            Tool.PAN: Qt.CursorShape.OpenHandCursor,
            Tool.TEXT_SELECT: Qt.CursorShape.IBeamCursor,
            Tool.ZOOM: Qt.CursorShape.CrossCursor,
            Tool.INK: Qt.CursorShape.CrossCursor,
            Tool.CROP: Qt.CursorShape.CrossCursor,
            Tool.REDACT: Qt.CursorShape.CrossCursor,
            Tool.NOTE: Qt.CursorShape.PointingHandCursor,
            Tool.MOVE_OBJECT: Qt.CursorShape.SizeAllCursor,
        }
        self.viewport().setCursor(cursors.get(tool, Qt.CursorShape.ArrowCursor))
        self._hover_line = None
        self._hover_object = None
        if tool is not Tool.MOVE_OBJECT:
            self.set_selected_object(None)
        self._clear_drag()

    @property
    def tool(self) -> Tool:
        return self._tool

    @property
    def selected_text(self) -> str:
        return self._selected_text

    def clear_selection(self) -> None:
        self._selection_rects.clear()
        self._selected_text = ""
        self.selection_changed.emit("")
        self.viewport().update()

    def select_all_on_page(self, index: int | None = None) -> None:
        if self._document is None:
            return
        page = self._current_page if index is None else index
        words = self._document.extract_words(page)
        self._selection_rects = [(page, rect) for rect, _ in words]
        self._selected_text = " ".join(word for _, word in words)
        self.selection_changed.emit(self._selected_text)
        self.viewport().update()

    def copy_selection(self) -> None:
        if self._selected_text:
            QGuiApplication.clipboard().setText(self._selected_text)
            self.status_message.emit(
                f"Copied {len(self._selected_text)} characters to the clipboard"
            )

    # ------------------------------------------------------------------ #
    # Coordinate conversion
    # ------------------------------------------------------------------ #
    def _viewport_to_document(self, position: QPointF) -> tuple[int, Point] | None:
        """Map a viewport point to ``(page index, page-space point)``."""
        for geo in self._geometry:
            rect = self.page_rect_in_view(geo.index)
            if rect is not None and rect.contains(position):
                return (
                    geo.index,
                    Point(
                        (position.x() - rect.x()) / self._zoom,
                        (position.y() - rect.y()) / self._zoom,
                    ),
                )
        return None

    def _nearest_page(self, position: QPointF) -> tuple[int, Point]:
        """Like :meth:`_viewport_to_document` but clamps to the closest page."""
        hit = self._viewport_to_document(position)
        if hit is not None:
            return hit
        best, best_distance = self._current_page, float("inf")
        for geo in self._geometry:
            rect = self.page_rect_in_view(geo.index)
            if rect is None:
                continue
            distance = (rect.center() - position).manhattanLength()
            if distance < best_distance:
                best, best_distance = geo.index, distance
        rect = self.page_rect_in_view(best)
        if rect is None:
            return best, Point(0, 0)
        return best, Point(
            max(0.0, (position.x() - rect.x()) / self._zoom),
            max(0.0, (position.y() - rect.y()) / self._zoom),
        )

    # ------------------------------------------------------------------ #
    # Painting
    # ------------------------------------------------------------------ #
    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self.viewport())
        painter.setRenderHints(
            QPainter.RenderHint.SmoothPixmapTransform | QPainter.RenderHint.Antialiasing
        )
        background = self.palette().color(self.backgroundRole())
        painter.fillRect(event.rect(), background)

        if self._document is None:
            self._paint_placeholder_message(painter)
            painter.end()
            return

        for index in self.visible_pages():
            rect = self.page_rect_in_view(index)
            if rect is None:
                continue
            self._paint_page(painter, index, rect)

        self._paint_hover(painter)
        self._paint_object_overlay(painter)
        self._paint_edit_mask(painter)
        self._paint_selection(painter)
        self._paint_search_hits(painter)
        self._paint_drag_feedback(painter)
        painter.end()

    def _paint_placeholder_message(self, painter: QPainter) -> None:
        painter.setPen(QPen(QColor("#8a8f98")))
        font = QFont(painter.font())
        font.setPointSize(13)
        painter.setFont(font)
        painter.drawText(
            self.viewport().rect(),
            Qt.AlignmentFlag.AlignCenter,
            "No document open\n\nOpen a PDF with Ctrl+O, or drag one here",
        )

    def _paint_page(self, painter: QPainter, index: int, rect: QRectF) -> None:
        # Layered translucent rectangles approximate a soft drop shadow far
        # more cheaply than a blur, and stay crisp at any zoom.
        for depth in range(6, 0, -1):
            painter.fillRect(
                rect.adjusted(depth * 0.6, depth * 0.9, depth * 0.6, depth * 0.9),
                QColor(0, 0, 0, 8 + depth * 2),
            )
        painter.fillRect(rect, QColor("#111111" if self._invert else "#ffffff"))

        image = self._images.get(index)
        if image is not None and not image.isNull():
            painter.drawImage(rect, image)
        else:
            self._request_render(index)
            self._paint_page_placeholder(painter, rect, index)

        is_current = index == self._current_page
        if is_current and not self._presentation:
            painter.setPen(QPen(QColor(61, 126, 255, 130), 1.5))
        else:
            painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.drawRect(rect)

        if self._presentation:
            return
        self._paint_page_badge(painter, rect, index, is_current)

    def _paint_page_badge(
        self, painter: QPainter, rect: QRectF, index: int, is_current: bool
    ) -> None:
        """Rounded page-number pill in the corner of each page."""
        label = str(index + 1)
        width = max(26.0, painter.fontMetrics().horizontalAdvance(label) + 16)
        badge = QRectF(rect.right() - width - 8, rect.top() + 8, width, 20)
        path = QPainterPath()
        path.addRoundedRect(badge, 10, 10)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(
            path, QColor(61, 126, 255, 220) if is_current else QColor(20, 22, 26, 150)
        )
        painter.setPen(QPen(QColor(255, 255, 255, 235)))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

    def _paint_page_placeholder(self, painter: QPainter, rect: QRectF, index: int) -> None:
        painter.save()
        painter.setPen(QPen(QColor(0, 0, 0, 40), 1, Qt.PenStyle.DashLine))
        painter.drawRect(rect.adjusted(8, 8, -8, -8))
        painter.setPen(QPen(QColor(120, 124, 132)))
        painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, f"Rendering page {index + 1}…")
        painter.restore()

    def _paint_edit_mask(self, painter: QPainter) -> None:
        """Cover the text behind the inline editor.

        The mask tracks the editor's live geometry rather than the original
        line box: once the text wraps the editor grows, and the untouched
        lines underneath would otherwise show through it.

        Two rules keep this from looking like a stray white patch:

        * a click on **empty space** starts a brand-new text box, so there are
          no glyphs to hide — masking there would paint over whatever the page
          already had, which is exactly the "white layer" complaint;
        * the fill is sampled from the rendered page just outside the masked
          rectangle rather than hard-coded white, so a tinted background, a
          letterhead or a dark page is matched instead of blanked.
        """
        if self._edit_mask is None:
            return
        page, rect, masked = self._edit_mask
        if not masked:
            return
        page_rect = self.page_rect_in_view(page)
        if page_rect is None:
            return
        area = self._to_view(page_rect, rect)
        editor = self._inline_editor
        if editor is not None:
            area = area.united(QRectF(editor.geometry()))
        area = area.adjusted(-2, -1, 2, 1)
        painter.fillRect(area, self._page_background_at(page, page_rect, area))

    def _page_background_at(self, page: int, page_rect: QRectF, area: QRectF) -> QColor:
        """Colour of the page immediately left of (or above) ``area``.

        Sampling the live render means the mask blends into coloured pages,
        scanned paper and dark documents. Falls back to the theme's paper
        colour when the page has not been rasterised yet.
        """
        fallback = QColor("#111111" if self._invert else "#ffffff")
        image = self._images.get(page)
        if image is None or image.isNull():
            return fallback
        scale_x = image.width() / max(page_rect.width(), 1.0)
        scale_y = image.height() / max(page_rect.height(), 1.0)
        mid_y = area.center().y() - page_rect.y()
        candidates = (
            (area.left() - page_rect.x() - 6, mid_y),
            (area.right() - page_rect.x() + 6, mid_y),
            (area.center().x() - page_rect.x(), area.top() - page_rect.y() - 6),
        )
        for cx, cy in candidates:
            px = int(cx * scale_x)
            py = int(cy * scale_y)
            if 0 <= px < image.width() and 0 <= py < image.height():
                return image.pixelColor(px, py)
        return fallback

    def _paint_selection(self, painter: QPainter) -> None:
        if not self._selection_rects:
            return
        colour = QColor(61, 126, 255, 70)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(colour))
        for page, rect in self._selection_rects:
            page_rect = self.page_rect_in_view(page)
            if page_rect is None:
                continue
            painter.drawRoundedRect(self._to_view(page_rect, rect).adjusted(-1, 0, 1, 0), 2, 2)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    def _paint_search_hits(self, painter: QPainter) -> None:
        if not self._search_hits:
            return
        for i, hit in enumerate(self._search_hits):
            page_rect = self.page_rect_in_view(hit.page)
            if page_rect is None:
                continue
            target = self._to_view(page_rect, hit.rect).adjusted(-2, -1, 2, 1)
            path = QPainterPath()
            path.addRoundedRect(target, 3, 3)
            if i == self._active_hit:
                painter.fillPath(path, QColor(255, 145, 0, 170))
                painter.setPen(QPen(QColor(214, 84, 0), 1.6))
                painter.drawPath(path)
            else:
                painter.fillPath(path, QColor(255, 214, 92, 120))

    def _paint_drag_feedback(self, painter: QPainter) -> None:
        if self._drag_start is None or self._drag_current is None:
            return
        rect = QRectF(self._drag_start, self._drag_current).normalized()
        match self._tool:
            case Tool.ZOOM | Tool.CROP:
                painter.setPen(QPen(QColor("#3d7eff"), 1, Qt.PenStyle.DashLine))
                painter.setBrush(QBrush(QColor(61, 126, 255, 40)))
                painter.drawRect(rect)
            case Tool.RECTANGLE | Tool.REDACT:
                painter.setPen(QPen(QColor("#e5484d"), 2))
                painter.setBrush(
                    QBrush(QColor(0, 0, 0, 120))
                    if self._tool is Tool.REDACT
                    else Qt.BrushStyle.NoBrush
                )
                painter.drawRect(rect)
            case Tool.ELLIPSE:
                painter.setPen(QPen(QColor("#e5484d"), 2))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(rect)
            case Tool.LINE | Tool.ARROW | Tool.MEASURE:
                painter.setPen(QPen(QColor("#e5484d"), 2))
                painter.drawLine(self._drag_start, self._drag_current)
                if self._tool is Tool.MEASURE:
                    self._paint_measurement_label(painter)
            case Tool.INK:
                painter.setPen(QPen(QColor("#111111"), 2, cap=Qt.PenCapStyle.RoundCap))
                page_rect = self.page_rect_in_view(self._drag_page)
                if page_rect is not None:
                    for stroke in self._ink_strokes:
                        for a, b in zip(stroke, stroke[1:], strict=False):  # consecutive pairs
                            painter.drawLine(
                                QPointF(
                                    page_rect.x() + a.x * self._zoom,
                                    page_rect.y() + a.y * self._zoom,
                                ),
                                QPointF(
                                    page_rect.x() + b.x * self._zoom,
                                    page_rect.y() + b.y * self._zoom,
                                ),
                            )
            case _:
                if self._tool in (Tool.HIGHLIGHT, Tool.UNDERLINE, Tool.STRIKEOUT):
                    painter.fillRect(rect, QColor(255, 224, 102, 90))

    def _paint_measurement_label(self, painter: QPainter) -> None:
        assert self._drag_start is not None and self._drag_current is not None
        distance_px = math.hypot(
            self._drag_current.x() - self._drag_start.x(),
            self._drag_current.y() - self._drag_start.y(),
        )
        points = distance_px / self._zoom
        unit = settings().data.ui.units
        value = {
            "mm": points * 25.4 / 72,
            "cm": points * 2.54 / 72,
            "in": points / 72,
            "pt": points,
        }.get(unit, points)
        label = f"{value:.2f} {unit}"
        midpoint = (self._drag_start + self._drag_current) / 2
        box = QRectF(midpoint.x() - 40, midpoint.y() - 24, 80, 18)
        painter.fillRect(box, QColor(0, 0, 0, 170))
        painter.setPen(QPen(QColor("#ffffff")))
        painter.drawText(box, Qt.AlignmentFlag.AlignCenter, label)

    def _to_view(self, page_rect: QRectF, rect: Rect) -> QRectF:
        return QRectF(
            page_rect.x() + rect.x0 * self._zoom,
            page_rect.y() + rect.y0 * self._zoom,
            max(1.0, rect.width * self._zoom),
            max(1.0, rect.height * self._zoom),
        )

    # ------------------------------------------------------------------ #
    # Rendering
    # ------------------------------------------------------------------ #
    def _request_render(self, index: int) -> None:
        if self._renderer is None or index in self._pending:
            return
        self._pending.add(index)
        request = RenderRequest(
            document_id=self._document.id if self._document else "",
            page=index,
            zoom=self._zoom,
            rotation=self._rotation,
            annotations=self._show_annotations,
            invert=self._invert,
            generation=self._renderer.generation,
        )

        def on_done(
            result: RenderedPage, page: int = index, generation: int = request.generation
        ) -> None:
            self._pending.discard(page)
            # Drop results from before the last edit. Rendering is
            # asynchronous, so a job started before a move/edit can finish
            # *after* refresh() cleared the cache and re-requested the page —
            # it would then overwrite the new image with the old one and the
            # canvas would show the pre-edit page until the next scroll.
            if self._renderer is None or generation < self._renderer.generation:
                return
            image = _to_qimage(result)
            if image is not None:
                self._images[page] = image
                # Bound the image cache to what is nearby.
                if len(self._images) > 24:
                    keep = set(self.visible_pages())
                    for stale in [k for k in self._images if k not in keep][:12]:
                        self._images.pop(stale, None)
                self.viewport().update()

        def on_error(exc: BaseException, page: int = index) -> None:
            self._pending.discard(page)
            log.warning("Render failed for page {}: {}", page + 1, exc)

        outcome = self._renderer.request(request, on_done, error_callback=on_error)
        if isinstance(outcome, RenderedPage):  # cache hit — already delivered
            self._pending.discard(index)

    def _prefetch_visible(self) -> None:
        if self._renderer is None:
            return
        self._renderer.prefetch(self._current_page, self._zoom, rotation=self._rotation)

    def _on_scrolled(self) -> None:
        centre = QPointF(self.viewport().width() / 2, self.viewport().height() / 3)
        hit = self._viewport_to_document(centre)
        if hit is not None:
            self._set_current_page(hit[0])
        self._prefetch_timer.start()
        self._sync_inline_editor()
        self.viewport().update()

    # ------------------------------------------------------------------ #
    # Events
    # ------------------------------------------------------------------ #
    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802
        super().resizeEvent(event)
        if self._zoom_mode is not ZoomMode.CUSTOM:
            self.set_zoom_mode(self._zoom_mode)
        else:
            self.relayout()

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802
        modifiers = event.modifiers()
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            delta = event.angleDelta().y()
            if delta:
                factor = 1.0 + (0.0016 * delta)
                self.set_zoom(self._zoom * factor, anchor=event.position())
            event.accept()
            return
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            bar = self.horizontalScrollBar()
            bar.setValue(bar.value() - event.angleDelta().y())
            event.accept()
            return
        super().wheelEvent(event)

    def event(self, event: QEvent) -> bool:
        """Handle pinch-zoom and other native gestures (trackpads, touch)."""
        if isinstance(event, QNativeGestureEvent):
            if event.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
                self.set_zoom(self._zoom * (1.0 + event.value()), anchor=event.position())
                return True
            if event.gestureType() == Qt.NativeGestureType.SmartZoomNativeGesture:
                self.set_zoom_mode(ZoomMode.FIT_PAGE if self._zoom > 1.01 else ZoomMode.ACTUAL)
                return True
        return super().event(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        position = event.position()
        middle = event.button() == Qt.MouseButton.MiddleButton
        space_pan = self._tool is Tool.PAN

        if middle or space_pan:
            self._panning = True
            self._pan_origin = event.pos()
            self._scroll_origin = (
                self.horizontalScrollBar().value(),
                self.verticalScrollBar().value(),
            )
            self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton and self._tool is Tool.MOVE_OBJECT:
            hit = self._viewport_to_document(position)
            if hit is not None:
                page, point = hit
                found = self._object_at(page, point)
                self.set_selected_object(found)
                if found is not None:
                    self._object_drag_origin = point
                    self._object_drag_delta = (0.0, 0.0)
                    self.viewport().setCursor(Qt.CursorShape.ClosedHandCursor)
                    self.status_message.emit(
                        f"{found.describe()} — drag or arrow keys to move · "
                        "Ctrl+C then Ctrl+V to copy · Ctrl+D to duplicate · "
                        "Shift+Delete to remove"
                    )
            else:
                self.set_selected_object(None)
            event.accept()
            return

        if event.button() == Qt.MouseButton.LeftButton:
            page, point = self._nearest_page(position)
            if self._inline_editor is not None:
                # Commit the current edit, then start one where the user clicked.
                self.finish_inline_edit(commit=True)
                if self._tool is Tool.EDIT_TEXT:
                    self._begin_text_edit(page, point)
                    event.accept()
                    return
            self._drag_page = page
            self._drag_start = position
            self._drag_current = position
            if self._tool is Tool.INK:
                self._ink_strokes = [[point]]
            elif self._tool is Tool.NOTE:
                self._emit_annotation(
                    {"type": AnnotationType.TEXT, "page": page, "point": point}
                )
                self._clear_drag()
            elif self._tool is Tool.EDIT_TEXT:
                # Alt selects the whole paragraph rather than a single line.
                self._begin_text_edit(
                    page,
                    point,
                    whole_paragraph=bool(event.modifiers() & Qt.KeyboardModifier.AltModifier),
                )
            elif self._tool in (Tool.SELECT, Tool.TEXT_SELECT):
                # Remember where the caret conceptually is, so the ribbon's
                # paragraph commands work after a plain click too.
                self._last_text_point = (page, point)
                self.clear_selection()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._panning:
            delta = event.pos() - self._pan_origin
            self.horizontalScrollBar().setValue(self._scroll_origin[0] - delta.x())
            self.verticalScrollBar().setValue(self._scroll_origin[1] - delta.y())
            event.accept()
            return

        if self._object_drag_origin is not None and self._selected_object is not None:
            hit = self._nearest_page(event.position())
            delta_x = hit[1].x - self._object_drag_origin.x
            delta_y = hit[1].y - self._object_drag_origin.y
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                # Shift constrains to the dominant axis, as in every editor.
                if abs(delta_x) >= abs(delta_y):
                    delta_y = 0.0
                else:
                    delta_x = 0.0
            self._object_drag_delta = (delta_x, delta_y)
            self.viewport().update()
            event.accept()
            return

        if self._tool is Tool.MOVE_OBJECT and self._object_drag_origin is None:
            hit = self._viewport_to_document(event.position())
            found = self._object_at(*hit) if hit is not None else None
            if found is not self._hover_object:
                self._hover_object = found
                self.viewport().update()

        if self._drag_start is None and self._tool in (Tool.EDIT_TEXT, Tool.SELECT):
            # Hovering editable text shows an I-beam and outlines the line, so
            # it is obvious what a click will edit.
            self._update_hover(event.position())

        if self._drag_start is not None:
            self._drag_current = event.position()
            if self._tool is Tool.INK and self._ink_strokes:
                hit = self._viewport_to_document(event.position())
                if hit is not None and hit[0] == self._drag_page:
                    self._ink_strokes[-1].append(hit[1])
            elif self._tool in (
                Tool.TEXT_SELECT,
                Tool.SELECT,
                Tool.HIGHLIGHT,
                Tool.UNDERLINE,
                Tool.STRIKEOUT,
            ):
                self._update_text_selection()
            self.viewport().update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._object_drag_origin is not None:
            dx, dy = self._object_drag_delta
            target = self._selected_object
            self._object_drag_origin = None
            self._object_drag_delta = (0.0, 0.0)
            self.viewport().setCursor(Qt.CursorShape.SizeAllCursor)
            if target is not None and (abs(dx) > 0.4 or abs(dy) > 0.4):
                self.object_moved.emit(target, dx, dy)
            else:
                self.viewport().update()
            event.accept()
            return

        if self._panning:
            self._panning = False
            self.viewport().setCursor(
                Qt.CursorShape.OpenHandCursor
                if self._tool is Tool.PAN
                else Qt.CursorShape.ArrowCursor
            )
            event.accept()
            return

        if self._drag_start is not None and self._drag_current is not None:
            self._finish_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        hit = self._viewport_to_document(event.position())
        if hit is None or self._document is None:
            return
        page, point = hit
        # Double-clicking text opens the in-place editor whatever tool is
        # active (except the markup tools, where a double-click means
        # "select a word"). This is the most discoverable route to editing.
        editing_tools = (Tool.EDIT_TEXT, Tool.SELECT, Tool.PAN, Tool.TEXT_SELECT)
        if self._tool in editing_tools:
            self._begin_text_edit(
                page,
                point,
                whole_paragraph=bool(event.modifiers() & Qt.KeyboardModifier.AltModifier),
            )
            return
        # Double-click selects the word under the cursor.
        for rect, word in self._document.extract_words(page):
            if rect.contains(point):
                self._selection_rects = [(page, rect)]
                self._selected_text = word
                self.selection_changed.emit(word)
                self.viewport().update()
                return

    def contextMenuEvent(self, event: Any) -> None:  # noqa: N802
        hit = self._viewport_to_document(QPointF(event.pos()))
        page = hit[0] if hit else self._current_page
        self.context_menu_requested.emit(event.globalPos(), page, hit[1] if hit else None)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        key = event.key()
        modifiers = event.modifiers()

        # Object clipboard. Handled before the nudge block so Ctrl+arrow keys
        # are not swallowed, and before the generic Ctrl+C so copying an
        # object wins over copying selected text.
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            action = {
                Qt.Key.Key_C: "copy",
                Qt.Key.Key_X: "cut",
                Qt.Key.Key_V: "paste",
                Qt.Key.Key_D: "duplicate",
            }.get(key)
            # Copy/cut/duplicate need something selected; paste only needs the
            # Move tool to be active, since it puts a new object on the page.
            wanted = self._selected_object is not None or (
                action == "paste" and self._tool is Tool.MOVE_OBJECT
            )
            if action is not None and wanted:
                self.object_clipboard_requested.emit(action, self._selected_object)
                event.accept()
                return

        # An object is selected: arrows nudge it instead of scrolling.
        if self._selected_object is not None and key in (
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
            Qt.Key.Key_Up,
            Qt.Key.Key_Down,
            Qt.Key.Key_Backspace,
            Qt.Key.Key_Delete,
            Qt.Key.Key_Escape,
        ):
            if key == Qt.Key.Key_Escape:
                self.set_selected_object(None)
                event.accept()
                return
            if key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
                if modifiers & Qt.KeyboardModifier.ShiftModifier:
                    self.object_delete_requested.emit(self._selected_object)
                else:
                    # Backspace pulls the object back towards the left margin,
                    # which is the quickest way to undo an unwanted indent.
                    self.nudge_selected(-self._nudge_step(modifiers), 0.0)
                event.accept()
                return
            step = self._nudge_step(modifiers)
            deltas = {
                Qt.Key.Key_Left: (-step, 0.0),
                Qt.Key.Key_Right: (step, 0.0),
                Qt.Key.Key_Up: (0.0, -step),
                Qt.Key.Key_Down: (0.0, step),
            }
            self.nudge_selected(*deltas[key])
            event.accept()
            return

        match key:
            case Qt.Key.Key_Space if modifiers == Qt.KeyboardModifier.NoModifier:
                self.verticalScrollBar().triggerAction(
                    QScrollBar.SliderAction.SliderPageStepAdd
                )
            case Qt.Key.Key_PageDown:
                self.next_page() if self._presentation else self.verticalScrollBar().triggerAction(
                    QScrollBar.SliderAction.SliderPageStepAdd
                )
            case Qt.Key.Key_PageUp:
                self.previous_page() if self._presentation else self.verticalScrollBar().triggerAction(
                    QScrollBar.SliderAction.SliderPageStepSub
                )
            case Qt.Key.Key_Home:
                self.first_page()
            case Qt.Key.Key_End:
                self.last_page()
            case Qt.Key.Key_Right | Qt.Key.Key_Down if self._presentation:
                self.next_page()
            case Qt.Key.Key_Left | Qt.Key.Key_Up if self._presentation:
                self.previous_page()
            case Qt.Key.Key_Escape:
                if self._presentation:
                    self.set_presentation_mode(False)
                else:
                    self.clear_selection()
            case Qt.Key.Key_Up if modifiers & Qt.KeyboardModifier.AltModifier:
                # Alt+Up/Down reorders lines, matching Word and every IDE.
                self.command_requested.emit("text.move_line_up")
            case Qt.Key.Key_Down if modifiers & Qt.KeyboardModifier.AltModifier:
                self.command_requested.emit("text.move_line_down")
            case Qt.Key.Key_A if modifiers & Qt.KeyboardModifier.ControlModifier:
                self.select_all_on_page()
            case Qt.Key.Key_C if modifiers & Qt.KeyboardModifier.ControlModifier:
                self.copy_selection()
            case _:
                super().keyPressEvent(event)
                return
        event.accept()

    # ------------------------------------------------------------------ #
    # Drag completion
    # ------------------------------------------------------------------ #
    def _finish_drag(self) -> None:
        assert self._drag_start is not None and self._drag_current is not None
        page = self._drag_page
        page_rect = self.page_rect_in_view(page)
        if page_rect is None:
            self._clear_drag()
            return
        start = Point(
            (self._drag_start.x() - page_rect.x()) / self._zoom,
            (self._drag_start.y() - page_rect.y()) / self._zoom,
        )
        end = Point(
            (self._drag_current.x() - page_rect.x()) / self._zoom,
            (self._drag_current.y() - page_rect.y()) / self._zoom,
        )
        rect = Rect(
            min(start.x, end.x), min(start.y, end.y), max(start.x, end.x), max(start.y, end.y)
        )
        tiny = rect.width < 3 and rect.height < 3

        match self._tool:
            case Tool.ZOOM if not tiny:
                self._zoom_to_rect(page_rect, rect)
            case Tool.CROP if not tiny:
                self._emit_annotation({"type": "crop", "page": page, "rect": rect})
            case Tool.RECTANGLE if not tiny:
                self._emit_annotation(
                    {"type": AnnotationType.SQUARE, "page": page, "rect": rect}
                )
            case Tool.ELLIPSE if not tiny:
                self._emit_annotation(
                    {"type": AnnotationType.CIRCLE, "page": page, "rect": rect}
                )
            case Tool.REDACT if not tiny:
                self._emit_annotation(
                    {"type": AnnotationType.REDACT, "page": page, "rect": rect}
                )
            case Tool.LINE | Tool.ARROW:
                self._emit_annotation(
                    {
                        "type": AnnotationType.LINE,
                        "page": page,
                        "start": start,
                        "end": end,
                        "arrow": self._tool is Tool.ARROW,
                    }
                )
            case Tool.MEASURE:
                self._emit_annotation(
                    {
                        "type": AnnotationType.DISTANCE,
                        "page": page,
                        "start": start,
                        "end": end,
                    }
                )
            case Tool.INK if self._ink_strokes and len(self._ink_strokes[0]) > 1:
                self._emit_annotation(
                    {"type": AnnotationType.INK, "page": page, "strokes": self._ink_strokes}
                )
            case Tool.TEXT_BOX if not tiny:
                self._emit_annotation(
                    {"type": AnnotationType.FREE_TEXT, "page": page, "rect": rect}
                )
            case Tool.HIGHLIGHT | Tool.UNDERLINE | Tool.STRIKEOUT:
                self._emit_markup()
            case Tool.EDIT_TEXT if not tiny:
                self.text_edit_requested.emit(page, rect)
        self._clear_drag()

    def _zoom_to_rect(self, page_rect: QRectF, rect: Rect) -> None:
        """Marquee zoom: fit the dragged rectangle to the viewport."""
        if rect.width <= 0 or rect.height <= 0:
            return
        factor = min(
            self.viewport().width() / (rect.width * self._zoom),
            self.viewport().height() / (rect.height * self._zoom),
        )
        centre_view = QPointF(
            page_rect.x() + rect.center.x * self._zoom,
            page_rect.y() + rect.center.y * self._zoom,
        )
        self.set_zoom(self._zoom * factor, anchor=centre_view)

    def _update_text_selection(self) -> None:
        """Select all words intersecting the drag rectangle."""
        if self._document is None or self._drag_start is None or self._drag_current is None:
            return
        page_rect = self.page_rect_in_view(self._drag_page)
        if page_rect is None:
            return
        x0 = (min(self._drag_start.x(), self._drag_current.x()) - page_rect.x()) / self._zoom
        x1 = (max(self._drag_start.x(), self._drag_current.x()) - page_rect.x()) / self._zoom
        y0 = (min(self._drag_start.y(), self._drag_current.y()) - page_rect.y()) / self._zoom
        y1 = (max(self._drag_start.y(), self._drag_current.y()) - page_rect.y()) / self._zoom
        band = Rect(x0, y0, x1, y1)
        selected = [
            (rect, word)
            for rect, word in self._document.extract_words(self._drag_page)
            if rect.intersects(band)
        ]
        self._selection_rects = [(self._drag_page, r) for r, _ in selected]
        self._selected_text = " ".join(w for _, w in selected)
        self.selection_changed.emit(self._selected_text)

    def _emit_markup(self) -> None:
        if not self._selection_rects:
            return
        kind = {
            Tool.HIGHLIGHT: AnnotationType.HIGHLIGHT,
            Tool.UNDERLINE: AnnotationType.UNDERLINE,
            Tool.STRIKEOUT: AnnotationType.STRIKEOUT,
        }[self._tool]
        self._emit_annotation(
            {
                "type": kind,
                "page": self._selection_rects[0][0],
                "quads": [r for _, r in self._selection_rects],
                "text": self._selected_text,
            }
        )
        self.clear_selection()

    def _begin_text_edit(
        self, page: int, point: Point, *, whole_paragraph: bool = False
    ) -> None:
        """Ask the controller to edit the text under ``point`` in place.

        The controller resolves the exact line (or paragraph) and opens an
        inline editor; the view deliberately does not hit-test blocks itself,
        which used to drag every sibling line into the edit.
        """
        if self._document is None:
            return
        self._last_text_point = (page, point)
        self.text_edit_requested.emit(page, None, point, whole_paragraph)

    # -- inline editing ------------------------------------------------------ #
    def begin_inline_edit(
        self,
        page: int,
        rect: Rect,
        text: str,
        style: Any,
        *,
        caret_at: Point | None = None,
        select_all: bool = False,
        max_height: float | None = None,
        mask_existing: bool = True,
    ) -> Any:
        """Open the in-place editor over ``rect`` and return it.

        ``mask_existing`` is ``False`` when the editor is creating *new* text
        on empty space: there is nothing underneath to hide, and painting a
        mask there would leave an opaque patch over the page.
        """
        from pdfstudio.ui.widgets.text_edit_overlay import InlineTextEditor

        self.finish_inline_edit()
        page_rect = self.page_rect_in_view(page)
        if page_rect is None:
            return None

        editor = InlineTextEditor(
            self.viewport(),
            text=text,
            rect=rect,
            style=style,
            zoom=self._zoom,
            max_height_points=max_height if max_height is not None else rect.height,
            view_rect=self._to_view(page_rect, rect),
        )
        self._inline_editor = editor
        self._inline_page = page
        # Hide the original glyphs while typing so the two do not overlap.
        self._edit_mask = (page, rect, mask_existing and bool(text.strip()))
        self.viewport().update()

        if select_all:
            editor.select_all_text()
        elif caret_at is not None:
            local = QPoint(
                int((caret_at.x - rect.x0) * self._zoom),
                int((caret_at.y - rect.y0) * self._zoom),
            )
            editor.setTextCursor(editor.cursorForPosition(local))
        return editor

    def finish_inline_edit(self, *, commit: bool = True) -> None:
        """Close any open inline editor."""
        editor = self._inline_editor
        if editor is None:
            return
        # Clear first: the commit handler calls back into this method, and the
        # cleared state turns that re-entrant call into a harmless no-op.
        self._inline_editor = None
        self._inline_page = None
        self._edit_mask = None
        if editor.is_active:
            if commit:
                editor.commit()
            else:
                editor.cancel()
        self.viewport().update()

    @property
    def inline_page(self) -> int | None:
        """Page the inline editor is open on, if any."""
        return self._inline_page

    @property
    def last_text_point(self) -> tuple[int, Point] | None:
        """Where the user last clicked or edited text.

        Paragraph commands on the ribbon need somewhere to act. Remembering
        the last text interaction means a button works straight away instead
        of demanding another click to place a cursor.
        """
        return self._last_text_point

    def set_last_text_point(self, page: int, point: Point) -> None:
        self._last_text_point = (page, point)

    @property
    def inline_editor(self) -> Any:
        return self._inline_editor

    def _sync_inline_editor(self) -> None:
        """Keep the editor glued to the page while scrolling or zooming."""
        editor = self._inline_editor
        if editor is None or self._inline_page is None:
            return
        page_rect = self.page_rect_in_view(self._inline_page)
        if page_rect is None:
            return
        editor.set_zoom(self._zoom)
        editor.set_view_rect(self._to_view(page_rect, editor.document_rect))

    def _emit_annotation(self, payload: dict[str, Any]) -> None:
        self.annotation_requested.emit(payload)

    def _update_hover(self, position: QPointF) -> None:
        """Track the line under the cursor for the hover affordance."""
        hit = self._viewport_to_document(position)
        previous = self._hover_line
        self._hover_line = None
        if hit is not None and self._document is not None:
            page, point = hit
            for block in self._document.extract_blocks(page):
                if not block.rect.expanded(2).contains(point):
                    continue
                for line in block.lines:
                    if line.rect.expanded(1).contains(point):
                        self._hover_line = (page, line.rect)
                        break
                break
        cursor = (
            Qt.CursorShape.IBeamCursor
            if self._hover_line is not None
            else (
                Qt.CursorShape.IBeamCursor
                if self._tool is Tool.TEXT_SELECT
                else Qt.CursorShape.ArrowCursor
            )
        )
        if self.viewport().cursor().shape() != cursor:
            self.viewport().setCursor(cursor)
        if previous != self._hover_line:
            self.viewport().update()

    def _paint_hover(self, painter: QPainter) -> None:
        """Outline the line the cursor is over when text editing is possible."""
        if self._hover_line is None or self._inline_editor is not None:
            return
        page, rect = self._hover_line
        page_rect = self.page_rect_in_view(page)
        if page_rect is None:
            return
        target = self._to_view(page_rect, rect).adjusted(-3, -2, 3, 2)
        path = QPainterPath()
        path.addRoundedRect(target, 4, 4)
        painter.fillPath(path, QColor(61, 126, 255, 26))
        painter.setPen(QPen(QColor(61, 126, 255, 120), 1, Qt.PenStyle.DashLine))
        painter.drawPath(path)

    # ------------------------------------------------------------------ #
    # Object selection and moving
    # ------------------------------------------------------------------ #
    def set_selected_object(self, target: Any) -> None:
        """Select (or clear) the object shown with move handles."""
        current = self._selected_object
        if target is current or (
            target is not None
            and current is not None
            and target.kind == current.kind
            and target.page == current.page
            and target.rect == current.rect
        ):
            self._selected_object = target  # keep the fresh payload
            return
        self._selected_object = target
        self._object_drag_delta = (0.0, 0.0)
        self.object_selected.emit(target)
        self.viewport().update()

    @property
    def selected_object(self) -> Any:
        return self._selected_object

    def nudge_selected(self, dx: float, dy: float) -> bool:
        """Move the selected object by ``(dx, dy)`` points. Returns success."""
        if self._selected_object is None:
            return False
        self.object_moved.emit(self._selected_object, dx, dy)
        return True

    def _object_at(self, page: int, point: Point) -> Any:
        if self._document is None:
            return None
        from pdfstudio.pdfengine.objects import ObjectService

        # Pick within a few points so hairline rules are still grabbable.
        tolerance = max(2.0, 5.0 / max(self._zoom, 0.1))
        return ObjectService(self._document).object_at(page, point, tolerance=tolerance)

    def _paint_object_overlay(self, painter: QPainter) -> None:
        """Outline the hovered object and draw handles on the selected one."""
        if self._tool is not Tool.MOVE_OBJECT:
            return

        hover = self._hover_object
        if hover is not None and hover is not self._selected_object:
            rect = self.page_rect_in_view(hover.page)
            if rect is not None:
                box = self._to_view(rect, hover.rect).adjusted(-3, -3, 3, 3)
                painter.setPen(QPen(QColor(61, 126, 255, 130), 1, Qt.PenStyle.DashLine))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(box)
                # Name what is under the cursor, so it is clear a click will
                # grab the rule rather than the heading behind it.
                self._paint_badge(painter, box, hover.kind.value.title())

        target = self._selected_object
        if target is None:
            return
        page_rect = self.page_rect_in_view(target.page)
        if page_rect is None:
            return
        dx, dy = self._object_drag_delta
        box = self._to_view(page_rect, target.rect).adjusted(-3, -3, 3, 3)
        box.translate(dx * self._zoom, dy * self._zoom)

        painter.setPen(QPen(QColor(61, 126, 255), 1.6))
        painter.setBrush(QBrush(QColor(61, 126, 255, 28)))
        painter.drawRect(box)

        # Corner handles make it obvious the object is grabbable.
        painter.setBrush(QBrush(QColor(61, 126, 255)))
        painter.setPen(QPen(QColor(255, 255, 255), 1))
        for x, y in (
            (box.left(), box.top()),
            (box.right(), box.top()),
            (box.left(), box.bottom()),
            (box.right(), box.bottom()),
        ):
            painter.drawRect(QRectF(x - 3.5, y - 3.5, 7, 7))
        painter.setBrush(Qt.BrushStyle.NoBrush)

        # A badge naming the object (and the live offset while dragging) so it
        # is obvious what is selected and that it really is moving.
        if abs(dx) > 0.01 or abs(dy) > 0.01:
            label = f"{target.kind.value.title()}   {dx:+.0f}, {dy:+.0f} pt"
        else:
            label = target.kind.value.title()
        self._paint_badge(painter, box, label)

    def _paint_badge(self, painter: QPainter, box: QRectF, label: str) -> None:
        """Rounded caption above ``box``, flipped below when there is no room."""
        metrics = painter.fontMetrics()
        width = metrics.horizontalAdvance(label) + 18
        height = metrics.height() + 6
        top = box.top() - height - 6
        if top < 2:
            top = box.bottom() + 6
        left = min(max(2.0, box.left()), max(2.0, self.viewport().width() - width - 2))
        badge = QRectF(left, top, width, height)

        path = QPainterPath()
        path.addRoundedRect(badge, 5, 5)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.fillPath(path, QColor(20, 22, 26, 225))
        painter.setPen(QPen(QColor(255, 255, 255, 240)))
        painter.drawText(badge, Qt.AlignmentFlag.AlignCenter, label)

    @staticmethod
    def _nudge_step(modifiers: Qt.KeyboardModifier) -> float:
        """Arrow-key step: fine with Ctrl, coarse with Shift."""
        if modifiers & Qt.KeyboardModifier.ControlModifier:
            return 0.5
        if modifiers & Qt.KeyboardModifier.ShiftModifier:
            return 10.0
        return 2.0

    def _clear_drag(self) -> None:
        self._drag_start = None
        self._drag_current = None
        self._ink_strokes = []
        self.viewport().update()

    # ------------------------------------------------------------------ #
    # Presentation mode
    # ------------------------------------------------------------------ #
    def set_presentation_mode(self, enabled: bool) -> None:
        """Full-screen, single-page, chrome-free reading."""
        self._presentation = enabled
        if enabled:
            self._previous_layout = self._layout
            self.set_layout(PageLayout.SINGLE)
            self.set_zoom_mode(ZoomMode.FIT_PAGE)
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        else:
            self.set_layout(getattr(self, "_previous_layout", PageLayout.CONTINUOUS))
            self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
            self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.viewport().update()

    @property
    def is_presentation(self) -> bool:
        return self._presentation


def _to_qimage(result: RenderedPage) -> QImage | None:
    """Wrap raw MuPDF samples in a ``QImage`` (copying so the buffer can go)."""
    if result.width <= 0 or result.height <= 0:
        return None
    fmt = {
        1: QImage.Format.Format_Grayscale8,
        3: QImage.Format.Format_RGB888,
        4: QImage.Format.Format_RGBA8888,
    }.get(result.channels)
    if fmt is None:
        return None
    image = QImage(result.samples, result.width, result.height, result.stride, fmt)
    return image.copy()
