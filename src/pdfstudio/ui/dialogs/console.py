"""Embedded Python scripting console for automation and macros.

The console executes in the application process with the active document, the
engine services and the plugin API pre-imported, so users can automate anything
the GUI can do.  Because that is powerful, the console is explicit about the
risk and keeps a transcript that can be saved as a reusable macro.
"""

from __future__ import annotations

import contextlib
import io
import traceback
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QKeyEvent, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.logging_setup import get_logger

log = get_logger("ui.console")

BANNER = """PDF Studio scripting console
Available names:
  doc        the active PdfDocument (None if nothing is open)
  window     the main window
  pages, text, images, vectors, annots, forms, security, search, ai
  PdfDocument, Rect, Point, Color, TextStyle
Press Ctrl+Enter to run, Ctrl+S to save the script.
"""


class ScriptConsole(QDialog):
    """A REPL bound to the running application."""

    def __init__(self, window: Any) -> None:
        super().__init__(window)
        self.setWindowTitle("Script console")
        self.resize(880, 640)
        self.window = window

        layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Vertical, self)

        mono = QFont("monospace")
        mono.setStyleHint(QFont.StyleHint.Monospace)

        self.output = QPlainTextEdit(splitter)
        self.output.setReadOnly(True)
        self.output.setFont(mono)
        self.output.setPlainText(BANNER)

        self.editor = _Editor(self, splitter)
        self.editor.setFont(mono)
        self.editor.setPlaceholderText(
            "# Example: highlight every occurrence of 'invoice'\n"
            "for page in range(doc.page_count):\n"
            "    annots.highlight_text(page, 'invoice')\n"
            "window.refresh_after_edit()"
        )

        splitter.addWidget(self.output)
        splitter.addWidget(self.editor)
        splitter.setSizes([380, 240])
        layout.addWidget(splitter, 1)

        buttons = QHBoxLayout()
        run_button = QPushButton("Run  (Ctrl+Enter)", self)
        run_button.setProperty("primary", True)
        clear_button = QPushButton("Clear output", self)
        load_button = QPushButton("Load…", self)
        save_button = QPushButton("Save…", self)
        close_button = QPushButton("Close", self)
        run_button.clicked.connect(self.run_script)
        clear_button.clicked.connect(self.output.clear)
        load_button.clicked.connect(self._load)
        save_button.clicked.connect(self._save)
        close_button.clicked.connect(self.accept)
        for button in (run_button, clear_button, load_button, save_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        warning = QLabel(
            "Scripts run with full access to your documents and files. "
            "Only run code you trust.",
            self,
        )
        warning.setStyleSheet("color: #e8a33d;")
        layout.addWidget(warning)

    def _namespace(self) -> dict[str, Any]:
        """Build the execution namespace for a run."""
        from pdfstudio.ai.assistant import AIAssistant
        from pdfstudio.pdfengine.annotations import AnnotationService
        from pdfstudio.pdfengine.content import (
            ImageEditor,
            TextEditor,
            TextStyle,
            VectorEditor,
        )
        from pdfstudio.pdfengine.document import PdfDocument
        from pdfstudio.pdfengine.forms import FormService
        from pdfstudio.pdfengine.pages import PageService
        from pdfstudio.pdfengine.search import SearchService
        from pdfstudio.pdfengine.security import SecurityService
        from pdfstudio.pdfengine.types import Color, Point, Rect

        document = self.window.document
        namespace: dict[str, Any] = {
            "doc": document,
            "window": self.window,
            "PdfDocument": PdfDocument,
            "Rect": Rect,
            "Point": Point,
            "Color": Color,
            "TextStyle": TextStyle,
            "Path": Path,
        }
        if document is not None:
            namespace.update(
                pages=PageService(document),
                text=TextEditor(document),
                images=ImageEditor(document),
                vectors=VectorEditor(document),
                annots=AnnotationService(document),
                forms=FormService(document),
                security=SecurityService(document),
                search=SearchService(document),
                ai=AIAssistant(document),
            )
        return namespace

    def run_script(self) -> None:
        """Execute the editor contents, capturing stdout and exceptions."""
        source = self.editor.toPlainText().strip()
        if not source:
            return
        self.output.appendPlainText(
            f"\n>>> {source.splitlines()[0]}" + (" …" if "\n" in source else "")
        )
        namespace = self._namespace()
        buffer = io.StringIO()
        try:
            with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
                try:
                    # Show the value of a single expression, like a REPL.
                    compiled = compile(source, "<console>", "eval")
                    result = eval(compiled, namespace)  # noqa: S307 - user's own code
                    if result is not None:
                        print(repr(result))
                except SyntaxError:
                    exec(compile(source, "<console>", "exec"), namespace)  # noqa: S102
        except Exception:
            buffer.write(traceback.format_exc())
        text = buffer.getvalue()
        if text:
            self.output.appendPlainText(text.rstrip())
        self.output.moveCursor(QTextCursor.MoveOperation.End)
        if self.window.document is not None:
            self.window.refresh_after_edit()

    def _load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load script", str(Path.home()), "Python (*.py)"
        )
        if path:
            self.editor.setPlainText(Path(path).read_text("utf-8"))

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save script", str(Path.home() / "macro.py"), "Python (*.py)"
        )
        if path:
            Path(path).write_text(self.editor.toPlainText(), "utf-8")


class _Editor(QPlainTextEdit):
    """Editor that runs the script on Ctrl+Enter."""

    def __init__(self, console: ScriptConsole, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._console = console

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and (
            event.modifiers() & Qt.KeyboardModifier.ControlModifier
        ):
            self._console.run_script()
            event.accept()
            return
        if event.key() == Qt.Key.Key_Tab:
            self.insertPlainText("    ")
            event.accept()
            return
        super().keyPressEvent(event)
