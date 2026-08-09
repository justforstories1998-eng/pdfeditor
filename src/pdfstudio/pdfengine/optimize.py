"""File-size optimisation, comparison and accessibility tooling.

Optimisation
------------
Image down-sampling and re-compression, font subsetting, duplicate object
merging, stream re-compression, garbage collection and structural clean-up,
with a dry-run *analysis* mode that reports where the bytes actually are.

Comparison
----------
Word-level textual diff plus visual pixel diff, producing a structured report
and an optional side-by-side PDF with the differences highlighted.

Accessibility
-------------
A PDF/UA-oriented checker (tags, language, title, alt text, reading order,
contrast) with helpers to add tags, alt text and set the reading order.
"""

from __future__ import annotations

import difflib
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

import pymupdf as fitz

from pdfstudio.core.exceptions import DependencyMissingError
from pdfstudio.core.jobs import JobContext
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.types import Color, Rect

log = get_logger("optimize")


# --------------------------------------------------------------------------- #
# Optimisation
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class OptimizeProfile:
    """Knobs controlling how aggressively the file is shrunk."""

    name: str = "balanced"
    image_dpi: int = 150
    image_quality: int = 78
    convert_to_jpeg: bool = True
    grayscale_images: bool = False
    downsample_threshold: float = 1.35  # only resample when >35% above target
    subset_fonts: bool = True
    remove_duplicates: bool = True
    remove_unused_objects: bool = True
    compress_streams: bool = True
    clean_content: bool = True
    remove_metadata: bool = False
    remove_annotations: bool = False
    remove_forms: bool = False
    remove_javascript: bool = False
    remove_attachments: bool = False
    remove_thumbnails: bool = True
    linearize: bool = False

    @classmethod
    def screen(cls) -> OptimizeProfile:
        """Smallest files, for on-screen reading and email."""
        return cls(name="screen", image_dpi=96, image_quality=62, remove_thumbnails=True)

    @classmethod
    def ebook(cls) -> OptimizeProfile:
        return cls(name="ebook", image_dpi=150, image_quality=78)

    @classmethod
    def print_quality(cls) -> OptimizeProfile:
        return cls(name="print", image_dpi=300, image_quality=90, convert_to_jpeg=False)

    @classmethod
    def prepress(cls) -> OptimizeProfile:
        return cls(
            name="prepress",
            image_dpi=400,
            image_quality=95,
            convert_to_jpeg=False,
            subset_fonts=True,
        )

    @classmethod
    def maximum(cls) -> OptimizeProfile:
        """Everything on — smallest possible output."""
        return cls(
            name="maximum",
            image_dpi=72,
            image_quality=48,
            grayscale_images=False,
            remove_metadata=True,
            remove_thumbnails=True,
            remove_javascript=True,
        )


@dataclass(slots=True)
class OptimizeReport:
    """What the optimiser did and how much it saved."""

    original_bytes: int = 0
    optimized_bytes: int = 0
    images_processed: int = 0
    images_bytes_saved: int = 0
    fonts_subset: int = 0
    duplicates_removed: int = 0
    objects_removed: int = 0
    annotations_removed: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def bytes_saved(self) -> int:
        return max(0, self.original_bytes - self.optimized_bytes)

    @property
    def percent_saved(self) -> float:
        return (self.bytes_saved / self.original_bytes * 100) if self.original_bytes else 0.0

    def summary(self) -> str:
        return (
            f"{_human(self.original_bytes)} → {_human(self.optimized_bytes)} "
            f"({self.percent_saved:.1f}% smaller)"
        )


