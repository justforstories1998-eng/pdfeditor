"""The PDF engine: document model and every manipulation service.

This layer is completely independent of Qt, so it can be used from scripts,
servers, notebooks and the CLI as well as from the desktop application.
"""

from __future__ import annotations

from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.types import (
    Annotation,
    AnnotationType,
    Attachment,
    Bookmark,
    Color,
    ConformanceLevel,
    DocumentMetadata,
    EncryptionMethod,
    FieldType,
    FormField,
    ImageInfo,
    Layer,
    Link,
    PageInfo,
    PageLayout,
    Point,
    Rect,
    SearchHit,
    SearchQuery,
    TextBlock,
    ZoomMode,
    format_page_ranges,
    parse_page_ranges,
)

__all__ = [
    "Annotation",
    "AnnotationType",
    "Attachment",
    "Bookmark",
    "Color",
    "ConformanceLevel",
    "DocumentMetadata",
    "EncryptionMethod",
    "FieldType",
    "FormField",
    "ImageInfo",
    "Layer",
    "Link",
    "PageInfo",
    "PageLayout",
    "PdfDocument",
    "Point",
    "Rect",
    "SaveOptions",
    "SearchHit",
    "SearchQuery",
    "TextBlock",
    "ZoomMode",
    "format_page_ranges",
    "parse_page_ranges",
]
