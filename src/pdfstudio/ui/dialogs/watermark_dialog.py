"""Watermark dialog with a live preview."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.pdfengine.types import Color
from pdfstudio.ui.dialogs.common import ColorButton, PercentSlider


class WatermarkDialog(QDialog):
    """Collects watermark text, appearance and placement."""

    POSITIONS = (
        "center",
        "top-left",
        "top-center",
        "top-right",
        "bottom-left",
        "bottom-center",
        "bottom-right",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add watermark")
        self.resize(520, 480)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.text = QLineEdit("CONFIDENTIAL", self)
        self.size = QSpinBox(self)
        self.size.setRange(6, 300)
        self.size.setValue(48)
        self.rotation = QSpinBox(self)
        self.rotation.setRange(-180, 180)
        self.rotation.setValue(45)
        self.rotation.setSuffix("°")
        self.opacity = PercentSlider(0.25, parent=self)
        self.color = ColorButton(Color(0.6, 0.6, 0.6), self)
        self.position = QComboBox(self)
        self.position.addItems(self.POSITIONS)
        self.tile = QCheckBox("Tile across the page", self)
        self.foreground = QCheckBox("Draw on top of the content", self)
        self.foreground.setChecked(True)

        form.addRow("Text:", self.text)
        form.addRow("Font size:", self.size)
        form.addRow("Rotation:", self.rotation)
        form.addRow("Opacity:", self.opacity)
        form.addRow("Colour:", self.color)
        form.addRow("Position:", self.position)
        form.addRow("", self.tile)
        form.addRow("", self.foreground)
        layout.addLayout(form)

        self.preview = QLabel(self)
        self.preview.setMinimumHeight(230)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid palette(mid);")
        layout.addWidget(self.preview, 1)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        box.button(QDialogButtonBox.StandardButton.Ok).setText("Apply")
        box.button(QDialogButtonBox.StandardButton.Ok).setProperty("primary", True)
        box.accepted.connect(self.accept)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        for signal in (
            self.text.textChanged,
            self.size.valueChanged,
            self.rotation.valueChanged,
            self.opacity.value_changed,
            self.color.color_changed,
            self.position.currentIndexChanged,
            self.tile.toggled,
        ):
            signal.connect(self._update_preview)
        self._update_preview()

    def _update_preview(self) -> None:
        """Draw a scaled A4 page with the watermark as configured."""
        width, height = 220, 300
        pixmap = QPixmap(width, height)
        pixmap.fill(QColor("#ffffff"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QColor("#e2e5ea"))
        for y in range(40, height - 30, 14):
            painter.drawLine(24, y, width - 24, y)

        colour = QColor(self.color.color.to_hex())
        colour.setAlphaF(self.opacity.value())
        painter.setPen(colour)
        font = QFont()
        font.setPointSizeF(max(5.0, self.size.value() * (width / 595.0)))
        painter.setFont(font)

        anchors = {
            "center": (width / 2, height / 2),
            "top-left": (width * 0.3, height * 0.22),
            "top-center": (width / 2, height * 0.22),
            "top-right": (width * 0.7, height * 0.22),
            "bottom-left": (width * 0.3, height * 0.78),
            "bottom-center": (width / 2, height * 0.78),
            "bottom-right": (width * 0.7, height * 0.78),
        }
        spots = (
            [(x, y) for y in range(40, height, 90) for x in range(40, width, 110)]
            if self.tile.isChecked()
            else [anchors[self.position.currentText()]]
        )
        for x, y in spots:
            painter.save()
            painter.translate(x, y)
            painter.rotate(-self.rotation.value())
            painter.drawText(
                -int(width / 2),
                0,
                width,
                20,
                Qt.AlignmentFlag.AlignCenter,
                self.text.text(),
            )
            painter.restore()
        painter.end()
        self.preview.setPixmap(pixmap)

    def values(self) -> dict[str, Any]:
        """The configuration chosen by the user."""
        return {
            "text": self.text.text(),
            "size": float(self.size.value()),
            "rotation": float(self.rotation.value()),
            "opacity": self.opacity.value(),
            "color": self.color.color,
            "position": self.position.currentText(),
            "tile": self.tile.isChecked(),
            "foreground": self.foreground.isChecked(),
        }
