"""Search engine: instant text search, regex, boolean queries and filters.

Searches page text, annotations, bookmarks, form fields and metadata.  Results
carry page-space rectangles so the viewer can highlight and scroll to them.

For very large documents an inverted page index is built lazily in the
background, making repeated searches effectively instantaneous.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from typing import Any

import pymupdf as fitz

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.exceptions import ValidationError
from pdfstudio.core.jobs import JobContext
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import Rect, SearchHit, SearchQuery

log = get_logger("search")

_BOOLEAN_TOKEN = re.compile(r'"[^"]*"|\(|\)|\bAND\b|\bOR\b|\bNOT\b|[^\s()]+')


class _BooleanExpression:
    """Parses and evaluates ``foo AND (bar OR NOT baz)`` style queries."""

    def __init__(self, expression: str, *, case_sensitive: bool = False) -> None:
        self.tokens = _BOOLEAN_TOKEN.findall(expression)
        self.case_sensitive = case_sensitive
        self._pos = 0
        self.terms: list[str] = [
            t.strip('"') for t in self.tokens if t not in ("AND", "OR", "NOT", "(", ")")
        ]
        self._ast = self._parse_or()

    # -- recursive descent parser ------------------------------------------- #
    def _peek(self) -> str | None:
        return self.tokens[self._pos] if self._pos < len(self.tokens) else None

    def _next(self) -> str | None:
        token = self._peek()
        if token is not None:
            self._pos += 1
        return token

    def _parse_or(self) -> Any:
        node = self._parse_and()
        while self._peek() == "OR":
            self._next()
            node = ("or", node, self._parse_and())
        return node

    def _parse_and(self) -> Any:
        node = self._parse_not()
        while self._peek() not in (None, ")", "OR"):
            if self._peek() == "AND":
                self._next()
            node = ("and", node, self._parse_not())
        return node

    def _parse_not(self) -> Any:
        if self._peek() == "NOT":
            self._next()
            return ("not", self._parse_not())
        # ``token`` is a search word, not a credential (ruff S105 false positive).
        token = self._next()
        if token == "(":  # noqa: S105
            node = self._parse_or()
            if self._peek() == ")":
                self._next()
            return node
        return ("term", (token or "").strip('"'))

    # -- evaluation ---------------------------------------------------------- #
    def matches(self, text: str) -> bool:
        haystack = text if self.case_sensitive else text.lower()

        def evaluate(node: Any) -> bool:
            match node[0]:
                case "term":
                    needle = node[1] if self.case_sensitive else node[1].lower()
                    return bool(needle) and needle in haystack
                case "and":
                    return evaluate(node[1]) and evaluate(node[2])
                case "or":
                    return evaluate(node[1]) or evaluate(node[2])
                case "not":
                    return not evaluate(node[1])
            return False

        return evaluate(self._ast)


class SearchIndex:
    """Lazy per-page text index supporting instant repeated searches."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document
        self._pages: dict[int, str] = {}
        self._lock = threading.RLock()
        self._complete = False

    def page_text(self, index: int) -> str:
        with self._lock:
            cached = self._pages.get(index)
        if cached is None:
            cached = self.doc.extract_text(index)
            with self._lock:
                self._pages[index] = cached
        return cached

    def build(self, ctx: JobContext | None = None) -> None:
        """Extract text for every page (background job)."""
        total = self.doc.page_count
        if ctx:
            ctx.set_total(total)
        for index in range(total):
            self.page_text(index)
            if ctx:
                ctx.progress(index + 1, f"Indexing page {index + 1}")
        self._complete = True
        log.debug("Search index complete ({} pages)", total)

    def invalidate(self, page: int | None = None) -> None:
        with self._lock:
            if page is None:
                self._pages.clear()
                self._complete = False
            else:
                self._pages.pop(page, None)

    @property
    def is_complete(self) -> bool:
        return self._complete


