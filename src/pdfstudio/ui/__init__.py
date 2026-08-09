"""The PySide6 presentation layer.

Importing this package pulls in Qt; the head-less layers never do.
"""

from __future__ import annotations

__all__ = ["DocumentController", "MainWindow", "ThemeManager"]


def __getattr__(name: str) -> object:
    """Import the heavy Qt widgets lazily so ``import pdfstudio`` stays fast."""
    if name == "MainWindow":
        from pdfstudio.ui.main_window import MainWindow

        return MainWindow
    if name == "DocumentController":
        from pdfstudio.ui.controller import DocumentController

        return DocumentController
    if name == "ThemeManager":
        from pdfstudio.ui.theme import ThemeManager

        return ThemeManager
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
