"""Page management: insert, delete, move, rotate, crop, split, merge, extract.

Every public method returns (or pushes) a :class:`~pdfstudio.core.undo.Command`
so page operations are fully undoable.  Because MuPDF cannot cheaply reverse a
structural edit, the commands snapshot the affected pages as a small in-memory
PDF and re-insert them on undo — accurate and, for realistic page counts, fast.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pymupdf as fitz

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.exceptions import ValidationError
from pdfstudio.core.jobs import JobContext
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.undo import Command
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.types import PAGE_SIZES, PageSize, Rect, Rotation

log = get_logger("pages")


def _publish(doc: PdfDocument, action: str, pages: Sequence[int] = ()) -> None:
    doc.mark_modified(action)
    bus().publish(
        Topic.PAGES_MUTATED,
        {"document_id": doc.id, "action": action, "pages": list(pages)},
        source="pages",
    )


class _PageSnapshot:
    """Serialises a set of pages so they can be restored on undo."""

    __slots__ = ("data", "indices")

    def __init__(self, doc: PdfDocument, indices: Sequence[int]) -> None:
        self.indices = sorted(indices)
        with doc.locked() as handle:
            buffer = fitz.open()
            for index in self.indices:
                buffer.insert_pdf(handle, from_page=index, to_page=index, annots=True)
            self.data = buffer.tobytes(garbage=0, deflate=True)
            buffer.close()

    def restore_into(self, doc: PdfDocument) -> None:
        """Re-insert the snapshot pages at their original positions."""
        with doc.locked() as handle:
            source = fitz.open(stream=self.data, filetype="pdf")
            try:
                for offset, target in enumerate(self.indices):
                    handle.insert_pdf(
                        source,
                        from_page=offset,
                        to_page=offset,
                        start_at=min(target, handle.page_count),
                        annots=True,
                    )
            finally:
                source.close()

    @property
    def size(self) -> int:
        return len(self.data)


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
class DeletePagesCommand(Command):
    """Delete pages, remembering them for undo."""

    def __init__(self, doc: PdfDocument, indices: Sequence[int]) -> None:
        count = len(set(indices))
        super().__init__(f"Delete {count} page{'s' if count != 1 else ''}")
        self._doc = doc
        self._indices = sorted(set(indices))
        self._snapshot: _PageSnapshot | None = None

    def execute(self) -> None:
        if self._snapshot is None:
            self._snapshot = _PageSnapshot(self._doc, self._indices)
        with self._doc.locked() as handle:
            if len(self._indices) >= handle.page_count:
                raise ValidationError("A document must keep at least one page.")
            handle.delete_pages(self._indices)
        _publish(self._doc, "delete-pages", self._indices)

    def undo(self) -> None:
        assert self._snapshot is not None
        self._snapshot.restore_into(self._doc)
        _publish(self._doc, "restore-pages", self._indices)

    def memory_cost(self) -> int:
        return self._snapshot.size if self._snapshot else 4096


class InsertPagesCommand(Command):
    """Insert blank pages or pages copied from another document."""

    def __init__(
        self,
        doc: PdfDocument,
        at: int,
        *,
        count: int = 1,
        width: float = 595,
        height: float = 842,
        source: PdfDocument | None = None,
        source_range: tuple[int, int] | None = None,
        label: str | None = None,
    ) -> None:
        super().__init__(label or f"Insert {count} page{'s' if count != 1 else ''}")
        self._doc = doc
        self._at = at
        self._count = count
        self._width = width
        self._height = height
        self._source = source
        self._range = source_range
        self._inserted: list[int] = []

    def execute(self) -> None:
        with self._doc.locked() as handle:
            at = max(0, min(self._at, handle.page_count))
            before = handle.page_count
            if self._source is not None:
                start, end = self._range or (0, self._source.page_count - 1)
                with self._source.locked() as src:
                    handle.insert_pdf(
                        src, from_page=start, to_page=end, start_at=at, annots=True
                    )
            else:
                for offset in range(self._count):
                    handle.new_page(pno=at + offset, width=self._width, height=self._height)
            self._inserted = list(range(at, at + handle.page_count - before))
        _publish(self._doc, "insert-pages", self._inserted)

    def undo(self) -> None:
        if not self._inserted:
            return
        with self._doc.locked() as handle:
            handle.delete_pages(self._inserted)
        _publish(self._doc, "delete-pages", self._inserted)


class MovePagesCommand(Command):
    """Reorder pages (drag & drop in the thumbnail panel)."""

    def __init__(self, doc: PdfDocument, indices: Sequence[int], target: int) -> None:
        super().__init__(f"Move {len(indices)} page(s)")
        self._doc = doc
        self._indices = sorted(set(indices))
        self._target = target
        self._before: list[int] = []

    def _current_order(self) -> list[int]:
        return list(range(self._doc.page_count))

    def execute(self) -> None:
        with self._doc.locked() as handle:
            order = self._current_order()
            self._before = order[:]
            moving = [i for i in order if i in set(self._indices)]
            rest = [i for i in order if i not in set(self._indices)]
            insert_at = self._target - sum(1 for i in self._indices if i < self._target)
            insert_at = max(0, min(insert_at, len(rest)))
            new_order = rest[:insert_at] + moving + rest[insert_at:]
            handle.select(new_order)
        _publish(self._doc, "move-pages", self._indices)

    def undo(self) -> None:
        with self._doc.locked() as handle:
            # Compute the permutation that restores the original order.
            order = self._before
            moving = [i for i in order if i in set(self._indices)]
            rest = [i for i in order if i not in set(self._indices)]
            insert_at = self._target - sum(1 for i in self._indices if i < self._target)
            insert_at = max(0, min(insert_at, len(rest)))
            current = rest[:insert_at] + moving + rest[insert_at:]
            inverse = [current.index(i) for i in order]
            handle.select(inverse)
        _publish(self._doc, "move-pages", self._indices)


class RotatePagesCommand(Command):
    """Rotate pages by a relative or absolute amount."""

    merge_id = 0x501

    def __init__(
        self,
        doc: PdfDocument,
        indices: Sequence[int],
        degrees: int,
        *,
        absolute: bool = False,
    ) -> None:
        super().__init__(f"Rotate {len(indices)} page(s) {degrees}°")
        self._doc = doc
        self._indices = list(indices)
        self._degrees = degrees
        self._absolute = absolute
        self._previous: dict[int, int] = {}

    def execute(self) -> None:
        with self._doc.locked() as handle:
            for index in self._indices:
                page = handle[index]
                self._previous.setdefault(index, page.rotation)
                value = self._degrees if self._absolute else page.rotation + self._degrees
                page.set_rotation(int(Rotation.normalise(value)))
        _publish(self._doc, "rotate-pages", self._indices)

    def undo(self) -> None:
        with self._doc.locked() as handle:
            for index, rotation in self._previous.items():
                handle[index].set_rotation(rotation)
        _publish(self._doc, "rotate-pages", self._indices)


class CropPagesCommand(Command):
    """Set the CropBox of one or more pages."""

    def __init__(self, doc: PdfDocument, indices: Sequence[int], box: Rect) -> None:
        super().__init__(f"Crop {len(indices)} page(s)")
        self._doc = doc
        self._indices = list(indices)
        self._box = box
        self._previous: dict[int, tuple[float, float, float, float]] = {}

    def execute(self) -> None:
        with self._doc.locked() as handle:
            for index in self._indices:
                page = handle[index]
                self._previous.setdefault(index, tuple(page.cropbox))
                media = page.mediabox
                box = fitz.Rect(
                    max(self._box.x0, media.x0),
                    max(self._box.y0, media.y0),
                    min(self._box.x1, media.x1),
                    min(self._box.y1, media.y1),
                )
                if box.is_empty:
                    raise ValidationError("Crop box is empty.")
                page.set_cropbox(box)
        _publish(self._doc, "crop-pages", self._indices)

    def undo(self) -> None:
        with self._doc.locked() as handle:
            for index, box in self._previous.items():
                handle[index].set_cropbox(fitz.Rect(*box))
        _publish(self._doc, "crop-pages", self._indices)


class DuplicatePagesCommand(Command):
    """Copy pages and insert the copies after the originals."""

    def __init__(self, doc: PdfDocument, indices: Sequence[int], copies: int = 1):
        super().__init__(f"Duplicate {len(indices)} page(s)")
        self._doc = doc
        self._indices = sorted(set(indices))
        self._copies = copies
        self._added: list[int] = []

    def execute(self) -> None:
        with self._doc.locked() as handle:
            added: list[int] = []
            offset = 0
            for index in self._indices:
                for _ in range(self._copies):
                    handle.fullcopy_page(index + offset, index + offset + 1)
                    offset += 1
                    added.append(index + offset)
            self._added = added
        _publish(self._doc, "duplicate-pages", self._added)

    def undo(self) -> None:
        with self._doc.locked() as handle:
            handle.delete_pages(sorted(self._added, reverse=True))
        _publish(self._doc, "delete-pages", self._added)


class ResizePagesCommand(Command):
    """Scale page content onto a different paper size."""

    def __init__(
        self,
        doc: PdfDocument,
        indices: Sequence[int],
        size: PageSize,
        *,
        keep_aspect: bool = True,
    ) -> None:
        super().__init__(f"Resize {len(indices)} page(s) to {size.name}")
        self._doc = doc
        self._indices = list(indices)
        self._size = size
        self._keep_aspect = keep_aspect
        self._backup: bytes = b""

    def execute(self) -> None:
        if not self._backup:
            self._backup = self._doc.to_bytes(SaveOptions.fast())
        # MuPDF forbids show_pdf_page() with source == target, so stamp from a
        # throw-away copy of the current state.
        source = fitz.open(stream=self._backup, filetype="pdf")
        try:
            with self._doc.locked() as handle:
                target = fitz.Rect(0, 0, self._size.width, self._size.height)
                for index in self._indices:
                    src_rect = source[index].rect
                    new_page = handle.new_page(
                        pno=index + 1, width=self._size.width, height=self._size.height
                    )
                    if self._keep_aspect:
                        scale = min(
                            target.width / src_rect.width,
                            target.height / src_rect.height,
                        )
                        w, h = src_rect.width * scale, src_rect.height * scale
                        place = fitz.Rect(
                            (target.width - w) / 2,
                            (target.height - h) / 2,
                            (target.width + w) / 2,
                            (target.height + h) / 2,
                        )
                    else:
                        place = target
                    new_page.show_pdf_page(place, source, index)
                handle.delete_pages(self._indices)
        finally:
            source.close()
        _publish(self._doc, "resize-pages", self._indices)

    def undo(self) -> None:
        with self._doc.locked() as handle:
            restored = fitz.open(stream=self._backup, filetype="pdf")
            try:
                handle.delete_pages(list(range(handle.page_count)))
                handle.insert_pdf(restored, annots=True)
            finally:
                restored.close()
        _publish(self._doc, "resize-pages", self._indices)

    def memory_cost(self) -> int:
        return len(self._backup) or 4096


# --------------------------------------------------------------------------- #
# Service facade
# --------------------------------------------------------------------------- #
class PageService:
    """High-level page operations bound to one :class:`PdfDocument`."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    # -- undoable operations ------------------------------------------------ #
    def delete(self, indices: Sequence[int]) -> None:
        """Delete pages (undoable)."""
        if not indices:
            return
        self.doc.undo_stack.push(DeletePagesCommand(self.doc, indices))

    def insert_blank(
        self,
        at: int,
        count: int = 1,
        size: PageSize | str = "A4",
        *,
        landscape: bool = False,
    ) -> None:
        """Insert ``count`` blank pages of ``size`` at index ``at``."""
        page_size = PAGE_SIZES[size] if isinstance(size, str) else size
        if landscape:
            page_size = page_size.landscape()
        self.doc.undo_stack.push(
            InsertPagesCommand(
                self.doc,
                at,
                count=count,
                width=page_size.width,
                height=page_size.height,
            )
        )

    def insert_from_document(
        self, at: int, source: PdfDocument, page_range: tuple[int, int] | None = None
    ) -> None:
        """Insert pages copied from ``source``."""
        self.doc.undo_stack.push(
            InsertPagesCommand(
                self.doc,
                at,
                source=source,
                source_range=page_range,
                label=f"Insert pages from {source.display_name}",
            )
        )

    def move(self, indices: Sequence[int], target: int) -> None:
        self.doc.undo_stack.push(MovePagesCommand(self.doc, indices, target))

    def rotate(self, indices: Sequence[int], degrees: int, *, absolute: bool = False) -> None:
        self.doc.undo_stack.push(
            RotatePagesCommand(self.doc, indices, degrees, absolute=absolute)
        )

    def crop(self, indices: Sequence[int], box: Rect) -> None:
        self.doc.undo_stack.push(CropPagesCommand(self.doc, indices, box))

    def duplicate(self, indices: Sequence[int], copies: int = 1) -> None:
        self.doc.undo_stack.push(DuplicatePagesCommand(self.doc, indices, copies))

    def resize(
        self, indices: Sequence[int], size: PageSize | str, *, keep_aspect: bool = True
    ) -> None:
        page_size = PAGE_SIZES[size] if isinstance(size, str) else size
        self.doc.undo_stack.push(
            ResizePagesCommand(self.doc, indices, page_size, keep_aspect=keep_aspect)
        )

    def replace(self, index: int, source: PdfDocument, source_index: int = 0) -> None:
        """Replace one page with a page from another document."""
        with self.doc.undo_stack.macro(f"Replace page {index + 1}"):
            self.insert_from_document(index, source, (source_index, source_index))
            self.delete([index + 1])

    # -- non-mutating operations -------------------------------------------- #
    def extract(self, indices: Sequence[int]) -> PdfDocument:
        """Return a **new** document containing only ``indices`` (in order)."""
        if not indices:
            raise ValidationError("No pages selected for extraction.")
        with self.doc.locked() as handle:
            out = fitz.open()
            for index in indices:
                out.insert_pdf(handle, from_page=index, to_page=index, annots=True)
            data = out.tobytes()
            out.close()
        name = f"{Path(self.doc.display_name).stem}-extract.pdf"
        return PdfDocument.from_bytes(data, name=name)

    def split_by_count(self, pages_per_file: int) -> list[PdfDocument]:
        """Split into chunks of ``pages_per_file`` pages."""
        if pages_per_file < 1:
            raise ValidationError("Pages per file must be >= 1.")
        total = self.doc.page_count
        return [
            self.extract(list(range(start, min(start + pages_per_file, total))))
            for start in range(0, total, pages_per_file)
        ]

    def split_at(self, boundaries: Sequence[int]) -> list[PdfDocument]:
        """Split before each 0-based index in ``boundaries``."""
        marks = sorted({b for b in boundaries if 0 < b < self.doc.page_count})
        starts = [0, *marks]
        ends = [*marks, self.doc.page_count]
        return [
            self.extract(list(range(s, e))) for s, e in zip(starts, ends, strict=True) if e > s
        ]

    def split_by_bookmarks(self, level: int = 1) -> list[tuple[str, PdfDocument]]:
        """Split at every bookmark of ``level``; returns ``(title, document)``."""
        marks = [bm for bm in _flatten(self.doc.bookmarks()) if bm.level == level]
        if not marks:
            raise ValidationError(f"No level {level} bookmarks found.")
        result: list[tuple[str, PdfDocument]] = []
        for i, bm in enumerate(marks):
            end = marks[i + 1].page if i + 1 < len(marks) else self.doc.page_count
            pages = list(range(bm.page, max(bm.page + 1, end)))
            result.append((bm.title, self.extract(pages)))
        return result

    def export_pages_to_files(
        self,
        directory: str | Path,
        indices: Sequence[int] | None = None,
        *,
        prefix: str = "page",
        ctx: JobContext | None = None,
    ) -> list[Path]:
        """Write one PDF per page into ``directory``."""
        out_dir = Path(directory)
        out_dir.mkdir(parents=True, exist_ok=True)
        targets = list(indices) if indices is not None else list(range(self.doc.page_count))
        if ctx:
            ctx.set_total(len(targets))
        width = len(str(max(targets, default=0) + 1))
        written: list[Path] = []
        for n, index in enumerate(targets, 1):
            single = self.extract([index])
            path = out_dir / f"{prefix}-{index + 1:0{width}d}.pdf"
            single.save_as(path)
            single.close()
            written.append(path)
            if ctx:
                ctx.progress(n, f"Exported page {index + 1}")
        return written


