"""Content editing: text, images and vector graphics.

Editing existing PDF content is the hardest part of any PDF editor because a
PDF stores *positioned glyphs*, not paragraphs.  PDF Studio uses the pragmatic
approach taken by commercial editors:

**Text editing** — the target span is located via the structured text
extractor, the original glyphs are removed with a *redaction without a mark*
(which physically deletes the underlying text operators), and the replacement
is drawn in the same font, size and colour at the same origin.  Reflow within
a text block is supported through :meth:`TextEditor.replace_block`, which
re-lays the paragraph using an HTML text-box writer.

**Image editing** — images are replaced in place by rewriting their XObject
stream, so position, clipping and transformation matrices stay intact.  Pixel
operations (crop, rotate, brightness, blur, …) are done with Pillow/NumPy.

**Vector editing** — new paths are written with a Shape; existing paths can be
deleted by content-stream filtering and redrawn with modified parameters.

Every operation is exposed as an undoable command that snapshots the page.
"""

from __future__ import annotations

import dataclasses
import io
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import pymupdf as fitz

from pdfstudio.core.exceptions import DependencyMissingError, ValidationError
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.undo import Command
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import (
    BLACK,
    BlendMode,
    Color,
    DrawingPath,
    ImageInfo,
    Point,
    Rect,
    TextBlock,
    TextLine,
    TextSpan,
)

log = get_logger("content")

Alignment = Literal["left", "center", "right", "justify"]

_ALIGN_MAP: dict[str, int] = {
    "left": fitz.TEXT_ALIGN_LEFT,
    "center": fitz.TEXT_ALIGN_CENTER,
    "right": fitz.TEXT_ALIGN_RIGHT,
    "justify": fitz.TEXT_ALIGN_JUSTIFY,
}

#: Base-14 fonts always available without embedding.
STANDARD_FONTS: dict[str, str] = {
    "Helvetica": "helv",
    "Helvetica-Bold": "hebo",
    "Helvetica-Oblique": "heit",
    "Helvetica-BoldOblique": "hebi",
    "Times-Roman": "tiro",
    "Times-Bold": "tibo",
    "Times-Italic": "tiit",
    "Times-BoldItalic": "tibi",
    "Courier": "cour",
    "Courier-Bold": "cobo",
    "Courier-Oblique": "coit",
    "Courier-BoldOblique": "cobi",
    "Symbol": "symb",
    "ZapfDingbats": "zadb",
}


class PageSnapshotCommand(Command):
    """Base class for edits that are undone by restoring the whole page.

    Restoring a page is O(page size) but exact — including annotations, form
    fields, resources and content streams — which is what a professional editor
    needs for arbitrary content mutations.
    """

    def __init__(self, doc: PdfDocument, page: int, label: str) -> None:
        super().__init__(label)
        self.doc = doc
        self.page = page
        self._before: bytes | None = None
        self._after: bytes | None = None

    def _capture(self) -> bytes:
        with self.doc.locked() as handle:
            buffer = fitz.open()
            buffer.insert_pdf(handle, from_page=self.page, to_page=self.page, annots=True)
            data = buffer.tobytes(garbage=0, deflate=True)
            buffer.close()
        return data

    def _restore(self, data: bytes) -> None:
        with self.doc.locked() as handle:
            source = fitz.open(stream=data, filetype="pdf")
            try:
                handle.insert_pdf(source, start_at=self.page, annots=True)
                handle.delete_pages([self.page + 1])
            finally:
                source.close()

    def apply(self) -> None:
        """Subclasses implement the actual mutation here."""
        raise NotImplementedError

    def execute(self) -> None:
        if self._before is None:
            self._before = self._capture()
        if self._after is not None:
            self._restore(self._after)
        else:
            self.apply()
            self._after = self._capture()
        self.doc.mark_modified(self.label)

    def undo(self) -> None:
        assert self._before is not None
        self._restore(self._before)
        self.doc.mark_modified(f"Undo {self.label}")

    def memory_cost(self) -> int:
        return len(self._before or b"") + len(self._after or b"")


# --------------------------------------------------------------------------- #
# Text editing
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class TextStyle:
    """Character/paragraph style used when writing text."""

    font: str = "Helvetica"
    size: float = 11.0
    color: Color = BLACK
    bold: bool = False
    italic: bool = False
    align: Alignment = "left"
    line_height: float = 1.25
    #: Extra indent applied to the *first* line only (positive) — a negative
    #: value produces a hanging indent, where wrapped lines sit further left.
    first_line_indent: float = 0.0
    #: Indent applied to every wrapped continuation line.
    wrap_indent: float = 0.0
    char_spacing: float = 0.0
    word_spacing: float = 0.0
    #: PDF has no underline or strike-through text attribute — they are drawn
    #: as thin rectangles after the glyphs (see :func:`draw_layout`).
    underline: bool = False
    strikethrough: bool = False
    render_mode: int = 0  # 0 fill, 1 stroke, 2 fill+stroke, 3 invisible
    embed: bool = True
    file: str | None = None  # path to a TTF/OTF for custom fonts

    def base14(self) -> str:
        """Best matching base-14 alias for this style."""
        family = self.font.split("-")[0].split(",")[0]
        suffix = ("Bold" if self.bold else "") + ("Italic" if self.italic else "")
        if family.lower().startswith("times"):
            table = {"": "tiro", "Bold": "tibo", "Italic": "tiit", "BoldItalic": "tibi"}
        elif family.lower().startswith("cour"):
            table = {"": "cour", "Bold": "cobo", "Italic": "coit", "BoldItalic": "cobi"}
        else:
            table = {"": "helv", "Bold": "hebo", "Italic": "heit", "BoldItalic": "hebi"}
        return table.get(suffix, table[""])


class AddTextCommand(PageSnapshotCommand):
    """Insert a new text box on a page."""

    def __init__(
        self,
        doc: PdfDocument,
        page: int,
        rect: Rect,
        text: str,
        style: TextStyle,
        *,
        rotate: int = 0,
    ) -> None:
        super().__init__(doc, page, "Add text")
        self.rect = rect
        self.text = text
        self.style = style
        self.rotate = rotate

    def apply(self) -> None:
        # Underline and strike-through are drawn by draw_layout(), which
        # insert_textbox() knows nothing about, so decorated text takes the
        # wrapping path instead. Rotation still needs insert_textbox.
        decorated = self.style.underline or self.style.strikethrough
        if decorated and not self.rotate:
            with self.doc.locked() as handle:
                page = handle[self.page]
                layout = fit_text(
                    self.text,
                    width=self.rect.width,
                    height=self.rect.height,
                    style=self.style,
                )
                draw_layout(page, layout, self.rect, self.style)
            return

        with self.doc.locked() as handle:
            page = handle[self.page]
            fontname, fontfile = _resolve_font(page, self.style)
            leftover = page.insert_textbox(
                fitz.Rect(*self.rect),
                self.text,
                fontname=fontname,
                fontfile=fontfile,
                fontsize=self.style.size,
                color=self.style.color.to_rgb_tuple(),
                align=_ALIGN_MAP[self.style.align],
                lineheight=self.style.line_height,
                rotate=self.rotate,
                render_mode=self.style.render_mode,
            )
            if leftover < 0:
                log.warning("Text box too small; {} points of text did not fit", -leftover)


