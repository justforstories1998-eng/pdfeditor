"""Batch processing: apply a pipeline of operations to many files.

A :class:`BatchJob` is an ordered list of :class:`BatchOperation` objects run
against a list of input files.  Operations are composable — for example
*OCR → watermark → compress → encrypt → rename* — and every step reports
progress and per-file errors without aborting the whole run.

The same pipeline definition is used by the GUI's Batch dialog, the CLI's
``pdfstudio batch`` command and by plugins.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pdfstudio.core.exceptions import ValidationError
from pdfstudio.core.jobs import JobContext
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.types import Color, EncryptionMethod

log = get_logger("batch")


@dataclass(slots=True)
class BatchItemResult:
    """Outcome for a single input file."""

    source: Path
    output: Path | None = None
    success: bool = True
    error: str = ""
    duration: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class BatchResult:
    """Outcome of a whole batch run."""

    items: list[BatchItemResult] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    @property
    def succeeded(self) -> list[BatchItemResult]:
        return [i for i in self.items if i.success]

    @property
    def failed(self) -> list[BatchItemResult]:
        return [i for i in self.items if not i.success]

    @property
    def duration(self) -> float:
        return (self.finished or time.time()) - self.started

    def summary(self) -> str:
        return (
            f"{len(self.succeeded)} succeeded, {len(self.failed)} failed "
            f"in {self.duration:.1f}s"
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "summary": self.summary(),
                "duration": round(self.duration, 2),
                "items": [
                    {
                        "source": str(i.source),
                        "output": str(i.output) if i.output else None,
                        "success": i.success,
                        "error": i.error,
                        "seconds": round(i.duration, 2),
                        **i.details,
                    }
                    for i in self.items
                ],
            },
            indent=2,
        )


class BatchOperation(ABC):
    """One step in a batch pipeline."""

    name: str = "operation"

    @abstractmethod
    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        """Mutate ``document`` in place. ``context`` carries per-file state."""

    def describe(self) -> str:
        return self.name


# --------------------------------------------------------------------------- #
# Concrete operations
# --------------------------------------------------------------------------- #
class OcrOperation(BatchOperation):
    """Run OCR over image-only pages."""

    name = "OCR"

    def __init__(
        self, languages: Sequence[str] = ("eng",), *, force: bool = False, dpi: int = 300
    ):
        self.languages = list(languages)
        self.force = force
        self.dpi = dpi

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.core.settings import OcrSettings
        from pdfstudio.ocr.engine import OcrService

        service = OcrService(
            document,
            OcrSettings(languages=self.languages, force=self.force, dpi=self.dpi),
        )
        results = service.run()
        context["ocr_pages"] = len(results)

    def describe(self) -> str:
        return f"OCR ({'+'.join(self.languages)})"


class WatermarkOperation(BatchOperation):
    """Stamp a text watermark on every page."""

    name = "Watermark"

    def __init__(
        self,
        text: str,
        *,
        opacity: float = 0.25,
        font_size: float = 48,
        rotate: float = 45,
        color: Color | None = None,
    ) -> None:
        self.text = text
        self.opacity = opacity
        self.font_size = font_size
        self.rotate = rotate
        self.color = color if color is not None else Color(0.6, 0.6, 0.6)

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.pdfengine.security import SecurityService

        SecurityService(document).watermark_text(
            self.text,
            opacity=self.opacity,
            font_size=self.font_size,
            rotate=self.rotate,
            color=self.color,
        )

    def describe(self) -> str:
        return f'Watermark "{self.text}"'


class CompressOperation(BatchOperation):
    """Shrink the file with an optimisation profile."""

    name = "Compress"

    def __init__(self, profile: str = "ebook") -> None:
        self.profile = profile

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.pdfengine.optimize import OptimizeProfile, Optimizer

        profiles = {
            "screen": OptimizeProfile.screen,
            "ebook": OptimizeProfile.ebook,
            "print": OptimizeProfile.print_quality,
            "prepress": OptimizeProfile.prepress,
            "maximum": OptimizeProfile.maximum,
        }
        factory = profiles.get(self.profile, OptimizeProfile.ebook)
        report = Optimizer(document).optimize(factory())
        context["saved_bytes"] = report.bytes_saved

    def describe(self) -> str:
        return f"Compress ({self.profile})"


class EncryptOperation(BatchOperation):
    """Apply password protection on save."""

    name = "Encrypt"

    def __init__(
        self,
        user_password: str = "",
        owner_password: str = "",
        *,
        method: EncryptionMethod = EncryptionMethod.AES_256,
        allow_printing: bool = True,
        allow_copy: bool = False,
    ) -> None:
        self.user_password = user_password
        self.owner_password = owner_password
        self.method = method
        self.allow_printing = allow_printing
        self.allow_copy = allow_copy

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.pdfengine.security import (
            EncryptionSettings,
            PermissionSet,
        )

        context["save_options"] = EncryptionSettings(
            method=self.method,
            user_password=self.user_password,
            owner_password=self.owner_password,
            permissions=PermissionSet(
                printing=self.allow_printing, copy=self.allow_copy, modify=False
            ),
        ).to_save_options(SaveOptions.optimized())


class DecryptOperation(BatchOperation):
    """Remove encryption (requires the password used to open the file)."""

    name = "Decrypt"

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        options = SaveOptions.optimized()
        options.encryption = EncryptionMethod.NONE
        context["save_options"] = options


class SanitizeOperation(BatchOperation):
    """Strip metadata, JavaScript and embedded files."""

    name = "Sanitise"

    def __init__(
        self, *, metadata: bool = True, javascript: bool = True, attachments: bool = True
    ):
        self.metadata = metadata
        self.javascript = javascript
        self.attachments = attachments

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.pdfengine.security import SecurityService

        context["sanitised"] = SecurityService(document).sanitize(
            metadata=self.metadata,
            javascript=self.javascript,
            embedded_files=self.attachments,
        )


class BatesOperation(BatchOperation):
    """Continuous Bates numbering across the whole batch."""

    name = "Bates numbering"

    def __init__(self, prefix: str = "", start: int = 1, digits: int = 6) -> None:
        self.prefix = prefix
        self.counter = start
        self.digits = digits

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.pdfengine.security import SecurityService

        stamps = SecurityService(document).bates_numbering(
            prefix=self.prefix, start=self.counter, digits=self.digits
        )
        self.counter += document.page_count
        context["bates"] = stamps[0] if stamps else ""


class ConvertOperation(BatchOperation):
    """Export each document to another format instead of writing a PDF."""

    name = "Convert"

    def __init__(self, fmt: str = "png", *, dpi: int = 200) -> None:
        self.fmt = fmt.lower()
        self.dpi = dpi

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        from pdfstudio.pdfengine.convert import Exporter, ExportOptions

        exporter = Exporter(document)
        out_dir: Path = context["output_dir"]
        stem: str = context["stem"]
        options = ExportOptions(dpi=self.dpi)
        match self.fmt:
            case "txt":
                target = out_dir / f"{stem}.txt"
                target.write_text(exporter.to_text(), "utf-8")
            case "html":
                target = out_dir / f"{stem}.html"
                target.write_text(exporter.to_html(), "utf-8")
            case "md":
                target = out_dir / f"{stem}.md"
                target.write_text(exporter.to_markdown(), "utf-8")
            case "docx":
                target = exporter.to_docx(out_dir / f"{stem}.docx")
            case "pptx":
                target = exporter.to_pptx(out_dir / f"{stem}.pptx")
            case "tiff":
                target = exporter.to_multipage_tiff(out_dir / f"{stem}.tiff", options)
            case _:
                written = exporter.to_images(
                    out_dir,
                    self.fmt,
                    options,
                    prefix=stem,  # type: ignore[arg-type]
                )
                target = written[0] if written else out_dir
        context["converted_to"] = str(target)
        context["skip_pdf_save"] = True

    def describe(self) -> str:
        return f"Convert to {self.fmt.upper()}"


class ExtractOperation(BatchOperation):
    """Extract text or images into the output directory."""

    name = "Extract"

    def __init__(self, what: str = "text") -> None:
        self.what = what

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        out_dir: Path = context["output_dir"]
        stem: str = context["stem"]
        if self.what == "text":
            (out_dir / f"{stem}.txt").write_text(document.extract_all_text(), "utf-8")
        else:
            from pdfstudio.pdfengine.content import ImageEditor

            target = out_dir / f"{stem}-images"
            target.mkdir(parents=True, exist_ok=True)
            for n, (info, data) in enumerate(ImageEditor(document).extract_all(), 1):
                (target / f"image-{n:03d}.{info.ext}").write_bytes(data)
        context["skip_pdf_save"] = True

    def describe(self) -> str:
        return f"Extract {self.what}"


class ScriptOperation(BatchOperation):
    """Run a user-supplied Python callable against each document."""

    name = "Custom script"

    def __init__(self, function: Callable[[PdfDocument, dict[str, Any]], None]) -> None:
        self.function = function

    def apply(self, document: PdfDocument, context: dict[str, Any]) -> None:
        self.function(document, context)


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class RenameRule:
    """Filename template with tokens.

    Tokens: ``{name}`` ``{stem}`` ``{ext}`` ``{index}`` ``{date}`` ``{time}``
    ``{pages}`` ``{title}`` ``{author}`` ``{bates}`` ``{counter:04d}``
    """

    template: str = "{stem}"
    start_index: int = 1

    def render(
        self, source: Path, index: int, document: PdfDocument, context: dict[str, Any]
    ) -> str:
        meta = document.metadata()
        now = datetime.now()
        values = {
            "name": source.name,
            "stem": source.stem,
            "ext": source.suffix.lstrip("."),
            "index": index,
            "counter": index,
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H%M%S"),
            "pages": document.page_count,
            "title": _safe(meta.title) or source.stem,
            "author": _safe(meta.author),
            "bates": context.get("bates", ""),
        }
        try:
            rendered = self.template.format(**values)
        except (KeyError, ValueError) as exc:
            raise ValidationError(f"Invalid rename template: {exc}") from exc
        return _safe(rendered) or source.stem


class BatchProcessor:
    """Runs a pipeline of operations over many files."""

    def __init__(
        self,
        operations: Sequence[BatchOperation] | None = None,
        *,
        output_dir: str | Path | None = None,
        rename: RenameRule | None = None,
        overwrite: bool = False,
        suffix: str = "",
        continue_on_error: bool = True,
    ) -> None:
        self.operations = list(operations or [])
        self.output_dir = Path(output_dir) if output_dir else None
        self.rename = rename
        self.overwrite = overwrite
        self.suffix = suffix
        self.continue_on_error = continue_on_error

    def add(self, operation: BatchOperation) -> BatchProcessor:
        """Append an operation (fluent)."""
        self.operations.append(operation)
        return self

    def describe(self) -> list[str]:
        return [op.describe() for op in self.operations]

    def run(
        self,
        inputs: Sequence[str | Path],
        *,
        passwords: dict[str, str] | None = None,
        ctx: JobContext | None = None,
    ) -> BatchResult:
        """Process every input file through the pipeline."""
        result = BatchResult()
        files = [Path(p) for p in inputs]
        passwords = passwords or {}
        if ctx:
            ctx.set_total(len(files))
        out_dir = self.output_dir or (files[0].parent if files else Path.cwd())
        out_dir.mkdir(parents=True, exist_ok=True)

        for index, source in enumerate(files, 1):
            if ctx:
                ctx.raise_if_cancelled()
            started = time.perf_counter()
            item = BatchItemResult(source=source)
            document: PdfDocument | None = None
            try:
                document = PdfDocument.open(source, passwords.get(str(source)))
                context: dict[str, Any] = {
                    "output_dir": out_dir,
                    "stem": source.stem,
                    "index": index,
                    "source": source,
                }
                for operation in self.operations:
                    operation.apply(document, context)

                if not context.get("skip_pdf_save"):
                    name = (
                        self.rename.render(source, index, document, context)
                        if self.rename
                        else source.stem
                    )
                    target = out_dir / f"{name}{self.suffix}.pdf"
                    if target.resolve() == source.resolve() and not self.overwrite:
                        target = out_dir / f"{name}{self.suffix or '-processed'}.pdf"
                    target = _unique(target) if not self.overwrite else target
                    document.save_as(
                        target, context.get("save_options") or SaveOptions.optimized()
                    )
                    item.output = target
                else:
                    item.output = Path(context.get("converted_to", out_dir))
                item.details = {
                    k: v
                    for k, v in context.items()
                    if k not in ("output_dir", "stem", "index", "source", "save_options")
                }
            except Exception as exc:
                item.success = False
                item.error = str(exc)
                log.error("Batch failed for {}: {}", source.name, exc)
                if not self.continue_on_error:
                    item.duration = time.perf_counter() - started
                    result.items.append(item)
                    break
            finally:
                if document is not None:
                    document.close()
                item.duration = time.perf_counter() - started
                result.items.append(item)
                if ctx:
                    ctx.progress(index, f"{source.name}: {'ok' if item.success else 'failed'}")

        result.finished = time.time()
        log.info("Batch complete — {}", result.summary())
        return result


def merge_files(
    inputs: Sequence[str | Path],
    output: str | Path,
    *,
    passwords: dict[str, str] | None = None,
    ctx: JobContext | None = None,
) -> Path:
    """Convenience wrapper: merge many files into one PDF."""
    from pdfstudio.pdfengine.pages import merge_documents

    merged = merge_documents(list(inputs), passwords=passwords, ctx=ctx)
    try:
        return merged.save_as(output, SaveOptions.optimized())
    finally:
        merged.close()


def split_file(
    source: str | Path,
    output_dir: str | Path,
    *,
    pages_per_file: int = 1,
    password: str | None = None,
    ctx: JobContext | None = None,
) -> list[Path]:
    """Convenience wrapper: split one file into chunks."""
    from pdfstudio.pdfengine.pages import PageService

    document = PdfDocument.open(source, password)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        parts = PageService(document).split_by_count(pages_per_file)
        if ctx:
            ctx.set_total(len(parts))
        for n, part in enumerate(parts, 1):
            target = out_dir / f"{Path(source).stem}-{n:03d}.pdf"
            part.save_as(target)
            part.close()
            written.append(target)
            if ctx:
                ctx.progress(n, f"Wrote {target.name}")
    finally:
        document.close()
    return written


def _safe(text: str) -> str:
    """Strip characters that are illegal in filenames on any platform."""
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", text).strip()


def _unique(path: Path) -> Path:
    """Return a non-existing path by appending ``-1``, ``-2``, …"""
    if not path.exists():
        return path
    stem, suffix, parent = path.stem, path.suffix, path.parent
    counter = 1
    while (candidate := parent / f"{stem}-{counter}{suffix}").exists():
        counter += 1
    return candidate
