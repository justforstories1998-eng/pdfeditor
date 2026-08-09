"""Object model: select, move, nudge and delete anything drawn on a page.

PDF has no notion of a "movable object" — a page is a flat stream of drawing
operators. This module reconstructs one: text blocks, images, vector drawings
(rules, boxes, logos) and annotations are enumerated as :class:`PageObject`
records with a bounding box, and can then be repositioned.

Moving is implemented by *redrawing*, not by rewriting the content stream:

* **Text** is erased with a redaction and re-drawn at the new origin in the
  same font, size and colour.
* **Images** are re-inserted at the new rectangle from their original XObject,
  so no pixels are resampled.
* **Vector art** is replayed through a Shape with every point translated,
  which preserves stroke width, colour, dashes and fill.
* **Annotations** simply get a new rectangle.

Every move is one undoable command.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

import pymupdf as fitz

from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.content import (
    PageSnapshotCommand,
    TextLayout,
    TextStyle,
    draw_layout,
)
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import Point, Rect

log = get_logger("objects")


class ObjectKind(StrEnum):
    """What sort of thing an object is."""

    TEXT = "text"
    IMAGE = "image"
    DRAWING = "drawing"
    ANNOTATION = "annotation"


@dataclass(slots=True)
class PageObject:
    """A movable item on a page."""

    kind: ObjectKind
    page: int
    rect: Rect
    index: int = 0
    label: str = ""
    #: Backend handles: image xref, annotation xref, or the raw drawing record.
    xref: int = 0
    payload: Any = None

    def describe(self) -> str:
        size = f"{self.rect.width:.0f}×{self.rect.height:.0f}"
        return f"{self.kind.value.title()} · {size} pt · {self.label}".strip(" ·")


@dataclass(slots=True)
class ObjectClip:
    """A copied object, detached from the page it came from.

    Holding a :class:`PageObject` would not survive a paste: its ``payload``
    points at extraction records that go stale as soon as the page is
    rewritten, and the source object may well be deleted (cut) before the
    paste happens. The clip therefore stores self-contained data — image
    bytes, per-line text and styles, the vector path, or the raw annotation
    definition.
    """

    kind: ObjectKind
    rect: Rect
    label: str = ""
    payload: Any = None

    def describe(self) -> str:
        size = f"{self.rect.width:.0f}×{self.rect.height:.0f}"
        return f"{self.kind.value.title()} · {size} pt · {self.label}".strip(" ·")


class ObjectService:
    """Enumerates and repositions the objects on a page."""

    #: Anything smaller than this in both axes is not worth picking.
    MIN_PICK_SIZE = 2.0

    #: A shape covering at least this fraction of the page is treated as the
    #: page background (a tint, a letterhead panel, a full-bleed photo) and is
    #: not offered to a plain click. Dragging one of these is almost never
    #: intended and looks exactly like "a white layer appeared over my text".
    BACKGROUND_COVERAGE = 0.9

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    # -- enumeration --------------------------------------------------------- #
    def objects(
        self,
        page: int,
        *,
        include_text: bool = True,
        include_images: bool = True,
        include_drawings: bool = True,
        include_annotations: bool = True,
    ) -> list[PageObject]:
        """Every movable object on ``page``."""
        found: list[PageObject] = []

        if include_text:
            for index, block in enumerate(self.doc.extract_blocks(page)):
                text = " ".join(block.text.split())
                if not text:
                    continue
                found.append(
                    PageObject(
                        kind=ObjectKind.TEXT,
                        page=page,
                        rect=block.rect,
                        index=index,
                        label=text[:48],
                        payload=block,
                    )
                )

        if include_images:
            for index, info in enumerate(self.doc.page_images(page)):
                if info.rect.is_empty:
                    continue
                found.append(
                    PageObject(
                        kind=ObjectKind.IMAGE,
                        page=page,
                        rect=info.rect,
                        index=index,
                        label=f"{info.width}×{info.height} {info.colorspace}".strip(),
                        xref=info.xref,
                        payload=info,
                    )
                )

        if include_drawings:
            for index, path in enumerate(self.doc.page_drawings(page)):
                rect = path.rect
                if rect.width < self.MIN_PICK_SIZE and rect.height < self.MIN_PICK_SIZE:
                    continue
                found.append(
                    PageObject(
                        kind=ObjectKind.DRAWING,
                        page=page,
                        rect=rect,
                        index=index,
                        label=_describe_drawing(path),
                        payload=path,
                    )
                )

        if include_annotations:
            for index, annotation in enumerate(self.doc.page_annotations(page)):
                found.append(
                    PageObject(
                        kind=ObjectKind.ANNOTATION,
                        page=page,
                        rect=annotation.rect,
                        index=index,
                        label=annotation.type.value,
                        xref=int(annotation.extra.get("xref", 0)),
                        payload=annotation,
                    )
                )
        return found

    def is_background(self, target: PageObject) -> bool:
        """Whether ``target`` is the page's background rather than content.

        A tinted page, a letterhead panel or a full-bleed image is a shape the
        size of the page. Selecting one by clicking an empty margin and then
        dragging it drops an opaque sheet over everything else — the "white
        layer on top of the text" people report. Such shapes are therefore
        excluded from ordinary hit-testing and must be picked deliberately.
        """
        if target.kind not in (ObjectKind.DRAWING, ObjectKind.IMAGE):
            return False
        width, height = self.doc.page_size(target.page)
        page_area = max(width * height, 1.0)
        return target.rect.area / page_area >= self.BACKGROUND_COVERAGE

    def object_at(
        self,
        page: int,
        point: Point,
        *,
        tolerance: float = 4.0,
        include_background: bool = False,
        **kinds: bool,
    ) -> PageObject | None:
        """The object under ``point``.

        Smaller objects win ties, so clicking a thin rule that overlaps a large
        text block selects the rule — which is what someone trying to grab a
        line expects.

        Page-sized backgrounds are skipped unless ``include_background`` is
        set: see :meth:`is_background`.
        """
        candidates = [
            item
            for item in self.objects(page, **kinds)
            if item.rect.expanded(tolerance).contains(point)
            and (include_background or not self.is_background(item))
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda item: max(item.rect.area, 1.0))

    # -- movement ------------------------------------------------------------- #
    def move(self, target: PageObject, dx: float, dy: float) -> None:
        """Move ``target`` by ``(dx, dy)`` points (undoable)."""
        if abs(dx) < 0.01 and abs(dy) < 0.01:
            return
        self.doc.undo_stack.push(MoveObjectCommand(self.doc, target, dx, dy))

    def move_to(self, target: PageObject, position: Point) -> None:
        """Move ``target`` so its top-left corner sits at ``position``."""
        self.move(target, position.x - target.rect.x0, position.y - target.rect.y0)

    def delete(self, target: PageObject) -> None:
        """Remove ``target`` from the page (undoable)."""
        self.doc.undo_stack.push(DeleteObjectCommand(self.doc, target))

    def resolve(self, target: PageObject, expected: Rect | None = None) -> PageObject | None:
        """Find ``target`` again after the page has been redrawn.

        Moving rewrites the page, which invalidates the cached ``payload``
        (its coordinates still describe the *old* position). Re-resolving keeps
        repeated nudges working instead of replaying the original geometry and
        duplicating the object.
        """
        wanted = expected if expected is not None else target.rect
        centre = wanted.center
        candidates = [item for item in self.objects(target.page) if item.kind is target.kind]
        if not candidates:
            return None
        # Prefer something covering the expected centre, else the nearest.
        covering = [c for c in candidates if c.rect.expanded(2.0).contains(centre)]
        pool = covering or candidates
        return min(
            pool,
            key=lambda c: abs(c.rect.x0 - wanted.x0) + abs(c.rect.y0 - wanted.y0),
        )

    def align_left(self, target: PageObject, margin: float = 60.0) -> None:
        """Snap an object's left edge to ``margin``."""
        self.move(target, margin - target.rect.x0, 0.0)

    def align(self, target: PageObject, edge: str, *, margin: float = 60.0) -> Rect:
        """Align ``target`` to a page edge or centre it.

        ``edge`` is one of ``left``, ``right``, ``center``/``centre``, ``top``,
        ``bottom`` or ``middle``. Horizontal and vertical alignment are handled
        by the same call so the UI does not have to know the difference.

        Returns the rectangle the object should now occupy, which callers need
        in order to re-find it on the rewritten page.
        """
        width, height = self.doc.page_size(target.page)
        rect = target.rect
        dx = dy = 0.0
        match edge:
            case "left":
                dx = margin - rect.x0
            case "right":
                dx = (width - margin) - rect.x1
            case "center" | "centre":
                dx = (width - rect.width) / 2 - rect.x0
            case "top":
                dy = margin - rect.y0
            case "bottom":
                dy = (height - margin) - rect.y1
            case "middle":
                dy = (height - rect.height) / 2 - rect.y0
            case _:
                raise ValueError(f"Unknown alignment edge: {edge!r}")
        self.move(target, dx, dy)
        return rect.translated(dx, dy)

    # -- clipboard ------------------------------------------------------------ #
    def copy(self, target: PageObject) -> ObjectClip:
        """Snapshot ``target`` so it can be pasted later.

        The clip captures everything needed to redraw the object independently
        of the source page, because the page may be edited — or the object
        deleted — between the copy and the paste.
        """
        match target.kind:
            case ObjectKind.IMAGE:
                data, ext = self.doc.extract_image(target.xref)
                payload: Any = (data, ext)
            case ObjectKind.TEXT:
                payload = _line_styles(target.payload)
            case ObjectKind.ANNOTATION:
                payload = self._annotation_source(target)
            case _:
                payload = target.payload
        return ObjectClip(
            kind=target.kind,
            rect=target.rect,
            label=target.label,
            payload=payload,
        )

    def cut(self, target: PageObject) -> ObjectClip:
        """Copy ``target`` then remove it from the page (undoable)."""
        clip = self.copy(target)
        self.delete(target)
        return clip

    def paste(
        self,
        clip: ObjectClip,
        page: int,
        *,
        at: Point | None = None,
        offset: float = 12.0,
    ) -> Rect:
        """Draw ``clip`` onto ``page`` (undoable). Returns the new rectangle.

        Without ``at`` the copy is offset diagonally from the original, the
        convention every editor uses so a pasted duplicate is visible rather
        than hidden exactly behind its source.
        """
        if at is not None:
            destination = Rect(at.x, at.y, at.x + clip.rect.width, at.y + clip.rect.height)
        else:
            destination = clip.rect.translated(offset, offset)
        destination = self._clamp_to_page(page, destination)
        self.doc.undo_stack.push(PasteObjectCommand(self.doc, page, clip, destination))
        return destination

    def duplicate(self, target: PageObject, *, offset: float = 12.0) -> Rect:
        """Copy and immediately paste ``target`` (undoable, one step)."""
        return self.paste(self.copy(target), target.page, offset=offset)

    def _clamp_to_page(self, page: int, rect: Rect) -> Rect:
        """Keep a pasted object inside the page box."""
        width, height = self.doc.page_size(page)
        dx = min(0.0, width - 4.0 - rect.x1) + max(0.0, 4.0 - rect.x0)
        dy = min(0.0, height - 4.0 - rect.y1) + max(0.0, 4.0 - rect.y0)
        return rect.translated(dx, dy)

    def _annotation_source(self, target: PageObject) -> str:
        """The raw PDF object definition of an annotation, as a string."""
        with self.doc.locked() as handle:
            return str(handle.xref_object(target.xref, compressed=True))