class ReplaceTextCommand(PageSnapshotCommand):
    """Replace an existing run of text in place, keeping the original style.

    The replacement is **word-wrapped** inside the target rectangle and, when
    the text no longer fits, the box grows downwards into the free space below
    it before the font is scaled down as a last resort. Writing with a single
    ``insert_text()`` call (as an earlier version did) draws one unbroken line
    that simply runs off the edge of the page.
    """

    def __init__(
        self,
        doc: PdfDocument,
        page: int,
        rect: Rect,
        new_text: str,
        *,
        style: TextStyle | None = None,
        background: Color | None = None,
        grow: bool = True,
        shrink_to_fit: bool = True,
    ) -> None:
        super().__init__(doc, page, "Edit text")
        self.rect = rect
        self.new_text = new_text
        self.style = style
        self.background = background
        self.grow = grow
        self.shrink_to_fit = shrink_to_fit

    def apply(self) -> None:
        style = self.style or _style_at(self.doc, self.page, self.rect)
        with self.doc.locked() as handle:
            page = handle[self.page]
            target = fitz.Rect(*self.rect)
            # Physically remove the original glyphs.
            #
            # ``fill`` must stay *false* unless the caller asked for a specific
            # background: a redaction with a colour paints an opaque rectangle
            # into the page, which shows up as a white patch on any page that
            # is not pure white (coloured CVs, letterheads, dark themes) and
            # covers whatever else happens to share the bounding box. Removing
            # the glyphs without painting anything leaves the real page
            # background — including images and tints — completely intact.
            page.add_redact_annot(target, fill=_redact_fill(self.background))
            page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
            if not self.new_text.strip():
                # Text was deleted: shift content below up to close the gap
                # so no blank space remains.
                _shift_content_up(self.doc, self.page, self.rect, self.rect.height)
                return

            available = self.rect.width
            if self.grow:
                # Allow the paragraph to reflow into the empty space below,
                # stopping short of whatever comes next on the page.
                bottom = _free_space_below(self.doc, self.page, self.rect)
            else:
                bottom = self.rect.y1

            layout = fit_text(
                self.new_text,
                width=available,
                height=max(self.rect.height, bottom - self.rect.y0),
                style=style,
                shrink_to_fit=self.shrink_to_fit,
            )
            # A replacement that needs more lines than it replaced would print
            # straight over the following paragraph, so shift that text down by
            # the extra height first.
            overflow = layout.height - self.rect.height
            if self.grow and overflow > 1.0:
                _shift_content_down(self.doc, self.page, self.rect, overflow, limit=bottom)
            draw_layout(page, layout, self.rect, style)


def _redact_fill(background: Color | None) -> tuple[float, float, float] | bool:
    """Fill argument for ``add_redact_annot``.

    PyMuPDF paints the redaction rectangle when ``fill`` is a colour and
    leaves the page untouched when it is ``False`` (``None`` defaults to
    white, which is *not* what we want). Every edit used to pass white, which
    stamped an opaque box over coloured backgrounds, images and neighbouring
    content — the "white layer" users had to clean up afterwards. Now only an
    explicitly requested background is painted.
    """
    return False if background is None else background.to_rgb_tuple()


def _free_space_below(
    doc: PdfDocument, page: int, rect: Rect, *, ignore_own_block: bool = True
) -> float:
    """Bottom coordinate the text in ``rect`` may grow to.

    Looks for the next *unrelated* content below ``rect`` and stops just above
    it. Lines belonging to the same paragraph are skipped by default: when a
    single line is edited its own siblings must not cap its growth, or it could
    never wrap onto a second line.
    """
    page_height = doc.page_size(page)[1]
    limit = page_height - 36.0
    column = Rect(rect.x0, rect.y1, rect.x1, limit)

    own_block: Rect | None = None
    if ignore_own_block:
        for block in doc.extract_blocks(page):
            if block.rect.expanded(2).contains(rect.center):
                own_block = block.rect
                break

    obstacles: list[float] = []
    for block in doc.extract_blocks(page):
        if own_block is not None and block.rect == own_block:
            continue
        if block.rect.y0 > rect.y1 - 1 and block.rect.intersects(column):
            obstacles.append(block.rect.y0)
    for info in doc.page_images(page):
        if info.rect.y0 > rect.y1 - 1 and info.rect.intersects(column):
            obstacles.append(info.rect.y0)

    ceiling = min(obstacles, default=limit)
    # Always allow at least a few lines of growth so short edits can wrap.
    minimum = rect.y1 + rect.height * 3
    return max(rect.y1, min(limit, max(ceiling - 4.0, min(minimum, limit))))


def _shift_content_down(
    doc: PdfDocument, page: int, rect: Rect, amount: float, *, limit: float
) -> None:
    """Move the text below ``rect`` down by ``amount`` points.

    Used when an edited paragraph grows: the lines that follow it are lifted
    out and re-drawn lower so nothing is printed on top of anything else. Only
    text in the same column is moved, and only while there is room.
    """
    if amount <= 0:
        return

    page_height = doc.page_size(page)[1]
    # Everything below the edit in this column has to move, right down to the
    # foot of the page. Capping the search at ``limit`` (as an earlier version
    # did) shifted only the first row or two and left the rest where they
    # were, so the growing paragraph printed straight over them.
    column = Rect(rect.x0 - 2, rect.y1 - 1, rect.x1 + 2, page_height)

    lines: list[tuple[Rect, str, TextStyle]] = []
    for block in doc.extract_blocks(page):
        for line in block.lines:
            if line.rect.y0 < rect.y1 - 0.5:
                continue
            spans = [sp for sp in line.spans if sp.text.strip()]
            if not spans:
                continue
            widest = max(spans, key=lambda sp: sp.rect.width)
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

    in_column = [index for index, item in enumerate(lines) if item[0].intersects(column)]
    if not in_column:
        return

    # A row label ("API Testing:") sits outside the value column but on the
    # same baseline as a line that is moving. Leaving it behind would tear the
    # row in half, so anything vertically overlapping a moved line comes too.
    rows = [lines[index][0] for index in in_column]

    def shares_a_row(candidate: Rect) -> bool:
        return any(candidate.y0 < row.y1 - 0.5 and candidate.y1 > row.y0 + 0.5 for row in rows)

    chosen = set(in_column)
    movable = [
        item for index, item in enumerate(lines) if index in chosen or shares_a_row(item[0])
    ]
    if not movable:
        return

    # Limit the shift to what fits on the page instead of silently returning.
    # Returning without shifting when there isn't room causes the replacement
    # text to overlap the lines below — the "text goes blank" symptom.
    max_bottom = max(line.y1 for line, _t, _s in movable) + amount
    if max_bottom > page_height - 18:
        available = (page_height - 18) - max(line.y1 for line, _t, _s in movable)
        if available <= 0:
            log.warning("Not enough room below to reflow; text may overlap")
            return
        amount = max(0.0, available)

    with doc.locked() as handle:
        target = handle[page]
        # Erase the old positions in one pass, then redraw them lower.
        # ``fill=False`` keeps the page background (see :func:`_redact_fill`).
        for line_rect, _text, _style in movable:
            target.add_redact_annot(fitz.Rect(*line_rect), fill=False)
        target.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
        for line_rect, text, line_style in movable:
            moved = Rect(
                line_rect.x0, line_rect.y0 + amount, line_rect.x1, line_rect.y1 + amount
            )
            draw_layout(
                target,
                TextLayout(
                    lines=[text],
                    font_size=line_style.size,
                    line_height=line_style.line_height,
                ),
                moved,
                line_style,
            )


