"""In-place text editing directly on the page canvas.

Clicking text with the Edit Text tool opens a transparent editor positioned
exactly over the original words, rendered in the same font, size and colour, so
the user types *on the page* rather than in a modal dialog.

Behaviour
---------
* The caret lands on the character that was clicked.
* Text wraps live at the width of the original box; the overlay grows
  downwards as content is added and shows an amber hint when it has had to
  shrink the font to fit.
* **Enter** commits, **Shift+Enter** inserts a line break, **Esc** cancels.
* Clicking elsewhere on the page commits and starts editing the next line, so
  a document can be corrected without ever touching a dialog.
* A slim floating toolbar offers bold, italic, size, colour and alignment.
"""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QFont,
    QFontMetricsF,
    QKeyEvent,
    QPainter,
    QPen,
    QTextOption,
)
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QPlainTextEdit,
    QToolButton,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.content import TextStyle
from pdfstudio.pdfengine.types import Color, Rect

log = get_logger("ui.textedit")

#: Map the PDF base-14 families onto families Qt is likely to have.
_FAMILY_MAP: dict[str, str] = {
    "helvetica": "Helvetica",
    "helv": "Helvetica",
    "arial": "Arial",
    "times": "Times New Roman",
    "tiro": "Times New Roman",
    "timesnewroman": "Times New Roman",
    "courier": "Courier New",
    "cour": "Courier New",
}


def qt_font_for(style: TextStyle, zoom: float) -> QFont:
    """Build the on-screen font matching a PDF :class:`TextStyle`."""
    family = style.font.split("+")[-1].split("-")[0].split(",")[0]
    font = QFont(_FAMILY_MAP.get(family.lower().replace(" ", ""), family or "Helvetica"))
    font.setPointSizeF(max(1.0, style.size * zoom))
    font.setBold(style.bold)
    font.setItalic(style.italic)
    font.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return font