def _describe_drawing(path: Any) -> str:
    """Readable name for a vector path so the UI can label it."""
    rect = path.rect
    if rect.height <= 2.0 and rect.width > 8.0:
        return "Horizontal rule"
    if rect.width <= 2.0 and rect.height > 8.0:
        return "Vertical rule"
    if path.fill is not None and path.stroke is None:
        return "Filled shape"
    return "Vector shape"


class MoveObjectCommand(PageSnapshotCommand):
    """Reposition one object; undo restores the whole page exactly."""

    def __init__(self, doc: PdfDocument, target: PageObject, dx: float, dy: float) -> None:
        super().__init__(doc, target.page, f"Move {target.kind.value}")
        self.target = target
        self.dx = dx
        self.dy = dy

    def apply(self) -> None:
        match self.target.kind:
            case ObjectKind.TEXT:
                _move_text(self.doc, self.target, self.dx, self.dy)
            case ObjectKind.IMAGE:
                _move_image(self.doc, self.target, self.dx, self.dy)
            case ObjectKind.DRAWING:
                _move_drawing(self.doc, self.target, self.dx, self.dy)
            case ObjectKind.ANNOTATION:
                _move_annotation(self.doc, self.target, self.dx, self.dy)


class DeleteObjectCommand(PageSnapshotCommand):
    """Erase one object from the page."""

    def __init__(self, doc: PdfDocument, target: PageObject) -> None:
        super().__init__(doc, target.page, f"Delete {target.kind.value}")
        self.target = target

    def apply(self) -> None:
        with self.doc.locked() as handle:
            page = handle[self.target.page]
            if self.target.kind is ObjectKind.IMAGE and self.target.xref:
                # delete_image() substitutes a blank stub rather than removing
                # the placement, so the image would still be listed. Redacting
                # with IMAGE_REMOVE drops it properly.
                page.add_redact_annot(fitz.Rect(*self.target.rect))
                page.apply_redactions(
                    images=fitz.PDF_REDACT_IMAGE_REMOVE,
                    text=fitz.PDF_REDACT_TEXT_NONE,
                    graphics=fitz.PDF_REDACT_LINE_ART_NONE,
                )
                return
            if self.target.kind is ObjectKind.ANNOTATION:
                for annotation in page.annots():
                    if annotation.xref == self.target.xref:
                        page.delete_annot(annotation)
                        return
                return
            # A hairline rule has a zero-height rectangle, which can never
            # "cover" the line art, so the deletion silently did nothing.
            # Expanding by a hair makes the box a real area, matching what
            # _move_drawing already does.
            box = self.target.rect
            if self.target.kind is ObjectKind.DRAWING:
                box = box.expanded(0.75)
            # fill=False removes the content without painting an opaque box
            # over the page background (see content._redact_fill).
            page.add_redact_annot(fitz.Rect(*box), fill=False)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            )


