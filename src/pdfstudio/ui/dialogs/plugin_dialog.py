"""Plugin manager dialog: enable, disable, reload and inspect plugins."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.exceptions import PluginError
from pdfstudio.core.paths import app_paths
from pdfstudio.plugins.manager import PluginManager


class PluginDialog(QDialog):
    """Table of installed plugins with lifecycle controls."""

    def __init__(self, manager: PluginManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Plugins")
        self.resize(820, 560)
        self.manager = manager

        layout = QVBoxLayout(self)

        self.table = QTableWidget(0, 5, self)
        self.table.setHorizontalHeaderLabels(["", "Name", "Version", "Author", "Commands"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.currentCellChanged.connect(lambda *_: self._show_details())
        layout.addWidget(self.table, 1)

        self.details = QPlainTextEdit(self)
        self.details.setReadOnly(True)
        self.details.setMaximumHeight(140)
        layout.addWidget(self.details)

        buttons = QHBoxLayout()
        for label, handler in (
            ("Enable", self._enable),
            ("Disable", self._disable),
            ("Reload", self._reload),
            ("Install from file…", self._install),
            ("Open folder", self._open_folder),
            ("Rescan", self._rescan),
        ):
            button = QPushButton(label, self)
            button.clicked.connect(handler)
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        box = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, self)
        box.rejected.connect(self.accept)
        layout.addWidget(box)

        self.reload_table()

    # -- table ---------------------------------------------------------------- #
    def reload_table(self) -> None:
        rows = self.manager.status()
        self.table.setRowCount(len(rows))
        for row, info in enumerate(rows):
            state = QTableWidgetItem("●" if info["enabled"] else "○")
            state.setToolTip("Enabled" if info["enabled"] else "Disabled")
            state.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(row, 0, state)
            name = QTableWidgetItem(info["name"])
            name.setData(Qt.ItemDataRole.UserRole, info["identifier"])
            self.table.setItem(row, 1, name)
            self.table.setItem(row, 2, QTableWidgetItem(info["version"]))
            self.table.setItem(row, 3, QTableWidgetItem(info["author"]))
            self.table.setItem(row, 4, QTableWidgetItem(str(info["commands"])))
        self.table.resizeColumnToContents(0)
        if rows:
            self.table.selectRow(0)

    def _selected_identifier(self) -> str:
        row = self.table.currentRow()
        item = self.table.item(row, 1) if row >= 0 else None
        return item.data(Qt.ItemDataRole.UserRole) if item else ""

    def _show_details(self) -> None:
        identifier = self._selected_identifier()
        info = next((i for i in self.manager.status() if i["identifier"] == identifier), None)
        if info is None:
            self.details.clear()
            return
        lines = [
            f"{info['name']} {info['version']}",
            f"Identifier: {info['identifier']}",
            f"Source: {info['source']}",
            f"Permissions: {', '.join(info['permissions']) or 'none'}",
            "",
            info["description"],
        ]
        if info["error"]:
            lines += ["", f"Error: {info['error']}"]
        self.details.setPlainText("\n".join(lines))

    # -- actions ---------------------------------------------------------------- #
    def _enable(self) -> None:
        identifier = self._selected_identifier()
        if identifier and not self.manager.enable(identifier):
            QMessageBox.warning(
                self, "Plugin", "The plugin failed to activate — see the details pane."
            )
        self.reload_table()

    def _disable(self) -> None:
        identifier = self._selected_identifier()
        if identifier:
            self.manager.disable(identifier)
        self.reload_table()

    def _reload(self) -> None:
        identifier = self._selected_identifier()
        if not identifier:
            return
        try:
            self.manager.reload(identifier)
        except PluginError as exc:
            QMessageBox.warning(self, "Reload failed", str(exc))
        self.reload_table()

    def _install(self) -> None:
        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getOpenFileName(
            self, "Install plugin", str(Path.home()), "Python files (*.py)"
        )
        if not path:
            return
        target = app_paths().ensure().plugins / Path(path).name
        target.write_bytes(Path(path).read_bytes())
        try:
            self.manager.load_path(target)
        except PluginError as exc:
            QMessageBox.warning(self, "Install failed", str(exc))
        self.reload_table()

    def _rescan(self) -> None:
        self.manager.load_all()
        self.reload_table()

    def _open_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_paths().plugins)))
