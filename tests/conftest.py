"""Shared pytest fixtures.

Every test runs against an isolated ``PDFSTUDIO_HOME`` so the developer's real
settings, cache and autosave directories are never touched.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def isolated_home() -> Iterator[Path]:
    """Redirect all application state into a temporary directory."""
    root = Path(tempfile.mkdtemp(prefix="pdfstudio-tests-"))
    os.environ["PDFSTUDIO_HOME"] = str(root)
    os.environ["PDFSTUDIO_NO_RECOVERY_PROMPT"] = "1"
    os.environ["PDFSTUDIO_DISCARD_ON_CLOSE"] = "1"
    os.environ["PDFSTUDIO_NO_DIALOGS"] = "1"
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from pdfstudio.core.paths import app_paths

    app_paths.cache_clear()
    from pdfstudio.core.logging_setup import setup_logging

    setup_logging(level="WARNING", console=False)
    yield root
    shutil.rmtree(root, ignore_errors=True)


@pytest.fixture(autouse=True)
def _shutdown_jobs() -> Iterator[None]:
    """Make sure no background job leaks between tests."""
    yield
    from pdfstudio.core.jobs import jobs

    jobs().cancel_all()


@pytest.fixture
def tmp_pdf(tmp_path: Path) -> Path:
    """A small three-page PDF with searchable text."""
    from pdfstudio.pdfengine.content import TextEditor, TextStyle
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.types import Rect

    document = PdfDocument.create(pages=3)
    editor = TextEditor(document)
    for index in range(3):
        editor.add(
            index,
            Rect(60, 60, 520, 160),
            f"Page {index + 1} heading\nInvoice 100{index} total {100 * (index + 1)}.00 GBP",
            TextStyle(size=14),
        )
    target = tmp_path / "sample.pdf"
    document.save_as(target)
    document.close()
    return target


@pytest.fixture
def document(tmp_pdf: Path) -> Iterator[PdfDocument]:  # noqa: F821
    """An open :class:`PdfDocument` for the sample file."""
    from pdfstudio.pdfengine.document import PdfDocument

    doc = PdfDocument.open(tmp_pdf)
    yield doc
    doc.close()


@pytest.fixture
def blank_document() -> Iterator[PdfDocument]:  # noqa: F821
    from pdfstudio.pdfengine.document import PdfDocument

    doc = PdfDocument.create()
    yield doc
    doc.close()


@pytest.fixture
def scanned_pdf(tmp_path: Path) -> Path:
    """A page containing only a rasterised image of text (needs OCR)."""
    from pdfstudio.pdfengine.content import ImageEditor, TextEditor, TextStyle
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.types import Rect

    source = PdfDocument.create()
    TextEditor(source).add(
        0,
        Rect(60, 60, 520, 300),
        "Scanned Invoice 2026\nTotal amount due: 1250.00 GBP\nCustomer: Ada Lovelace",
        TextStyle(size=22),
    )
    with source.locked() as handle:
        pixels = handle[0].get_pixmap(dpi=150).tobytes("png")
    source.close()

    scan = PdfDocument.create()
    ImageEditor(scan).insert(0, Rect(0, 0, 595, 842), pixels)
    target = tmp_path / "scan.pdf"
    scan.save_as(target)
    scan.close()
    return target


@pytest.fixture
def sample_image(tmp_path: Path) -> Path:
    from PIL import Image

    path = tmp_path / "image.png"
    Image.new("RGB", (640, 480), (40, 110, 200)).save(path)
    return path


@pytest.fixture(scope="session")
def qapp() -> Iterator[QApplication]:  # noqa: F821
    """A single ``QApplication`` shared by every GUI test."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    yield app
    app.processEvents()