class SearchService:
    """Full-text and structured search across one document."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document
        self.index = SearchIndex(document)

    # -- main entry point ---------------------------------------------------- #
    def search(
        self, query: SearchQuery | str, *, ctx: JobContext | None = None
    ) -> list[SearchHit]:
        """Run ``query`` and return hits ordered by page then position."""
        q = SearchQuery(text=query) if isinstance(query, str) else query
        if not q.text.strip():
            return []

        hits: list[SearchHit] = []
        pages = self._page_range(q)
        if ctx:
            ctx.set_total(len(pages))

        for n, index in enumerate(pages, 1):
            if ctx:
                ctx.progress(n, f"Searching page {index + 1}")
            hits.extend(self._search_page(index, q))
            if len(hits) >= q.max_hits:
                hits = hits[: q.max_hits]
                break

        if q.include_annotations:
            hits.extend(self._search_annotations(q))
        if q.include_bookmarks:
            hits.extend(self._search_bookmarks(q))
        if q.include_forms:
            hits.extend(self._search_forms(q))
        if q.include_metadata:
            hits.extend(self._search_metadata(q))

        for i, hit in enumerate(hits):
            hit.index = i
        bus().publish(
            Topic.SEARCH_RESULTS,
            {"document_id": self.doc.id, "query": q.text, "count": len(hits)},
            source="search",
        )
        log.debug("Search {!r} -> {} hit(s)", q.text, len(hits))
        return hits

    def _page_range(self, q: SearchQuery) -> list[int]:
        if q.pages is None:
            return list(range(self.doc.page_count))
        start, end = q.pages
        return list(range(max(0, start), min(self.doc.page_count, end + 1)))

    def _search_page(self, index: int, q: SearchQuery) -> list[SearchHit]:
        # MuPDF's native search is case-insensitive only, so anything needing
        # exact case, regular expressions, word boundaries or boolean logic
        # goes through the (slower but precise) Python matcher.
        if q.regex or q.boolean or q.whole_words or q.case_sensitive:
            return self._search_page_python(index, q)
        with self.doc.locked() as handle:
            try:
                quads = handle[index].search_for(q.text, flags=fitz.TEXT_DEHYPHENATE)
            except Exception:
                quads = []
        page_text = self.index.page_text(index)
        return [
            SearchHit(
                page=index,
                rect=Rect(r.x0, r.y0, r.x1, r.y1),
                text=q.text,
                # occurrence=n so each hit shows its own snippet rather than
                # repeating the context of the first match on the page.
                context=_context(page_text, q.text, q.case_sensitive, occurrence=n),
            )
            for n, r in enumerate(quads)
        ]

    def _search_page_python(self, index: int, q: SearchQuery) -> list[SearchHit]:
        """Regex / boolean / whole-word search mapped back onto word boxes."""
        words = self.doc.extract_words(index)
        text = self.index.page_text(index)
        hits: list[SearchHit] = []

        if q.boolean:
            expression = _BooleanExpression(q.text, case_sensitive=q.case_sensitive)
            if not expression.matches(text):
                return []
            for term in expression.terms:
                sub = SearchQuery(
                    text=term, case_sensitive=q.case_sensitive, whole_words=q.whole_words
                )
                hits.extend(self._search_page(index, sub))
            return hits

        pattern = self._compile(q)
        seen: dict[str, int] = {}
        for rect, word in words:
            if pattern.search(word):
                occurrence = seen.get(word, 0)
                seen[word] = occurrence + 1
                hits.append(
                    SearchHit(
                        page=index,
                        rect=rect,
                        text=word,
                        context=_context(text, word, q.case_sensitive, occurrence=occurrence),
                    )
                )
        if not hits and pattern.search(text):
            # Match spans multiple words: fall back to a line-level box.
            for block in self.doc.extract_blocks(index):
                for line in block.lines:
                    if pattern.search(line.text):
                        hits.append(
                            SearchHit(
                                page=index,
                                rect=line.rect,
                                text=line.text,
                                context=line.text,
                            )
                        )
        return hits

    def _compile(self, q: SearchQuery) -> re.Pattern[str]:
        flags = 0 if q.case_sensitive else re.IGNORECASE
        source = q.text if q.regex else re.escape(q.text)
        if q.whole_words:
            source = rf"\b{source}\b"
        try:
            return re.compile(source, flags)
        except re.error as exc:
            raise ValidationError(f"Invalid regular expression: {exc}") from exc

    # -- structured sources --------------------------------------------------- #
    def _search_annotations(self, q: SearchQuery) -> list[SearchHit]:
        pattern = self._compile(q)
        return [
            SearchHit(
                page=a.page,
                rect=a.rect,
                text=a.contents or a.subject,
                context=f"{a.author}: {a.contents}",
                source="annotation",
            )
            for a in self.doc.all_annotations()
            if pattern.search(a.contents or "") or pattern.search(a.subject or "")
        ]

    def _search_bookmarks(self, q: SearchQuery) -> list[SearchHit]:
        pattern = self._compile(q)
        out: list[SearchHit] = []
        for root in self.doc.bookmarks():
            for bm in root.flatten():
                if pattern.search(bm.title):
                    out.append(
                        SearchHit(
                            page=bm.page,
                            rect=Rect(0, bm.y, 0, bm.y),
                            text=bm.title,
                            context=bm.title,
                            source="bookmark",
                        )
                    )
        return out

    def _search_forms(self, q: SearchQuery) -> list[SearchHit]:
        from pdfstudio.pdfengine.forms import FormService

        pattern = self._compile(q)
        out: list[SearchHit] = []
        for f in FormService(self.doc).fields():
            haystack = f"{f.name} {f.value}"
            if pattern.search(haystack):
                out.append(
                    SearchHit(
                        page=f.page,
                        rect=f.rect,
                        text=str(f.value),
                        context=f"{f.name} = {f.value}",
                        source="form",
                    )
                )
        return out

    def _search_metadata(self, q: SearchQuery) -> list[SearchHit]:
        pattern = self._compile(q)
        meta = self.doc.metadata().as_dict()
        return [
            SearchHit(
                page=0,
                rect=Rect(0, 0, 0, 0),
                text=value,
                context=f"{key}: {value}",
                source="metadata",
            )
            for key, value in meta.items()
            if value and pattern.search(value)
        ]

    # -- convenience ----------------------------------------------------------- #
    def count(self, text: str, **kwargs: Any) -> int:
        return len(self.search(SearchQuery(text=text, **kwargs)))

    def find_next(self, hits: Sequence[SearchHit], current: int) -> SearchHit | None:
        return hits[(current + 1) % len(hits)] if hits else None

    def find_previous(self, hits: Sequence[SearchHit], current: int) -> SearchHit | None:
        return hits[(current - 1) % len(hits)] if hits else None

    def highlight_all(self, hits: Sequence[SearchHit]) -> None:
        """Turn search results into highlight annotations."""
        from pdfstudio.pdfengine.annotations import AnnotationService
        from pdfstudio.pdfengine.types import AnnotationType

        service = AnnotationService(self.doc)
        by_page: dict[int, list[Rect]] = {}
        for hit in hits:
            if hit.source == "text":
                by_page.setdefault(hit.page, []).append(hit.rect)
        with self.doc.undo_stack.macro("Highlight search results"):
            for page, rects in by_page.items():
                service.markup(page, rects, AnnotationType.HIGHLIGHT)


def search_documents(
    documents: Sequence[PdfDocument],
    query: SearchQuery | str,
    *,
    ctx: JobContext | None = None,
) -> dict[str, list[SearchHit]]:
    """Search several open documents at once (the "search all tabs" feature)."""
    results: dict[str, list[SearchHit]] = {}
    if ctx:
        ctx.set_total(len(documents))
    for n, doc in enumerate(documents, 1):
        results[doc.display_name] = SearchService(doc).search(query)
        if ctx:
            ctx.progress(n, f"Searched {doc.display_name}")
    return results


def _context(
    text: str,
    needle: str,
    case_sensitive: bool,
    width: int = 60,
    *,
    occurrence: int = 0,
) -> str:
    """Snippet of ``text`` around the ``occurrence``-th match of ``needle``."""
    haystack = text if case_sensitive else text.lower()
    probe = needle if case_sensitive else needle.lower()
    position = -1
    for _ in range(occurrence + 1):
        position = haystack.find(probe, position + 1)
        if position < 0:
            break
    if position < 0:
        position = haystack.find(probe)
    if position < 0:
        return text[:width].replace("\n", " ").strip()
    start = max(0, position - width // 2)
    end = min(len(text), position + len(needle) + width // 2)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}".replace("\n", " ").strip()