def _shift_content_up(
    doc: PdfDocument, page: int, rect: Rect, amount: float
) -> None:
    """Move the text below ``rect`` up by ``amount`` points.

    Used when text is deleted: the lines that follow it are lifted
    out and re-drawn higher to close the gap. Only text in the same
    column is moved.
    """
    if amount <= 0:
        return

    page_height = doc.page_size(page)[1]
    column = Rect(rect.x0 - 2, rect.y1 - 1, rect.x1 + 2, page_height)

    lines: list[tuple[Rect, str, TextStyle]] = []
    for block in doc.extract_blocks(page):
        for line in block.lines:
            if line.rect.y0 < rect.y1 - 0.5:
                continue
            spans = [sp for sp in line.spans if sp.text.strip()]
            if not spans:
                continue
            widest = max(spans, key=lambda sp: sp.rect.width)
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

    in_column = [index for index, item in enumerate(lines) if item[0].intersects(column)]
    if not in_column:
        return

    rows = [lines[index][0] for index in in_column]

    def shares_a_row(candidate: Rect) -> bool:
        return any(candidate.y0 < row.y1 - 0.5 and candidate.y1 > row.y0 + 0.5 for row in rows)

    chosen = set(in_column)
    movable = [
        item for index, item in enumerate(lines) if index in chosen or shares_a_row(item[0])
    ]
    if not movable:
        return

    # Don't shift above the top of the page
    min_top = min(line.y0 for line, _t, _s in movable) - amount
    if min_top < 0:
        amount = max(0.0, min(line.y0 for line, _t, _s in movable))

    with doc.locked() as handle:
        target = handle[page]
        # Erase the old positions in one pass, then redraw them higher.
        for line_rect, _text, _style in movable:
            target.add_redact_annot(fitz.Rect(*line_rect), fill=False)
        target.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
        )
        for line_rect, text, line_style in movable:
            moved = Rect(
                line_rect.x0, line_rect.y0 - amount, line_rect.x1, line_rect.y1 - amount
            )
            draw_layout(
                target,
                TextLayout(
                    lines=[text],
                    font_size=line_style.size,
                    line_height=line_style.line_height,
                ),
                moved,
                line_style,
            )


@dataclass(slots=True)
class TextLayout:
    """A wrapped block of text ready to be drawn."""

    lines: list[str]
    font_size: float
    line_height: float
    fits: bool = True

    @property
    def height(self) -> float:
        return len(self.lines) * self.font_size * self.line_height


def _font_for(style: TextStyle) -> fitz.Font:
    """A measuring font matching ``style`` (falls back to Helvetica)."""
    if style.file:
        try:
            return fitz.Font(fontfile=style.file)
        except Exception:
            pass
    try:
        return fitz.Font(style.base14())
    except Exception:
        return fitz.Font("helv")


def wrap_text(
    text: str, width: float, style: TextStyle, *, font: fitz.Font | None = None
) -> list[str]:
    """Break ``text`` into lines that fit within ``width`` points.

    Existing newlines are honoured as hard breaks; a word longer than the line
    (a URL, say) is split by character so it can never overflow. First-line and
    wrap indents shrink the usable width of the lines they apply to, so an
    indented paragraph still wraps inside its column.
    """
    measure = font or _font_for(style)
    size = max(1.0, style.size)
    base_limit = max(1.0, width)
    spacing = style.char_spacing

    def advance(chunk: str) -> float:
        return measure.text_length(chunk, size) + spacing * len(chunk)

    def limit_for(index: int) -> float:
        indent = style.first_line_indent if index == 0 else style.wrap_indent
        return max(1.0, base_limit - max(0.0, indent))

    lines: list[str] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not paragraph.strip():
            lines.append("")
            continue
        current = ""
        for word in paragraph.split(" "):
            limit = limit_for(len(lines))
            candidate = f"{current} {word}".strip() if current else word
            if advance(candidate) <= limit or not current:
                if advance(candidate) > limit and not current:
                    # A single word wider than the box: split it by character.
                    piece = ""
                    for character in word:
                        if advance(piece + character) > limit and piece:
                            lines.append(piece)
                            piece = character
                        else:
                            piece += character
                    current = piece
                    continue
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
    return lines


def fit_text(
    text: str,
    *,
    width: float,
    height: float,
    style: TextStyle,
    shrink_to_fit: bool = True,
    minimum_size: float = 4.0,
) -> TextLayout:
    """Wrap ``text`` to ``width``, scaling the font down if it overflows ``height``."""
    font = _font_for(style)
    size = max(minimum_size, style.size)
    line_height = max(1.0, style.line_height)

    while True:
        scaled = dataclasses.replace(style, size=size)
        lines = wrap_text(text, width, scaled, font=font)
        needed = len(lines) * size * line_height
        if needed <= height or not shrink_to_fit or size <= minimum_size:
            return TextLayout(
                lines=lines,
                font_size=size,
                line_height=line_height,
                fits=needed <= height,
            )
        # Scale towards the size that would fit, with a floor on the step so
        # the loop always terminates.
        size = max(minimum_size, min(size - 0.25, size * (height / needed) ** 0.5))


def draw_layout(page: fitz.Page, layout: TextLayout, rect: Rect, style: TextStyle) -> None:
    """Draw a wrapped :class:`TextLayout` into ``rect`` honouring alignment."""
    fontname, fontfile = _resolve_font(page, style)
    font = _font_for(style)
    size = layout.font_size
    step = size * layout.line_height
    # Place the first baseline where the original text sat: the top of a glyph
    # box is roughly the ascender above its baseline.
    # ``Font.ascender`` includes accent headroom (~1.07 for Helvetica), which
    # pushes the first line too low; the cap height is the better anchor.
    ascender = min(float(getattr(font, "ascender", 0.8) or 0.8), 1.0) * 0.86
    baseline = rect.y0 + ascender * size

    for number, line in enumerate(layout.lines):
        if line:
            width = font.text_length(line, size)
            indent = style.first_line_indent if number == 0 else style.wrap_indent
            available = max(1.0, rect.width - max(0.0, indent))
            if style.align == "center":
                x = rect.x0 + indent + max(0.0, (available - width) / 2)
            elif style.align == "right":
                x = rect.x0 + indent + max(0.0, available - width)
            else:
                x = rect.x0 + indent
            page.insert_text(
                fitz.Point(x, baseline),
                line,
                fontname=fontname,
                fontfile=fontfile,
                fontsize=size,
                color=style.color.to_rgb_tuple(),
                render_mode=style.render_mode,
            )
            # PDF has no underline/strike-through attribute, so they are drawn
            # as thin filled rectangles positioned from the font metrics.
            if style.underline or style.strikethrough:
                thickness = max(0.4, size * 0.055)
                colour = style.color.to_rgb_tuple()
                if style.underline:
                    y = baseline + size * 0.12
                    page.draw_rect(
                        fitz.Rect(x, y, x + width, y + thickness),
                        color=None,
                        fill=colour,
                    )
                if style.strikethrough:
                    y = baseline - size * 0.26
                    page.draw_rect(
                        fitz.Rect(x, y, x + width, y + thickness),
                        color=None,
                        fill=colour,
                    )
        baseline += step