class PasteObjectCommand(PageSnapshotCommand):
    """Draw a copied object onto a page at a new rectangle."""

    def __init__(
        self, doc: PdfDocument, page: int, clip: ObjectClip, destination: Rect
    ) -> None:
        super().__init__(doc, page, f"Paste {clip.kind.value}")
        self.clip = clip
        self.destination = destination

    def apply(self) -> None:
        dx = self.destination.x0 - self.clip.rect.x0
        dy = self.destination.y0 - self.clip.rect.y0
        match self.clip.kind:
            case ObjectKind.TEXT:
                _paste_text(self.doc, self.page, self.clip, dx, dy)
            case ObjectKind.IMAGE:
                _paste_image(self.doc, self.page, self.clip, self.destination)
            case ObjectKind.DRAWING:
                _paste_drawing(self.doc, self.page, self.clip, dx, dy)
            case ObjectKind.ANNOTATION:
                _paste_annotation(self.doc, self.page, self.clip, self.destination)


def _paste_text(doc: PdfDocument, page: int, clip: ObjectClip, dx: float, dy: float) -> None:
    """Redraw each captured line at the offset position."""
    lines: list[tuple[Rect, str, TextStyle]] = clip.payload or []
    if not lines:
        return
    with doc.locked() as handle:
        target = handle[page]
        for rect, text, style in lines:
            _draw_line(target, rect.translated(dx, dy), text, style)


