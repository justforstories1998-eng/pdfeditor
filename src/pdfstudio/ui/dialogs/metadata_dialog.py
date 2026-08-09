"""Document properties / metadata editor including custom fields and XMP."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.pdfengine.document import PdfDocument


class MetadataDialog(QDialog):
    """Edit the info dictionary, custom properties and raw XMP."""

    def __init__(self, document: PdfDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Document properties")
        self.resize(660, 560)
        self.document = document
        metadata = document.metadata()

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        layout.addWidget(tabs, 1)

        # -- description tab -------------------------------------------------- #
        description = QWidget(tabs)
        form = QFormLayout(description)
        self.title = QLineEdit(metadata.title, description)
        self.author = QLineEdit(metadata.author, description)
        self.subject = QLineEdit(metadata.subject, description)
        self.keywords = QLineEdit(metadata.keywords, description)
        self.creator = QLineEdit(metadata.creator, description)
        self.producer = QLineEdit(metadata.producer, description)
        for label, widget in (
            ("Title:", self.title),
            ("Author:", self.author),
            ("Subject:", self.subject),
            ("Keywords:", self.keywords),
            ("Creator:", self.creator),
            ("Producer:", self.producer),
        ):
            form.addRow(label, widget)
        form.addRow("Created:", _readonly(metadata.creation_date))
        form.addRow("Modified:", _readonly(metadata.modification_date))
        tabs.addTab(description, "Description")

        # -- custom fields tab -------------------------------------------------- #
        custom = QWidget(tabs)
        custom_layout = QVBoxLayout(custom)
        self.custom_table = QTableWidget(0, 2, custom)
        self.custom_table.setHorizontalHeaderLabels(["Key", "Value"])
        self.custom_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        for key, value in metadata.custom.items():
            self._add_custom_row(key, value)
        custom_layout.addWidget(self.custom_table, 1)
        buttons = QHBoxLayout()
        add_button = QPushButton("Add", custom)
        remove_button = QPushButton("Remove", custom)
        add_button.clicked.connect(lambda: self._add_custom_row("", ""))
        remove_button.clicked.connect(
            lambda: self.custom_table.removeRow(self.custom_table.currentRow())
        )
        buttons.addWidget(add_button)
        buttons.addWidget(remove_button)
        buttons.addStretch(1)
        custom_layout.addLayout(buttons)
        tabs.addTab(custom, "Custom")

        # -- statistics tab -------------------------------------------------- #
        stats = QWidget(tabs)
        stats_form = QFormLayout(stats)
        for key, value in document.statistics().items():
            if key == "permissions":
                continue
            stats_form.addRow(f"{key.replace('_', ' ').title()}:", _readonly(str(value)))
        tabs.addTab(stats, "Statistics")

        # -- XMP tab -------------------------------------------------- #
        xmp = QWidget(tabs)
        xmp_layout = QVBoxLayout(xmp)
        self.xmp_edit = QPlainTextEdit(metadata.xmp, xmp)
        self.xmp_edit.setPlaceholderText("No XMP metadata")
        xmp_layout.addWidget(self.xmp_edit)
        tabs.addTab(xmp, "XMP")

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.Reset
        )
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.Reset).clicked.connect(self._clear)
        layout.addWidget(box)

    def _add_custom_row(self, key: str, value: str) -> None:
        row = self.custom_table.rowCount()
        self.custom_table.insertRow(row)
        self.custom_table.setItem(row, 0, QTableWidgetItem(key))
        self.custom_table.setItem(row, 1, QTableWidgetItem(value))

    def _clear(self) -> None:
        if (
            QMessageBox.question(
                self, "Clear metadata", "Remove all metadata from this document?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.document.clear_metadata()
            self.accept()

    def _save(self) -> None:
        metadata = self.document.metadata()
        metadata.title = self.title.text()
        metadata.author = self.author.text()
        metadata.subject = self.subject.text()
        metadata.keywords = self.keywords.text()
        metadata.creator = self.creator.text()
        metadata.producer = self.producer.text()
        metadata.xmp = self.xmp_edit.toPlainText()
        custom: dict[str, str] = {}
        for row in range(self.custom_table.rowCount()):
            key_item = self.custom_table.item(row, 0)
            value_item = self.custom_table.item(row, 1)
            if key_item and key_item.text().strip():
                custom[key_item.text().strip()] = value_item.text() if value_item else ""
        metadata.custom = custom
        self.document.set_metadata(metadata)
        self.accept()


def _readonly(text: str) -> QLineEdit:
    field = QLineEdit(text)
    field.setReadOnly(True)
    field.setStyleSheet("color: palette(mid);")
    return field