class SwapLinesCommand(PageSnapshotCommand):
    """Exchange the positions of two lines of text (undoable).

    Both lines are erased in a single redaction pass and redrawn at each
    other's origin. Erasing one at a time would let the first redaction remove
    glyphs that the second still needs to measure.
    """

    def __init__(self, doc: PdfDocument, page: int, first: TextLine, second: TextLine) -> None:
        super().__init__(doc, page, "Move line")
        self.first = first
        self.second = second

    def apply(self) -> None:
        upper, lower = sorted((self.first, self.second), key=lambda ln: ln.rect.y0)
        upper_style = TextEditor._style_of_line(upper)
        lower_style = TextEditor._style_of_line(lower)
        with self.doc.locked() as handle:
            page = handle[self.page]
            for line in (upper, lower):
                page.add_redact_annot(fitz.Rect(*line.rect), fill=False)
            page.apply_redactions(
                images=fitz.PDF_REDACT_IMAGE_NONE,
                graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            )
            # Each line simply takes the other's box. Deriving the destination
            # from the *target* rectangle rather than composing it from the
            # moving line's own height keeps the text on the original
            # baselines: mixing the two made every swap land a couple of
            # points high, and repeated swaps walked the paragraph up the page
            # until it fragmented into separate blocks.
            _draw_single_line(page, lower.rect, upper.text, upper_style)
            _draw_single_line(page, upper.rect, lower.text, lower_style)


#: Bullet glyphs and numbering that :func:`_strip_list_marker` recognises.
_LIST_MARKER = re.compile(r"^\s*(?:[\u2022\u25cf\u25aa\u2023\-\*\u00b7]|\d+[.)]|[a-z][.)])\s+")


def _strip_list_marker(line: str) -> str:
    """Remove an existing bullet or number so markers never accumulate."""
    return _LIST_MARKER.sub("", line).strip()


def _convert_case(text: str, mode: str) -> str:
    """Case conversion matching a word processor's expectations."""
    match mode:
        case "upper":
            return text.upper()
        case "lower":
            return text.lower()
        case "title":
            # str.title() mangles apostrophes ("Don'T"), so words are
            # capitalised individually instead.
            return "\n".join(
                " ".join(w[:1].upper() + w[1:].lower() if w else w for w in line.split(" "))
                for line in text.splitlines()
            )
        case "sentence":
            result: list[str] = []
            for line in text.splitlines():
                lowered = line.lower()
                out: list[str] = []
                capitalise = True
                for char in lowered:
                    if capitalise and char.isalpha():
                        out.append(char.upper())
                        capitalise = False
                    else:
                        out.append(char)
                        if char in ".!?":
                            capitalise = True
                result.append("".join(out))
            return "\n".join(result)
        case _:
            raise ValidationError(f"Unknown case mode: {mode!r}")


def _draw_single_line(page: fitz.Page, rect: Rect, text: str, style: TextStyle) -> None:
    """Draw one unwrapped line so its glyph box matches ``rect`` exactly.

    ``draw_layout`` places the baseline using a cap-height approximation that
    suits reflowed paragraphs but is a couple of points out for an exact
    reposition. When a line is swapped with its neighbour that error is
    cumulative: each swap walked the paragraph up the page until MuPDF stopped
    reporting it as one block. Here the baseline is derived from the font's
    own ascender against the measured line box instead.
    """
    font = _font_for(style)
    fontname, fontfile = _resolve_font(page, style)
    ascender = float(getattr(font, "ascender", 0.8) or 0.8)
    descender = abs(float(getattr(font, "descender", 0.2) or 0.2))
    span = ascender + descender
    # The extracted rect spans ascender..descender, so the baseline sits an
    # ascender-proportion down from its top edge.
    baseline = rect.y0 + rect.height * (ascender / span if span else 0.8)
    page.insert_text(
        fitz.Point(rect.x0, baseline),
        text,
        fontname=fontname,
        fontfile=fontfile,
        fontsize=style.size,
        color=style.color.to_rgb_tuple(),
        render_mode=style.render_mode,
    )


class DeleteTextCommand(ReplaceTextCommand):
    """Erase text within a rectangle."""

    def __init__(
        self, doc: PdfDocument, page: int, rect: Rect, *, background: Color | None = None
    ) -> None:
        super().__init__(doc, page, rect, "", background=background)
        self.label = "Delete text"