def _paste_image(doc: PdfDocument, page: int, clip: ObjectClip, destination: Rect) -> None:
    """Place the captured image bytes at the destination rectangle."""
    data, _ext = clip.payload
    with doc.locked() as handle:
        handle[page].insert_image(
            fitz.Rect(*destination), stream=data, keep_proportion=False, overlay=True
        )


def _paste_drawing(doc: PdfDocument, page: int, clip: ObjectClip, dx: float, dy: float) -> None:
    """Replay the captured vector path translated to the new position."""
    with doc.locked() as handle:
        _replay_path(handle[page], clip.payload, dx, dy)


def _paste_annotation(doc: PdfDocument, page: int, clip: ObjectClip, destination: Rect) -> None:
    """Clone an annotation by copying its PDF object and re-listing it.

    There is no ``copy_annot()`` in PyMuPDF, so the annotation dictionary is
    duplicated into a fresh xref, given the new ``/Rect`` and appended to the
    page's ``/Annots`` array. Copying the dictionary preserves the appearance
    stream, colours, author and contents exactly.
    """
    source = clip.payload
    if not isinstance(source, str) or not source.strip():
        return
    with doc.locked() as handle:
        target = handle[page]
        # annot_xrefs() yields (xref, type, name) tuples, not bare xrefs.
        existing = {entry[0] for entry in target.annot_xrefs()}

        # ``_addAnnot_FromString`` is the only route PyMuPDF offers for
        # inserting a ready-made annotation dictionary; there is no public
        # ``copy_annot()``. It pushes the object into the page's /Annots array
        # for us, which hand-editing the array via ``xref_set_key`` does not do
        # in a way the C layer notices.
        target._addAnnot_FromString((source,))
        # The page's cached annotation table is now stale, so ``annots()``
        # would raise "xref N is not an annot of this page". Resetting the page
        # references rebuilds it. ``reload_page()`` would be the obvious call
        # but it asserts whenever the Page object is still referenced.
        handle._reset_page_refs()

        target = handle[page]
        new_xrefs = [e[0] for e in target.annot_xrefs() if e[0] not in existing]
        if not new_xrefs:
            log.warning("Pasted annotation could not be located on page {}", page)
            return
        xref = new_xrefs[-1]
        for annotation in target.annots():
            if annotation.xref != xref:
                continue
            # set_rect() rather than writing /Rect: the raw key is in PDF's
            # bottom-up space while the rest of the API is top-down, so writing
            # it directly mirrors the paste vertically.
            annotation.set_rect(fitz.Rect(*destination))
            annotation.update()
            # A duplicate /NM makes the copy indistinguishable from its source
            # to anything tracking comments by name (replies, XFDF round-trips).
            # ``set_info()`` has no field for it, so /NM is written directly.
            handle.xref_set_key(xref, "NM", f"(pdfstudio-copy-{xref})")
            break