class FormatToolbar(QFrame):
    """Small floating bar shown above the text being edited."""

    style_changed = Signal(object)  # TextStyle

    def __init__(self, style: TextStyle, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("InlineFormatBar")
        self.setFrameShape(QFrame.Shape.NoFrame)
        self._style = style
        self._emitting = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 4)
        layout.setSpacing(3)

        self.family = QComboBox(self)
        self.family.addItems(
            ["Helvetica", "Arial", "Times New Roman", "Courier New", "Georgia", "Verdana"]
        )
        self.family.setCurrentText(
            _FAMILY_MAP.get(style.font.split("-")[0].lower(), style.font.split("-")[0])
            or "Helvetica"
        )
        self.family.setFixedWidth(126)
        self.family.setToolTip("Font family")

        self.size = QComboBox(self)
        self.size.setEditable(True)
        self.size.addItems([str(s) for s in (6, 8, 9, 10, 11, 12, 14, 16, 18, 24, 32, 48)])
        self.size.setCurrentText(f"{style.size:g}")
        self.size.setFixedWidth(58)
        self.size.setToolTip("Font size")

        self.bold = self._toggle("B", "Bold  (Ctrl+B)", style.bold, weight=True)
        self.italic = self._toggle("I", "Italic  (Ctrl+I)", style.italic, italic=True)

        self.color = QToolButton(self)
        self.color.setFixedSize(26, 24)
        self.color.setToolTip("Text colour")
        self._refresh_color_swatch()
        self.color.clicked.connect(self._choose_color)

        # Alignment as three toggle buttons rather than a drop-down: the
        # current alignment is then visible at a glance and one click away,
        # which is what every other editor does.
        self.align_group = QButtonGroup(self)
        self.align_group.setExclusive(True)
        self.align_buttons: dict[str, QToolButton] = {}
        # Plain letters rather than glyphs: the usual alignment pictograms are
        # not in the base fonts Qt can rely on across platforms and render as
        # tofu boxes on a bare Linux install.
        for value, glyph, tip in (
            ("left", "L", "Align left  (Ctrl+L)"),
            ("center", "C", "Centre  (Ctrl+E)"),
            ("right", "R", "Align right  (Ctrl+R)"),
        ):
            button = self._toggle(glyph, tip, style.align == value)
            button.setProperty("alignValue", value)
            self.align_group.addButton(button)
            self.align_buttons[value] = button
        if style.align not in self.align_buttons:
            self.align_buttons["left"].setChecked(True)

        # Outdent/indent act on the wrapped continuation lines, which is how a
        # stray indent on a wrapped line gets pulled back to the margin.
        self.outdent = self._toggle("\u2039", "Decrease indent", False)
        self.outdent.setCheckable(False)
        self.indent = self._toggle("\u203a", "Increase indent", False)
        self.indent.setCheckable(False)

        for widget in (
            self.family,
            self.size,
            self.bold,
            self.italic,
            self.color,
            *self.align_buttons.values(),
            self.outdent,
            self.indent,
        ):
            layout.addWidget(widget)

        self.family.currentTextChanged.connect(lambda _t: self._emit())
        self.size.currentTextChanged.connect(lambda _t: self._emit())
        self.bold.toggled.connect(lambda _s: self._emit())
        self.italic.toggled.connect(lambda _s: self._emit())
        for button in self.align_buttons.values():
            button.toggled.connect(self._on_align_toggled)
        self.outdent.clicked.connect(lambda: self._nudge_indent(-12.0))
        self.indent.clicked.connect(lambda: self._nudge_indent(12.0))

        # The bar floats above the (white) page, so it carries its own opaque
        # palette rather than inheriting whatever is behind it.
        self.setAutoFillBackground(True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

        shadow = QGraphicsDropShadowEffect(self)
        shadow.setBlurRadius(24)
        shadow.setOffset(0, 5)
        shadow.setColor(QColor(0, 0, 0, 130))
        self.setGraphicsEffect(shadow)

    def _toggle(
        self, text: str, tip: str, checked: bool, *, weight: bool = False, italic: bool = False
    ) -> QToolButton:
        button = QToolButton(self)
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(True)
        button.setChecked(checked)
        button.setFixedSize(26, 24)
        font = QFont(button.font())
        font.setBold(weight)
        font.setItalic(italic)
        button.setFont(font)
        return button

    def _refresh_color_swatch(self) -> None:
        self.color.setStyleSheet(
            f"QToolButton {{ background: {self._style.color.to_hex()};"
            " border: 1px solid rgba(255,255,255,0.35); border-radius: 4px; }"
        )

    def _choose_color(self) -> None:
        from PySide6.QtWidgets import QColorDialog

        chosen = QColorDialog.getColor(QColor(self._style.color.to_hex()), self, "Text colour")
        if chosen.isValid():
            self._style.color = Color(chosen.redF(), chosen.greenF(), chosen.blueF())
            self._refresh_color_swatch()
            self._emit()

    def _on_align_toggled(self, checked: bool) -> None:
        """Emit once, when a button becomes checked.

        An exclusive :class:`QButtonGroup` fires ``toggled`` twice per click
        (off for the old button, on for the new one); reacting to both would
        apply the *previous* alignment for one frame.
        """
        if checked:
            self._emit()

    def _nudge_indent(self, delta: float) -> None:
        """Change the wrapped-line indent and re-apply the style."""
        self._style = replace(
            self.current_style(),
            wrap_indent=max(0.0, self._style.wrap_indent + delta),
            first_line_indent=self._style.first_line_indent,
        )
        self.style_changed.emit(self._style)

    def current_alignment(self) -> str:
        for value, button in self.align_buttons.items():
            if button.isChecked():
                return value
        return "left"

    def set_alignment(self, value: str) -> None:
        """Check the button for ``value`` (used by the ribbon commands)."""
        button = self.align_buttons.get(value)
        if button is not None and not button.isChecked():
            button.setChecked(True)

    def current_style(self) -> TextStyle:
        """The style described by the toolbar controls."""
        try:
            size = float(self.size.currentText())
        except ValueError:
            size = self._style.size
        return TextStyle(
            font=self.family.currentText(),
            size=max(1.0, size),
            color=self._style.color,
            bold=self.bold.isChecked(),
            italic=self.italic.isChecked(),
            align=self.current_alignment(),
            line_height=self._style.line_height,
            # Indents are toolbar state too: rebuilding the style without them
            # would silently reset an indent every time the font changed.
            first_line_indent=self._style.first_line_indent,
            wrap_indent=self._style.wrap_indent,
        )

    def _emit(self) -> None:
        if self._emitting:
            return
        self._emitting = True
        try:
            self._style = self.current_style()
            self.style_changed.emit(self._style)
        finally:
            self._emitting = False


class InlineTextEditor(QPlainTextEdit):
    """Transparent editor that sits exactly over the text being replaced."""

    committed = Signal(str, object)  # text, TextStyle
    cancelled = Signal()
    #: Emitted when the user clicks elsewhere: commit, then edit there.
    commit_and_move = Signal(QPoint)

    def __init__(
        self,
        parent: QWidget,
        *,
        text: str,
        rect: Rect,
        style: TextStyle,
        zoom: float,
        max_height_points: float,
        view_rect: QRectF | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("InlineTextEditor")
        self._style = style
        self._zoom = zoom
        self._doc_rect = rect
        self._max_height = max(rect.height, max_height_points)
        self._original = text
        self._committed = False
        self._destroyed = False
        # Known before the first layout so wrapping is right immediately.
        self._view_rect = view_rect
        self._toolbar: FormatToolbar | None = None

        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setPlainText(text)
        self.setTabChangesFocus(True)
        self.document().setDocumentMargin(0)
        self.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, True)

        self.apply_style(style)
        self.textChanged.connect(self._on_text_changed)

        self._toolbar = FormatToolbar(style, parent)
        self._toolbar.style_changed.connect(self.apply_style)
        self._toolbar.show()

        self.reposition()
        self.show()
        self.setFocus(Qt.FocusReason.MouseFocusReason)

    # -- appearance -------------------------------------------------------- #
    def apply_style(self, style: TextStyle) -> None:
        """Re-render the editor with ``style`` (called by the toolbar)."""
        self._style = style
        font = qt_font_for(style, self._zoom)
        self.setFont(font)
        self.document().setDefaultFont(font)
        colour = QColor(style.color.to_hex())
        # The size must also be in the style sheet: the theme sets a global
        # ``QWidget { font-size }`` rule, which outranks setFont().
        self.setStyleSheet(
            "QPlainTextEdit#InlineTextEditor {"
            f"  color: {colour.name()};"
            f"  font-family: '{font.family()}';"
            f"  font-size: {font.pointSizeF():.2f}pt;"
            f"  font-weight: {'bold' if style.bold else 'normal'};"
            f"  font-style: {'italic' if style.italic else 'normal'};"
            "  background: rgba(61, 126, 255, 0.10);"
            "  border: none;"
            "  selection-background-color: rgba(61, 126, 255, 0.45);"
            "}"
        )

        # Mirror the alignment on screen. Without this the toolbar changed the
        # style that would be *written* but the text under the caret never
        # moved, so alignment looked like it did nothing at all.
        option = QTextOption(self.document().defaultTextOption())
        option.setAlignment(
            {
                "left": Qt.AlignmentFlag.AlignLeft,
                "center": Qt.AlignmentFlag.AlignHCenter,
                "right": Qt.AlignmentFlag.AlignRight,
                "justify": Qt.AlignmentFlag.AlignJustify,
            }.get(style.align, Qt.AlignmentFlag.AlignLeft)
        )
        option.setWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.document().setDefaultTextOption(option)
        # The indent is drawn as a left margin so the on-screen wrap width
        # matches the width used when the text is written back to the page.
        self.setViewportMargins(int(max(0.0, style.wrap_indent) * self._zoom), 0, 0, 0)
        self._sync_geometry()

    @property
    def style(self) -> TextStyle:
        return self._style

    @property
    def document_rect(self) -> Rect:
        return self._doc_rect

    def set_zoom(self, zoom: float) -> None:
        self._zoom = zoom
        self.apply_style(self._style)

    # -- geometry ---------------------------------------------------------- #
    def reposition(self, view_rect: QRectF | None = None) -> None:
        """Move the editor to follow the page (scrolling, zooming)."""
        if view_rect is not None:
            self._view_rect = view_rect
        self._sync_geometry()

    def set_view_rect(self, rect: QRectF) -> None:
        """Reposition over the page (called on scroll and zoom)."""
        self._view_rect = rect
        self._sync_geometry()

    def content_height(self) -> float:
        """Laid-out height of the text in pixels.

        ``QPlainTextEdit.document().size()`` reports *lines*, not pixels, so
        the block bounding rectangles are summed instead.
        """
        document = self.document()
        layout = document.documentLayout()
        total = 0.0
        block = document.begin()
        while block.isValid():
            total += layout.blockBoundingRect(block).height()
            block = block.next()
        if total <= 0:
            total = QFontMetricsF(self.font()).lineSpacing()
        return total + 2 * document.documentMargin()

    def _sync_geometry(self) -> None:
        rect = self._view_rect
        if rect is None:
            return
        width = int(max(24.0, rect.width()))
        # Two passes: the text only wraps once the widget has its final width,
        # so set that first and measure afterwards. Measuring before the
        # resize returns a stale (much too tall) layout.
        if self.width() != width:
            self.setGeometry(int(rect.x()), int(rect.y()), width, max(1, self.height()))
        # Wrapping is driven by the document's text width; without setting it
        # explicitly the layout keeps a stale value and never re-wraps.
        wrap_width = float(max(16, self.viewport().width()))
        if abs(self.document().textWidth() - wrap_width) > 0.5:
            self.document().setTextWidth(wrap_width)
        padding = QFontMetricsF(self.font()).descent() + 6
        needed = self.content_height() + padding
        height = int(max(rect.height(), min(needed, self._max_height * self._zoom + padding)))
        self.setGeometry(int(rect.x()), int(rect.y()), width, height)

        # Park the toolbar above the text, flipping below when there is no room.
        if self._toolbar is None:  # still constructing
            return
        bar = self._toolbar.sizeHint()
        x = int(min(max(6, rect.x()), self.parentWidget().width() - bar.width() - 6))
        y = int(rect.y() - bar.height() - 8)
        if y < 6:
            y = int(rect.y() + height + 8)
        self._toolbar.move(x, y)
        self._toolbar.raise_()
        self.raise_()

    def _on_text_changed(self) -> None:
        self._sync_geometry()
        self.viewport().update()
        # The canvas draws the mask behind this widget and must follow it as
        # the editor grows.
        parent = self.parentWidget()
        if parent is not None:
            parent.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt API
        return self.size()

    # -- painting ---------------------------------------------------------- #
    def paintEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # A dashed outline marks the editable region and its wrap width.
        pen = QPen(QColor(61, 126, 255, 200), 1.2, Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.drawRect(self.viewport().rect().adjusted(0, 0, -1, -1))

        if self.will_shrink():
            painter.setPen(QPen(QColor(232, 163, 61), 1.4))
            painter.drawLine(
                0,
                self.viewport().height() - 1,
                self.viewport().width(),
                self.viewport().height() - 1,
            )
        painter.end()

    def will_shrink(self) -> bool:
        """``True`` when the text needs more room than the box can grow to."""
        return self.content_height() > self._max_height * self._zoom + 1

    # -- interaction -------------------------------------------------------- #
    def place_caret_at(self, viewport_point: QPoint) -> None:
        """Put the caret where the user clicked."""
        cursor = self.cursorForPosition(
            self.mapFromParent(viewport_point) if self.parentWidget() else viewport_point
        )
        self.setTextCursor(cursor)

    def select_all_text(self) -> None:
        self.selectAll()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt API
        key = event.key()
        modifiers = event.modifiers()
        shift = bool(modifiers & Qt.KeyboardModifier.ShiftModifier)
        control = bool(modifiers & Qt.KeyboardModifier.ControlModifier)

        if key == Qt.Key.Key_Escape:
            self.cancel()
            event.accept()
            return
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not shift:
            self.commit()
            event.accept()
            return
        if control and key == Qt.Key.Key_B and self._toolbar is not None:
            self._toolbar.bold.toggle()
            event.accept()
            return
        if control and key == Qt.Key.Key_I and self._toolbar is not None:
            self._toolbar.italic.toggle()
            event.accept()
            return
        if control and self._toolbar is not None:
            alignment = {
                Qt.Key.Key_L: "left",
                Qt.Key.Key_E: "center",
                Qt.Key.Key_R: "right",
            }.get(key)
            if alignment is not None:
                self._toolbar.set_alignment(alignment)
                event.accept()
                return
            if key in (Qt.Key.Key_BracketLeft, Qt.Key.Key_BracketRight):
                self._toolbar._nudge_indent(-12.0 if key == Qt.Key.Key_BracketLeft else 12.0)
                event.accept()
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt API
        super().focusOutEvent(event)
        # Focus moving into the format bar must not end the edit.
        toolbar = self._toolbar
        if toolbar is not None and toolbar.isAncestorOf(self.focusWidget() or self):
            return
        if event.reason() in (
            Qt.FocusReason.PopupFocusReason,
            Qt.FocusReason.ActiveWindowFocusReason,
        ):
            return
        QTimer.singleShot(0, self._commit_if_focus_lost)

    def _commit_if_focus_lost(self) -> None:
        if self._committed:
            return
        focus = self.focusWidget()
        toolbar = self._toolbar
        if focus is self or (
            focus is not None and toolbar is not None and toolbar.isAncestorOf(focus)
        ):
            return
        self.commit()

    # -- lifecycle ---------------------------------------------------------- #
    def commit(self) -> None:
        """Apply the edit (no-op when the text is unchanged).

        The signal is emitted *before* the widget is destroyed: a receiver
        typically calls back into the view to tidy up, and a deleted C++ object
        would make that re-entrant call crash.
        """
        if self._committed:
            return
        self._committed = True
        text = self.toPlainText()
        style = self._style
        self.hide()
        if self._toolbar is not None:
            self._toolbar.hide()
        if text != self._original:
            self.committed.emit(text, style)
        else:
            self.cancelled.emit()
        self._teardown()

    def cancel(self) -> None:
        """Abandon the edit and restore the original text."""
        if self._committed:
            return
        self._committed = True
        self.hide()
        if self._toolbar is not None:
            self._toolbar.hide()
        self.cancelled.emit()
        self._teardown()

    def _teardown(self) -> None:
        """Destroy the widgets (safe to call more than once)."""
        if self._toolbar is not None:
            self._toolbar.deleteLater()
            self._toolbar = None
        if not self._destroyed:
            self._destroyed = True
            self.deleteLater()

    @property
    def is_active(self) -> bool:
        return not self._committed