# --------------------------------------------------------------------------- #
# Cross-document helpers
# --------------------------------------------------------------------------- #
def merge_documents(
    sources: Sequence[PdfDocument | str | Path],
    *,
    keep_bookmarks: bool = True,
    passwords: dict[str, str] | None = None,
    ctx: JobContext | None = None,
) -> PdfDocument:
    """Merge documents (paths or open documents) into a new document.

    Args:
        sources: Documents in the desired order.
        keep_bookmarks: Prefix each source's outline with its file name.
        passwords: Optional ``{path: password}`` map for encrypted inputs.
        ctx: Optional job context for progress reporting.

    Returns:
        A new in-memory :class:`PdfDocument`.
    """
    if not sources:
        raise ValidationError("Nothing to merge.")
    passwords = passwords or {}
    output = fitz.open()
    toc: list[list[Any]] = []
    opened: list[PdfDocument] = []
    if ctx:
        ctx.set_total(len(sources))
    try:
        for n, item in enumerate(sources, 1):
            if isinstance(item, PdfDocument):
                doc = item
            else:
                path = Path(item)
                doc = PdfDocument.open(path, passwords.get(str(path)))
                opened.append(doc)
            offset = output.page_count
            with doc.locked() as handle:
                output.insert_pdf(handle, annots=True, show_progress=0)
                if keep_bookmarks:
                    toc.append([1, Path(doc.display_name).stem, offset + 1])
                    for entry in handle.get_toc(simple=True):
                        toc.append([entry[0] + 1, entry[1], entry[2] + offset])
            if ctx:
                ctx.progress(n, f"Merged {doc.display_name}")
        if keep_bookmarks and toc:
            output.set_toc(toc)
        data = output.tobytes(garbage=3, deflate=True)
    finally:
        output.close()
        for doc in opened:
            doc.close()
    log.info("Merged {} documents", len(sources))
    return PdfDocument.from_bytes(data, name="merged.pdf")