# --------------------------------------------------------------------------- #
# Per-kind movement
# --------------------------------------------------------------------------- #
def _line_styles(block: Any) -> list[tuple[Rect, str, TextStyle]]:
    """Rect, text and dominant style for every line of a text block."""
    lines: list[tuple[Rect, str, TextStyle]] = []
    for line in block.lines:
        spans = [s for s in line.spans if s.text.strip()]
        if not spans:
            continue
        widest = max(spans, key=lambda s: s.rect.width)
        lines.append(
            (
                line.rect,
                line.text,
                TextStyle(
                    font=widest.font,
                    size=widest.size or 11.0,
                    color=widest.color,
                    bold=widest.bold,
                    italic=widest.italic,
                ),
            )
        )
    return lines


def _move_text(doc: PdfDocument, target: PageObject, dx: float, dy: float) -> None:
    """Erase a text block and redraw it, line by line, at the new position."""
    lines = _line_styles(target.payload)
    if not lines:
        return
    with doc.locked() as handle:
        page = handle[target.page]
        # fill=False: erase the glyphs but leave the page background alone,
        # otherwise moving text on a tinted or illustrated page leaves a white
        # rectangle behind (see content._redact_fill).
        page.add_redact_annot(fitz.Rect(*target.rect), fill=False)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
        for rect, text, style in lines:
            _draw_line(page, rect.translated(dx, dy), text, style)


