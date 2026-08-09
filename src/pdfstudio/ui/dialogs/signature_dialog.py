"""Digital signature dialog: draw, type or import a signature, then sign."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QImage, QMouseEvent, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.exceptions import DependencyMissingError, PdfStudioError
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.security import SecurityService
from pdfstudio.pdfengine.types import Rect

log = get_logger("ui.signature")


class SignaturePad(QWidget):
    """Mouse/stylus drawing surface producing a transparent PNG."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(440, 170)
        self.setStyleSheet("background: white; border: 1px dashed #888;")
        self._strokes: list[list[QPointF]] = []
        self._drawing = False
        self.pen_width = 2.6
        self.pen_color = QColor("#101a3a")

    def clear(self) -> None:
        self._strokes.clear()
        self.update()
        self.changed.emit()

    @property
    def is_empty(self) -> bool:
        return not any(len(stroke) > 1 for stroke in self._strokes)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drawing = True
        self._strokes.append([event.position()])

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._drawing and self._strokes:
            self._strokes[-1].append(event.position())
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        self._drawing = False
        self.changed.emit()

    def paintEvent(self, event: Any) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor("#ffffff"))
        painter.setPen(
            QPen(
                self.pen_color,
                self.pen_width,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        for stroke in self._strokes:
            for a, b in zip(stroke, stroke[1:], strict=False):  # consecutive pairs
                painter.drawLine(a, b)
        if self.is_empty:
            painter.setPen(QColor("#bbbbbb"))
            painter.drawText(
                self.rect(), Qt.AlignmentFlag.AlignCenter, "Draw your signature here"
            )
        painter.end()

    def to_png(self) -> bytes:
        """Render the strokes onto a transparent canvas."""
        image = QImage(self.size() * 2, QImage.Format.Format_ARGB32)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.scale(2, 2)
        painter.setPen(
            QPen(
                self.pen_color,
                self.pen_width,
                Qt.PenStyle.SolidLine,
                Qt.PenCapStyle.RoundCap,
                Qt.PenJoinStyle.RoundJoin,
            )
        )
        for stroke in self._strokes:
            for a, b in zip(stroke, stroke[1:], strict=False):  # consecutive pairs
                painter.drawLine(a, b)
        painter.end()
        from PySide6.QtCore import QBuffer

        qbuffer = QBuffer()
        qbuffer.open(QBuffer.OpenModeFlag.WriteOnly)
        image.save(qbuffer, "PNG")
        return bytes(qbuffer.data())


class SignatureDialog(QDialog):
    """Create a signature appearance and optionally sign cryptographically."""

    def __init__(self, document: PdfDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Sign document")
        self.resize(640, 700)
        self.document = document

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs, 1)

        # -- draw --------------------------------------------------------- #
        draw_page = QWidget(self.tabs)
        draw_layout = QVBoxLayout(draw_page)
        self.pad = SignaturePad(draw_page)
        draw_layout.addWidget(self.pad)
        clear = QPushButton("Clear", draw_page)
        clear.clicked.connect(self.pad.clear)
        draw_layout.addWidget(clear)
        self.tabs.addTab(draw_page, "Draw")

        # -- type --------------------------------------------------------- #
        type_page = QWidget(self.tabs)
        type_form = QFormLayout(type_page)
        self.typed_name = QLineEdit(type_page)
        self.typed_font = QComboBox(type_page)
        self.typed_font.addItems(
            ["Segoe Script", "Brush Script MT", "Comic Sans MS", "Georgia", "Times New Roman"]
        )
        self.typed_size = QSpinBox(type_page)
        self.typed_size.setRange(12, 72)
        self.typed_size.setValue(30)
        self.typed_preview = QLabel(type_page)
        self.typed_preview.setMinimumHeight(90)
        self.typed_preview.setStyleSheet("background: white; border: 1px solid #888;")
        type_form.addRow("Name:", self.typed_name)
        type_form.addRow("Style:", self.typed_font)
        type_form.addRow("Size:", self.typed_size)
        type_form.addRow("Preview:", self.typed_preview)
        for signal in (
            self.typed_name.textChanged,
            self.typed_font.currentTextChanged,
            self.typed_size.valueChanged,
        ):
            signal.connect(self._update_typed_preview)
        self.tabs.addTab(type_page, "Type")

        # -- image --------------------------------------------------------- #
        image_page = QWidget(self.tabs)
        image_layout = QVBoxLayout(image_page)
        self.image_preview = QLabel("No image selected", image_page)
        self.image_preview.setMinimumHeight(150)
        self.image_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_preview.setStyleSheet("background: white; border: 1px solid #888;")
        choose = QPushButton("Choose image…", image_page)
        choose.clicked.connect(self._choose_image)
        image_layout.addWidget(self.image_preview, 1)
        image_layout.addWidget(choose)
        self._image_bytes: bytes | None = None
        self.tabs.addTab(image_page, "Image")

        # -- certificate --------------------------------------------------------- #
        cert_page = QWidget(self.tabs)
        cert_form = QFormLayout(cert_page)
        self.p12_path = QLineEdit(cert_page)
        browse = QPushButton("Browse…", cert_page)
        browse.clicked.connect(self._choose_p12)
        row = QHBoxLayout()
        row.addWidget(self.p12_path, 1)
        row.addWidget(browse)
        self.p12_password = QLineEdit(cert_page)
        self.p12_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.timestamp_url = QLineEdit(cert_page)
        self.timestamp_url.setPlaceholderText("https://freetsa.org/tsr (optional)")
        cert_form.addRow("PKCS#12 file:", row)
        cert_form.addRow("Password:", self.p12_password)
        cert_form.addRow("Timestamp server:", self.timestamp_url)
        note = QLabel(
            "A certificate signature is cryptographically verifiable and requires "
            "the optional pyHanko package. Without one, PDF Studio adds a visible "
            "signature appearance only.",
            cert_page,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")
        cert_form.addRow("", note)
        self.tabs.addTab(cert_page, "Certificate")

        # -- placement --------------------------------------------------------- #
        placement = QFormLayout()
        self.reason = QLineEdit("I approve this document", self)
        self.location = QLineEdit(self)
        self.page_spin = QSpinBox(self)
        self.page_spin.setRange(1, max(1, document.page_count))
        self.page_spin.setValue(document.page_count)
        self.invisible = QCheckBox("Invisible signature (no visible block)", self)
        placement.addRow("Reason:", self.reason)
        placement.addRow("Location:", self.location)
        placement.addRow("Page:", self.page_spin)
        placement.addRow("", self.invisible)
        layout.addLayout(placement)

        box = QDialogButtonBox(self)
        apply_button = box.addButton("Apply signature", QDialogButtonBox.ButtonRole.AcceptRole)
        apply_button.setProperty("primary", True)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        apply_button.clicked.connect(self._apply)
        box.rejected.connect(self.reject)
        layout.addWidget(box)

        self._update_typed_preview()

    # -- helpers -------------------------------------------------------------- #
    def _update_typed_preview(self) -> None:
        pixmap = QPixmap(self.typed_preview.width() or 380, 90)
        pixmap.fill(QColor("#ffffff"))
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        font = QFont(self.typed_font.currentText(), self.typed_size.value())
        font.setItalic(True)
        painter.setFont(font)
        painter.setPen(QColor("#101a3a"))
        painter.drawText(pixmap.rect(), Qt.AlignmentFlag.AlignCenter, self.typed_name.text())
        painter.end()
        self.typed_preview.setPixmap(pixmap)

    def _choose_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Signature image", str(Path.home()), "Images (*.png *.jpg *.jpeg)"
        )
        if not path:
            return
        self._image_bytes = Path(path).read_bytes()
        pixmap = QPixmap(path)
        self.image_preview.setPixmap(
            pixmap.scaled(
                self.image_preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _choose_p12(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "PKCS#12 key store", str(Path.home()), "Key stores (*.p12 *.pfx)"
        )
        if path:
            self.p12_path.setText(path)

    def signature_image(self) -> bytes | None:
        """Render the chosen appearance to PNG bytes."""
        match self.tabs.currentIndex():
            case 0:
                return None if self.pad.is_empty else self.pad.to_png()
            case 1:
                if not self.typed_name.text():
                    return None
                from PySide6.QtCore import QBuffer

                image = QImage(760, 180, QImage.Format.Format_ARGB32)
                image.fill(Qt.GlobalColor.transparent)
                painter = QPainter(image)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)
                font = QFont(self.typed_font.currentText(), self.typed_size.value() * 2)
                font.setItalic(True)
                painter.setFont(font)
                painter.setPen(QColor("#101a3a"))
                painter.drawText(
                    image.rect(), Qt.AlignmentFlag.AlignCenter, self.typed_name.text()
                )
                painter.end()
                buffer = QBuffer()
                buffer.open(QBuffer.OpenModeFlag.WriteOnly)
                image.save(buffer, "PNG")
                return bytes(buffer.data())
            case 2:
                return self._image_bytes
        return None

    def _apply(self) -> None:
        service = SecurityService(self.document)
        page = self.page_spin.value() - 1
        width, height = self.document.page_size(page)
        rect = Rect(width - 300, height - 150, width - 60, height - 60)
        name = self.typed_name.text() or _current_user()

        if self.p12_path.text():
            try:
                target, _ = QFileDialog.getSaveFileName(
                    self,
                    "Save signed document",
                    str(Path.home() / f"{Path(self.document.display_name).stem}-signed.pdf"),
                    "PDF documents (*.pdf)",
                )
                if not target:
                    return
                service.sign(
                    target,
                    pkcs12_file=self.p12_path.text(),
                    pkcs12_password=self.p12_password.text(),
                    page=page,
                    rect=None if self.invisible.isChecked() else rect,
                    reason=self.reason.text(),
                    location=self.location.text(),
                    timestamp_url=self.timestamp_url.text(),
                )
                QMessageBox.information(
                    self, "Signed", f"Wrote a signed copy to {Path(target).name}."
                )
                self.accept()
                return
            except (DependencyMissingError, PdfStudioError) as exc:
                QMessageBox.warning(self, "Signing failed", str(exc))
                return

        if self.invisible.isChecked():
            QMessageBox.warning(
                self,
                "Certificate required",
                "An invisible signature needs a PKCS#12 certificate.",
            )
            return
        service.draw_signature_appearance(
            page,
            rect,
            name=name,
            reason=self.reason.text(),
            location=self.location.text(),
            image=self.signature_image(),
        )
        self.accept()


def _current_user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:
        return "Signer"
