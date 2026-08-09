"""Batch processing dialog: build a pipeline and run it over many files."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.jobs import JobContext, jobs
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.services.batch import (
    BatchProcessor,
    BatesOperation,
    CompressOperation,
    ConvertOperation,
    DecryptOperation,
    EncryptOperation,
    ExtractOperation,
    OcrOperation,
    RenameRule,
    SanitizeOperation,
    WatermarkOperation,
)
from pdfstudio.ui.dialogs.common import GuiInvoker

log = get_logger("ui.batch")


class BatchDialog(QDialog):
    """Choose files, compose operations, run and review the results."""

    OPERATIONS = (
        "OCR",
        "Watermark",
        "Compress",
        "Encrypt",
        "Decrypt",
        "Sanitise",
        "Bates numbering",
        "Convert",
        "Extract text",
        "Extract images",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Batch processing")
        self.resize(880, 700)
        self._job: Any = None
        self._to_gui = GuiInvoker(self)

        layout = QVBoxLayout(self)

        # -- files ---------------------------------------------------------- #
        files_group = QGroupBox("Input files", self)
        files_layout = QVBoxLayout(files_group)
        self.files = QListWidget(files_group)
        self.files.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        files_layout.addWidget(self.files, 1)
        file_buttons = QHBoxLayout()
        add_button = QPushButton("Add files…", files_group)
        folder_button = QPushButton("Add folder…", files_group)
        remove_button = QPushButton("Remove", files_group)
        clear_button = QPushButton("Clear", files_group)
        add_button.clicked.connect(self._add_files)
        folder_button.clicked.connect(self._add_folder)
        remove_button.clicked.connect(self._remove_selected)
        clear_button.clicked.connect(self.files.clear)
        for button in (add_button, folder_button, remove_button, clear_button):
            file_buttons.addWidget(button)
        file_buttons.addStretch(1)
        files_layout.addLayout(file_buttons)
        layout.addWidget(files_group, 1)

        # -- pipeline ---------------------------------------------------------- #
        pipeline_group = QGroupBox("Operations", self)
        pipeline_layout = QHBoxLayout(pipeline_group)

        self.available = QListWidget(pipeline_group)
        self.available.addItems(self.OPERATIONS)
        self.pipeline = QListWidget(pipeline_group)
        self.pipeline.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)

        middle = QVBoxLayout()
        add_op = QPushButton("→", pipeline_group)
        remove_op = QPushButton("←", pipeline_group)
        add_op.clicked.connect(self._add_operation)
        remove_op.clicked.connect(lambda: self.pipeline.takeItem(self.pipeline.currentRow()))
        middle.addStretch(1)
        middle.addWidget(add_op)
        middle.addWidget(remove_op)
        middle.addStretch(1)

        pipeline_layout.addWidget(self.available, 1)
        pipeline_layout.addLayout(middle)
        pipeline_layout.addWidget(self.pipeline, 1)
        layout.addWidget(pipeline_group, 1)

        # -- options ---------------------------------------------------------- #
        options = QGroupBox("Output", self)
        form = QFormLayout(options)
        output_row = QHBoxLayout()
        self.output_dir = QLineEdit(str(Path.home() / "PDF Studio batch"), options)
        browse = QPushButton("Browse…", options)
        browse.clicked.connect(self._choose_output)
        output_row.addWidget(self.output_dir, 1)
        output_row.addWidget(browse)
        form.addRow("Folder:", output_row)

        self.rename = QLineEdit("{stem}", options)
        self.rename.setToolTip(
            "Tokens: {stem} {name} {index} {counter} {date} {time} {pages} {title} {author} {bates}"
        )
        form.addRow("File name:", self.rename)

        self.watermark_text = QLineEdit("CONFIDENTIAL", options)
        form.addRow("Watermark text:", self.watermark_text)

        self.password = QLineEdit(options)
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Encryption password:", self.password)

        self.convert_format = QComboBox(options)
        self.convert_format.addItems(
            ["png", "jpg", "tiff", "txt", "html", "md", "docx", "pptx"]
        )
        form.addRow("Convert to:", self.convert_format)

        self.profile = QComboBox(options)
        self.profile.addItems(["screen", "ebook", "print", "prepress", "maximum"])
        self.profile.setCurrentText("ebook")
        form.addRow("Compression profile:", self.profile)

        self.continue_on_error = QCheckBox("Continue after errors", options)
        self.continue_on_error.setChecked(True)
        form.addRow("", self.continue_on_error)
        layout.addWidget(options)

        # -- progress ---------------------------------------------------------- #
        self.progress = QProgressBar(self)
        self.progress.setVisible(False)
        layout.addWidget(self.progress)
        self.log_view = QPlainTextEdit(self)
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumHeight(120)
        layout.addWidget(self.log_view)

        box = QDialogButtonBox(self)
        self.run_button = box.addButton("Run", QDialogButtonBox.ButtonRole.AcceptRole)
        self.run_button.setProperty("primary", True)
        self.cancel_button = box.addButton("Close", QDialogButtonBox.ButtonRole.RejectRole)
        self.run_button.clicked.connect(self._run)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(box)

    # -- helpers -------------------------------------------------------------- #
    def _add_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Add PDFs", str(Path.home()), "PDF documents (*.pdf)"
        )
        self.files.addItems(paths)

    def _add_folder(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Add folder", str(Path.home()))
        if directory:
            self.files.addItems(sorted(str(p) for p in Path(directory).rglob("*.pdf")))

    def _remove_selected(self) -> None:
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))

    def _choose_output(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Output folder", self.output_dir.text()
        )
        if directory:
            self.output_dir.setText(directory)

    def _add_operation(self) -> None:
        item = self.available.currentItem()
        if item:
            self.pipeline.addItem(QListWidgetItem(item.text()))

    def _build_processor(self) -> BatchProcessor:
        processor = BatchProcessor(
            output_dir=self.output_dir.text(),
            rename=RenameRule(self.rename.text() or "{stem}"),
            continue_on_error=self.continue_on_error.isChecked(),
        )
        for row in range(self.pipeline.count()):
            match self.pipeline.item(row).text():
                case "OCR":
                    processor.add(OcrOperation())
                case "Watermark":
                    processor.add(WatermarkOperation(self.watermark_text.text()))
                case "Compress":
                    processor.add(CompressOperation(self.profile.currentText()))
                case "Encrypt":
                    processor.add(EncryptOperation(user_password=self.password.text()))
                case "Decrypt":
                    processor.add(DecryptOperation())
                case "Sanitise":
                    processor.add(SanitizeOperation())
                case "Bates numbering":
                    processor.add(BatesOperation())
                case "Convert":
                    processor.add(ConvertOperation(self.convert_format.currentText()))
                case "Extract text":
                    processor.add(ExtractOperation("text"))
                case "Extract images":
                    processor.add(ExtractOperation("images"))
        return processor

    def _run(self) -> None:
        files = [self.files.item(i).text() for i in range(self.files.count())]
        if not files:
            QMessageBox.warning(self, "Batch", "Add at least one file.")
            return
        if self.pipeline.count() == 0:
            QMessageBox.warning(self, "Batch", "Add at least one operation.")
            return
        processor = self._build_processor()
        self.run_button.setEnabled(False)
        self.progress.setVisible(True)
        self.progress.setRange(0, len(files))
        self.log_view.clear()
        self.log_view.appendPlainText(
            f"Running {' → '.join(processor.describe())} over {len(files)} file(s)…"
        )

        def work(ctx: JobContext) -> Any:
            return processor.run(files, ctx=ctx)

        job = jobs().submit("Batch processing", work, with_context=True)

        def finished(completed: Any) -> None:
            def deliver() -> None:
                self.run_button.setEnabled(True)
                self.progress.setVisible(False)
                error = completed.future.exception()
                if error is not None:
                    self.log_view.appendPlainText(f"Failed: {error}")
                    return
                result = completed.future.result()
                self.log_view.appendPlainText(result.summary())
                for item in result.items:
                    status = "ok" if item.success else f"FAILED — {item.error}"
                    self.log_view.appendPlainText(f"  {item.source.name}: {status}")

            self._to_gui(deliver)

        job.add_done_callback(finished)
        self._job = job