def _draw_line(page: fitz.Page, rect: Rect, text: str, style: TextStyle) -> None:
    """Draw one already-measured line of text at ``rect``.

    Shared by move and paste so both reproduce the original font, size and
    colour identically.
    """
    draw_layout(
        page,
        TextLayout(lines=[text], font_size=style.size, line_height=style.line_height),
        rect,
        style,
    )


def _move_image(doc: PdfDocument, target: PageObject, dx: float, dy: float) -> None:
    """Re-place an image at the translated rectangle (no resampling).

    ``Page.delete_image()`` does not remove the placement — it swaps in a blank
    1×1 stub, so the old position keeps occupying the page and the image count
    grows on every move. Redacting the rectangle with ``PDF_REDACT_IMAGE_REMOVE``
    drops the placement properly.
    """
    data, _ext = doc.extract_image(target.xref)
    with doc.locked() as handle:
        page = handle[target.page]
        page.add_redact_annot(fitz.Rect(*target.rect))
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_REMOVE,
            text=fitz.PDF_REDACT_TEXT_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
        page.insert_image(
            fitz.Rect(
                target.rect.x0 + dx,
                target.rect.y0 + dy,
                target.rect.x1 + dx,
                target.rect.y1 + dy,
            ),
            stream=data,
            keep_proportion=False,
            overlay=True,
        )


def _move_drawing(doc: PdfDocument, target: PageObject, dx: float, dy: float) -> None:
    """Erase a vector path and replay it with every point translated."""
    path = target.payload
    with doc.locked() as handle:
        page = handle[target.page]
        # Only line art fully covered by the object's box is removed, so
        # neighbouring text and shapes are left untouched.
        page.add_redact_annot(fitz.Rect(*target.rect.expanded(0.75)))
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
            text=fitz.PDF_REDACT_TEXT_NONE,
        )

        _replay_path(page, path, dx, dy)


def _replay_path(page: fitz.Page, path: Any, dx: float, dy: float) -> None:
    """Redraw a vector path translated by ``(dx, dy)``.

    Replaying the individual operators (rather than rewriting the content
    stream) preserves stroke width, colour, dashes, fill rule and opacity.
    Shared by move and paste.
    """
    shape = page.new_shape()
    for operator, points in path.items:
        moved = [fitz.Point(p.x + dx, p.y + dy) for p in points]
        if len(moved) < 2:
            continue
        match operator:
            case "l":
                shape.draw_line(moved[0], moved[1])
            case "re":
                shape.draw_rect(fitz.Rect(moved[0], moved[1]))
            case "qu" if len(moved) >= 4:
                shape.draw_quad(fitz.Quad(*moved[:4]))
            case "c" if len(moved) >= 4:
                shape.draw_bezier(moved[0], moved[1], moved[2], moved[3])
            case _:
                shape.draw_polyline(moved)
    shape.finish(
        color=path.stroke.to_rgb_tuple() if path.stroke else None,
        fill=path.fill.to_rgb_tuple() if path.fill else None,
        width=path.width or 0.0,
        dashes=path.dashes or None,
        closePath=path.closed,
        even_odd=path.even_odd,
        fill_opacity=path.opacity,
        stroke_opacity=path.opacity,
    )
    shape.commit()


def _move_annotation(doc: PdfDocument, target: PageObject, dx: float, dy: float) -> None:
    """Give an annotation a translated rectangle."""
    with doc.locked() as handle:
        page = handle[target.page]
        for annotation in page.annots():
            if annotation.xref != target.xref:
                continue
            rect = annotation.rect
            annotation.set_rect(
                fitz.Rect(rect.x0 + dx, rect.y0 + dy, rect.x1 + dx, rect.y1 + dy)
            )
            annotation.update()
            return
