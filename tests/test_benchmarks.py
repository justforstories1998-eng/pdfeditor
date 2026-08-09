"""Performance benchmarks and stress tests.

Run with ``pytest tests/test_benchmarks.py -m benchmark -s`` to see the timings.
The assertions are deliberately generous — they exist to catch order-of-
magnitude regressions (an accidental O(n²) or a lost cache), not to enforce
absolute speed on unknown hardware.
"""

from __future__ import annotations

import gc
import time
from collections.abc import Callable
from pathlib import Path

import pytest

pytestmark = pytest.mark.benchmark

from pdfstudio.pdfengine.content import TextEditor, TextStyle  # noqa: E402
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions  # noqa: E402
from pdfstudio.pdfengine.pages import PageService  # noqa: E402
from pdfstudio.pdfengine.search import SearchService  # noqa: E402
from pdfstudio.pdfengine.types import Rect  # noqa: E402
from pdfstudio.render.renderer import PageRenderer, RenderRequest  # noqa: E402


def timed(label: str, fn: Callable[[], object], *, repeat: int = 1) -> float:
    """Run ``fn`` and report the best wall-clock time in milliseconds."""
    best = float("inf")
    for _ in range(repeat):
        start = time.perf_counter()
        fn()
        best = min(best, (time.perf_counter() - start) * 1000)
    print(f"\n  {label}: {best:.1f} ms")
    return best


