"""Print dialog with preview, scaling, duplex, booklet and poster modes."""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import parse_page_ranges

log = get_logger("ui.print")


class PrintDialog(QDialog):
    """Collects print settings and hands the job to Qt's print system."""

    def __init__(self, document: PdfDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Print")
        self.resize(720, 620)
        self.document = document

        layout = QHBoxLayout(self)

        options = QWidget(self)
        form = QFormLayout(options)

        self.printer_combo = QComboBox(options)
        self.printer_combo.addItems(_printer_names())

        self.range_edit = QLineEdit("all", options)
        self.range_edit.setToolTip("e.g. all, 1-5, 2,4,9-, even, odd")

        self.copies = QSpinBox(options)
        self.copies.setRange(1, 999)

        self.mode = QComboBox(options)
        self.mode.addItems(
            ["Normal", "Booklet", "Poster (2×2)", "Poster (3×3)", "2-up", "4-up"]
        )

        self.scaling = QComboBox(options)
        self.scaling.addItems(["Fit to page", "Actual size", "Shrink oversized", "Custom"])

        self.custom_scale = QSpinBox(options)
        self.custom_scale.setRange(10, 400)
        self.custom_scale.setValue(100)
        self.custom_scale.setSuffix("%")
        self.custom_scale.setEnabled(False)

        self.duplex = QComboBox(options)
        self.duplex.addItems(["Single sided", "Long edge (portrait)", "Short edge (landscape)"])

        self.colour = QComboBox(options)
        self.colour.addItems(["Colour", "Greyscale"])

        self.dpi = QComboBox(options)
        self.dpi.addItems(["150", "300", "600", "1200"])
        self.dpi.setCurrentText("300")

        self.annotations = QCheckBox("Print comments", options)
        self.annotations.setChecked(True)
        self.auto_rotate = QCheckBox("Auto-rotate and centre", options)
        self.auto_rotate.setChecked(True)
        self.reverse = QCheckBox("Reverse order", options)

        form.addRow("Printer:", self.printer_combo)
        form.addRow("Pages:", self.range_edit)
        form.addRow("Copies:", self.copies)
        form.addRow("Mode:", self.mode)
        form.addRow("Scaling:", self.scaling)
        form.addRow("Custom scale:", self.custom_scale)
        form.addRow("Two-sided:", self.duplex)
        form.addRow("Colour:", self.colour)
        form.addRow("Resolution:", self.dpi)
        form.addRow("", self.annotations)
        form.addRow("", self.auto_rotate)
        form.addRow("", self.reverse)
        layout.addWidget(options, 1)

        preview_box = QVBoxLayout()
        self.preview = QLabel(self)
        self.preview.setMinimumSize(280, 380)
        self.preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview.setStyleSheet("border: 1px solid palette(mid); background: #333;")
        self.preview_page = QSpinBox(self)
        self.preview_page.setRange(1, max(1, document.page_count))
        self.preview_page.valueChanged.connect(self._update_preview)
        preview_box.addWidget(QLabel("Preview", self))
        preview_box.addWidget(self.preview, 1)
        preview_box.addWidget(self.preview_page)

        self.summary = QLabel("", self)
        self.summary.setWordWrap(True)
        self.summary.setStyleSheet("color: palette(mid);")
        preview_box.addWidget(self.summary)

        box = QDialogButtonBox(self)
        print_button = box.addButton("Print", QDialogButtonBox.ButtonRole.AcceptRole)
        print_button.setProperty("primary", True)
        pdf_button = box.addButton("Print to PDF…", QDialogButtonBox.ButtonRole.ActionRole)
        box.addButton(QDialogButtonBox.StandardButton.Cancel)
        print_button.clicked.connect(self._print)
        pdf_button.clicked.connect(self._print_to_pdf)
        box.rejected.connect(self.reject)
        preview_box.addWidget(box)
        layout.addLayout(preview_box)

        self.scaling.currentTextChanged.connect(
            lambda text: self.custom_scale.setEnabled(text == "Custom")
        )
        self.range_edit.textChanged.connect(self._update_summary)
        self._update_preview()
        self._update_summary()

    # -- preview ---------------------------------------------------------------- #
    def _update_preview(self) -> None:
        index = self.preview_page.value() - 1
        try:
            with self.document.locked() as handle:
                pixmap_data = handle[index].get_pixmap(dpi=52).tobytes("png")
        except Exception:
            return
        pixmap = QPixmap()
        pixmap.loadFromData(pixmap_data)
        self.preview.setPixmap(
            pixmap.scaled(
                self.preview.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )

    def _update_summary(self) -> None:
        try:
            pages = parse_page_ranges(self.range_edit.text(), self.document.page_count)
        except ValueError:
            self.summary.setText("Invalid page range")
            return
        sheets = len(pages) * self.copies.value()
        if "2-up" in self.mode.currentText():
            sheets = -(-sheets // 2)
        elif "4-up" in self.mode.currentText():
            sheets = -(-sheets // 4)
        if self.duplex.currentIndex() > 0:
            sheets = -(-sheets // 2)
        self.summary.setText(f"{len(pages)} page(s) → about {sheets} sheet(s)")

    def selected_pages(self) -> list[int]:
        try:
            return parse_page_ranges(self.range_edit.text(), self.document.page_count)
        except ValueError:
            return list(range(self.document.page_count))

    # -- printing ---------------------------------------------------------------- #
    def _print(self) -> None:
        try:
            from PySide6.QtPrintSupport import QPrintDialog, QPrinter
        except ImportError:
            QMessageBox.warning(
                self,
                "Printing unavailable",
                "Qt print support is not installed.\n\n"
                "Install PySide6-Addons, or use “Print to PDF”.",
            )
            return

        printer = QPrinter(QPrinter.PrinterMode.HighResolution)
        printer.setDocName(self.document.display_name)
        printer.setCopyCount(self.copies.value())
        if self.colour.currentText() == "Greyscale":
            printer.setColorMode(QPrinter.ColorMode.GrayScale)
        if self.duplex.currentIndex() == 1:
            printer.setDuplex(QPrinter.DuplexMode.DuplexLongSide)
        elif self.duplex.currentIndex() == 2:
            printer.setDuplex(QPrinter.DuplexMode.DuplexShortSide)

        dialog = QPrintDialog(printer, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._render_to_printer(printer)
        self.accept()

    def _render_to_printer(self, printer: Any) -> None:
        """Paint each selected page onto the printer device."""
        pages = self.selected_pages()
        if self.reverse.isChecked():
            pages.reverse()
        dpi = int(self.dpi.currentText())
        painter = QPainter()
        if not painter.begin(printer):
            QMessageBox.warning(self, "Print", "Could not start the print job.")
            return
        try:
            page_rect = printer.pageRect(printer.Unit.DevicePixel)
            for n, index in enumerate(pages):
                if n:
                    printer.newPage()
                with self.document.locked() as handle:
                    data = (
                        handle[index]
                        .get_pixmap(dpi=dpi, annots=self.annotations.isChecked())
                        .tobytes("png")
                    )
                image = QImage()
                image.loadFromData(data)
                scaled = image.scaled(
                    int(page_rect.width()),
                    int(page_rect.height()),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
                x = int((page_rect.width() - scaled.width()) / 2)
                y = int((page_rect.height() - scaled.height()) / 2)
                painter.drawImage(x, y, scaled)
        finally:
            painter.end()
        log.info("Printed {} page(s)", len(pages))

    def _print_to_pdf(self) -> None:
        """Export the selected pages as a new PDF (works without a printer)."""
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        from pdfstudio.pdfengine.pages import PageService, make_booklet, n_up

        path, _ = QFileDialog.getSaveFileName(
            self,
            "Print to PDF",
            str(Path.home() / f"{Path(self.document.display_name).stem}-print.pdf"),
            "PDF documents (*.pdf)",
        )
        if not path:
            return
        pages = self.selected_pages()
        service = PageService(self.document)
        output = service.extract(pages)
        mode = self.mode.currentText()
        if mode == "Booklet":
            output = make_booklet(output)
        elif mode == "2-up":
            output = n_up(output, 2, 1, landscape=True)
        elif mode == "4-up":
            output = n_up(output, 2, 2)
        elif mode.startswith("Poster"):
            grid = 2 if "2×2" in mode else 3
            output = _poster(output, grid)
        output.save_as(path)
        output.close()
        QMessageBox.information(self, "Print to PDF", f"Wrote {Path(path).name}.")
        self.accept()


def _poster(document: PdfDocument, grid: int) -> PdfDocument:
    """Split every page into a ``grid × grid`` set of tiles for poster printing."""
    import pymupdf as fitz

    out = fitz.open()
    with document.locked() as source:
        for index in range(source.page_count):
            rect = source[index].rect
            tile_w = rect.width / grid
            tile_h = rect.height / grid
            for row in range(grid):
                for column in range(grid):
                    page = out.new_page(width=tile_w, height=tile_h)
                    clip = fitz.Rect(
                        column * tile_w,
                        row * tile_h,
                        (column + 1) * tile_w,
                        (row + 1) * tile_h,
                    )
                    page.show_pdf_page(page.rect, source, index, clip=clip)
        data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return PdfDocument.from_bytes(data, name="poster.pdf")


def _printer_names() -> list[str]:
    try:
        from PySide6.QtPrintSupport import QPrinterInfo

        names = [info.printerName() for info in QPrinterInfo.availablePrinters()]
        return names or ["No printers found"]
    except ImportError:
        return ["Print to PDF only (QtPrintSupport not installed)"]