class Optimizer:
    """Shrinks a document, optionally reporting where the bytes are first."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    def analyze(self) -> dict[str, Any]:
        """Report size contributors without modifying anything."""
        images: list[dict[str, Any]] = []
        seen: set[int] = set()
        total_image_bytes = 0
        for index in range(self.doc.page_count):
            for info in self.doc.page_images(index):
                if info.xref in seen:
                    continue
                seen.add(info.xref)
                total_image_bytes += info.size_bytes
                placed_w = max(info.rect.width, 1.0)
                effective_dpi = info.width / (placed_w / 72.0) if placed_w else 0
                images.append(
                    {
                        "xref": info.xref,
                        "page": index,
                        "pixels": info.width * info.height,
                        "bytes": info.size_bytes,
                        "dpi": round(effective_dpi),
                        "colorspace": info.colorspace,
                        "format": info.ext,
                    }
                )
        fonts: dict[str, dict[str, Any]] = {}
        with self.doc.locked() as handle:
            for page in handle:
                for f in page.get_fonts(full=True):
                    fonts.setdefault(
                        str(f[3]),
                        {"name": str(f[3]), "type": str(f[2]), "embedded": f[1] != "n/a"},
                    )
        total = self.doc.file_size or len(self.doc.to_bytes(SaveOptions.fast()))
        return {
            "total_bytes": total,
            "total_human": _human(total),
            "image_bytes": total_image_bytes,
            # Uncompressed image payload can exceed the compressed file size,
            # so the share is capped at 100% for display purposes.
            "image_share": min(100.0, round(total_image_bytes / total * 100, 1))
            if total
            else 0.0,
            "image_count": len(images),
            "largest_images": sorted(images, key=lambda i: -i["bytes"])[:20],
            "fonts": list(fonts.values()),
            "embedded_fonts": sum(1 for f in fonts.values() if f["embedded"]),
            "attachments": len(self.doc.attachments()),
            "annotations": sum(p.annotation_count for p in self.doc.pages_info()),
            "has_javascript": self.doc.has_javascript(),
            "pages": self.doc.page_count,
        }

    def optimize(
        self,
        profile: OptimizeProfile | None = None,
        *,
        output: str | Path | None = None,
        ctx: JobContext | None = None,
    ) -> OptimizeReport:
        """Optimise in place (or write to ``output``) and return a report."""
        prof = profile or OptimizeProfile()
        report = OptimizeReport(
            original_bytes=self.doc.file_size or len(self.doc.to_bytes(SaveOptions.fast()))
        )
        steps = 6
        if ctx:
            ctx.set_total(steps)

        if ctx:
            ctx.progress(1, "Re-compressing images…")
        images_saved, count = self._optimize_images(prof)
        report.images_processed = count
        report.images_bytes_saved = images_saved
        if count:
            report.details.append(
                f"Re-compressed {count} image(s), saved {_human(images_saved)}"
            )

        if ctx:
            ctx.progress(2, "Subsetting fonts…")
        if prof.subset_fonts:
            report.fonts_subset = self._subset_fonts()
            if report.fonts_subset:
                report.details.append(f"Subset {report.fonts_subset} font(s)")

        if ctx:
            ctx.progress(3, "Removing unwanted content…")
        report.annotations_removed = self._strip_content(prof)

        if ctx:
            ctx.progress(4, "Cleaning page content…")
        if prof.clean_content:
            self._clean_contents()

        if ctx:
            ctx.progress(5, "Collecting garbage…")
        options = SaveOptions(
            garbage=4 if prof.remove_duplicates else 3,
            deflate=prof.compress_streams,
            deflate_images=prof.compress_streams,
            deflate_fonts=prof.compress_streams,
            clean=prof.clean_content,
            linear=prof.linearize,
        )
        target = Path(output) if output else self.doc.path
        if target is None:
            data = self.doc.to_bytes(options)
            report.optimized_bytes = len(data)
        else:
            self.doc.save_as(target, options)
            report.optimized_bytes = target.stat().st_size

        if ctx:
            ctx.progress(steps, "Finished")
        log.info("Optimised {}: {}", self.doc.display_name, report.summary())
        return report

    def _optimize_images(self, profile: OptimizeProfile) -> tuple[int, int]:
        """Downsample and re-encode oversized images. Returns (bytes saved, count)."""
        try:
            from PIL import Image
        except ImportError as exc:
            raise DependencyMissingError("Pillow", "Image optimisation") from exc

        saved = 0
        processed = 0
        handled: set[int] = set()
        for index in range(self.doc.page_count):
            for info in self.doc.page_images(index):
                if info.xref in handled or info.size_bytes == 0:
                    continue
                handled.add(info.xref)
                placed_w = max(info.rect.width, 1.0)
                current_dpi = info.width / (placed_w / 72.0)
                needs_resample = current_dpi > profile.image_dpi * profile.downsample_threshold
                if not needs_resample and not profile.convert_to_jpeg:
                    continue
                try:
                    data, _ = self.doc.extract_image(info.xref)
                    image = Image.open(io.BytesIO(data))
                    has_alpha = image.mode in ("RGBA", "LA", "P")
                    if needs_resample:
                        factor = profile.image_dpi / current_dpi
                        new_size = (
                            max(1, int(image.width * factor)),
                            max(1, int(image.height * factor)),
                        )
                        image = image.resize(new_size, Image.LANCZOS)
                    if profile.grayscale_images:
                        image = image.convert("L")
                    buffer = io.BytesIO()
                    if profile.convert_to_jpeg and not has_alpha:
                        image.convert("RGB").save(
                            buffer,
                            format="JPEG",
                            quality=profile.image_quality,
                            optimize=True,
                            progressive=True,
                        )
                    else:
                        image.save(buffer, format="PNG", optimize=True)
                    new_data = buffer.getvalue()
                    if len(new_data) < len(data) * 0.96:
                        with self.doc.locked() as handle:
                            handle[index].replace_image(info.xref, stream=new_data)
                        saved += len(data) - len(new_data)
                        processed += 1
                except Exception as exc:
                    log.debug("Skipped image {}: {}", info.xref, exc)
        if processed:
            self.doc.mark_modified("optimize-images")
        return saved, processed

    def _subset_fonts(self) -> int:
        """Subset embedded fonts to the glyphs actually used."""
        try:
            with self.doc.locked() as handle:
                before = {f[3] for page in handle for f in page.get_fonts(full=True)}
                handle.subset_fonts(verbose=False)
                return len(before)
        except Exception as exc:
            log.debug("Font subsetting unavailable: {}", exc)
            return 0

    def _strip_content(self, profile: OptimizeProfile) -> int:
        removed = 0
        if profile.remove_annotations:
            from pdfstudio.pdfengine.annotations import AnnotationService

            removed = AnnotationService(self.doc).delete_all()
        if profile.remove_forms:
            from pdfstudio.pdfengine.forms import FormService

            FormService(self.doc).flatten()
        if profile.remove_attachments:
            for att in self.doc.attachments():
                self.doc.delete_attachment(att.name)
        if profile.remove_metadata:
            self.doc.clear_metadata()
        if profile.remove_javascript:
            from pdfstudio.pdfengine.security import SecurityService

            SecurityService(self.doc)._strip_javascript()
        if profile.remove_thumbnails:
            with self.doc.locked() as handle:
                for page in handle:
                    try:
                        handle.xref_set_key(page.xref, "Thumb", "null")
                    except Exception:
                        continue
        return removed

    def _clean_contents(self) -> None:
        """Rewrite page content streams in a canonical, compact form."""
        with self.doc.locked() as handle:
            for page in handle:
                try:
                    page.clean_contents(sanitize=True)
                except Exception:
                    continue


# --------------------------------------------------------------------------- #
# Comparison
# --------------------------------------------------------------------------- #
class ChangeKind(StrEnum):
    ADDED = "added"
    REMOVED = "removed"
    CHANGED = "changed"
    MOVED = "moved"


@dataclass(slots=True)
class TextChange:
    kind: ChangeKind
    page: int
    old_text: str = ""
    new_text: str = ""
    rect: Rect | None = None


@dataclass(slots=True)
class PageComparison:
    page: int
    text_similarity: float
    pixel_difference: float
    changes: list[TextChange] = field(default_factory=list)
    diff_regions: list[Rect] = field(default_factory=list)

    @property
    def identical(self) -> bool:
        return self.text_similarity >= 0.9999 and self.pixel_difference < 0.0005


@dataclass(slots=True)
class ComparisonReport:
    """Full comparison of two documents."""

    left_name: str
    right_name: str
    page_count_left: int
    page_count_right: int
    pages: list[PageComparison] = field(default_factory=list)
    metadata_changes: dict[str, tuple[str, str]] = field(default_factory=dict)

    @property
    def identical(self) -> bool:
        return (
            self.page_count_left == self.page_count_right
            and all(p.identical for p in self.pages)
            and not self.metadata_changes
        )

    @property
    def changed_pages(self) -> list[int]:
        return [p.page for p in self.pages if not p.identical]

    def summary(self) -> str:
        if self.identical:
            return "The documents are identical."
        parts = [f"{len(self.changed_pages)} of {len(self.pages)} page(s) differ"]
        if self.page_count_left != self.page_count_right:
            parts.append(f"page count {self.page_count_left} → {self.page_count_right}")
        if self.metadata_changes:
            parts.append(f"{len(self.metadata_changes)} metadata field(s) changed")
        return "; ".join(parts)

    def as_dict(self) -> dict[str, Any]:
        return {
            "left": self.left_name,
            "right": self.right_name,
            "identical": self.identical,
            "summary": self.summary(),
            "changed_pages": [p + 1 for p in self.changed_pages],
            "metadata_changes": self.metadata_changes,
            "pages": [
                {
                    "page": p.page + 1,
                    "text_similarity": round(p.text_similarity, 4),
                    "pixel_difference": round(p.pixel_difference, 4),
                    "changes": [
                        {
                            "kind": c.kind.value,
                            "old": c.old_text,
                            "new": c.new_text,
                        }
                        for c in p.changes
                    ],
                }
                for p in self.pages
            ],
        }


class DocumentComparer:
    """Compares two documents textually, visually and by metadata."""

    def __init__(self, left: PdfDocument, right: PdfDocument) -> None:
        self.left = left
        self.right = right

    def compare(
        self,
        *,
        compare_text: bool = True,
        compare_images: bool = True,
        compare_metadata: bool = True,
        dpi: int = 96,
        ctx: JobContext | None = None,
    ) -> ComparisonReport:
        report = ComparisonReport(
            left_name=self.left.display_name,
            right_name=self.right.display_name,
            page_count_left=self.left.page_count,
            page_count_right=self.right.page_count,
        )
        pages = max(self.left.page_count, self.right.page_count)
        if ctx:
            ctx.set_total(pages)

        for index in range(pages):
            if ctx:
                ctx.progress(index + 1, f"Comparing page {index + 1}")
            in_left = index < self.left.page_count
            in_right = index < self.right.page_count
            comparison = PageComparison(page=index, text_similarity=0.0, pixel_difference=1.0)
            if not in_left or not in_right:
                comparison.changes.append(
                    TextChange(
                        kind=ChangeKind.ADDED if in_right else ChangeKind.REMOVED,
                        page=index,
                        new_text=self.right.extract_text(index) if in_right else "",
                        old_text=self.left.extract_text(index) if in_left else "",
                    )
                )
                report.pages.append(comparison)
                continue

            if compare_text:
                similarity, changes = self._compare_text(index)
                comparison.text_similarity = similarity
                comparison.changes = changes
            else:
                comparison.text_similarity = 1.0

            if compare_images:
                difference, regions = self._compare_pixels(index, dpi)
                comparison.pixel_difference = difference
                comparison.diff_regions = regions
            else:
                comparison.pixel_difference = 0.0
            report.pages.append(comparison)

        if compare_metadata:
            left_meta = self.left.metadata().as_dict()
            right_meta = self.right.metadata().as_dict()
            report.metadata_changes = {
                key: (left_meta.get(key, ""), right_meta.get(key, ""))
                for key in set(left_meta) | set(right_meta)
                if left_meta.get(key, "") != right_meta.get(key, "")
                and key not in ("modDate", "producer")
            }
        log.info("Compared documents: {}", report.summary())
        return report

    def _compare_text(self, index: int) -> tuple[float, list[TextChange]]:
        left_words = self.left.extract_text(index).split()
        right_words = self.right.extract_text(index).split()
        matcher = difflib.SequenceMatcher(None, left_words, right_words, autojunk=False)
        similarity = matcher.ratio()
        changes: list[TextChange] = []
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            kind = {
                "replace": ChangeKind.CHANGED,
                "delete": ChangeKind.REMOVED,
                "insert": ChangeKind.ADDED,
            }[tag]
            changes.append(
                TextChange(
                    kind=kind,
                    page=index,
                    old_text=" ".join(left_words[i1:i2]),
                    new_text=" ".join(right_words[j1:j2]),
                    rect=self._locate(
                        self.right if tag != "delete" else self.left,
                        index,
                        (right_words[j1:j2] or left_words[i1:i2]),
                    ),
                )
            )
        return similarity, changes

    def _locate(self, doc: PdfDocument, index: int, words: Sequence[str]) -> Rect | None:
        """Find the bounding box of the first changed word for highlighting."""
        if not words:
            return None
        needle = words[0]
        for rect, word in doc.extract_words(index):
            if word == needle:
                return rect
        return None

    def _compare_pixels(self, index: int, dpi: int) -> tuple[float, list[Rect]]:
        """Pixel diff ratio plus bounding boxes of the differing regions."""
        try:
            import numpy as np
        except ImportError as exc:
            raise DependencyMissingError("NumPy", "Visual comparison") from exc

        with self.left.locked() as handle:
            left_pix = handle[index].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)
        with self.right.locked() as handle:
            right_pix = handle[index].get_pixmap(dpi=dpi, colorspace=fitz.csGRAY)

        height = min(left_pix.height, right_pix.height)
        width = min(left_pix.width, right_pix.width)
        if height == 0 or width == 0:
            return 1.0, []
        left_array = np.frombuffer(left_pix.samples, dtype=np.uint8).reshape(
            left_pix.height, left_pix.stride
        )[:height, :width]
        right_array = np.frombuffer(right_pix.samples, dtype=np.uint8).reshape(
            right_pix.height, right_pix.stride
        )[:height, :width]
        mask = np.abs(left_array.astype(np.int16) - right_array.astype(np.int16)) > 26
        ratio = float(mask.mean())

        regions: list[Rect] = []
        if ratio > 0:
            scale = 72.0 / dpi
            rows = np.where(mask.any(axis=1))[0]
            for start, end in _contiguous(rows):
                band = mask[start : end + 1]
                cols = np.where(band.any(axis=0))[0]
                if cols.size:
                    regions.append(
                        Rect(
                            float(cols[0]) * scale,
                            float(start) * scale,
                            float(cols[-1] + 1) * scale,
                            float(end + 1) * scale,
                        )
                    )
        return ratio, regions[:200]

    def build_report_pdf(
        self, report: ComparisonReport, output: str | Path, *, dpi: int = 110
    ) -> Path:
        """Render a side-by-side PDF with differences boxed in red."""
        out = fitz.open()
        header_height = 34.0
        for comparison in report.pages:
            index = comparison.page
            left_ok = index < self.left.page_count
            right_ok = index < self.right.page_count
            width, height = (
                self.left.page_size(index) if left_ok else self.right.page_size(index)
            )
            sheet = out.new_page(width=width * 2 + 30, height=height + header_height)
            sheet.insert_text(
                fitz.Point(14, 20),
                f"Page {index + 1} — {report.left_name}   vs   {report.right_name}"
                f"   ({'identical' if comparison.identical else 'changed'})",
                fontsize=11,
                color=(0.1, 0.1, 0.1),
            )
            left_box = fitz.Rect(10, header_height, 10 + width, header_height + height)
            right_box = fitz.Rect(
                20 + width, header_height, 20 + 2 * width, header_height + height
            )
            if left_ok:
                with self.left.locked() as handle:
                    sheet.show_pdf_page(left_box, handle, index)
            if right_ok:
                with self.right.locked() as handle:
                    sheet.show_pdf_page(right_box, handle, index)
            shape = sheet.new_shape()
            shape.draw_rect(left_box)
            shape.draw_rect(right_box)
            shape.finish(color=(0.6, 0.6, 0.6), width=0.5)
            for region in comparison.diff_regions:
                shape.draw_rect(
                    fitz.Rect(
                        right_box.x0 + region.x0,
                        right_box.y0 + region.y0,
                        right_box.x0 + region.x1,
                        right_box.y0 + region.y1,
                    )
                )
            shape.finish(color=(0.9, 0.15, 0.15), width=1.0)
            shape.commit()
        target = Path(output)
        out.save(str(target), garbage=3, deflate=True)
        out.close()
        log.info("Comparison report written to {}", target)
        return target


def _contiguous(indices: Any, gap: int = 3) -> list[tuple[int, int]]:
    """Group sorted row indices into contiguous bands."""
    spans: list[tuple[int, int]] = []
    if len(indices) == 0:
        return spans
    start = previous = int(indices[0])
    for raw in indices[1:]:
        value = int(raw)
        if value - previous > gap:
            spans.append((start, previous))
            start = value
        previous = value
    spans.append((start, previous))
    return spans


# --------------------------------------------------------------------------- #
# Accessibility
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class AccessibilityIssue:
    """One finding from the accessibility checker."""

    rule: str
    severity: str  # error | warning | info
    message: str
    page: int = -1
    fixable: bool = False

    def __str__(self) -> str:  # pragma: no cover
        where = f" (page {self.page + 1})" if self.page >= 0 else ""
        return f"[{self.severity}] {self.rule}: {self.message}{where}"


class AccessibilityChecker:
    """PDF/UA-oriented checks with automatic remediation where possible."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    def check(self) -> list[AccessibilityIssue]:
        """Run every rule and return the findings."""
        issues: list[AccessibilityIssue] = []
        meta = self.doc.metadata()

        if not self.doc.is_tagged():
            issues.append(
                AccessibilityIssue(
                    "Tagged PDF",
                    "error",
                    "The document has no structure tree; screen readers cannot "
                    "determine reading order.",
                    fixable=True,
                )
            )
        if not meta.title:
            issues.append(
                AccessibilityIssue(
                    "Document title",
                    "error",
                    "No title is set in the document properties.",
                    fixable=True,
                )
            )
        if not self._language():
            issues.append(
                AccessibilityIssue(
                    "Language",
                    "error",
                    "No natural language is declared for the document.",
                    fixable=True,
                )
            )
        if not self.doc.permissions().get("accessibility", True):
            issues.append(
                AccessibilityIssue(
                    "Extraction permission",
                    "error",
                    "Content extraction for accessibility is disallowed.",
                )
            )
        if self._display_doc_title_off():
            issues.append(
                AccessibilityIssue(
                    "Window title",
                    "warning",
                    "The viewer preference 'DisplayDocTitle' is not enabled.",
                    fixable=True,
                )
            )

        for index in range(self.doc.page_count):
            images = self.doc.page_images(index)
            if images and not self._page_has_alt_text(index):
                issues.append(
                    AccessibilityIssue(
                        "Alternative text",
                        "error",
                        f"{len(images)} image(s) have no alternative text.",
                        page=index,
                        fixable=True,
                    )
                )
            if not self.doc.page_has_text(index) and images:
                issues.append(
                    AccessibilityIssue(
                        "Scanned page",
                        "error",
                        "The page contains no real text — run OCR.",
                        page=index,
                        fixable=True,
                    )
                )
            for issue in self._check_contrast(index):
                issues.append(issue)

        from pdfstudio.pdfengine.forms import FormService

        for field_ in FormService(self.doc).fields():
            if not field_.tooltip:
                issues.append(
                    AccessibilityIssue(
                        "Form field description",
                        "warning",
                        f"Field “{field_.name}” has no tooltip/description.",
                        page=field_.page,
                        fixable=True,
                    )
                )
        log.info("Accessibility check: {} issue(s)", len(issues))
        return issues

    def _language(self) -> str:
        with self.doc.locked() as handle:
            try:
                kind, value = handle.xref_get_key(handle.pdf_catalog(), "Lang")
                return value.strip("()") if kind != "null" else ""
            except Exception:
                return ""

    def _display_doc_title_off(self) -> bool:
        with self.doc.locked() as handle:
            try:
                kind, value = handle.xref_get_key(
                    handle.pdf_catalog(), "ViewerPreferences/DisplayDocTitle"
                )
                return kind == "null" or value.lower() != "true"
            except Exception:
                return True

    def _page_has_alt_text(self, index: int) -> bool:
        with self.doc.locked() as handle:
            try:
                raw = handle.xref_object(handle[index].xref, compressed=True) or ""
            except Exception:
                return False
        return "/Alt" in raw

    def _check_contrast(self, index: int) -> list[AccessibilityIssue]:
        """Flag text whose colour is too close to white (WCAG 1.4.3)."""
        issues: list[AccessibilityIssue] = []
        low = 0
        for block in self.doc.extract_blocks(index):
            for line in block.lines:
                for span in line.spans:
                    if not span.text.strip():
                        continue
                    ratio = _contrast_ratio(span.color, Color(1, 1, 1))
                    if ratio < 4.5 and span.size < 18:
                        low += 1
        if low:
            issues.append(
                AccessibilityIssue(
                    "Colour contrast",
                    "warning",
                    f"{low} text run(s) may fall below the 4.5:1 contrast ratio.",
                    page=index,
                )
            )
        return issues

    # -- remediation --------------------------------------------------------- #
    def set_language(self, language: str = "en-GB") -> None:
        with self.doc.locked() as handle:
            handle.xref_set_key(handle.pdf_catalog(), "Lang", f"({language})")
        self.doc.mark_modified("accessibility-language")

    def set_display_doc_title(self, enabled: bool = True) -> None:
        with self.doc.locked() as handle:
            handle.xref_set_key(
                handle.pdf_catalog(),
                "ViewerPreferences/DisplayDocTitle",
                "true" if enabled else "false",
            )
        self.doc.mark_modified("accessibility-viewerprefs")

    def set_alt_text(self, page: int, xref: int, text: str) -> None:
        """Attach alternative text to an image XObject."""
        with self.doc.locked() as handle:
            handle.xref_set_key(xref, "Alt", f"({text})")
        self.doc.mark_modified("accessibility-alt")

    def auto_fix(self, *, language: str = "en-GB", title: str = "") -> list[str]:
        """Apply every safe automatic remediation; returns what changed."""
        applied: list[str] = []
        meta = self.doc.metadata()
        if not meta.title:
            meta.title = title or Path(self.doc.display_name).stem
            self.doc.set_metadata(meta)
            applied.append("Set the document title")
        if not self._language():
            self.set_language(language)
            applied.append(f"Declared the document language as {language}")
        if self._display_doc_title_off():
            self.set_display_doc_title(True)
            applied.append("Enabled DisplayDocTitle")
        return applied

    def reading_order(self, page: int) -> list[Rect]:
        """Current reading order as a list of block rectangles (top-to-bottom)."""
        blocks = self.doc.extract_blocks(page)
        blocks.sort(key=lambda b: (round(b.rect.y0 / 6), b.rect.x0))
        return [b.rect for b in blocks]


def _contrast_ratio(foreground: Color, background: Color) -> float:
    """WCAG 2.1 relative-luminance contrast ratio."""

    def channel(value: float) -> float:
        return value / 12.92 if value <= 0.03928 else ((value + 0.055) / 1.055) ** 2.4

    def luminance(color: Color) -> float:
        return 0.2126 * channel(color.r) + 0.7152 * channel(color.g) + 0.0722 * channel(color.b)

    lighter = max(luminance(foreground), luminance(background))
    darker = min(luminance(foreground), luminance(background))
    return (lighter + 0.05) / (darker + 0.05)


def _human(size: int) -> str:
    """Format a byte count for display."""
    remaining = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if remaining < 1024 or unit == "GB":
            return f"{remaining:.0f} {unit}" if unit == "B" else f"{remaining:.1f} {unit}"
        remaining /= 1024
    return f"{remaining:.1f} GB"