@pytest.fixture(scope="module")
def large_document(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A 500-page document with text on every page."""
    directory = tmp_path_factory.mktemp("bench")
    target = directory / "large.pdf"
    document = PdfDocument.create(pages=500)
    editor = TextEditor(document)
    for index in range(0, 500, 5):  # text on every fifth page keeps setup quick
        editor.add(
            index,
            Rect(60, 60, 520, 260),
            f"Page {index + 1}\nSection {index // 10}\n"
            f"Reference number {index:05d} and the keyword benchmark.",
            TextStyle(size=12),
        )
    document.save_as(target, SaveOptions.fast())
    document.close()
    return target


class TestOpenAndRender:
    def test_open_large_document_is_fast(self, large_document: Path) -> None:
        """Opening must be O(1) — MuPDF should not parse every page."""
        elapsed = timed(
            "open 500 pages",
            lambda: PdfDocument.open(large_document).close(),
            repeat=3,
        )
        assert elapsed < 500

    def test_page_metadata_access(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            elapsed = timed(
                "page_info x100",
                lambda: [document.page_info(i) for i in range(100)],
            )
            assert elapsed < 4000

    def test_render_throughput(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            renderer = PageRenderer(document)
            elapsed = timed(
                "render 20 pages @1x",
                lambda: [
                    renderer.render(RenderRequest(document.id, i, zoom=1.0)) for i in range(20)
                ],
            )
            per_page = elapsed / 20
            print(f"    ({per_page:.1f} ms per page)")
            assert per_page < 250

    def test_cache_speedup(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            renderer = PageRenderer(document)
            request = RenderRequest(document.id, 0, zoom=2.0)
            cold = timed("cold render", lambda: renderer.render(request))
            warm = timed("cached render", lambda: renderer.render(request), repeat=5)
            assert warm < max(1.0, cold / 5)

    def test_thumbnail_batch(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            renderer = PageRenderer(document)
            elapsed = timed(
                "50 thumbnails",
                lambda: [renderer.thumbnail(i, size=140) for i in range(50)],
            )
            assert elapsed / 50 < 120


class TestTextAndSearch:
    def test_text_extraction(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            elapsed = timed(
                "extract text from 100 pages",
                lambda: [document.extract_text(i) for i in range(100)],
            )
            assert elapsed < 3000

    def test_search_whole_document(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            service = SearchService(document)
            elapsed = timed("search 500 pages", lambda: service.search("benchmark"))
            assert elapsed < 12000
            assert len(service.search("benchmark")) == 100

    def test_repeat_search_uses_the_index(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            service = SearchService(document)
            service.search("benchmark")
            elapsed = timed(
                "repeat search (indexed)",
                lambda: service.search("Section"),
            )
            assert elapsed < 12000


class TestPageOperations:
    def test_bulk_rotation(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            service = PageService(document)
            elapsed = timed("rotate 500 pages", lambda: service.rotate(list(range(500)), 90))
            assert elapsed < 5000

    def test_delete_and_undo(self, large_document: Path) -> None:
        with PdfDocument.open(large_document) as document:
            service = PageService(document)
            pages = list(range(100, 200))
            elapsed = timed("delete 100 pages", lambda: service.delete(pages))
            assert elapsed < 8000
            undo = timed("undo the deletion", document.undo_stack.undo)
            assert undo < 8000
            assert document.page_count == 500

    def test_save_throughput(self, large_document: Path, tmp_path: Path) -> None:
        with PdfDocument.open(large_document) as document:
            elapsed = timed(
                "save 500 pages",
                lambda: document.save_as(tmp_path / "out.pdf", SaveOptions.fast()),
            )
            assert elapsed < 6000


class TestMemory:
    def test_render_cache_respects_its_budget(self, large_document: Path) -> None:
        from pdfstudio.render.cache import MemoryCache

        cache: MemoryCache[str, object] = MemoryCache(16 * 1024 * 1024)
        with PdfDocument.open(large_document) as document:
            renderer = PageRenderer(document, memory_cache=cache)
            for index in range(40):
                renderer.render(RenderRequest(document.id, index, zoom=1.5))
            print(f"\n  cache: {cache.bytes_used / 1024 / 1024:.1f} MB, {len(cache)} entries")
            assert cache.bytes_used <= 16 * 1024 * 1024

    def test_documents_are_released(self, large_document: Path) -> None:
        """Opening and closing many documents must not leak memory."""
        import tracemalloc

        gc.collect()
        tracemalloc.start()
        baseline = tracemalloc.take_snapshot()
        for _ in range(12):
            document = PdfDocument.open(large_document)
            document.extract_text(0)
            document.close()
            del document
        gc.collect()
        current = tracemalloc.take_snapshot()
        growth = sum(stat.size_diff for stat in current.compare_to(baseline, "filename"))
        tracemalloc.stop()
        print(f"\n  growth after 12 open/close cycles: {growth / 1024:.0f} KB")
        assert growth < 40 * 1024 * 1024

    def test_undo_stack_memory_bound(self) -> None:
        from pdfstudio.core.undo import FunctionCommand, UndoStack

        stack = UndoStack(memory_limit_mb=1)
        for i in range(400):
            command = FunctionCommand(f"c{i}", lambda: None, lambda: None, cost=10_000)
            stack.push(command)
        print(f"\n  undo entries retained: {len(stack)}")
        assert stack.memory_usage() <= 1024 * 1024 * 1.2


class TestStress:
    @pytest.mark.slow
    def test_very_large_page_count(self, tmp_path: Path) -> None:
        """A 5 000-page document must stay responsive."""
        document = PdfDocument.create(pages=5000)
        target = tmp_path / "huge.pdf"
        elapsed = timed(
            "save 5 000 pages", lambda: document.save_as(target, SaveOptions.fast())
        )
        document.close()
        assert elapsed < 30000

        with PdfDocument.open(target) as reopened:
            assert reopened.page_count == 5000
            access = timed(
                "random page access x200",
                lambda: [reopened.page_size(i * 25) for i in range(200)],
            )
            assert access < 4000

    @pytest.mark.slow
    def test_many_annotations(self, tmp_path: Path) -> None:
        from pdfstudio.pdfengine.annotations import AnnotationService
        from pdfstudio.pdfengine.types import Point

        document = PdfDocument.create(pages=10)
        service = AnnotationService(document)
        elapsed = timed(
            "500 annotations",
            lambda: [
                service.sticky_note(
                    i % 10, Point(50 + (i % 10) * 40, 60 + (i // 10) * 12), f"n{i}"
                )
                for i in range(500)
            ],
        )
        assert elapsed < 30000
        assert len(document.all_annotations()) == 500
        read = timed("read all annotations", document.all_annotations)
        assert read < 5000
        document.close()

    def test_concurrent_rendering(self, large_document: Path) -> None:
        """Many parallel render requests must not corrupt the document."""
        from concurrent.futures import ThreadPoolExecutor

        with PdfDocument.open(large_document) as document:
            renderer = PageRenderer(document)

            def render(index: int) -> int:
                page = renderer.render(RenderRequest(document.id, index % 50, zoom=1.0))
                return page.width

            start = time.perf_counter()
            with ThreadPoolExecutor(max_workers=8) as pool:
                widths = list(pool.map(render, range(60)))
            print(f"\n  60 concurrent renders: {(time.perf_counter() - start) * 1000:.0f} ms")
            assert all(w > 0 for w in widths)
            assert len(set(widths)) == 1  # identical zoom → identical width
