"""Small reusable dialog widgets."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import (
    QColorDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QWidget,
)

from pdfstudio.pdfengine.types import Color


class ColorButton(QPushButton):
    """A button showing (and choosing) a colour."""

    color_changed = Signal(object)  # Color

    def __init__(self, color: Color | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._color = color or Color(0, 0, 0)
        self.setFixedSize(46, 24)
        self.clicked.connect(self._choose)
        self._refresh()

    @property
    def color(self) -> Color:
        return self._color

    def set_color(self, color: Color) -> None:
        self._color = color
        self._refresh()
        self.color_changed.emit(color)

    def _refresh(self) -> None:
        pixmap = QPixmap(self.width() - 10, self.height() - 10)
        pixmap.fill(QColor(self._color.to_hex()))
        painter = QPainter(pixmap)
        painter.setPen(QColor("#666666"))
        painter.drawRect(0, 0, pixmap.width() - 1, pixmap.height() - 1)
        painter.end()
        self.setIcon(pixmap)
        self.setToolTip(self._color.to_hex())

    def _choose(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color.to_hex()), self, "Choose a colour")
        if chosen.isValid():
            self.set_color(
                Color(chosen.redF(), chosen.greenF(), chosen.blueF(), chosen.alphaF())
            )


class PercentSlider(QWidget):
    """Labelled 0-100% slider."""

    value_changed = Signal(float)

    def __init__(
        self,
        value: float = 1.0,
        *,
        minimum: int = 0,
        maximum: int = 100,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.slider = QSlider(Qt.Orientation.Horizontal, self)
        self.slider.setRange(minimum, maximum)
        self.slider.setValue(int(value * 100))
        self.label = QLabel(f"{int(value * 100)}%", self)
        self.label.setFixedWidth(42)
        layout.addWidget(self.slider, 1)
        layout.addWidget(self.label)
        self.slider.valueChanged.connect(self._on_change)

    def _on_change(self, value: int) -> None:
        self.label.setText(f"{value}%")
        self.value_changed.emit(value / 100)

    def value(self) -> float:
        return self.slider.value() / 100


def standard_buttons(
    accept: Callable[[], None], reject: Callable[[], None], *, ok_text: str = "OK"
) -> QDialogButtonBox:
    """OK/Cancel button box with a customisable primary label."""
    box = QDialogButtonBox(
        QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
    )
    ok_button = box.button(QDialogButtonBox.StandardButton.Ok)
    ok_button.setText(ok_text)
    ok_button.setProperty("primary", True)
    box.accepted.connect(accept)
    box.rejected.connect(reject)
    return box


class GuiInvoker(QObject):
    """Runs callables on the GUI thread from worker threads.

    Qt timers created on a thread without an event loop never fire, so
    ``QTimer.singleShot`` is not a valid way to hop threads. A queued signal
    connection is; this helper wraps that pattern for dialogs.
    """

    _invoke = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._invoke.connect(self._run, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _run(self, fn: Callable[[], object]) -> None:
        fn()

    def __call__(self, fn: Callable[[], object]) -> None:
        """Schedule ``fn`` on the GUI thread."""
        self._invoke.emit(fn)