class TextEditor:
    """Facade for all text editing on one document."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    def add(
        self,
        page: int,
        rect: Rect,
        text: str,
        style: TextStyle | None = None,
        *,
        rotate: int = 0,
    ) -> None:
        """Add a text box (undoable)."""
        self.doc.undo_stack.push(
            AddTextCommand(self.doc, page, rect, text, style or TextStyle(), rotate=rotate)
        )

    def replace(
        self,
        page: int,
        rect: Rect,
        text: str,
        style: TextStyle | None = None,
        *,
        background: Color | None = None,
        grow: bool = True,
        shrink_to_fit: bool = True,
    ) -> None:
        """Replace the text inside ``rect`` (undoable).

        The replacement is word-wrapped to the width of ``rect``; if it needs
        more room the box grows into the free space below before the font is
        scaled down.
        """
        self.doc.undo_stack.push(
            ReplaceTextCommand(
                self.doc,
                page,
                rect,
                text,
                style=style,
                background=background,
                grow=grow,
                shrink_to_fit=shrink_to_fit,
            )
        )

    def delete(self, page: int, rect: Rect, *, background: Color | None = None) -> None:
        self.doc.undo_stack.push(DeleteTextCommand(self.doc, page, rect, background=background))

    # -- Word-style paragraph operations ------------------------------------ #
    def transform_case(self, page: int, point: Point, mode: str) -> bool:
        """Change the case of the paragraph under ``point`` (undoable).

        ``mode`` is ``upper``, ``lower``, ``title`` or ``sentence``.
        """
        region = self.edit_region(page, point, whole_paragraph=True)
        if region is None:
            return False
        rect, text, style = region
        converted = _convert_case(text, mode)
        if converted == text:
            return False
        # Upper case is materially wider than lower case, so the paragraph is
        # allowed to use the full column; otherwise every line wraps and a
        # three-line list becomes six.
        widened = Rect(rect.x0, rect.y0, self._column_right_for(page, rect), rect.y1)
        self.replace(page, widened, converted, style)
        return True

    def set_list_style(self, page: int, point: Point, marker: str) -> bool:
        """Turn the paragraph under ``point`` into a bulleted/numbered list.

        ``marker`` is ``bullet``, ``number`` or ``none``. Each *line* becomes
        one list item, which matches how a reader sees the paragraph.
        """
        region = self.edit_region(page, point, whole_paragraph=True)
        if region is None:
            return False
        rect, text, style = region
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            return False
        stripped = [_strip_list_marker(ln) for ln in lines]
        if marker == "none":
            rebuilt = stripped
        elif marker == "bullet":
            rebuilt = [f"\u2022  {ln}" for ln in stripped]
        elif marker == "number":
            rebuilt = [f"{i}.  {ln}" for i, ln in enumerate(stripped, start=1)]
        else:
            raise ValidationError(f"Unknown list marker: {marker!r}")
        new_text = "\n".join(rebuilt)
        if new_text == text:
            return False
        # A hanging indent keeps wrapped text aligned under the first word
        # rather than under the bullet, as a word processor does.
        indent = 0.0 if marker == "none" else style.size * 1.4
        # The marker makes every line wider. Without widening the box the last
        # word wraps, and because each visual line becomes an item, a second
        # pass would number the wrapped fragment as its own entry
        # ("3. overruns."). Growing into the column keeps one item per line.
        widened = Rect(rect.x0, rect.y0, self._column_right_for(page, rect), rect.y1)
        self.replace(page, widened, new_text, dataclasses.replace(style, wrap_indent=indent))
        return True

    def _column_right_for(self, page: int, rect: Rect) -> float:
        """Right-hand edge a paragraph in ``rect`` may grow to."""
        page_width = self.doc.page_size(page)[0]
        margin = min(rect.x0, 72.0)
        right = page_width - margin
        neighbours = [
            block.rect.x0
            for block in self.doc.extract_blocks(page)
            if block.rect.x0 > rect.x1 + 8
            and block.rect.y1 > rect.y0
            and block.rect.y0 < rect.y1
        ]
        if neighbours:
            right = min(right, min(neighbours) - 8.0)
        return max(rect.x1, right)

    def set_line_spacing(self, page: int, point: Point, spacing: float) -> bool:
        """Re-set the paragraph's line spacing (1.0 = single, 2.0 = double)."""
        if spacing <= 0:
            raise ValidationError("Line spacing must be greater than zero.")
        region = self.edit_region(page, point, whole_paragraph=True)
        if region is None:
            return False
        rect, text, style = region
        self.replace(page, rect, text, dataclasses.replace(style, line_height=spacing))
        return True

    # -- line reordering ---------------------------------------------------- #
    def paragraph_lines(self, page: int, point: Point) -> list[TextLine]:
        """Every line of the paragraph under ``point``, in reading order."""
        block = self.block_at(page, point)
        if block is None:
            return []
        return sorted(block.lines, key=lambda ln: ln.rect.y0)

    def duplicate_line(self, page: int, point: Point) -> bool:
        """Copy the line under ``point`` onto a new line below it (undoable).

        The rest of the paragraph is pushed down so the copy does not land on
        top of the following line.
        """
        line = self.line_at(page, point)
        if line is None:
            return False
        block = self.block_at(page, point)
        if block is None:
            return False
        rect = block.rect
        ordered = sorted(block.lines, key=lambda ln: ln.rect.y0)
        index = min(range(len(ordered)), key=lambda i: abs(ordered[i].rect.y0 - line.rect.y0))
        lines = [ln.text for ln in ordered]
        lines.insert(index + 1, lines[index])
        style = self._style_of_line(line)
        widened = Rect(rect.x0, rect.y0, self._column_right_for(page, rect), rect.y1)
        self.replace(page, widened, "\n".join(lines), style)
        return True

    def delete_line(self, page: int, point: Point) -> bool:
        """Remove the line under ``point`` and close the gap (undoable)."""
        line = self.line_at(page, point)
        if line is None:
            return False
        block = self.block_at(page, point)
        if block is None:
            return False
        ordered = sorted(block.lines, key=lambda ln: ln.rect.y0)
        index = min(range(len(ordered)), key=lambda i: abs(ordered[i].rect.y0 - line.rect.y0))
        remaining = [ln.text for i, ln in enumerate(ordered) if i != index]
        style = self._style_of_line(ordered[0])
        if not remaining:
            self.delete(page, block.rect)
            return True
        rect = block.rect
        widened = Rect(rect.x0, rect.y0, self._column_right_for(page, rect), rect.y1)
        self.replace(page, widened, "\n".join(remaining), style)
        return True

    def apply_style_to_paragraph(self, page: int, point: Point, style: TextStyle) -> bool:
        """Re-draw the paragraph under ``point`` in ``style`` (format painter)."""
        region = self.edit_region(page, point, whole_paragraph=True)
        if region is None:
            return False
        rect, text, _current = region
        widened = Rect(rect.x0, rect.y0, self._column_right_for(page, rect), rect.y1)
        self.replace(page, widened, text, style)
        return True

    def move_line(self, page: int, point: Point, direction: int) -> bool:
        """Swap the line under ``point`` with its neighbour (undoable).

        ``direction`` is -1 for up, +1 for down. Returns ``False`` when there
        is nothing to swap with, so the caller can report why nothing moved.

        The two lines exchange *positions* rather than being nudged: each is
        redrawn at the other's left edge and baseline. Nudging by a fixed
        amount would drift whenever the lines have different heights.
        """
        if direction not in (-1, 1):
            raise ValidationError("Direction must be -1 (up) or +1 (down).")
        lines = self.paragraph_lines(page, point)
        if len(lines) < 2:
            return False
        current = self.line_at(page, point)
        if current is None:
            return False
        index = min(
            range(len(lines)),
            key=lambda i: abs(lines[i].rect.y0 - current.rect.y0),
        )
        target = index + direction
        if not 0 <= target < len(lines):
            return False
        self.doc.undo_stack.push(SwapLinesCommand(self.doc, page, lines[index], lines[target]))
        return True

    def replace_all(
        self,
        search: str,
        replacement: str,
        *,
        regex: bool = False,
        case_sensitive: bool = False,
        pages: Sequence[int] | None = None,
        style: TextStyle | None = None,
    ) -> int:
        """Find & replace across the document. Returns the number of changes."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        flags = 0 if case_sensitive else re.IGNORECASE
        pattern = re.compile(search if regex else re.escape(search), flags)
        changes = 0
        with self.doc.undo_stack.macro(f"Replace “{search}”"):
            for index in targets:
                for span in self._spans(index):
                    match = pattern.search(span.text)
                    if not match:
                        continue
                    new_text = pattern.sub(replacement, span.text)
                    span_style = style or TextStyle(
                        font=span.font, size=span.size, color=span.color
                    )
                    self.replace(index, span.rect, new_text, span_style)
                    changes += 1
        log.info("Replaced {} occurrence(s) of {!r}", changes, search)
        return changes

    def _spans(self, page: int) -> list[TextSpan]:
        return [
            span
            for block in self.doc.extract_blocks(page)
            for line in block.lines
            for span in line.spans
        ]

    def spans_in(self, page: int, rect: Rect) -> list[TextSpan]:
        """Spans intersecting ``rect`` — used by the text selection tool."""
        return [s for s in self._spans(page) if s.rect.intersects(rect)]

    def line_at(self, page: int, point: Point) -> TextLine | None:
        """The single line of text under ``point``.

        Editing must target one line, not the whole paragraph: hit-testing the
        block would pull every sibling line into the editor.
        """
        best: TextLine | None = None
        best_distance = float("inf")
        for block in self.doc.extract_blocks(page):
            if not block.rect.expanded(2).contains(point):
                continue
            for line in block.lines:
                if line.rect.expanded(1).contains(point):
                    return line
                # Fall back to the vertically closest line in the block so a
                # click in the leading between lines still selects one.
                distance = abs(line.rect.center.y - point.y)
                if distance < best_distance:
                    best, best_distance = line, distance
        return best

    def block_at(self, page: int, point: Point) -> TextBlock | None:
        """The paragraph under ``point`` (used by "edit paragraph").

        MuPDF often reports each visual line as its own block, so neighbouring
        blocks that share a column and sit a normal line-height apart are
        merged into the paragraph a reader would recognise.
        """
        blocks = self.doc.extract_blocks(page)
        index = next(
            (i for i, b in enumerate(blocks) if b.rect.expanded(2).contains(point)),
            None,
        )
        if index is None:
            return None
        group = self._paragraph_group(blocks, index)
        if len(group) == 1:
            return group[0]
        lines = [line for block in group for line in block.lines]
        rect = group[0].rect
        for block in group[1:]:
            rect = rect.united(block.rect)
        return TextBlock(lines=lines, rect=rect, block_no=group[0].block_no)

    @staticmethod
    def _paragraph_group(blocks: list[TextBlock], index: int) -> list[TextBlock]:
        """Blocks that read as one paragraph with ``blocks[index]``."""

        def line_height(block: TextBlock) -> float:
            """Height of a single line, not of the whole block.

            Using the block's full height meant a three-line paragraph
            reported a 45 pt "leading", so the blank gap before the *next*
            paragraph looked like ordinary line spacing and everything on the
            page merged into one selection.
            """
            heights = [ln.rect.height for ln in block.lines if ln.rect.height > 0]
            if heights:
                return sum(heights) / len(heights)
            return max(block.rect.height, 1.0)

        def font_size(block: TextBlock) -> float:
            sizes = [
                sp.size for ln in block.lines for sp in ln.spans if sp.size and sp.text.strip()
            ]
            return max(sizes) if sizes else 0.0

        def joins(upper: TextBlock, lower: TextBlock) -> bool:
            gap = lower.rect.y0 - upper.rect.y1
            leading = max(line_height(upper), line_height(lower), 1.0)
            # A heading is set larger than its body text, so a marked size
            # change is a paragraph boundary even when the spacing is tight.
            upper_size, lower_size = font_size(upper), font_size(lower)
            if upper_size and lower_size:
                larger = max(upper_size, lower_size)
                smaller = min(upper_size, lower_size)
                if larger - smaller > 0.5 and larger / smaller > 1.15:
                    return False
            # More lenient merging: allow up to 10pt margin difference and
            # gap up to 1.0 * line_height to handle more paragraph layouts.
            return (
                abs(lower.rect.x0 - upper.rect.x0) <= 10.0  # same left margin
                # Consecutive lines sit within about one line-height of each
                # other; a wider gap is deliberate paragraph spacing.
                and -2.0 <= gap <= leading * 1.0
            )

        ordered = sorted(blocks, key=lambda b: (round(b.rect.y0, 1), b.rect.x0))
        position = ordered.index(blocks[index])
        group = [ordered[position]]
        cursor = position
        while cursor > 0 and joins(ordered[cursor - 1], group[0]):
            cursor -= 1
            group.insert(0, ordered[cursor])
        cursor = position
        while cursor + 1 < len(ordered) and joins(group[-1], ordered[cursor + 1]):
            cursor += 1
            group.append(ordered[cursor])
        return group

    def edit_region(
        self, page: int, point: Point, *, whole_paragraph: bool = False
    ) -> tuple[Rect, str, TextStyle] | None:
        """Resolve what a click should edit.

        Returns the rectangle to replace, its current text and the style to
        reuse — or ``None`` when there is no text under the cursor.

        A line's own bounding box is only as wide as its glyphs, so it is
        widened to the column (the paragraph's width) — otherwise a short line
        such as the last one in a paragraph would wrap after a couple of words.
        """
        if whole_paragraph:
            block = self.block_at(page, point)
            if block is None:
                return None
            style = self._style_of_line(block.lines[0]) if block.lines else TextStyle()
            return block.rect, block.text, style

        line = self.line_at(page, point)
        if line is None:
            return None
        rect = Rect(line.rect.x0, line.rect.y0, self._column_right(page, line), line.rect.y1)
        return rect, line.text, self._style_of_line(line)

    def _column_right(self, page: int, line: TextLine) -> float:
        """Right-hand edge the edited line may wrap against.

        A text block is only as wide as the glyphs it happens to contain, so a
        short line would wrap after a word or two. The column edge is inferred
        from the widest neighbouring block that shares this line's left margin,
        falling back to the page's right margin.
        """
        page_width = self.doc.page_size(page)[0]
        margin = min(line.rect.x0, 72.0)
        right = page_width - margin

        # Blocks that overlap this line vertically and sit to its right mark a
        # real column boundary (a second column, a sidebar, a figure).
        neighbours = [
            block.rect.x0
            for block in self.doc.extract_blocks(page)
            if block.rect.x0 > line.rect.x1 + 8
            and block.rect.y1 > line.rect.y0
            and block.rect.y0 < line.rect.y1
        ]
        for info in self.doc.page_images(page):
            if (
                info.rect.x0 > line.rect.x1 + 8
                and info.rect.y1 > line.rect.y0
                and info.rect.y0 < line.rect.y1
            ):
                neighbours.append(info.rect.x0)
        if neighbours:
            right = min(right, min(neighbours) - 8.0)
        return max(line.rect.x1, right)

    @staticmethod
    def _style_of_line(line: TextLine) -> TextStyle:
        """Style of the widest span on a line (its dominant formatting)."""
        spans = [s for s in line.spans if s.text.strip()]
        if not spans:
            return TextStyle()
        span = max(spans, key=lambda s: s.rect.width)
        return TextStyle(
            font=span.font,
            size=span.size or 11.0,
            color=span.color,
            bold=span.bold,
            italic=span.italic,
        )

    def style_at(self, page: int, point: Point) -> TextStyle | None:
        """Style of the span under ``point`` (for the properties panel)."""
        for span in self._spans(page):
            if span.rect.contains(point):
                return TextStyle(
                    font=span.font,
                    size=span.size,
                    color=span.color,
                    bold=span.bold,
                    italic=span.italic,
                )
        return None

    def fonts(self, page: int | None = None) -> list[dict[str, Any]]:
        """List fonts used by the document (or one page) with embedding state."""
        pages = [page] if page is not None else range(self.doc.page_count)
        seen: dict[str, dict[str, Any]] = {}
        with self.doc.locked() as handle:
            for index in pages:
                for font in handle[index].get_fonts(full=True):
                    xref, ext, ftype, basefont, name = (
                        font[0],
                        font[1],
                        font[2],
                        font[3],
                        font[4],
                    )
                    seen.setdefault(
                        basefont,
                        {
                            "xref": xref,
                            "name": basefont,
                            "type": ftype,
                            "embedded": ext not in ("n/a", ""),
                            "ext": ext,
                            "ref": name,
                            "pages": [],
                        },
                    )["pages"].append(index)
        return list(seen.values())

    def add_rich_text(
        self,
        page: int,
        rect: Rect,
        html: str,
        *,
        css: str = "",
    ) -> None:
        """Insert HTML-formatted rich text (bold, italic, lists, colours)."""

        class _AddHtml(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    handle[page].insert_htmlbox(fitz.Rect(*rect), html, css=css)

        self.doc.undo_stack.push(_AddHtml(self.doc, page, "Add rich text"))

    def replace_block(
        self,
        page: int,
        rect: Rect,
        html: str,
        *,
        css: str = "",
        background: Color | None = None,
    ) -> None:
        """Erase a paragraph and reflow new HTML content into its rectangle."""

        class _ReplaceHtml(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    p = handle[page]
                    p.add_redact_annot(fitz.Rect(*rect), fill=_redact_fill(background))
                    p.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
                    p.insert_htmlbox(fitz.Rect(*rect), html, css=css)

        self.doc.undo_stack.push(_ReplaceHtml(self.doc, page, "Edit paragraph"))


def _style_at(doc: PdfDocument, page: int, rect: Rect) -> TextStyle:
    """Infer the dominant style inside ``rect`` so replacements blend in."""
    best: TextSpan | None = None
    best_area = 0.0
    for block in doc.extract_blocks(page):
        for line in block.lines:
            for span in line.spans:
                if not span.rect.intersects(rect):
                    continue
                area = span.rect.intersection(rect).area
                if area > best_area:
                    best, best_area = span, area
    if best is None:
        return TextStyle()
    return TextStyle(
        font=best.font,
        size=best.size or 11.0,
        color=best.color,
        bold=best.bold,
        italic=best.italic,
    )


def _resolve_font(page: fitz.Page, style: TextStyle) -> tuple[str, str | None]:
    """Map a :class:`TextStyle` to ``(fontname, fontfile)`` for PyMuPDF."""
    if style.file:
        alias = re.sub(r"\W", "", style.font)[:16] or "custom"
        try:
            page.insert_font(fontname=alias, fontfile=style.file)
            return alias, style.file
        except Exception:
            log.warning("Could not embed {}; falling back to base-14", style.file)
    # When bold or italic is set, use base14() to get the correct font variant
    # (e.g., "Helvetica-Bold" instead of just "Helvetica").
    if style.bold or style.italic:
        return style.base14(), None
    if style.font in STANDARD_FONTS:
        return STANDARD_FONTS[style.font], None
    return style.base14(), None


# --------------------------------------------------------------------------- #
# Image editing
# --------------------------------------------------------------------------- #
def _require_pillow() -> Any:
    try:
        from PIL import Image

        return Image
    except ImportError as exc:  # pragma: no cover
        raise DependencyMissingError("Pillow", "Image editing") from exc


@dataclass(slots=True)
class ImageAdjustments:
    """Pixel adjustments applied when replacing/inserting an image."""

    brightness: float = 1.0
    contrast: float = 1.0
    saturation: float = 1.0
    gamma: float = 1.0
    sharpen: float = 0.0
    blur: float = 0.0
    opacity: float = 1.0
    grayscale: bool = False
    invert: bool = False
    mirror: bool = False
    flip: bool = False
    rotate: int = 0
    denoise: bool = False
    auto_contrast: bool = False
    remove_background: bool = False

    def is_identity(self) -> bool:
        return self == ImageAdjustments()

    def apply(self, data: bytes) -> bytes:
        """Return adjusted PNG bytes."""
        Image = _require_pillow()
        from PIL import ImageEnhance, ImageFilter, ImageOps

        img = Image.open(io.BytesIO(data))
        img = img.convert("RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB")
        if self.rotate:
            img = img.rotate(-self.rotate, expand=True)
        if self.mirror:
            img = ImageOps.mirror(img)
        if self.flip:
            img = ImageOps.flip(img)
        if self.grayscale:
            img = ImageOps.grayscale(img).convert(img.mode)
        if self.invert:
            rgb = img.convert("RGB")
            inverted = ImageOps.invert(rgb)
            img = inverted.convert(img.mode)
        if self.auto_contrast:
            rgb = ImageOps.autocontrast(img.convert("RGB"))
            img = rgb.convert(img.mode)
        if self.brightness != 1.0:
            img = ImageEnhance.Brightness(img).enhance(self.brightness)
        if self.contrast != 1.0:
            img = ImageEnhance.Contrast(img).enhance(self.contrast)
        if self.saturation != 1.0:
            img = ImageEnhance.Color(img).enhance(self.saturation)
        if self.sharpen:
            img = ImageEnhance.Sharpness(img).enhance(1.0 + self.sharpen)
        if self.blur:
            img = img.filter(ImageFilter.GaussianBlur(self.blur))
        if self.denoise:
            img = img.filter(ImageFilter.MedianFilter(3))
        if self.gamma != 1.0:
            lut = [min(255, int((i / 255) ** (1 / self.gamma) * 255)) for i in range(256)]
            channels = len(img.getbands())
            img = img.point(lut * channels)
        if self.remove_background:
            img = _remove_background(img)
        if self.opacity < 1.0:
            img = img.convert("RGBA")
            alpha = img.getchannel("A").point(lambda v: int(v * self.opacity))
            img.putalpha(alpha)
        out = io.BytesIO()
        img.save(out, format="PNG")
        return out.getvalue()


def _remove_background(img: Any, tolerance: int = 28) -> Any:
    """Make near-uniform border colour transparent (simple chroma keying)."""
    import numpy as np

    rgba = img.convert("RGBA")
    arr = np.array(rgba)
    corners = np.vstack([arr[0, :, :3], arr[-1, :, :3], arr[:, 0, :3], arr[:, -1, :3]])
    key = np.median(corners, axis=0)
    distance = np.abs(arr[:, :, :3].astype(int) - key.astype(int)).sum(axis=2)
    arr[:, :, 3] = np.where(distance < tolerance * 3, 0, arr[:, :, 3])
    Image = _require_pillow()
    return Image.fromarray(arr, "RGBA")


class InsertImageCommand(PageSnapshotCommand):
    """Place a new image on a page."""

    def __init__(
        self,
        doc: PdfDocument,
        page: int,
        rect: Rect,
        data: bytes,
        *,
        keep_aspect: bool = True,
        rotate: int = 0,
        overlay: bool = True,
    ) -> None:
        super().__init__(doc, page, "Insert image")
        self.rect = rect
        self.data = data
        self.keep_aspect = keep_aspect
        self.rotate = rotate
        self.overlay = overlay

    def apply(self) -> None:
        with self.doc.locked() as handle:
            handle[self.page].insert_image(
                fitz.Rect(*self.rect),
                stream=self.data,
                keep_proportion=self.keep_aspect,
                rotate=self.rotate,
                overlay=self.overlay,
            )

    def memory_cost(self) -> int:
        return super().memory_cost() + len(self.data)


class ReplaceImageCommand(PageSnapshotCommand):
    """Swap the pixels of an existing image, keeping its placement."""

    def __init__(self, doc: PdfDocument, page: int, xref: int, data: bytes) -> None:
        super().__init__(doc, page, "Replace image")
        self.xref = xref
        self.data = data

    def apply(self) -> None:
        with self.doc.locked() as handle:
            handle[self.page].replace_image(self.xref, stream=self.data)


class DeleteImageCommand(PageSnapshotCommand):
    """Remove an image from the page."""

    def __init__(self, doc: PdfDocument, page: int, xref: int) -> None:
        super().__init__(doc, page, "Delete image")
        self.xref = xref

    def apply(self) -> None:
        with self.doc.locked() as handle:
            handle[self.page].delete_image(self.xref)


class ImageEditor:
    """Facade for raster image operations."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    def list(self, page: int) -> list[ImageInfo]:
        return self.doc.page_images(page)

    def insert(
        self,
        page: int,
        rect: Rect,
        data: bytes | str,
        *,
        adjustments: ImageAdjustments | None = None,
        keep_aspect: bool = True,
        rotate: int = 0,
    ) -> None:
        """Insert an image from bytes or a file path."""
        payload = _load_image_bytes(data)
        if adjustments and not adjustments.is_identity():
            payload = adjustments.apply(payload)
        self.doc.undo_stack.push(
            InsertImageCommand(
                self.doc, page, rect, payload, keep_aspect=keep_aspect, rotate=rotate
            )
        )

    def replace(
        self,
        page: int,
        xref: int,
        data: bytes | str,
        *,
        adjustments: ImageAdjustments | None = None,
    ) -> None:
        payload = _load_image_bytes(data)
        if adjustments and not adjustments.is_identity():
            payload = adjustments.apply(payload)
        self.doc.undo_stack.push(ReplaceImageCommand(self.doc, page, xref, payload))

    def delete(self, page: int, xref: int) -> None:
        self.doc.undo_stack.push(DeleteImageCommand(self.doc, page, xref))

    def adjust(self, page: int, xref: int, adjustments: ImageAdjustments) -> None:
        """Apply pixel adjustments to an image already on the page."""
        data, _ = self.doc.extract_image(xref)
        self.replace(page, xref, adjustments.apply(data))

    def crop(self, page: int, xref: int, box: Rect) -> None:
        """Crop an image using fractional (0-1) coordinates of the image."""
        Image = _require_pillow()
        data, _ = self.doc.extract_image(xref)
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        pixels = (
            int(box.x0 * w) if box.x1 <= 1 else int(box.x0),
            int(box.y0 * h) if box.y1 <= 1 else int(box.y0),
            int(box.x1 * w) if box.x1 <= 1 else int(box.x1),
            int(box.y1 * h) if box.y1 <= 1 else int(box.y1),
        )
        out = io.BytesIO()
        img.crop(pixels).save(out, format="PNG")
        self.replace(page, xref, out.getvalue())

    def resample(
        self, page: int, xref: int, max_pixels: int = 2_000_000, quality: int = 82
    ) -> int:
        """Downsample + JPEG-compress an image. Returns the bytes saved."""
        Image = _require_pillow()
        data, _ = self.doc.extract_image(xref)
        img = Image.open(io.BytesIO(data)).convert("RGB")
        w, h = img.size
        if w * h > max_pixels:
            scale = (max_pixels / (w * h)) ** 0.5
            img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=quality, optimize=True)
        new_data = out.getvalue()
        if len(new_data) >= len(data):
            return 0
        self.replace(page, xref, new_data)
        return len(data) - len(new_data)

    def extract_all(self, page: int | None = None) -> list[tuple[ImageInfo, bytes]]:
        """Extract every embedded image (optionally from a single page)."""
        pages = [page] if page is not None else range(self.doc.page_count)
        out: list[tuple[ImageInfo, bytes]] = []
        seen: set[int] = set()
        for index in pages:
            for info in self.doc.page_images(index):
                if info.xref in seen:
                    continue
                seen.add(info.xref)
                try:
                    data, _ = self.doc.extract_image(info.xref)
                except Exception:
                    continue
                out.append((info, data))
        return out


