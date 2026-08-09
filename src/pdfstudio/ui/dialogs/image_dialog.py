"""Image editing dialog: adjust, crop, replace, extract and compress images."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.pdfengine.content import ImageAdjustments, ImageEditor
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import ImageInfo
from pdfstudio.ui.dialogs.common import PercentSlider


class ImageEditDialog(QDialog):
    """Pick an image on the page and apply non-destructive adjustments."""

    def __init__(
        self,
        document: PdfDocument,
        page: int,
        images: list[ImageInfo],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Edit image")
        self.resize(880, 620)
        self.document = document
        self.page = page
        self.images = images
        self.editor = ImageEditor(document)
        self._original: bytes = b""

        layout = QHBoxLayout(self)

        # -- list ------------------------------------------------------- #
        left = QVBoxLayout()
        self.list = QListWidget(self)
        for n, info in enumerate(images, 1):
            self.list.addItem(
                f"{n}. {info.width}×{info.height} {info.colorspace} "
                f"({info.size_bytes // 1024} KB)"
            )
        self.list.currentRowChanged.connect(self._load_image)
        left.addWidget(QLabel("Images on this page", self))
        left.addWidget(self.list, 1)
        layout.addLayout(left, 1)

        # -- preview and controls ---------------------------------------- #
        right = QVBoxLayout()
        self.preview = QLabel(self)
        self.preview.setMinimumSize(380, 300)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("background: #2b2b2b; border: 1px solid #555;")
        right.addWidget(self.preview, 1)

        adjustments = QGroupBox("Adjustments", self)
        form = QFormLayout(adjustments)
        self.brightness = PercentSlider(1.0, minimum=10, maximum=250, parent=self)
        self.contrast = PercentSlider(1.0, minimum=10, maximum=250, parent=self)
        self.saturation = PercentSlider(1.0, minimum=0, maximum=250, parent=self)
        self.gamma = PercentSlider(1.0, minimum=20, maximum=300, parent=self)
        self.sharpen = PercentSlider(0.0, minimum=0, maximum=200, parent=self)
        self.blur = PercentSlider(0.0, minimum=0, maximum=100, parent=self)
        self.opacity = PercentSlider(1.0, parent=self)
        self.rotate = QComboBox(self)
        self.rotate.addItems(["0°", "90°", "180°", "270°"])
        self.grayscale = QCheckBox("Greyscale", self)
        self.invert = QCheckBox("Invert", self)
        self.mirror = QCheckBox("Mirror", self)
        self.flip = QCheckBox("Flip", self)
        self.denoise = QCheckBox("Reduce noise", self)
        self.auto_contrast = QCheckBox("Auto contrast", self)
        self.remove_bg = QCheckBox("Remove background", self)

        form.addRow("Brightness:", self.brightness)
        form.addRow("Contrast:", self.contrast)
        form.addRow("Saturation:", self.saturation)
        form.addRow("Gamma:", self.gamma)
        form.addRow("Sharpen:", self.sharpen)
        form.addRow("Blur:", self.blur)
        form.addRow("Opacity:", self.opacity)
        form.addRow("Rotate:", self.rotate)
        checks = QHBoxLayout()
        for box in (self.grayscale, self.invert, self.mirror, self.flip):
            checks.addWidget(box)
        form.addRow("", _wrap(checks))
        checks2 = QHBoxLayout()
        for box in (self.denoise, self.auto_contrast, self.remove_bg):
            checks2.addWidget(box)
        form.addRow("", _wrap(checks2))
        right.addWidget(adjustments)

        actions = QHBoxLayout()
        for label, handler in (
            ("Replace…", self._replace),
            ("Extract…", self._extract),
            ("Compress", self._compress),
            ("Delete", self._delete),
            ("Reset", self._reset),
        ):
            button = QPushButton(label, self)
            button.clicked.connect(handler)
            actions.addWidget(button)
        right.addLayout(actions)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Apply | QDialogButtonBox.StandardButton.Close
        )
        apply_button = box.button(QDialogButtonBox.StandardButton.Apply)
        apply_button.setProperty("primary", True)
        apply_button.clicked.connect(self._apply)
        box.rejected.connect(self.reject)
        right.addWidget(box)
        layout.addLayout(right, 2)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(180)
        self._debounce.timeout.connect(self._update_preview)
        for widget in (
            self.brightness,
            self.contrast,
            self.saturation,
            self.gamma,
            self.sharpen,
            self.blur,
            self.opacity,
        ):
            widget.value_changed.connect(lambda _v: self._debounce.start())
        for check in (
            self.grayscale,
            self.invert,
            self.mirror,
            self.flip,
            self.denoise,
            self.auto_contrast,
            self.remove_bg,
        ):
            check.toggled.connect(lambda _s: self._debounce.start())
        self.rotate.currentIndexChanged.connect(lambda _i: self._debounce.start())

        if images:
            self.list.setCurrentRow(0)

    # -- state ------------------------------------------------------------- #
    @property
    def current(self) -> ImageInfo | None:
        row = self.list.currentRow()
        return self.images[row] if 0 <= row < len(self.images) else None

    def adjustments(self) -> ImageAdjustments:
        return ImageAdjustments(
            brightness=self.brightness.value(),
            contrast=self.contrast.value(),
            saturation=self.saturation.value(),
            gamma=max(0.2, self.gamma.value()),
            sharpen=self.sharpen.value(),
            blur=self.blur.value() * 5,
            opacity=self.opacity.value(),
            grayscale=self.grayscale.isChecked(),
            invert=self.invert.isChecked(),
            mirror=self.mirror.isChecked(),
            flip=self.flip.isChecked(),
            rotate=int(self.rotate.currentText().rstrip("°")),
            denoise=self.denoise.isChecked(),
            auto_contrast=self.auto_contrast.isChecked(),
            remove_background=self.remove_bg.isChecked(),
        )

    def _load_image(self, row: int) -> None:
        info = self.current
        if info is None:
            return
        try:
            self._original, _ = self.document.extract_image(info.xref)
        except Exception as exc:
            self.preview.setText(f"Could not read the image:\n{exc}")
            return
        self._update_preview()

    def _update_preview(self) -> None:
        if not self._original:
            return
        try:
            data = self.adjustments().apply(self._original)
        except Exception:
            data = self._original
        image = QImage()
        image.loadFromData(data)
        self.preview.setPixmap(
            QPixmap.fromImage(image).scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    # -- actions ------------------------------------------------------------- #
    def _apply(self) -> None:
        info = self.current
        if info is None:
            return
        self.editor.adjust(self.page, info.xref, self.adjustments())
        QMessageBox.information(self, "Image", "Adjustments applied (undoable).")
        self.accept()

    def _replace(self) -> None:
        info = self.current
        if info is None:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Replace image", str(Path.home()), "Images (*.png *.jpg *.jpeg *.webp)"
        )
        if path:
            self.editor.replace(self.page, info.xref, path)
            self.accept()

    def _extract(self) -> None:
        info = self.current
        if info is None:
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Extract image", str(Path.home() / f"image.{info.ext}")
        )
        if path:
            data, _ = self.document.extract_image(info.xref)
            Path(path).write_bytes(data)

    def _compress(self) -> None:
        info = self.current
        if info is None:
            return
        saved = self.editor.resample(self.page, info.xref)
        QMessageBox.information(
            self,
            "Compress",
            f"Saved {saved // 1024} KB." if saved else "The image is already optimal.",
        )
        if saved:
            self.accept()

    def _delete(self) -> None:
        info = self.current
        if info is None:
            return
        if QMessageBox.question(self, "Delete image", "Remove this image?") == (
            QMessageBox.StandardButton.Yes
        ):
            self.editor.delete(self.page, info.xref)
            self.accept()

    def _reset(self) -> None:
        for widget in (self.brightness, self.contrast, self.saturation, self.gamma):
            widget.slider.setValue(100)
        for widget in (self.sharpen, self.blur):
            widget.slider.setValue(0)
        self.opacity.slider.setValue(100)
        self.rotate.setCurrentIndex(0)
        for check in (
            self.grayscale,
            self.invert,
            self.mirror,
            self.flip,
            self.denoise,
            self.auto_contrast,
            self.remove_bg,
        ):
            check.setChecked(False)
        self._update_preview()


def _wrap(layout: Any) -> QWidget:
    widget = QWidget()
    widget.setLayout(layout)
    return widget
