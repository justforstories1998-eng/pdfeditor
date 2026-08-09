"""Optical character recognition with pluggable engines."""

from __future__ import annotations

from pdfstudio.ocr.engine import (
    EasyOCREngine,
    MuPdfEngine,
    OcrEngine,
    OcrResult,
    OcrService,
    OcrWord,
    PreprocessOptions,
    TesseractEngine,
    available_engines,
    get_engine,
    preprocess,
)

__all__ = [
    "EasyOCREngine",
    "MuPdfEngine",
    "OcrEngine",
    "OcrResult",
    "OcrService",
    "OcrWord",
    "PreprocessOptions",
    "TesseractEngine",
    "available_engines",
    "get_engine",
    "preprocess",
]