def _load_image_bytes(data: bytes | str) -> bytes:
    if isinstance(data, bytes):
        return data
    from pathlib import Path

    path = Path(data)
    if not path.exists():
        raise ValidationError(f"Image not found: {data}")
    return path.read_bytes()


# --------------------------------------------------------------------------- #
# Vector graphics
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class ShapeStyle:
    """Stroke/fill parameters for vector drawing."""

    stroke: Color | None = BLACK
    fill: Color | None = None
    width: float = 1.0
    opacity: float = 1.0
    fill_opacity: float = 1.0
    dashes: str = ""
    line_cap: int = 0
    line_join: int = 0
    blend: BlendMode = BlendMode.NORMAL
    even_odd: bool = False
    closed: bool = True


class DrawCommand(PageSnapshotCommand):
    """Draw a vector shape onto a page."""

    def __init__(
        self,
        doc: PdfDocument,
        page: int,
        kind: str,
        points: Sequence[Point],
        style: ShapeStyle,
        *,
        label: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(doc, page, label or f"Draw {kind}")
        self.kind = kind
        self.points = list(points)
        self.style = style
        self.extra = extra or {}

    def apply(self) -> None:
        with self.doc.locked() as handle:
            page = handle[self.page]
            shape = page.new_shape()
            pts = [fitz.Point(p.x, p.y) for p in self.points]
            match self.kind:
                case "line":
                    shape.draw_line(pts[0], pts[1])
                case "rect":
                    shape.draw_rect(fitz.Rect(pts[0], pts[1]))
                case "round-rect":
                    shape.draw_rect(
                        fitz.Rect(pts[0], pts[1]), radius=self.extra.get("radius", 0.1)
                    )
                case "circle" | "ellipse":
                    shape.draw_oval(fitz.Rect(pts[0], pts[1]))
                case "polyline":
                    shape.draw_polyline(pts)
                case "polygon":
                    shape.draw_polyline([*pts, pts[0]])
                case "bezier":
                    shape.draw_bezier(pts[0], pts[1], pts[2], pts[3])
                case "curve":
                    shape.draw_curve(pts[0], pts[1], pts[2])
                case "quad":
                    shape.draw_quad(fitz.Quad(*pts[:4]))
                case "sector":
                    shape.draw_sector(pts[0], pts[1], self.extra.get("angle", 90.0))
                case "squiggle":
                    shape.draw_squiggle(pts[0], pts[1])
                case "zigzag":
                    shape.draw_zigzag(pts[0], pts[1])
                case _:
                    raise ValidationError(f"Unknown shape {self.kind!r}")
            shape.finish(
                color=self.style.stroke.to_rgb_tuple() if self.style.stroke else None,
                fill=self.style.fill.to_rgb_tuple() if self.style.fill else None,
                width=self.style.width,
                dashes=self.style.dashes or None,
                lineCap=self.style.line_cap,
                lineJoin=self.style.line_join,
                closePath=self.style.closed,
                even_odd=self.style.even_odd,
                stroke_opacity=self.style.opacity,
                fill_opacity=self.style.fill_opacity,
            )
            shape.commit()


class VectorEditor:
    """Facade for vector drawing and path inspection."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    def paths(self, page: int) -> list[DrawingPath]:
        return self.doc.page_drawings(page)

    def draw(
        self,
        page: int,
        kind: str,
        points: Sequence[Point],
        style: ShapeStyle | None = None,
        **extra: Any,
    ) -> None:
        """Draw ``kind`` (line/rect/circle/polygon/bezier/…) from ``points``."""
        self.doc.undo_stack.push(
            DrawCommand(self.doc, page, kind, points, style or ShapeStyle(), extra=extra)
        )

    def draw_gradient(
        self,
        page: int,
        rect: Rect,
        start: Color,
        end: Color,
        *,
        steps: int = 96,
        horizontal: bool = False,
    ) -> None:
        """Approximate a linear gradient with banded fills."""

        class _Gradient(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    p = handle[page]
                    shape = p.new_shape()
                    span = (rect.x1 - rect.x0) if horizontal else (rect.y1 - rect.y0)
                    step = span / steps
                    for i in range(steps):
                        t = i / max(1, steps - 1)
                        color = (
                            start.r + (end.r - start.r) * t,
                            start.g + (end.g - start.g) * t,
                            start.b + (end.b - start.b) * t,
                        )
                        if horizontal:
                            band = fitz.Rect(
                                rect.x0 + i * step,
                                rect.y0,
                                rect.x0 + (i + 1) * step + 0.5,
                                rect.y1,
                            )
                        else:
                            band = fitz.Rect(
                                rect.x0,
                                rect.y0 + i * step,
                                rect.x1,
                                rect.y0 + (i + 1) * step + 0.5,
                            )
                        shape.draw_rect(band)
                        shape.finish(color=None, fill=color, width=0)
                    shape.commit()

        self.doc.undo_stack.push(_Gradient(self.doc, page, "Draw gradient"))

    def import_svg(self, page: int, rect: Rect, svg: str | bytes) -> None:
        """Place an SVG document onto the page as vector content."""
        payload = svg.encode() if isinstance(svg, str) else svg

        class _Svg(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                svg_doc = fitz.open("svg", payload)
                try:
                    pdf_bytes = svg_doc.convert_to_pdf()
                finally:
                    svg_doc.close()
                overlay = fitz.open("pdf", pdf_bytes)
                try:
                    with self.doc.locked() as handle:
                        handle[page].show_pdf_page(fitz.Rect(*rect), overlay, 0)
                finally:
                    overlay.close()

        self.doc.undo_stack.push(_Svg(self.doc, page, "Import SVG"))

    def clear_page_vectors(self, page: int) -> None:
        """Remove all vector drawings from a page (keeps text and images)."""

        class _Clear(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    p = handle[page]
                    for drawing in p.get_drawings():
                        p.add_redact_annot(drawing["rect"])
                    p.apply_redactions(
                        images=fitz.PDF_REDACT_IMAGE_NONE,
                        text=fitz.PDF_REDACT_TEXT_NONE,
                    )

        self.doc.undo_stack.push(_Clear(self.doc, page, "Clear vector graphics"))
