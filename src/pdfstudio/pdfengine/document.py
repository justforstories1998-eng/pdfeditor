"""The central :class:`PdfDocument` — a thread-safe wrapper around PyMuPDF.

Responsibilities
----------------
* Open (optionally encrypted / linearised / portfolio) documents, in-memory
  buffers, or brand new blank documents.
* Expose page metadata, text, images, drawings, layers, bookmarks, links,
  attachments and metadata in the backend-neutral types from
  :mod:`pdfstudio.pdfengine.types`.
* Provide save / save-as / incremental-save with all optimisation switches.
* Own the document's :class:`~pdfstudio.core.undo.UndoStack` and publish
  modification events on the bus.

Thread safety
-------------
MuPDF documents are *not* thread safe.  Every access goes through
:attr:`PdfDocument.lock` (an ``RLock``); render workers acquire the same lock.
Use the :meth:`PdfDocument.locked` context manager when performing multi-step
operations that must be atomic.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Self

import pymupdf as fitz

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.exceptions import (
    DocumentError,
    PasswordRequiredError,
    PermissionDeniedError,
)
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.undo import UndoStack
from pdfstudio.pdfengine.types import (
    Annotation,
    AnnotationType,
    Attachment,
    Bookmark,
    Color,
    DocumentMetadata,
    DrawingPath,
    EncryptionMethod,
    ImageInfo,
    Layer,
    Link,
    PageInfo,
    Permission,
    Point,
    Rect,
    Rotation,
    TextBlock,
    TextLine,
    TextSpan,
)

log = get_logger("document")

_ENCRYPTION_MAP: dict[EncryptionMethod, int] = {
    EncryptionMethod.NONE: fitz.PDF_ENCRYPT_NONE,
    EncryptionMethod.RC4_40: fitz.PDF_ENCRYPT_RC4_40,
    EncryptionMethod.RC4_128: fitz.PDF_ENCRYPT_RC4_128,
    EncryptionMethod.AES_128: fitz.PDF_ENCRYPT_AES_128,
    EncryptionMethod.AES_256: fitz.PDF_ENCRYPT_AES_256,
}

_ANNOT_TYPE_MAP: dict[int, AnnotationType] = {
    fitz.PDF_ANNOT_TEXT: AnnotationType.TEXT,
    fitz.PDF_ANNOT_LINK: AnnotationType.WIDGET,
    fitz.PDF_ANNOT_FREE_TEXT: AnnotationType.FREE_TEXT,
    fitz.PDF_ANNOT_LINE: AnnotationType.LINE,
    fitz.PDF_ANNOT_SQUARE: AnnotationType.SQUARE,
    fitz.PDF_ANNOT_CIRCLE: AnnotationType.CIRCLE,
    fitz.PDF_ANNOT_POLYGON: AnnotationType.POLYGON,
    fitz.PDF_ANNOT_POLY_LINE: AnnotationType.POLYLINE,
    fitz.PDF_ANNOT_HIGHLIGHT: AnnotationType.HIGHLIGHT,
    fitz.PDF_ANNOT_UNDERLINE: AnnotationType.UNDERLINE,
    fitz.PDF_ANNOT_SQUIGGLY: AnnotationType.SQUIGGLY,
    fitz.PDF_ANNOT_STRIKE_OUT: AnnotationType.STRIKEOUT,
    fitz.PDF_ANNOT_STAMP: AnnotationType.STAMP,
    fitz.PDF_ANNOT_CARET: AnnotationType.CARET,
    fitz.PDF_ANNOT_INK: AnnotationType.INK,
    fitz.PDF_ANNOT_POPUP: AnnotationType.POPUP,
    fitz.PDF_ANNOT_FILE_ATTACHMENT: AnnotationType.FILE_ATTACHMENT,
    fitz.PDF_ANNOT_SOUND: AnnotationType.SOUND,
    fitz.PDF_ANNOT_MOVIE: AnnotationType.MOVIE,
    fitz.PDF_ANNOT_WIDGET: AnnotationType.WIDGET,
    fitz.PDF_ANNOT_REDACT: AnnotationType.REDACT,
}


def _to_rect(r: Any) -> Rect:
    return Rect(float(r[0]), float(r[1]), float(r[2]), float(r[3]))


def _to_fitz_rect(r: Rect) -> fitz.Rect:
    return fitz.Rect(r.x0, r.y0, r.x1, r.y1)


def _color_from_seq(seq: Any, alpha: float = 1.0) -> Color | None:
    if not seq:
        return None
    values = list(seq)
    if len(values) == 1:
        g = float(values[0])
        return Color(g, g, g, alpha)
    if len(values) == 3:
        return Color(*(float(v) for v in values), alpha)
    if len(values) == 4:  # CMYK
        c, m, y, k = (float(v) for v in values)
        return Color((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k), alpha)
    return None


class SaveOptions:
    """Bundle of every save/optimisation switch supported by the engine."""

    __slots__ = (
        "ascii",
        "clean",
        "deflate",
        "deflate_fonts",
        "deflate_images",
        "encryption",
        "expand",
        "garbage",
        "incremental",
        "linear",
        "no_new_id",
        "owner_password",
        "permissions",
        "pretty",
        "user_password",
    )

    def __init__(
        self,
        *,
        incremental: bool = False,
        garbage: int = 3,
        deflate: bool = True,
        deflate_images: bool = True,
        deflate_fonts: bool = True,
        clean: bool = False,
        linear: bool = False,
        ascii_: bool = False,
        expand: int = 0,
        pretty: bool = False,
        no_new_id: bool = False,
        encryption: EncryptionMethod = EncryptionMethod.NONE,
        owner_password: str = "",
        user_password: str = "",
        permissions: int = Permission.all_bits(),
    ) -> None:
        self.incremental = incremental
        self.garbage = garbage
        self.deflate = deflate
        self.deflate_images = deflate_images
        self.deflate_fonts = deflate_fonts
        self.clean = clean
        self.linear = linear
        self.ascii = ascii_
        self.expand = expand
        self.pretty = pretty
        self.no_new_id = no_new_id
        self.encryption = encryption
        self.owner_password = owner_password
        self.user_password = user_password
        self.permissions = permissions

    @staticmethod
    def linearization_supported() -> bool:
        """``True`` when the bundled MuPDF can still write linearised files.

        MuPDF removed linearisation in 1.26+; PDF Studio keeps the option in
        its API (and the Save dialog) but silently degrades to an optimised
        non-linear save, warning once, rather than failing the save.
        """
        try:
            major, minor = (int(p) for p in fitz.VersionBind.split(".")[:2])
        except Exception:
            return False
        return (major, minor) < (1, 26)

    def to_kwargs(self) -> dict[str, Any]:
        """Translate to PyMuPDF ``Document.save`` keyword arguments."""
        kwargs: dict[str, Any] = {
            "garbage": 0 if self.incremental else self.garbage,
            "deflate": self.deflate,
            "deflate_images": self.deflate_images,
            "deflate_fonts": self.deflate_fonts,
            "clean": self.clean,
            "ascii": self.ascii,
            "expand": self.expand,
            "pretty": self.pretty,
            "no_new_id": self.no_new_id,
        }
        if self.incremental:
            kwargs["incremental"] = True
        elif self.linear:
            if self.linearization_supported():
                kwargs["linear"] = True
            else:
                log.warning("This MuPDF build cannot linearise; saving optimised instead.")
                kwargs["garbage"] = max(kwargs["garbage"], 4)
                kwargs["clean"] = True
        if self.encryption is not EncryptionMethod.NONE:
            kwargs["encryption"] = _ENCRYPTION_MAP[self.encryption]
            kwargs["owner_pw"] = self.owner_password or self.user_password
            kwargs["user_pw"] = self.user_password
            kwargs["permissions"] = self.permissions
        return kwargs

    @classmethod
    def fast(cls) -> SaveOptions:
        """Quickest possible save (no cleanup)."""
        return cls(garbage=0, deflate=False, clean=False)

    @classmethod
    def optimized(cls) -> SaveOptions:
        """Maximum size reduction."""
        return cls(garbage=4, deflate=True, clean=True)

    @classmethod
    def web(cls) -> SaveOptions:
        """Optimised and linearised for byte-serving."""
        return cls(garbage=4, deflate=True, clean=True, linear=True)


class PdfDocument:
    """A single open PDF document.

    Example:
        >>> doc = PdfDocument.open("report.pdf")           # doctest: +SKIP
        >>> doc.page_count                                  # doctest: +SKIP
        42
        >>> doc.extract_text(0)[:20]                        # doctest: +SKIP
        'Quarterly report ...'
    """

    # -- construction ------------------------------------------------------- #
    def __init__(
        self,
        handle: fitz.Document,
        *,
        path: Path | None = None,
        password: str | None = None,
    ) -> None:
        self._doc = handle
        self._path = path
        self._password = password
        self._lock = threading.RLock()
        self.id = uuid.uuid4().hex[:12]
        self.undo_stack = UndoStack(document_id=self.id)
        self._modified = False
        self._closed = False
        self._page_labels: dict[int, str] = {}
        self._opened_at = time.time()
        self._temp_files: list[Path] = []
        self.display_name = path.name if path else "Untitled"
        # Snapshot the encryption state — see :attr:`is_encrypted`. Only
        # ``is_encrypted`` and the metadata are safe to read here; touching
        # ``needs_pass`` after authenticate() corrupts decryption in PyMuPDF
        # 1.28, so the callers that open the file record that flag themselves
        # *before* authenticating.
        try:
            self._encryption_method = (handle.metadata or {}).get("encryption") or ""
            self._was_encrypted = bool(handle.is_encrypted) or bool(self._encryption_method)
        except Exception:
            self._was_encrypted = False
            self._encryption_method = ""

    @classmethod
    def open(
        cls,
        path: str | Path,
        password: str | None = None,
        *,
        readonly: bool = False,
    ) -> PdfDocument:
        """Open a document from disk.

        Raises:
            PasswordRequiredError: when encrypted and no/incorrect password.
            DocumentError: for any other failure (corrupt file, missing, …).
        """
        p = Path(path).expanduser()
        if not p.exists():
            raise DocumentError(f"File not found: {p}")
        try:
            handle = fitz.open(p)
        except Exception as exc:
            raise DocumentError(f"Cannot open {p.name}", detail=str(exc)) from exc

        encrypted = bool(handle.needs_pass)
        if encrypted:
            if password is None:
                handle.close()
                raise PasswordRequiredError(str(p))
            if not handle.authenticate(password):
                handle.close()
                raise PasswordRequiredError(str(p), wrong_password=True)

        doc = cls(handle, path=p, password=password)
        doc._was_encrypted = encrypted or doc._was_encrypted
        doc._load_page_labels()
        log.info(
            "Opened {} ({} pages, encrypted={}, linearised={})",
            p.name,
            handle.page_count,
            bool(handle.is_encrypted),
            doc.is_linearized,
        )
        bus().publish(
            Topic.DOCUMENT_OPENED,
            {"document_id": doc.id, "path": str(p), "pages": handle.page_count},
            source="document",
        )
        return doc

    @classmethod
    def from_bytes(
        cls, data: bytes, *, name: str = "Untitled.pdf", password: str | None = None
    ) -> PdfDocument:
        """Open a PDF held in memory (cloud downloads, clipboard, tests)."""
        try:
            handle = fitz.open(stream=data, filetype="pdf")
        except Exception as exc:
            raise DocumentError("Cannot parse in-memory PDF", detail=str(exc)) from exc
        encrypted = bool(handle.needs_pass)
        if encrypted and (password is None or not handle.authenticate(password)):
            handle.close()
            raise PasswordRequiredError(name, wrong_password=password is not None)
        doc = cls(handle, path=None, password=password)
        doc._was_encrypted = encrypted or doc._was_encrypted
        doc.display_name = name
        return doc

    @classmethod
    def create(cls, width: float = 595, height: float = 842, pages: int = 1) -> PdfDocument:
        """Create a new, empty document."""
        handle = fitz.open()
        for _ in range(max(0, pages)):
            handle.new_page(width=width, height=height)
        doc = cls(handle)
        doc._modified = True
        log.info("Created blank document ({} pages)", pages)
        return doc

    # -- lifecycle ---------------------------------------------------------- #
    def close(self) -> None:
        """Release MuPDF resources and delete temporary files."""
        if self._closed:
            return
        with self._lock:
            try:
                self._doc.close()
            except Exception:
                log.warning("Error closing {}", self.display_name)
            self._closed = True
        for tmp in self._temp_files:
            tmp.unlink(missing_ok=True)
        bus().publish(Topic.DOCUMENT_CLOSED, {"document_id": self.id}, source="document")
        log.info("Closed {}", self.display_name)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False

    def __del__(self) -> None:  # pragma: no cover - best effort
        with contextlib.suppress(Exception):
            self.close()

    @contextmanager
    def locked(self) -> Iterator[fitz.Document]:
        """Acquire the document lock and yield the raw MuPDF handle.

        All engine services use this; plugins should too when touching the
        low-level API directly.
        """
        with self._lock:
            if self._closed:
                raise DocumentError("Document is closed")
            yield self._doc

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def raw(self) -> fitz.Document:
        """Raw PyMuPDF handle (prefer :meth:`locked`)."""
        return self._doc

    # -- identity / state --------------------------------------------------- #
    @property
    def path(self) -> Path | None:
        return self._path

    @path.setter
    def path(self, value: Path | None) -> None:
        self._path = value
        if value:
            self.display_name = value.name

    @property
    def password(self) -> str | None:
        return self._password

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_modified(self) -> bool:
        return self._modified or not self.undo_stack.is_clean

    def mark_modified(self, reason: str = "") -> None:
        """Flag the document dirty and notify the UI (tab asterisk, etc.)."""
        self._modified = True
        bus().publish(
            Topic.DOCUMENT_MODIFIED,
            {"document_id": self.id, "reason": reason},
            source="document",
        )

    def mark_saved(self) -> None:
        self._modified = False
        self.undo_stack.set_clean()

    @property
    def page_count(self) -> int:
        with self._lock:
            return self._doc.page_count

    def __len__(self) -> int:
        return self.page_count

    # -- document properties ------------------------------------------------ #
    @property
    def is_encrypted(self) -> bool:
        """``True`` when the file this document was loaded from is encrypted.

        MuPDF clears ``Document.is_encrypted`` once a password is accepted, so
        the real state is captured at open time instead. (Reading
        ``needs_pass`` after authentication is avoided entirely: in PyMuPDF
        1.28 that access corrupts the decryption state and every subsequent
        text extraction silently returns an empty string.)
        """
        return self._was_encrypted

    @property
    def encryption_method(self) -> str:
        """Human readable cipher, e.g. ``"Standard V5 R6 256-bit AES"``."""
        return self._encryption_method or "None"

    @property
    def is_linearized(self) -> bool:
        with self._lock:
            try:
                return bool(self._doc.is_fast_webaccess)
            except Exception:
                return False

    @property
    def is_pdf(self) -> bool:
        with self._lock:
            return bool(self._doc.is_pdf)

    @property
    def is_form(self) -> bool:
        with self._lock:
            return bool(self._doc.is_form_pdf)

    @property
    def is_portfolio(self) -> bool:
        """``True`` for PDF collections (portfolios) with embedded documents."""
        with self._lock:
            try:
                if self._doc.embfile_count() == 0:
                    return False
                root = self._doc.pdf_catalog()
                return "/Collection" in (self._doc.xref_object(root, compressed=True) or "")
            except Exception:
                return False

    @property
    def is_repaired(self) -> bool:
        with self._lock:
            return bool(getattr(self._doc, "is_repaired", False))

    @property
    def pdf_version(self) -> str:
        with self._lock:
            try:
                header = self._doc.xref_object(-1) or ""
            except Exception:
                header = ""
            return self._doc.metadata.get("format", header) or "PDF"

    @property
    def file_size(self) -> int:
        return self._path.stat().st_size if self._path and self._path.exists() else 0

    def permissions(self) -> dict[str, bool]:
        """Effective permission flags for the current authentication level."""
        with self._lock:
            perm = int(self._doc.permissions)
        return {
            "print": bool(perm & fitz.PDF_PERM_PRINT),
            "modify": bool(perm & fitz.PDF_PERM_MODIFY),
            "copy": bool(perm & fitz.PDF_PERM_COPY),
            "annotate": bool(perm & fitz.PDF_PERM_ANNOTATE),
            "fill_forms": bool(perm & fitz.PDF_PERM_FORM),
            "accessibility": bool(perm & fitz.PDF_PERM_ACCESSIBILITY),
            "assemble": bool(perm & fitz.PDF_PERM_ASSEMBLE),
            "print_high_res": bool(perm & fitz.PDF_PERM_PRINT_HQ),
        }

    def require_permission(self, name: str) -> None:
        """Raise :class:`PermissionDeniedError` when ``name`` is not granted."""
        if not self.permissions().get(name, True):
            raise PermissionDeniedError(
                f"This document does not allow '{name}'.",
                detail="Open it with the owner password to change permissions.",
            )

    def fingerprint(self) -> str:
        """Stable hash of the file contents (cache keys, comparison, sync)."""
        if self._path and self._path.exists():
            h = hashlib.blake2b(digest_size=16)
            with self._path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        with self._lock:
            return hashlib.blake2b(self._doc.tobytes(), digest_size=16).hexdigest()

    # -- pages -------------------------------------------------------------- #
    def page_info(self, index: int) -> PageInfo:
        """Cheap descriptor used by thumbnails, page list and properties."""
        with self._lock:
            page = self._doc[index]
            rect = page.rect
            annots = sum(1 for _ in page.annots()) if self.is_pdf else 0
            images = len(page.get_images(full=False)) if self.is_pdf else 0
            return PageInfo(
                index=index,
                width=rect.width,
                height=rect.height,
                rotation=page.rotation,
                label=self._page_labels.get(index, ""),
                has_text=bool(page.get_text("text").strip()),
                annotation_count=annots,
                image_count=images,
            )

    def pages_info(self) -> list[PageInfo]:
        return [self.page_info(i) for i in range(self.page_count)]

    def page_size(self, index: int) -> tuple[float, float]:
        with self._lock:
            r = self._doc[index].rect
            return (r.width, r.height)

    def page_rotation(self, index: int) -> int:
        with self._lock:
            return int(self._doc[index].rotation)

    def set_page_rotation(self, index: int, degrees: int) -> None:
        """Set absolute rotation (0/90/180/270)."""
        with self._lock:
            self._doc[index].set_rotation(int(Rotation.normalise(degrees)))
        self.mark_modified("rotate")

    def _load_page_labels(self) -> None:
        """Read the ``/PageLabels`` number tree if present."""
        try:
            with self._lock:
                labels = self._doc.get_page_labels()
                if not labels:
                    return
                for i in range(self._doc.page_count):
                    self._page_labels[i] = self._doc[i].get_label() or ""
        except Exception:
            self._page_labels.clear()

    def page_label(self, index: int) -> str:
        return self._page_labels.get(index, "") or str(index + 1)

    def set_page_labels(self, rules: list[dict[str, Any]]) -> None:
        """Apply ``/PageLabels`` rules, e.g. ``[{'startpage':0,'style':'r'}]``."""
        with self._lock:
            self._doc.set_page_labels(rules)
        self._load_page_labels()
        self.mark_modified("page-labels")

    # -- text --------------------------------------------------------------- #
    def extract_text(self, index: int, *, mode: str = "text", clip: Rect | None = None) -> str:
        """Extract page text. ``mode`` is any PyMuPDF mode (text/html/xml/json)."""
        with self._lock:
            page = self._doc[index]
            return page.get_text(mode, clip=_to_fitz_rect(clip) if clip else None)

    def extract_all_text(self, separator: str = "\n\n") -> str:
        return separator.join(self.extract_text(i) for i in range(self.page_count))

    def extract_blocks(self, index: int) -> list[TextBlock]:
        """Structured text: blocks → lines → spans with fonts and colours."""
        with self._lock:
            raw = self._doc[index].get_text("dict")
        blocks: list[TextBlock] = []
        for bno, block in enumerate(raw.get("blocks", [])):
            if block.get("type") != 0:  # 0 = text, 1 = image
                continue
            lines: list[TextLine] = []
            for line in block.get("lines", []):
                spans: list[TextSpan] = []
                for span in line.get("spans", []):
                    flags = int(span.get("flags", 0))
                    rgb = int(span.get("color", 0))
                    spans.append(
                        TextSpan(
                            text=span.get("text", ""),
                            rect=_to_rect(span["bbox"]),
                            font=span.get("font", ""),
                            size=float(span.get("size", 0)),
                            color=Color.from_bytes(
                                (rgb >> 16) & 255, (rgb >> 8) & 255, rgb & 255
                            ),
                            bold=bool(flags & 2**4),
                            italic=bool(flags & 2**1),
                            origin=Point(*span.get("origin", (0, 0))),
                        )
                    )
                if spans:
                    lines.append(TextLine(spans=spans, rect=_to_rect(line["bbox"])))
            if lines:
                blocks.append(
                    TextBlock(lines=lines, rect=_to_rect(block["bbox"]), block_no=bno)
                )
        return blocks

    def extract_words(self, index: int) -> list[tuple[Rect, str]]:
        """Word-level extraction used for text selection and hit-testing."""
        with self._lock:
            words = self._doc[index].get_text("words")
        return [(_to_rect(w[:4]), w[4]) for w in words]

    def page_has_text(self, index: int) -> bool:
        return bool(self.extract_text(index).strip())

    def needs_ocr(self, sample: int = 8) -> bool:
        """Heuristic: does this document look like a scan needing OCR?"""
        count = min(sample, self.page_count)
        if count == 0:
            return False
        empty = sum(0 if self.page_has_text(i) else 1 for i in range(count))
        return empty / count > 0.6

    # -- images & drawings -------------------------------------------------- #
    def page_images(self, index: int) -> list[ImageInfo]:
        """Enumerate raster images placed on a page."""
        result: list[ImageInfo] = []
        with self._lock:
            page = self._doc[index]
            for info in page.get_images(full=True):
                xref = int(info[0])
                try:
                    rects = page.get_image_rects(xref)
                    rect = _to_rect(rects[0]) if rects else Rect(0, 0, 0, 0)
                except Exception:
                    rect = Rect(0, 0, 0, 0)
                try:
                    meta = self._doc.extract_image(xref)
                    size = len(meta.get("image", b""))
                    ext = meta.get("ext", "png")
                    cs = meta.get("colorspace", 0)
                    cs_name = {1: "GRAY", 3: "RGB", 4: "CMYK"}.get(int(cs), str(cs))
                except Exception:
                    size, ext, cs_name = 0, "png", ""
                result.append(
                    ImageInfo(
                        xref=xref,
                        rect=rect,
                        width=int(info[2]),
                        height=int(info[3]),
                        colorspace=cs_name,
                        bpc=int(info[4]),
                        size_bytes=size,
                        ext=ext,
                        smask=int(info[1]),
                        name=str(info[7]),
                    )
                )
        return result

    def extract_image(self, xref: int) -> tuple[bytes, str]:
        """Return ``(data, extension)`` for an image XREF."""
        with self._lock:
            meta = self._doc.extract_image(xref)
        return meta["image"], meta.get("ext", "png")

    def page_drawings(self, index: int) -> list[DrawingPath]:
        """Extract vector graphics (paths) from a page."""
        with self._lock:
            raw = self._doc[index].get_drawings()
        paths: list[DrawingPath] = []
        for d in raw:
            items: list[tuple[str, list[Point]]] = []
            for item in d.get("items", []):
                op = item[0]
                pts: list[Point] = []
                for arg in item[1:]:
                    if isinstance(arg, fitz.Point):
                        pts.append(Point(arg.x, arg.y))
                    elif isinstance(arg, fitz.Rect):
                        pts.extend([Point(arg.x0, arg.y0), Point(arg.x1, arg.y1)])
                    elif isinstance(arg, fitz.Quad):
                        pts.extend(Point(p.x, p.y) for p in (arg.ul, arg.ur, arg.lr, arg.ll))
                items.append((op, pts))
            paths.append(
                DrawingPath(
                    items=items,
                    rect=_to_rect(d["rect"]),
                    stroke=_color_from_seq(d.get("color")),
                    fill=_color_from_seq(d.get("fill")),
                    width=float(d.get("width") or 0.0),
                    opacity=float(d.get("fill_opacity", 1.0) or 1.0),
                    closed=bool(d.get("closePath", False)),
                    # MuPDF reports a solid line as dashes=None; str() would
                    # turn that into the literal "None", which is then written
                    # back into the content stream and makes the parser fail
                    # with "unknown keyword: 'None'".
                    dashes=str(d.get("dashes") or ""),
                    even_odd=bool(d.get("even_odd", False)),
                )
            )
        return paths

    # -- annotations -------------------------------------------------------- #
    def page_annotations(self, index: int) -> list[Annotation]:
        """Read all annotations on a page as neutral records."""
        out: list[Annotation] = []
        with self._lock:
            page = self._doc[index]
            for annot in page.annots():
                info = annot.info
                atype = _ANNOT_TYPE_MAP.get(annot.type[0], AnnotationType.STAMP)
                colors = annot.colors or {}
                vertices = (
                    [Point(p[0], p[1]) for p in (annot.vertices or [])]
                    if annot.vertices
                    else []
                )
                ink: list[list[Point]] = []
                if atype is AnnotationType.INK and annot.vertices:
                    ink = [
                        [Point(p[0], p[1]) for p in stroke]
                        for stroke in annot.vertices
                        if isinstance(stroke, (list, tuple))
                        and stroke
                        and isinstance(stroke[0], (list, tuple))
                    ]
                out.append(
                    Annotation(
                        id=info.get("id") or f"{index}-{annot.xref}",
                        type=atype,
                        page=index,
                        rect=_to_rect(annot.rect),
                        author=info.get("title", ""),
                        contents=info.get("content", ""),
                        subject=info.get("subject", ""),
                        color=_color_from_seq(colors.get("stroke")),
                        interior_color=_color_from_seq(colors.get("fill")),
                        opacity=float(annot.opacity if annot.opacity >= 0 else 1.0),
                        border_width=float((annot.border or {}).get("width", 1.0) or 1.0),
                        created=info.get("creationDate", ""),
                        modified=info.get("modDate", ""),
                        flags=int(annot.flags),
                        vertices=vertices,
                        ink_list=ink,
                        name=info.get("name", ""),
                        extra={"xref": annot.xref},
                    )
                )
        return out

    def all_annotations(self) -> list[Annotation]:
        return [a for i in range(self.page_count) for a in self.page_annotations(i)]

    # -- outline / bookmarks ------------------------------------------------ #
    def bookmarks(self) -> list[Bookmark]:
        """Return the outline as a tree of :class:`Bookmark`."""
        with self._lock:
            toc = self._doc.get_toc(simple=False)
        roots: list[Bookmark] = []
        stack: list[Bookmark] = []
        for entry in toc:
            level, title, page = int(entry[0]), str(entry[1]), int(entry[2])
            details = entry[3] if len(entry) > 3 and isinstance(entry[3], dict) else {}
            point = details.get("to")
            bm = Bookmark(
                title=title,
                page=max(0, page - 1),
                level=level,
                y=float(point.y) if point is not None else 0.0,
                zoom=float(details.get("zoom", 0) or 0),
                bold=bool(int(details.get("bold", 0) or 0)),
                italic=bool(int(details.get("italic", 0) or 0)),
                color=_color_from_seq(details.get("color")),
                open=bool(details.get("collapse") in (None, 0, False)),
                uri=str(details.get("uri", "") or ""),
                named_destination=str(details.get("nameddest", "") or ""),
            )
            while stack and stack[-1].level >= level:
                stack.pop()
            (stack[-1].children if stack else roots).append(bm)
            stack.append(bm)
        return roots

    def set_bookmarks(self, bookmarks: Sequence[Bookmark]) -> None:
        """Replace the whole outline."""
        toc: list[list[Any]] = []

        def walk(items: Sequence[Bookmark], level: int) -> None:
            for bm in items:
                toc.append(
                    [level, bm.title, bm.page + 1, {"kind": 1, "to": fitz.Point(0, bm.y)}]
                )
                walk(bm.children, level + 1)

        walk(bookmarks, 1)
        with self._lock:
            self._doc.set_toc(toc)
        self.mark_modified("bookmarks")

    # -- links -------------------------------------------------------------- #
    def page_links(self, index: int) -> list[Link]:
        with self._lock:
            raw = self._doc[index].get_links()
        links: list[Link] = []
        for item in raw:
            kind_map = {
                fitz.LINK_URI: "uri",
                fitz.LINK_GOTO: "goto",
                fitz.LINK_GOTOR: "gotor",
                fitz.LINK_LAUNCH: "launch",
                fitz.LINK_NAMED: "named",
            }
            point = item.get("to")
            links.append(
                Link(
                    rect=_to_rect(item["from"]),
                    page=index,
                    kind=kind_map.get(item.get("kind", 0), "uri"),
                    uri=item.get("uri", "") or "",
                    target_page=int(item.get("page", -1)),
                    target_point=Point(point.x, point.y) if point is not None else None,
                    file=item.get("file", "") or "",
                    zoom=float(item.get("zoom", 0) or 0),
                )
            )
        return links

    # -- layers (OCGs) ------------------------------------------------------ #
    def layers(self) -> list[Layer]:
        with self._lock:
            try:
                ocgs = self._doc.get_ocgs()
            except Exception:
                return []
            states = self._doc.get_oc_states() if hasattr(self._doc, "get_oc_states") else {}
        visible_off: set[int] = set()
        if isinstance(states, dict):
            visible_off = {int(x) for x in states.get("off", [])}
        return [
            Layer(
                xref=int(xref),
                name=str(info.get("name", f"Layer {xref}")),
                visible=int(xref) not in visible_off,
                intent=str(info.get("intent", ["View"])[0])
                if isinstance(info.get("intent"), list)
                else str(info.get("intent", "View")),
                usage=dict(info.get("usage", {}))
                if isinstance(info.get("usage"), dict)
                else {},
            )
            for xref, info in (ocgs or {}).items()
        ]

    def set_layer_visible(self, xref: int, visible: bool) -> None:
        with self._lock:
            self._doc.set_layer(
                -1, on=[xref] if visible else None, off=None if visible else [xref]
            )
        self.mark_modified("layer-visibility")

    # -- attachments -------------------------------------------------------- #
    def attachments(self) -> list[Attachment]:
        out: list[Attachment] = []
        with self._lock:
            for i in range(self._doc.embfile_count()):
                info = self._doc.embfile_info(i)
                out.append(
                    Attachment(
                        name=info.get("filename", info.get("name", f"file{i}")),
                        size=int(info.get("size", 0)),
                        description=info.get("desc", ""),
                        mime=info.get("ufilename", ""),
                        created=info.get("creationDate", ""),
                        modified=info.get("modDate", ""),
                        checksum=str(info.get("checksum", "")),
                    )
                )
        return out

    def add_attachment(
        self, name: str, data: bytes, *, description: str = "", mime: str = ""
    ) -> None:
        with self._lock:
            self._doc.embfile_add(name, data, filename=name, desc=description)
        self.mark_modified("attachment-add")

    def extract_attachment(self, name: str) -> bytes:
        with self._lock:
            return self._doc.embfile_get(name)

    def delete_attachment(self, name: str) -> None:
        with self._lock:
            self._doc.embfile_del(name)
        self.mark_modified("attachment-delete")

    # -- metadata ----------------------------------------------------------- #
    def metadata(self) -> DocumentMetadata:
        with self._lock:
            meta = dict(self._doc.metadata or {})
            try:
                xmp = self._doc.get_xml_metadata() or ""
            except Exception:
                xmp = ""
        return DocumentMetadata(
            title=meta.get("title", "") or "",
            author=meta.get("author", "") or "",
            subject=meta.get("subject", "") or "",
            keywords=meta.get("keywords", "") or "",
            creator=meta.get("creator", "") or "",
            producer=meta.get("producer", "") or "",
            creation_date=meta.get("creationDate", "") or "",
            modification_date=meta.get("modDate", "") or "",
            trapped=meta.get("trapped", "") or "",
            xmp=xmp,
        )

    def set_metadata(self, metadata: DocumentMetadata) -> None:
        with self._lock:
            self._doc.set_metadata(metadata.as_dict())
            if metadata.xmp:
                self._doc.set_xml_metadata(metadata.xmp)
        self.mark_modified("metadata")

    def clear_metadata(self) -> None:
        """Strip the info dictionary and XMP (privacy / sanitisation)."""
        with self._lock:
            self._doc.set_metadata({})
            with contextlib.suppress(Exception):  # XMP may be absent
                self._doc.del_xml_metadata()
        self.mark_modified("metadata-clear")

    # -- javascript --------------------------------------------------------- #
    def javascript(self) -> list[tuple[str, str]]:
        """Return document level JavaScript as ``(name, source)`` pairs.

        Walks ``/Names /JavaScript`` in the catalogue and dereferences each
        action's ``/JS`` entry (either a literal string or a stream).
        """
        scripts: list[tuple[str, str]] = []
        with self._lock:
            try:
                catalog = self._doc.pdf_catalog()
                names_ref = self._doc.xref_get_key(catalog, "Names")
                if names_ref[0] == "null":
                    return []
                names_xref = int(names_ref[1].split()[0])
                js_ref = self._doc.xref_get_key(names_xref, "JavaScript/Names")
                if js_ref[0] == "null":
                    return []
                entries = js_ref[1].strip().lstrip("[").rstrip("]").split()
                # entries alternate: name, action-reference ("12 0 R")
                for i in range(0, len(entries) - 3, 4):
                    label = entries[i].strip("()")
                    action_xref = int(entries[i + 1])
                    kind, value = self._doc.xref_get_key(action_xref, "JS")
                    if kind == "string":
                        source = value.strip("()")
                    elif kind == "xref":
                        stream = self._doc.xref_stream(int(value.split()[0]))
                        source = (stream or b"").decode("utf-8", "replace")
                    else:
                        source = value
                    scripts.append((label, source))
            except Exception:
                log.debug("Could not enumerate document JavaScript")
        return scripts

    def has_javascript(self) -> bool:
        with self._lock:
            try:
                catalog = self._doc.xref_object(self._doc.pdf_catalog(), compressed=True) or ""
            except Exception:
                return False
        return "/JavaScript" in catalog or "/JS" in catalog

    # -- saving ------------------------------------------------------------- #
    def save(self, options: SaveOptions | None = None) -> Path:
        """Save in place (requires a path). Falls back to full save when needed."""
        if self._path is None:
            raise DocumentError("Document has no path — use save_as().")
        return self.save_as(self._path, options)

    def save_as(self, path: str | Path, options: SaveOptions | None = None) -> Path:
        """Write the document to ``path``.

        Writing over the currently open file is handled safely by saving to a
        temporary file in the same directory and atomically replacing it.
        """
        target = Path(path).expanduser()
        opts = options or SaveOptions()
        target.parent.mkdir(parents=True, exist_ok=True)
        same_file = self._path is not None and target.resolve() == self._path.resolve()

        with self._lock:
            if opts.incremental and same_file:
                self._doc.save(str(target), **opts.to_kwargs())
            elif same_file:
                fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), suffix=".pdfstudio.tmp")
                os.close(fd)
                tmp = Path(tmp_name)
                try:
                    kwargs = opts.to_kwargs()
                    kwargs.pop("incremental", None)
                    self._doc.save(str(tmp), **kwargs)
                    self._doc.close()
                    shutil.move(str(tmp), str(target))
                    self._doc = fitz.open(target)
                    # Read the flag before authenticating (see is_encrypted).
                    self._was_encrypted = bool(self._doc.needs_pass)
                    if self._password and self._was_encrypted:
                        self._doc.authenticate(self._password)
                finally:
                    tmp.unlink(missing_ok=True)
            else:
                kwargs = opts.to_kwargs()
                kwargs.pop("incremental", None)
                self._doc.save(str(target), **kwargs)

        self._path = target
        self.display_name = target.name
        if opts.encryption is not EncryptionMethod.NONE:
            self._was_encrypted = True
            self._encryption_method = opts.encryption.value
        elif options is not None:
            self._was_encrypted = False
            self._encryption_method = ""
        self.mark_saved()
        bus().publish(
            Topic.DOCUMENT_SAVED,
            {"document_id": self.id, "path": str(target)},
            source="document",
        )
        log.info("Saved {} ({} bytes)", target.name, target.stat().st_size)
        return target

    def to_bytes(self, options: SaveOptions | None = None) -> bytes:
        """Serialise to memory (cloud upload, comparison, preview)."""
        opts = options or SaveOptions()
        kwargs = opts.to_kwargs()
        kwargs.pop("incremental", None)
        kwargs.pop("linear", None)
        with self._lock:
            return self._doc.tobytes(**kwargs)

    def copy(self) -> PdfDocument:
        """Deep copy through serialisation (used by compare & batch pipelines)."""
        clone = PdfDocument.from_bytes(self.to_bytes(), name=self.display_name)
        clone._path = self._path
        return clone

    # -- misc --------------------------------------------------------------- #
    def register_temp_file(self, path: Path) -> None:
        """Track a temp file to be removed when the document closes."""
        self._temp_files.append(path)

    def statistics(self) -> dict[str, Any]:
        """Summary used by the Properties dialog and the CLI ``info`` command."""
        pages = self.pages_info()
        return {
            "path": str(self._path) if self._path else "",
            "name": self.display_name,
            "pages": len(pages),
            "size_bytes": self.file_size,
            "encrypted": self.is_encrypted,
            "encryption": self.encryption_method,
            "linearized": self.is_linearized,
            "form": self.is_form,
            "portfolio": self.is_portfolio,
            "tagged": self.is_tagged(),
            "javascript": self.has_javascript(),
            "attachments": len(self.attachments()),
            "annotations": sum(p.annotation_count for p in pages),
            "images": sum(p.image_count for p in pages),
            "pages_without_text": sum(0 if p.has_text else 1 for p in pages),
            "version": self.pdf_version,
            "permissions": self.permissions(),
        }

    def is_tagged(self) -> bool:
        """``True`` when a structure tree exists (required for PDF/UA)."""
        with self._lock:
            try:
                catalog = self._doc.xref_object(self._doc.pdf_catalog(), compressed=True) or ""
            except Exception:
                return False
        return "/StructTreeRoot" in catalog

    def __repr__(self) -> str:  # pragma: no cover
        state = "closed" if self._closed else f"{self.page_count}p"
        return f"<PdfDocument {self.display_name!r} {state}>"
