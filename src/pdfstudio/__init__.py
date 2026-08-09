"""PDF Studio — a professional-grade, modular PDF editor written in Python.

The package is organised in clean layers::

    pdfstudio.core       Cross-cutting infrastructure (config, logging, undo, jobs)
    pdfstudio.pdfengine  Document model + all PDF manipulation services
    pdfstudio.render     Rasterisation, tiling and caching
    pdfstudio.ocr        Optical character recognition back-ends
    pdfstudio.ai         Document intelligence (summaries, Q&A, tagging)
    pdfstudio.services   Application level services (recent files, batch, session)
    pdfstudio.plugins    Plugin API + loader
    pdfstudio.ui         PySide6 presentation layer (views + view-models)

Only :mod:`pdfstudio.ui` depends on Qt; every other layer is head-less and can be
driven from the CLI, a REST worker or a test-suite.
"""

from __future__ import annotations

__all__ = ["APP_ID", "APP_NAME", "ORGANISATION", "__version__"]

__version__ = "1.4.0"
APP_NAME = "PDF Studio"
APP_ID = "org.pdfstudio.PDFStudio"
ORGANISATION = "PDF Studio Project"
