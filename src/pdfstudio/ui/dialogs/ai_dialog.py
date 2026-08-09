"""AI dialogs: result viewer and chat-with-PDF."""

from __future__ import annotations

from typing import Any

from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.jobs import jobs
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.ui.dialogs.common import GuiInvoker

log = get_logger("ui.ai")


class AIResultDialog(QDialog):
    """Shows generated text with copy/save actions."""

    def __init__(self, title: str, text: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(720, 520)
        self._text = text

        layout = QVBoxLayout(self)
        self.view = QPlainTextEdit(text, self)
        self.view.setReadOnly(True)
        layout.addWidget(self.view, 1)

        buttons = QHBoxLayout()
        copy_button = QPushButton("Copy", self)
        save_button = QPushButton("Save as…", self)
        close_button = QPushButton("Close", self)
        close_button.setProperty("primary", True)
        copy_button.clicked.connect(self._copy)
        save_button.clicked.connect(self._save)
        close_button.clicked.connect(self.accept)
        buttons.addWidget(copy_button)
        buttons.addWidget(save_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _copy(self) -> None:
        QGuiApplication.clipboard().setText(self._text)

    def _save(self) -> None:
        from pathlib import Path

        from PySide6.QtWidgets import QFileDialog

        path, _ = QFileDialog.getSaveFileName(
            self, "Save text", str(Path.home() / "summary.txt"), "Text (*.txt);;Markdown (*.md)"
        )
        if path:
            Path(path).write_text(self._text, "utf-8")


class AIChatDialog(QDialog):
    """Conversational question answering grounded in the open document."""

    def __init__(self, assistant: Any, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Chat with PDF")
        self.resize(760, 620)
        self.assistant = assistant
        self._to_gui = GuiInvoker(self)

        layout = QVBoxLayout(self)
        self.transcript = QTextBrowser(self)
        self.transcript.setOpenExternalLinks(True)
        self.transcript.setHtml(
            "<p style='color:#888'>Ask a question about this document. "
            "Answers cite the pages they came from.</p>"
        )
        layout.addWidget(self.transcript, 1)

        row = QHBoxLayout()
        self.input = QLineEdit(self)
        self.input.setPlaceholderText("Ask a question…")
        self.send_button = QPushButton("Send", self)
        self.send_button.setProperty("primary", True)
        row.addWidget(self.input, 1)
        row.addWidget(self.send_button)
        layout.addLayout(row)

        suggestions = QHBoxLayout()
        for prompt in (
            "Summarise this document",
            "What are the key dates?",
            "List the action items",
            "Who are the parties?",
        ):
            button = QPushButton(prompt, self)
            button.clicked.connect(lambda _c=False, p=prompt: self._ask(p))
            suggestions.addWidget(button)
        layout.addLayout(suggestions)

        self.input.returnPressed.connect(self._send)
        self.send_button.clicked.connect(self._send)

    def _send(self) -> None:
        question = self.input.text().strip()
        if question:
            self.input.clear()
            self._ask(question)

    def _ask(self, question: str) -> None:
        self._append("You", question, "#3d7eff")
        self.send_button.setEnabled(False)
        self._append("Assistant", "Thinking…", "#9aa0aa")

        job = jobs().submit("AI question", self.assistant.ask, question, with_context=False)

        def finished(completed: Any) -> None:
            def deliver() -> None:
                self.send_button.setEnabled(True)
                error = completed.future.exception()
                if error is not None:
                    self._replace_last(f"<i style='color:#e5484d'>{error}</i>")
                    return
                response = completed.future.result()
                citations = "".join(
                    f"<div style='color:#9aa0aa;font-size:11px'>[page {c['page'] + 1}] "
                    f"{c['text'][:150]}…</div>"
                    for c in response.citations[:3]
                )
                self._replace_last(f"{response.text}{citations}")

            self._to_gui(deliver)

        job.add_done_callback(finished)

    def _append(self, speaker: str, text: str, colour: str) -> None:
        self.transcript.append(f"<p><b style='color:{colour}'>{speaker}:</b> {text}</p>")
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)

    def _replace_last(self, html: str) -> None:
        """Swap the placeholder "Thinking…" line for the real answer."""
        document = self.transcript.document()
        cursor = QTextCursor(document)
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.select(QTextCursor.SelectionType.BlockUnderCursor)
        cursor.removeSelectedText()
        self.transcript.append(f"<p><b style='color:#2fbf71'>Assistant:</b> {html}</p>")
        self.transcript.moveCursor(QTextCursor.MoveOperation.End)