def n_up(
    document: PdfDocument,
    columns: int = 2,
    rows: int = 1,
    *,
    size: PageSize | str = "A4",
    landscape: bool = False,
    margin: float = 18.0,
    gap: float = 9.0,
) -> PdfDocument:
    """Impose ``columns × rows`` source pages onto each output page.

    Used for booklet printing, handouts and contact sheets.
    """
    page_size = PAGE_SIZES[size] if isinstance(size, str) else size
    if landscape:
        page_size = page_size.landscape()
    per_sheet = max(1, columns * rows)
    out = fitz.open()
    with document.locked() as src:
        sheets = math.ceil(src.page_count / per_sheet)
        cell_w = (page_size.width - 2 * margin - gap * (columns - 1)) / columns
        cell_h = (page_size.height - 2 * margin - gap * (rows - 1)) / rows
        for sheet in range(sheets):
            page = out.new_page(width=page_size.width, height=page_size.height)
            for slot in range(per_sheet):
                index = sheet * per_sheet + slot
                if index >= src.page_count:
                    break
                col, row = slot % columns, slot // columns
                x = margin + col * (cell_w + gap)
                y = margin + row * (cell_h + gap)
                page.show_pdf_page(fitz.Rect(x, y, x + cell_w, y + cell_h), src, index)
        data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return PdfDocument.from_bytes(data, name=f"{Path(document.display_name).stem}-nup.pdf")


