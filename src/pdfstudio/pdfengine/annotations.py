"""Annotation engine: markup, drawing, stamps, measurement, redaction, replies.

Covers the full Acrobat annotation set: text markup (highlight/underline/
strikeout/squiggly), sticky notes, free text and callouts, ink, geometric
shapes, polygons/polylines, clouds, stamps (built-in, custom image and text),
measurement annotations, file attachments, media and redaction.

Also implements the comment *workflow* features — replies, review states
(accepted / rejected / resolved), and import/export of comment sets as XFDF or
JSON so annotations can travel between documents and reviewers.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pymupdf as fitz

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.exceptions import ValidationError
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.undo import Command
from pdfstudio.pdfengine.content import PageSnapshotCommand
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import (
    BLACK,
    RED,
    YELLOW,
    Annotation,
    AnnotationType,
    Color,
    Point,
    Rect,
)

log = get_logger("annotations")

#: Built-in stamp names supported by the PDF specification.
STANDARD_STAMPS: tuple[str, ...] = (
    "Approved",
    "AsIs",
    "Confidential",
    "Departmental",
    "Draft",
    "Experimental",
    "Expired",
    "Final",
    "ForComment",
    "ForPublicRelease",
    "NotApproved",
    "NotForPublicRelease",
    "Sold",
    "TopSecret",
)

_STAMP_IDS = {name: i for i, name in enumerate(STANDARD_STAMPS)}

#: Icons for sticky-note annotations.
NOTE_ICONS: tuple[str, ...] = (
    "Note",
    "Comment",
    "Help",
    "Insert",
    "Key",
    "NewParagraph",
    "Paragraph",
)


def _now() -> str:
    return datetime.now(UTC).strftime("D:%Y%m%d%H%M%SZ")


def _read_source(source: str | Path) -> str:
    """Return the text of ``source``, which may be a path or literal content.

    Guards against ``OSError`` when a long literal document is passed where a
    path is also accepted (file names have a length limit on every OS).
    """
    if isinstance(source, Path):
        return source.read_text("utf-8")
    text = str(source)
    if len(text) < 4096 and "\n" not in text:
        try:
            candidate = Path(text)
            if candidate.exists():
                return candidate.read_text("utf-8")
        except OSError:  # pragma: no cover - defensive
            pass
    return text


@dataclass(slots=True)
class AnnotationStyle:
    """Visual style applied to new annotations."""

    color: Color = YELLOW
    interior: Color | None = None
    opacity: float = 1.0
    width: float = 1.5
    dashes: tuple[int, ...] = ()
    author: str = ""
    subject: str = ""
    font: str = "Helvetica"
    font_size: float = 11.0
    text_color: Color = BLACK
    cloud_intensity: int = 0  # >0 turns rect/polygon borders into clouds
    line_start: str = "None"
    line_end: str = "None"


class AddAnnotationCommand(Command):
    """Create an annotation; undo removes it by xref."""

    def __init__(
        self,
        doc: PdfDocument,
        page: int,
        factory: Any,
        label: str,
    ) -> None:
        super().__init__(label)
        self.doc = doc
        self.page = page
        self._factory = factory
        self._xref: int = 0

    def execute(self) -> None:
        with self.doc.locked() as handle:
            annot = self._factory(handle[self.page])
            self._xref = annot.xref if annot else 0
        self.doc.mark_modified(self.label)
        bus().publish(
            Topic.ANNOTATION_ADDED,
            {"document_id": self.doc.id, "page": self.page, "xref": self._xref},
            source="annotations",
        )

    def undo(self) -> None:
        with self.doc.locked() as handle:
            page = handle[self.page]
            for annot in page.annots():
                if annot.xref == self._xref:
                    page.delete_annot(annot)
                    break
        self.doc.mark_modified(f"Undo {self.label}")
        bus().publish(
            Topic.ANNOTATION_REMOVED,
            {"document_id": self.doc.id, "page": self.page, "xref": self._xref},
            source="annotations",
        )


class DeleteAnnotationCommand(PageSnapshotCommand):
    """Delete one or more annotations (page snapshot for exact restore)."""

    def __init__(self, doc: PdfDocument, page: int, xrefs: Sequence[int]) -> None:
        super().__init__(doc, page, f"Delete {len(xrefs)} annotation(s)")
        self.xrefs = set(xrefs)

    def apply(self) -> None:
        with self.doc.locked() as handle:
            page = handle[self.page]
            for annot in list(page.annots()):
                if annot.xref in self.xrefs:
                    page.delete_annot(annot)


class UpdateAnnotationCommand(PageSnapshotCommand):
    """Change properties of an existing annotation."""

    def __init__(self, doc: PdfDocument, page: int, xref: int, changes: dict[str, Any]) -> None:
        super().__init__(doc, page, "Edit annotation")
        self.xref = xref
        self.changes = changes

    def apply(self) -> None:
        with self.doc.locked() as handle:
            page = handle[self.page]
            annot = next((a for a in page.annots() if a.xref == self.xref), None)
            if annot is None:
                raise ValidationError("Annotation no longer exists.")
            info = annot.info
            for key in ("content", "title", "subject"):
                if key in self.changes:
                    info[key] = self.changes[key]
            info["modDate"] = _now()
            annot.set_info(info)
            if "color" in self.changes:
                color: Color = self.changes["color"]
                annot.set_colors(stroke=color.to_rgb_tuple())
            if self.changes.get("interior"):
                annot.set_colors(fill=self.changes["interior"].to_rgb_tuple())
            if "opacity" in self.changes:
                annot.set_opacity(float(self.changes["opacity"]))
            if "rect" in self.changes:
                annot.set_rect(fitz.Rect(*self.changes["rect"]))
            if "width" in self.changes:
                annot.set_border(width=float(self.changes["width"]))
            if "flags" in self.changes:
                annot.set_flags(int(self.changes["flags"]))
            annot.update()


class AnnotationService:
    """Create, modify, query, import and export annotations."""

    def __init__(self, document: PdfDocument, *, author: str = "") -> None:
        self.doc = document
        self.author = author or "PDF Studio user"

    # -- helpers ------------------------------------------------------------ #
    def _finish(self, annot: fitz.Annot, style: AnnotationStyle, contents: str) -> None:
        info = annot.info
        info["title"] = style.author or self.author
        info["content"] = contents
        info["subject"] = style.subject
        info["creationDate"] = _now()
        info["modDate"] = _now()
        annot.set_info(info)
        if style.opacity < 1.0:
            annot.set_opacity(style.opacity)
        annot.update()

    def _push(self, page: int, factory: Any, label: str) -> None:
        self.doc.undo_stack.push(AddAnnotationCommand(self.doc, page, factory, label))

    # -- text markup -------------------------------------------------------- #
    def markup(
        self,
        page: int,
        quads: Sequence[Rect],
        kind: AnnotationType = AnnotationType.HIGHLIGHT,
        *,
        style: AnnotationStyle | None = None,
        contents: str = "",
    ) -> None:
        """Add highlight / underline / strikeout / squiggly over ``quads``."""
        st = style or AnnotationStyle(color=YELLOW if kind is AnnotationType.HIGHLIGHT else RED)
        makers = {
            AnnotationType.HIGHLIGHT: "add_highlight_annot",
            AnnotationType.UNDERLINE: "add_underline_annot",
            AnnotationType.STRIKEOUT: "add_strikeout_annot",
            AnnotationType.SQUIGGLY: "add_squiggly_annot",
        }
        if kind not in makers:
            raise ValidationError(f"{kind} is not a text-markup annotation.")

        def factory(p: fitz.Page) -> fitz.Annot:
            rects = [fitz.Rect(*q) for q in quads]
            annot = getattr(p, makers[kind])(rects)
            annot.set_colors(stroke=st.color.to_rgb_tuple())
            self._finish(annot, st, contents)
            return annot

        self._push(page, factory, f"Add {kind.value}")

    def highlight_text(
        self, page: int, needle: str, *, style: AnnotationStyle | None = None
    ) -> int:
        """Highlight every occurrence of ``needle`` on a page."""
        with self.doc.locked() as handle:
            quads = handle[page].search_for(needle)
        if not quads:
            return 0
        rects = [Rect(q.x0, q.y0, q.x1, q.y1) for q in quads]
        self.markup(page, rects, AnnotationType.HIGHLIGHT, style=style)
        return len(rects)

    # -- notes and free text ------------------------------------------------ #
    def sticky_note(
        self,
        page: int,
        point: Point,
        contents: str,
        *,
        icon: str = "Note",
        style: AnnotationStyle | None = None,
    ) -> None:
        st = style or AnnotationStyle(color=YELLOW)

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_text_annot(fitz.Point(point.x, point.y), contents, icon=icon)
            annot.set_colors(stroke=st.color.to_rgb_tuple())
            self._finish(annot, st, contents)
            return annot

        self._push(page, factory, "Add sticky note")

    def free_text(
        self,
        page: int,
        rect: Rect,
        text: str,
        *,
        style: AnnotationStyle | None = None,
        align: int = 0,
        rotate: int = 0,
    ) -> None:
        """A free-text (typewriter) annotation."""
        st = style or AnnotationStyle(color=BLACK)

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_freetext_annot(
                fitz.Rect(*rect),
                text,
                fontsize=st.font_size,
                fontname=st.font.split("-")[0][:8] or "Helv",
                text_color=st.text_color.to_rgb_tuple(),
                fill_color=st.interior.to_rgb_tuple() if st.interior else None,
                border_width=st.width,
                align=align,
                rotate=rotate,
            )
            self._finish(annot, st, text)
            return annot

        self._push(page, factory, "Add text box")

    def callout(
        self,
        page: int,
        rect: Rect,
        text: str,
        target: Point,
        *,
        style: AnnotationStyle | None = None,
    ) -> None:
        """Free text with a leader line pointing at ``target``."""
        st = style or AnnotationStyle(color=BLACK)

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_freetext_annot(
                fitz.Rect(*rect),
                text,
                fontsize=st.font_size,
                text_color=st.text_color.to_rgb_tuple(),
                fill_color=(st.interior or Color(1, 1, 0.85)).to_rgb_tuple(),
                border_width=st.width,
            )
            # /CL is written in native PDF space (origin bottom-left) while
            # our Point/Rect use the top-left convention, so flip Y.
            height = p.rect.height
            tx, ty = target.x, height - target.y
            sx, sy = rect.x0, height - rect.y1
            kx, ky = (tx + sx) / 2, (ty + sy) / 2
            p.parent.xref_set_key(
                annot.xref, "CL", f"[{tx:.2f} {ty:.2f} {kx:.2f} {ky:.2f} {sx:.2f} {sy:.2f}]"
            )
            p.parent.xref_set_key(annot.xref, "IT", "/FreeTextCallout")
            p.parent.xref_set_key(annot.xref, "LE", "/OpenArrow")
            self._finish(annot, st, text)
            return annot

        self._push(page, factory, "Add callout")

    # -- drawing ------------------------------------------------------------ #
    def ink(
        self,
        page: int,
        strokes: Sequence[Sequence[Point]],
        *,
        style: AnnotationStyle | None = None,
    ) -> None:
        """Freehand ink (pen, pencil, marker) — one entry per stroke."""
        st = style or AnnotationStyle(color=BLACK, width=2.0)

        def factory(p: fitz.Page) -> fitz.Annot:
            # PyMuPDF expects a sequence of strokes, each a sequence of (x, y).
            annot = p.add_ink_annot(
                [[(float(pt.x), float(pt.y)) for pt in stroke] for stroke in strokes]
            )
            annot.set_colors(stroke=st.color.to_rgb_tuple())
            annot.set_border(width=st.width, dashes=list(st.dashes) or None)
            self._finish(annot, st, "")
            return annot

        self._push(page, factory, "Draw ink")

    def line(
        self,
        page: int,
        start: Point,
        end: Point,
        *,
        style: AnnotationStyle | None = None,
        arrow: bool = False,
    ) -> None:
        st = style or AnnotationStyle(color=RED, width=1.5)

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_line_annot(fitz.Point(start.x, start.y), fitz.Point(end.x, end.y))
            annot.set_colors(stroke=st.color.to_rgb_tuple())
            annot.set_border(width=st.width, dashes=list(st.dashes) or None)
            if arrow or st.line_end != "None":
                annot.set_line_ends(
                    fitz.PDF_ANNOT_LE_NONE,
                    fitz.PDF_ANNOT_LE_CLOSED_ARROW if arrow else fitz.PDF_ANNOT_LE_NONE,
                )
            self._finish(annot, st, "")
            return annot

        self._push(page, factory, "Draw arrow" if arrow else "Draw line")

    def shape(
        self,
        page: int,
        rect: Rect,
        kind: AnnotationType = AnnotationType.SQUARE,
        *,
        style: AnnotationStyle | None = None,
    ) -> None:
        """Rectangle or ellipse annotation, optionally cloud-bordered."""
        st = style or AnnotationStyle(color=RED)

        def factory(p: fitz.Page) -> fitz.Annot:
            if kind is AnnotationType.CIRCLE:
                annot = p.add_circle_annot(fitz.Rect(*rect))
            else:
                annot = p.add_rect_annot(fitz.Rect(*rect))
            annot.set_colors(
                stroke=st.color.to_rgb_tuple(),
                fill=st.interior.to_rgb_tuple() if st.interior else None,
            )
            annot.set_border(width=st.width, dashes=list(st.dashes) or None)
            if st.cloud_intensity > 0:
                annot.set_border(width=st.width, clouds=int(st.cloud_intensity))
            self._finish(annot, st, "")
            return annot

        self._push(page, factory, f"Draw {kind.value}")

    def polygon(
        self,
        page: int,
        points: Sequence[Point],
        *,
        closed: bool = True,
        style: AnnotationStyle | None = None,
    ) -> None:
        st = style or AnnotationStyle(color=RED)
        if len(points) < 2:
            raise ValidationError("A polygon needs at least two points.")

        def factory(p: fitz.Page) -> fitz.Annot:
            pts = [fitz.Point(pt.x, pt.y) for pt in points]
            annot = p.add_polygon_annot(pts) if closed else p.add_polyline_annot(pts)
            annot.set_colors(
                stroke=st.color.to_rgb_tuple(),
                fill=st.interior.to_rgb_tuple() if st.interior else None,
            )
            annot.set_border(width=st.width)
            if st.cloud_intensity > 0:
                annot.set_border(width=st.width, clouds=int(st.cloud_intensity))
            self._finish(annot, st, "")
            return annot

        self._push(page, factory, "Draw polygon" if closed else "Draw polyline")

    def cloud(
        self, page: int, points: Sequence[Point], *, style: AnnotationStyle | None = None
    ) -> None:
        """Cloud-shaped revision annotation."""
        st = style or AnnotationStyle(color=RED, cloud_intensity=2)
        self.polygon(page, points, closed=True, style=st)

    # -- stamps ------------------------------------------------------------- #
    def stamp(
        self,
        page: int,
        rect: Rect,
        name: str = "Approved",
        *,
        style: AnnotationStyle | None = None,
    ) -> None:
        """Standard PDF stamp."""
        st = style or AnnotationStyle()
        if name not in _STAMP_IDS:
            raise ValidationError(f"Unknown standard stamp {name!r}")

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_stamp_annot(fitz.Rect(*rect), stamp=_STAMP_IDS[name])
            self._finish(annot, st, name)
            return annot

        self._push(page, factory, f"Stamp: {name}")

    def image_stamp(
        self, page: int, rect: Rect, image: bytes | str, *, opacity: float = 1.0
    ) -> None:
        """Custom stamp from an image (signatures, logos, approval marks)."""
        from pdfstudio.pdfengine.content import _load_image_bytes

        payload = _load_image_bytes(image)

        class _ImageStamp(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    handle[page].insert_image(fitz.Rect(*rect), stream=payload, overlay=True)

        self.doc.undo_stack.push(_ImageStamp(self.doc, page, "Add image stamp"))

    def text_stamp(
        self,
        page: int,
        rect: Rect,
        text: str,
        *,
        style: AnnotationStyle | None = None,
        border: bool = True,
    ) -> None:
        """Custom text stamp drawn as a free-text annotation with a border."""
        st = style or AnnotationStyle(
            color=RED, text_color=RED, font_size=18.0, width=2.0 if border else 0.0
        )
        self.free_text(page, rect, text, style=st, align=1)

    # -- measurement -------------------------------------------------------- #
    def measure_distance(
        self,
        page: int,
        start: Point,
        end: Point,
        *,
        scale: float = 1.0,
        unit: str = "pt",
        style: AnnotationStyle | None = None,
    ) -> float:
        """Draw a distance measurement and return the measured value."""
        distance = start.distance_to(end) * scale
        st = style or AnnotationStyle(color=RED, width=1.0)
        label = f"{distance:.2f} {unit}"
        self.line(page, start, end, style=st, arrow=True)
        mid = Point((start.x + end.x) / 2, (start.y + end.y) / 2 - 12)
        self.free_text(
            page,
            Rect(mid.x - 45, mid.y - 10, mid.x + 45, mid.y + 10),
            label,
            style=AnnotationStyle(
                color=st.color, text_color=st.color, font_size=9, subject="Distance"
            ),
            align=1,
        )
        return distance

    def measure_area(
        self,
        page: int,
        points: Sequence[Point],
        *,
        scale: float = 1.0,
        unit: str = "pt",
        style: AnnotationStyle | None = None,
    ) -> float:
        """Polygon area measurement (shoelace formula)."""
        if len(points) < 3:
            raise ValidationError("Area measurement needs at least three points.")
        area = 0.0
        for i, p in enumerate(points):
            q = points[(i + 1) % len(points)]
            area += p.x * q.y - q.x * p.y
        area = abs(area) / 2 * scale * scale
        st = style or AnnotationStyle(color=RED, interior=None)
        with self.doc.undo_stack.macro("Measure area"):
            self.polygon(page, points, closed=True, style=st)
            centre = Point(
                sum(p.x for p in points) / len(points),
                sum(p.y for p in points) / len(points),
            )
            self.free_text(
                page,
                Rect(centre.x - 55, centre.y - 10, centre.x + 55, centre.y + 10),
                f"{area:.2f} {unit}²",
                style=AnnotationStyle(
                    color=st.color, text_color=st.color, font_size=9, subject="Area"
                ),
                align=1,
            )
        return area

    def measure_perimeter(
        self,
        page: int,
        points: Sequence[Point],
        *,
        scale: float = 1.0,
        unit: str = "pt",
        style: AnnotationStyle | None = None,
    ) -> float:
        """Perimeter of an open or closed path."""
        total = (
            sum(points[i].distance_to(points[i + 1]) for i in range(len(points) - 1)) * scale
        )
        st = style or AnnotationStyle(color=RED)
        with self.doc.undo_stack.macro("Measure perimeter"):
            self.polygon(page, points, closed=False, style=st)
            last = points[-1]
            self.free_text(
                page,
                Rect(last.x, last.y - 10, last.x + 110, last.y + 10),
                f"{total:.2f} {unit}",
                style=AnnotationStyle(
                    color=st.color, text_color=st.color, font_size=9, subject="Perimeter"
                ),
            )
        return total

    # -- attachments & media ------------------------------------------------ #
    def file_attachment(
        self,
        page: int,
        point: Point,
        filename: str,
        data: bytes,
        *,
        description: str = "",
        icon: str = "PushPin",
    ) -> None:
        """Attach a file at a point on the page."""

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_file_annot(
                fitz.Point(point.x, point.y),
                data,
                filename,
                desc=description,
                icon=icon,
            )
            self._finish(annot, AnnotationStyle(), description or filename)
            return annot

        self._push(page, factory, f"Attach {filename}")

    def media(
        self,
        page: int,
        rect: Rect,
        filename: str,
        data: bytes,
        *,
        mime: str = "video/mp4",
        poster: bytes | None = None,
    ) -> None:
        """Embed audio/video as a screen annotation with a rich-media action."""

        class _Media(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    p = handle[page]
                    if poster:
                        p.insert_image(fitz.Rect(*rect), stream=poster, overlay=True)
                    else:
                        shape = p.new_shape()
                        shape.draw_rect(fitz.Rect(*rect))
                        shape.finish(color=(0.2, 0.2, 0.2), fill=(0.9, 0.9, 0.92), width=1)
                        shape.commit()
                    handle.embfile_add(filename, data, filename=filename, desc=mime)
                    annot = p.add_file_annot(
                        fitz.Point(rect.x0 + 6, rect.y0 + 6), data, filename, desc=mime
                    )
                    annot.update()

        self.doc.undo_stack.push(_Media(self.doc, page, f"Embed {filename}"))

    # -- redaction ---------------------------------------------------------- #
    def mark_redaction(
        self,
        page: int,
        rect: Rect,
        *,
        fill: Color = BLACK,
        overlay_text: str = "",
        style: AnnotationStyle | None = None,
    ) -> None:
        """Mark an area for redaction (apply later with :meth:`apply_redactions`)."""
        st = style or AnnotationStyle(color=RED)

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_redact_annot(
                fitz.Rect(*rect),
                text=overlay_text or None,
                fill=fill.to_rgb_tuple(),
                text_color=(1, 1, 1),
                fontsize=st.font_size,
                align=fitz.TEXT_ALIGN_CENTER,
            )
            self._finish(annot, st, overlay_text)
            return annot

        self._push(page, factory, "Mark for redaction")

    def apply_redactions(
        self, pages: Sequence[int] | None = None, *, remove_images: bool = True
    ) -> int:
        """Permanently remove content under redaction marks.

        Returns the number of pages changed.  This is irreversible in the saved
        file — the undo entry only restores the in-memory document.
        """
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        changed = 0
        with self.doc.undo_stack.macro("Apply redactions"):
            for index in targets:
                if not any(
                    a.type is AnnotationType.REDACT for a in self.doc.page_annotations(index)
                ):
                    continue

                class _Apply(PageSnapshotCommand):
                    def apply(inner, i: int = index) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            handle[i].apply_redactions(
                                images=(
                                    fitz.PDF_REDACT_IMAGE_PIXELS
                                    if remove_images
                                    else fitz.PDF_REDACT_IMAGE_NONE
                                ),
                                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_TOUCHED,
                                text=fitz.PDF_REDACT_TEXT_REMOVE,
                            )

                self.doc.undo_stack.push(_Apply(self.doc, index, f"Redact page {index + 1}"))
                changed += 1
        log.info("Applied redactions on {} page(s)", changed)
        return changed

    def redact_text(
        self, pattern: str, *, regex: bool = False, pages: Sequence[int] | None = None
    ) -> int:
        """Mark every match of ``pattern`` for redaction. Returns match count."""
        import re as _re

        targets = list(pages) if pages is not None else range(self.doc.page_count)
        count = 0
        with self.doc.undo_stack.macro(f"Mark “{pattern}” for redaction"):
            for index in targets:
                if regex:
                    compiled = _re.compile(pattern)
                    for rect, word in self.doc.extract_words(index):
                        if compiled.search(word):
                            self.mark_redaction(index, rect)
                            count += 1
                else:
                    with self.doc.locked() as handle:
                        quads = handle[index].search_for(pattern)
                    for q in quads:
                        self.mark_redaction(index, Rect(q.x0, q.y0, q.x1, q.y1))
                        count += 1
        return count

    # -- modification / workflow -------------------------------------------- #
    def update(self, page: int, xref: int, **changes: Any) -> None:
        self.doc.undo_stack.push(UpdateAnnotationCommand(self.doc, page, xref, changes))

    def delete(self, page: int, xrefs: Sequence[int]) -> None:
        self.doc.undo_stack.push(DeleteAnnotationCommand(self.doc, page, xrefs))

    def delete_all(self, pages: Sequence[int] | None = None) -> int:
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        removed = 0
        with self.doc.undo_stack.macro("Delete all annotations"):
            for index in targets:
                xrefs = [
                    a.extra["xref"]
                    for a in self.doc.page_annotations(index)
                    if "xref" in a.extra
                ]
                if xrefs:
                    self.delete(index, xrefs)
                    removed += len(xrefs)
        return removed

    def reply(self, page: int, xref: int, text: str, *, author: str = "") -> None:
        """Add a threaded reply to an existing comment."""
        parent = next(
            (a for a in self.doc.page_annotations(page) if a.extra.get("xref") == xref),
            None,
        )
        if parent is None:
            raise ValidationError("Parent annotation not found.")
        st = AnnotationStyle(author=author or self.author, subject="Reply")

        def factory(p: fitz.Page) -> fitz.Annot:
            annot = p.add_text_annot(
                fitz.Point(parent.rect.x1 + 4, parent.rect.y0), text, icon="Comment"
            )
            self._finish(annot, st, text)
            p.parent.xref_set_key(annot.xref, "IRT", f"{xref} 0 R")
            p.parent.xref_set_key(annot.xref, "RT", "/R")
            return annot

        self._push(page, factory, "Reply to comment")

    def set_state(self, page: int, xref: int, state: str, *, model: str = "Review") -> None:
        """Set a review state: Accepted, Rejected, Cancelled, Completed, None,
        or Marked/Unmarked with ``model='Marked'``."""
        valid = {
            "Review": {"Accepted", "Rejected", "Cancelled", "Completed", "None"},
            "Marked": {"Marked", "Unmarked"},
        }
        if state not in valid.get(model, set()):
            raise ValidationError(f"Invalid state {state!r} for model {model!r}")
        with self.doc.locked() as handle:
            handle.xref_set_key(xref, "State", f"({state})")
            handle.xref_set_key(xref, "StateModel", f"({model})")
        self.doc.mark_modified("annotation-state")

    def resolve(self, page: int, xref: int, *, resolved: bool = True) -> None:
        """Mark a comment thread resolved (uses the Review state model)."""
        self.set_state(page, xref, "Completed" if resolved else "None")

    # -- query -------------------------------------------------------------- #
    def all(self, pages: Sequence[int] | None = None) -> list[Annotation]:
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        return [a for i in targets for a in self.doc.page_annotations(i)]

    def by_author(self, author: str) -> list[Annotation]:
        return [a for a in self.all() if a.author == author]

    def authors(self) -> list[str]:
        return sorted({a.author for a in self.all() if a.author})

    def search(self, text: str) -> list[Annotation]:
        needle = text.lower()
        return [
            a for a in self.all() if needle in a.contents.lower() or needle in a.subject.lower()
        ]

    def flatten(self, pages: Sequence[int] | None = None) -> None:
        """Burn annotations into page content (they stop being editable)."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        with self.doc.undo_stack.macro("Flatten annotations"):
            for index in targets:

                class _Flatten(PageSnapshotCommand):
                    def apply(inner, i: int = index) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            for annot in list(page.annots()):
                                pix = annot.get_pixmap(dpi=150, alpha=True)
                                rect = annot.rect
                                page.delete_annot(annot)
                                if pix.width and pix.height:
                                    page.insert_image(rect, pixmap=pix, overlay=True)

                self.doc.undo_stack.push(_Flatten(self.doc, index, f"Flatten page {index + 1}"))

    # -- import / export ---------------------------------------------------- #
    def export_json(self, path: str | Path | None = None) -> str:
        """Export all comments as JSON (round-trips through :meth:`import_json`)."""
        payload = {
            "document": self.doc.display_name,
            "exported": datetime.now(UTC).isoformat(),
            "annotations": [
                {
                    **{
                        k: v
                        for k, v in asdict(a).items()
                        if k
                        not in (
                            "color",
                            "interior_color",
                            "rect",
                            "vertices",
                            "ink_list",
                            "quad_points",
                        )
                    },
                    "type": a.type.value,
                    "rect": list(a.rect),
                    "color": a.color.to_hex() if a.color else None,
                    "interior_color": a.interior_color.to_hex() if a.interior_color else None,
                    "vertices": [[p.x, p.y] for p in a.vertices],
                    "ink_list": [[[p.x, p.y] for p in s] for s in a.ink_list],
                    "quad_points": [list(q) for q in a.quad_points],
                }
                for a in self.all()
            ],
        }
        text = json.dumps(payload, indent=2)
        if path:
            Path(path).write_text(text, "utf-8")
        return text

    def import_json(self, source: str | Path) -> int:
        """Import comments previously exported with :meth:`export_json`."""
        data = json.loads(_read_source(source))
        items = data.get("annotations", data if isinstance(data, list) else [])
        added = 0
        with self.doc.undo_stack.macro("Import comments"):
            for item in items:
                page = int(item.get("page", 0))
                if page >= self.doc.page_count:
                    continue
                rect = Rect(*item["rect"])
                kind = AnnotationType(item.get("type", "text"))
                style = AnnotationStyle(
                    color=Color.from_hex(item["color"]) if item.get("color") else YELLOW,
                    author=item.get("author", ""),
                    subject=item.get("subject", ""),
                    opacity=float(item.get("opacity", 1.0)),
                )
                contents = item.get("contents", "")
                if kind in (
                    AnnotationType.HIGHLIGHT,
                    AnnotationType.UNDERLINE,
                    AnnotationType.STRIKEOUT,
                    AnnotationType.SQUIGGLY,
                ):
                    quads = [Rect(*q) for q in item.get("quad_points", [])] or [rect]
                    self.markup(page, quads, kind, style=style, contents=contents)
                elif kind is AnnotationType.TEXT:
                    self.sticky_note(page, Point(rect.x0, rect.y0), contents, style=style)
                elif kind is AnnotationType.FREE_TEXT:
                    self.free_text(page, rect, contents, style=style)
                elif kind is AnnotationType.INK and item.get("ink_list"):
                    strokes = [[Point(*p) for p in s] for s in item["ink_list"]]
                    self.ink(page, strokes, style=style)
                elif kind in (AnnotationType.SQUARE, AnnotationType.CIRCLE):
                    self.shape(page, rect, kind, style=style)
                elif kind in (AnnotationType.POLYGON, AnnotationType.POLYLINE):
                    pts = [Point(*p) for p in item.get("vertices", [])]
                    if pts:
                        self.polygon(
                            page, pts, closed=kind is AnnotationType.POLYGON, style=style
                        )
                else:
                    self.sticky_note(page, Point(rect.x0, rect.y0), contents, style=style)
                added += 1
        log.info("Imported {} annotation(s)", added)
        return added

    def export_xfdf(self, path: str | Path | None = None) -> str:
        """Export annotations as XFDF (interoperable with Acrobat/Foxit)."""
        ns = "http://ns.adobe.com/xfdf/"
        root = ET.Element("xfdf", xmlns=ns)
        annots_el = ET.SubElement(root, "annots")
        for a in self.all():
            tag = {
                AnnotationType.HIGHLIGHT: "highlight",
                AnnotationType.UNDERLINE: "underline",
                AnnotationType.STRIKEOUT: "strikeout",
                AnnotationType.SQUIGGLY: "squiggly",
                AnnotationType.TEXT: "text",
                AnnotationType.FREE_TEXT: "freetext",
                AnnotationType.INK: "ink",
                AnnotationType.SQUARE: "square",
                AnnotationType.CIRCLE: "circle",
                AnnotationType.POLYGON: "polygon",
                AnnotationType.POLYLINE: "polyline",
                AnnotationType.STAMP: "stamp",
            }.get(a.type, "text")
            el = ET.SubElement(annots_el, tag)
            el.set("page", str(a.page))
            el.set("rect", ",".join(f"{v:.2f}" for v in a.rect))
            el.set("title", a.author)
            el.set("subject", a.subject)
            el.set("date", a.created or _now())
            el.set("opacity", f"{a.opacity:.2f}")
            if a.color:
                el.set("color", a.color.to_hex().upper())
            if a.ink_list:
                el.set(
                    "inklist",
                    ";".join(
                        ",".join(f"{p.x:.2f},{p.y:.2f}" for p in stroke)
                        for stroke in a.ink_list
                    ),
                )
            contents = ET.SubElement(el, "contents")
            contents.text = a.contents
        text = ET.tostring(root, encoding="unicode")
        if path:
            Path(path).write_text(text, "utf-8")
        return text

    def import_xfdf(self, source: str | Path) -> int:
        """Import an XFDF comment file produced by Acrobat or PDF Studio."""
        root = ET.fromstring(_read_source(source))
        added = 0
        with self.doc.undo_stack.macro("Import XFDF comments"):
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if tag not in {
                    "highlight",
                    "underline",
                    "strikeout",
                    "squiggly",
                    "text",
                    "freetext",
                    "ink",
                    "square",
                    "circle",
                    "polygon",
                    "polyline",
                }:
                    continue
                try:
                    page = int(el.get("page", "0"))
                    rect = Rect(*[float(v) for v in (el.get("rect") or "0,0,0,0").split(",")])
                except ValueError:
                    continue
                if page >= self.doc.page_count:
                    continue
                contents_el = el.find("{*}contents") or el.find("contents")
                contents = (contents_el.text or "") if contents_el is not None else ""
                style = AnnotationStyle(
                    color=Color.from_hex(el.get("color", "#ffe066")),
                    author=el.get("title", ""),
                    subject=el.get("subject", ""),
                    opacity=float(el.get("opacity", "1") or 1),
                )
                match tag:
                    case "highlight" | "underline" | "strikeout" | "squiggly":
                        self.markup(
                            page, [rect], AnnotationType(tag), style=style, contents=contents
                        )
                    case "text":
                        self.sticky_note(page, Point(rect.x0, rect.y0), contents, style=style)
                    case "freetext":
                        self.free_text(page, rect, contents, style=style)
                    case "square" | "circle":
                        self.shape(
                            page,
                            rect,
                            AnnotationType.SQUARE if tag == "square" else AnnotationType.CIRCLE,
                            style=style,
                        )
                    case "ink":
                        strokes = []
                        for stroke in (el.get("inklist") or "").split(";"):
                            nums = [float(v) for v in stroke.split(",") if v]
                            strokes.append(
                                [
                                    Point(nums[i], nums[i + 1])
                                    for i in range(0, len(nums) - 1, 2)
                                ]
                            )
                        if strokes and strokes[0]:
                            self.ink(page, strokes, style=style)
                    case _:
                        continue
                added += 1
        return added

    def summary(self) -> dict[str, Any]:
        """Statistics for the Comments panel header."""
        items = self.all()
        by_type: dict[str, int] = {}
        for a in items:
            by_type[a.type.value] = by_type.get(a.type.value, 0) + 1
        return {
            "total": len(items),
            "by_type": by_type,
            "authors": self.authors(),
            "pages_with_comments": len({a.page for a in items}),
        }