def make_booklet(document: PdfDocument, *, size: PageSize | str = "A4") -> PdfDocument:
    """Reorder pages for saddle-stitch booklet printing (2-up, duplex)."""
    page_size = PAGE_SIZES[size] if isinstance(size, str) else size
    sheet = page_size.landscape()
    total = document.page_count
    padded = total + (-total % 4)
    order: list[int | None] = []
    for i in range(padded // 4):
        order += [padded - 2 * i - 1, 2 * i, 2 * i + 1, padded - 2 * i - 2]
    out = fitz.open()
    with document.locked() as src:
        half = sheet.width / 2
        for pair_start in range(0, len(order), 2):
            page = out.new_page(width=sheet.width, height=sheet.height)
            for slot, index in enumerate(order[pair_start : pair_start + 2]):
                if index is None or index >= total:
                    continue
                rect = fitz.Rect(slot * half, 0, (slot + 1) * half, sheet.height)
                page.show_pdf_page(rect, src, index)
        data = out.tobytes(garbage=3, deflate=True)
    out.close()
    return PdfDocument.from_bytes(data, name="booklet.pdf")


def _flatten(bookmarks: Sequence[Any]) -> list[Any]:
    out: list[Any] = []
    for bm in bookmarks:
        out.append(bm)
        out.extend(_flatten(bm.children))
    return out
