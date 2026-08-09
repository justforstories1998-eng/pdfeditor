"""Tests for rendering, conversion, optimisation, OCR, AI, plugins and services."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from pdfstudio.ai.assistant import AIAssistant, _extractive_summary
from pdfstudio.core.exceptions import PluginError
from pdfstudio.pdfengine.content import TextEditor, TextStyle
from pdfstudio.pdfengine.convert import Exporter, ExportOptions, Importer
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.optimize import (
    AccessibilityChecker,
    DocumentComparer,
    OptimizeProfile,
    Optimizer,
)
from pdfstudio.pdfengine.security import (
    PermissionSet,
    SecurityService,
    generate_password,
    password_strength,
)
from pdfstudio.pdfengine.types import Rect
from pdfstudio.render.cache import DiskCache, MemoryCache
from pdfstudio.render.renderer import PageRenderer, RenderRequest
from pdfstudio.services.autosave import AutosaveService, RecoveryManager
from pdfstudio.services.batch import (
    BatchProcessor,
    CompressOperation,
    RenameRule,
    WatermarkOperation,
    merge_files,
    split_file,
)
from pdfstudio.services.history import HistoryStore


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
class TestRenderer:
    def test_render_page(self, document: PdfDocument) -> None:
        renderer = PageRenderer(document)
        result = renderer.render(RenderRequest(document.id, 0, zoom=1.0))
        assert result.width > 0 and result.height > 0
        assert result.channels in (1, 3, 4)
        assert len(result.samples) == result.stride * result.height

    def test_cache_hit(self, document: PdfDocument) -> None:
        renderer = PageRenderer(document)
        request = RenderRequest(document.id, 0, zoom=1.5)
        renderer.render(request)
        cached = renderer.render(request)
        assert cached.from_cache

    def test_zoom_changes_size(self, document: PdfDocument) -> None:
        renderer = PageRenderer(document)
        small = renderer.render(RenderRequest(document.id, 0, zoom=0.5))
        large = renderer.render(RenderRequest(document.id, 0, zoom=2.0))
        assert large.width > small.width * 3

    def test_invalidation(self, document: PdfDocument) -> None:
        renderer = PageRenderer(document)
        renderer.render(RenderRequest(document.id, 0, zoom=1.0))
        renderer.invalidate()
        assert renderer.statistics()["entries"] == 0

    def test_async_request(self, document: PdfDocument) -> None:
        renderer = PageRenderer(document)
        received: list[object] = []
        outcome = renderer.request(
            RenderRequest(document.id, 1, zoom=1.0, generation=99),
            lambda page: received.append(page),
        )
        if hasattr(outcome, "result"):
            outcome.result(timeout=20)
        deadline = time.time() + 10
        while not received and time.time() < deadline:
            time.sleep(0.02)
        assert received

    def test_tiling(self, document: PdfDocument) -> None:
        renderer = PageRenderer(document)
        tiles = renderer.tiles_for(0, 4.0, Rect(0, 0, 600, 800))
        assert len(tiles) > 1
        assert all(t.clip is not None for t in tiles)
        assert renderer.should_tile(0, 10.0)

    def test_thumbnail(self, document: PdfDocument) -> None:
        thumbnail = PageRenderer(document).thumbnail(0, size=120)
        assert max(thumbnail.width, thumbnail.height) == pytest.approx(120, abs=2)


class TestCaches:
    def test_memory_cache_evicts_by_bytes(self) -> None:
        cache: MemoryCache[str, bytes] = MemoryCache(1000)
        for i in range(5):
            cache.put(f"k{i}", b"x" * 300, 300)
        assert cache.bytes_used <= 1000
        assert len(cache) == 3

    def test_memory_cache_lru_order(self) -> None:
        cache: MemoryCache[str, int] = MemoryCache(300)
        cache.put("a", 1, 100)
        cache.put("b", 2, 100)
        cache.get("a")  # refresh a
        cache.put("c", 3, 100)
        cache.put("d", 4, 100)
        assert cache.get("a") is not None
        assert cache.get("b") is None

    def test_invalidate_predicate(self) -> None:
        cache: MemoryCache[str, int] = MemoryCache(10_000)
        for i in range(5):
            cache.put(f"doc1|{i}", i, 10)
        cache.put("doc2|0", 9, 10)
        assert cache.invalidate(lambda k: k.startswith("doc1|")) == 5
        assert len(cache) == 1

    def test_disk_cache(self, tmp_path: Path) -> None:
        cache = DiskCache(tmp_path / "cache", max_mb=1)
        cache.put("key", b"payload")
        assert cache.get("key") == b"payload"
        assert cache.get("missing") is None
        cache.clear()
        assert cache.get("key") is None


# --------------------------------------------------------------------------- #
# Conversion
# --------------------------------------------------------------------------- #
class TestConversion:
    def test_import_markdown(self, tmp_path: Path) -> None:
        source = tmp_path / "doc.md"
        source.write_text("# Title\n\nBody **bold** text.\n\n- one\n- two\n", "utf-8")
        document = Importer().import_file(source)
        text = document.extract_text(0)
        assert "Title" in text and "bold" in text
        document.close()

    def test_import_html(self, tmp_path: Path) -> None:
        source = tmp_path / "doc.html"
        source.write_text("<h1>Heading</h1><p>Paragraph</p>", "utf-8")
        document = Importer().import_file(source)
        assert "Heading" in document.extract_text(0)
        document.close()

    def test_import_images(self, sample_image: Path) -> None:
        document = Importer().import_images([sample_image, sample_image])
        assert document.page_count == 2
        assert document.page_size(0) == (595.0, 842.0)
        document.close()

    def test_import_text(self) -> None:
        document = Importer().import_text("Line one\nLine two\n" * 40)
        assert document.page_count >= 1
        assert "Line one" in document.extract_text(0)
        document.close()

    def test_export_text_and_markdown(self, document: PdfDocument) -> None:
        exporter = Exporter(document)
        assert "Page 1 heading" in exporter.to_text()
        assert "Page 1 heading" in exporter.to_markdown()
        assert "<html" in exporter.to_html()

    def test_export_images(self, document: PdfDocument, tmp_path: Path) -> None:
        written = Exporter(document).to_images(tmp_path / "out", "png", ExportOptions(dpi=72))
        assert len(written) == 3
        assert all(p.exists() and p.stat().st_size > 0 for p in written)

    def test_export_svg(self, document: PdfDocument) -> None:
        svg = Exporter(document).to_svg(0)
        assert svg.startswith("<?xml") or "<svg" in svg

    def test_export_docx(self, document: PdfDocument, tmp_path: Path) -> None:
        pytest.importorskip("docx")
        target = Exporter(document).to_docx(tmp_path / "out.docx")
        assert target.exists() and target.stat().st_size > 1000

    def test_export_pdfa(self, document: PdfDocument, tmp_path: Path) -> None:
        from pdfstudio.pdfengine.types import ConformanceLevel

        target = Exporter(document).to_conformance(
            tmp_path / "a.pdf", ConformanceLevel.PDF_A_2B
        )
        with PdfDocument.open(target) as archived:
            assert "pdfaid" in archived.metadata().xmp


# --------------------------------------------------------------------------- #
# Optimisation and comparison
# --------------------------------------------------------------------------- #
class TestOptimize:
    def test_analyze(self, document: PdfDocument) -> None:
        info = Optimizer(document).analyze()
        assert info["pages"] == 3
        assert info["total_bytes"] > 0
        assert 0 <= info["image_share"] <= 100

    def test_compress_image_heavy_document(
        self, blank_document: PdfDocument, tmp_path: Path
    ) -> None:
        from PIL import Image

        from pdfstudio.pdfengine.content import ImageEditor

        big = tmp_path / "big.png"
        Image.new("RGB", (2400, 1800), (120, 60, 200)).save(big)
        ImageEditor(blank_document).insert(0, Rect(20, 20, 575, 430), str(big))
        blank_document.save_as(tmp_path / "big.pdf")

        report = Optimizer(blank_document).optimize(
            OptimizeProfile.screen(), output=tmp_path / "small.pdf"
        )
        assert report.optimized_bytes < report.original_bytes
        assert report.percent_saved > 10

    def test_compare_identical(self, document: PdfDocument, tmp_pdf: Path) -> None:
        with PdfDocument.open(tmp_pdf) as other:
            report = DocumentComparer(document, other).compare()
            assert report.identical
            assert not report.changed_pages

    def test_compare_detects_changes(self, document: PdfDocument, tmp_pdf: Path) -> None:
        with PdfDocument.open(tmp_pdf) as other:
            TextEditor(other).replace_all("Invoice", "Receipt")
            report = DocumentComparer(document, other).compare()
            assert not report.identical
            assert 0 in report.changed_pages
            changes = report.pages[0].changes
            assert any("Receipt" in c.new_text for c in changes)

    def test_comparison_report_pdf(
        self, document: PdfDocument, tmp_pdf: Path, tmp_path: Path
    ) -> None:
        with PdfDocument.open(tmp_pdf) as other:
            comparer = DocumentComparer(document, other)
            report = comparer.compare(compare_images=False)
            target = comparer.build_report_pdf(report, tmp_path / "report.pdf")
            assert target.exists()

    def test_accessibility_checker(self, document: PdfDocument) -> None:
        checker = AccessibilityChecker(document)
        issues = checker.check()
        rules = {issue.rule for issue in issues}
        assert "Document title" in rules
        applied = checker.auto_fix(title="Fixed")
        assert applied
        assert "Document title" not in {i.rule for i in checker.check()}


# --------------------------------------------------------------------------- #
# Security
# --------------------------------------------------------------------------- #
class TestSecurity:
    def test_watermark(self, document: PdfDocument) -> None:
        SecurityService(document).watermark_text("DRAFT", opacity=0.3)
        assert "DRAFT" in document.extract_text(0)

    def test_bates_numbering(self, document: PdfDocument) -> None:
        stamps = SecurityService(document).bates_numbering(prefix="ACME-", start=1)
        assert stamps[0] == "ACME-000001"
        assert "ACME-000001" in document.extract_text(0)
        assert "ACME-000003" in document.extract_text(2)

    def test_header_footer_tokens(self, document: PdfDocument) -> None:
        SecurityService(document).header_footer(footer_center="Page {page} of {pages}")
        assert "Page 1 of 3" in document.extract_text(0)
        assert "Page 3 of 3" in document.extract_text(2)

    def test_sanitize(self, document: PdfDocument) -> None:
        metadata = document.metadata()
        metadata.author = "Secret author"
        document.set_metadata(metadata)
        document.add_attachment("hidden.txt", b"data")
        removed = SecurityService(document).sanitize()
        assert removed["embedded_files"] == 1
        assert document.metadata().author == ""

    def test_permission_bits(self) -> None:
        assert PermissionSet().to_bits() > PermissionSet.read_only().to_bits()
        assert PermissionSet.none().to_bits() == 0

    def test_password_helpers(self) -> None:
        password = generate_password(24)
        assert len(password) == 24
        score, verdict = password_strength(password)
        # A random 24-character password occasionally omits a symbol, so the
        # contract is "strong", not a fixed score.
        assert score >= 75
        assert verdict in ("Strong", "Very strong")
        assert password_strength("abc")[0] < 40

    def test_password_strength_scale(self) -> None:
        assert password_strength("")[1] == "Very weak"
        assert password_strength("password")[0] < password_strength("P@ssw0rd!x9Q")[0]
        assert password_strength("Aa1!" * 6)[0] >= 75

    def test_secure_delete(self, tmp_path: Path) -> None:
        victim = tmp_path / "secret.txt"
        victim.write_bytes(b"sensitive" * 100)
        SecurityService(PdfDocument.create()).secure_delete_file(victim)
        assert not victim.exists()


# --------------------------------------------------------------------------- #
# OCR
# --------------------------------------------------------------------------- #
class TestOcr:
    def test_engine_discovery(self) -> None:
        from pdfstudio.ocr.engine import available_engines

        assert isinstance(available_engines(), list)

    def test_preprocessing(self, sample_image: Path) -> None:
        from pdfstudio.ocr.engine import PreprocessOptions, preprocess

        data, angle = preprocess(
            sample_image.read_bytes(), PreprocessOptions(deskew=False, binarize=True)
        )
        assert data[:4] == b"\x89PNG"
        assert angle == 0.0

    def test_needs_ocr_detection(self, scanned_pdf: Path, document: PdfDocument) -> None:
        with PdfDocument.open(scanned_pdf) as scan:
            assert scan.needs_ocr()
        assert not document.needs_ocr()

    @pytest.mark.ocr
    def test_ocr_makes_text_searchable(self, scanned_pdf: Path) -> None:
        from pdfstudio.core.settings import OcrSettings
        from pdfstudio.ocr.engine import OcrService, available_engines

        if not available_engines():
            pytest.skip("no OCR engine installed")
        with PdfDocument.open(scanned_pdf) as scan:
            service = OcrService(scan, OcrSettings(dpi=200))
            results = service.run()
            assert results and results[0].word_count > 3
            assert "Lovelace" in scan.extract_text(0)
            report = service.confidence_report(results)
            assert report["mean_confidence"] > 50


# --------------------------------------------------------------------------- #
# AI
# --------------------------------------------------------------------------- #
class TestAI:
    def test_local_summary(self, document: PdfDocument) -> None:
        response = AIAssistant(document).summarize()
        assert response.text
        assert response.provider == "local"
        assert "[Page" not in response.text

    def test_extractive_summary_is_ordered(self) -> None:
        text = (
            "The first sentence introduces the topic clearly. "
            "The second sentence adds detail about revenue and growth. "
            "A third sentence discusses costs and efficiency measures. "
            "The final sentence concludes the analysis of the results."
        )
        summary = _extractive_summary(text, 2)
        assert summary.count(".") >= 1
        assert len(summary) < len(text)

    def test_question_answering_cites_pages(self, document: PdfDocument) -> None:
        response = AIAssistant(document).ask("What is the invoice total?")
        assert response.text
        assert response.citations
        assert all("page" in c for c in response.citations)

    def test_keyword_tagging(self, document: PdfDocument) -> None:
        tags = AIAssistant(document).auto_tag()
        assert tags and all(isinstance(t, str) for t in tags)

    def test_generate_bookmarks(self, blank_document: PdfDocument) -> None:
        editor = TextEditor(blank_document)
        editor.add(0, Rect(50, 40, 545, 90), "Main Title", TextStyle(size=26, bold=True))
        editor.add(0, Rect(50, 110, 545, 150), "Section One", TextStyle(size=17, bold=True))
        editor.add(0, Rect(50, 170, 545, 300), "Body text " * 12, TextStyle(size=11))
        bookmarks = AIAssistant(blank_document).generate_bookmarks()
        titles = [b.title for b in bookmarks]
        assert "Main Title" in titles
        assert any("Section One" in c.title for b in bookmarks for c in b.children)

    def test_metadata_suggestions(self, document: PdfDocument) -> None:
        suggestion = AIAssistant(document).generate_metadata()
        assert suggestion["title"]
        assert suggestion["keywords"]

    def test_grammar_checks(self, blank_document: PdfDocument) -> None:
        findings = AIAssistant(blank_document).check_grammar("This is is wrong .And this too")
        kinds = {f["type"] for f in findings}
        assert "duplicate word" in kinds
        assert "space before punctuation" in kinds

    def test_citations(self, document: PdfDocument) -> None:
        assistant = AIAssistant(document)
        assert "(" in assistant.generate_citation("apa")
        assert assistant.generate_citation("bibtex").startswith("@misc")


# --------------------------------------------------------------------------- #
# Services
# --------------------------------------------------------------------------- #
class TestHistory:
    def test_recent_files(self, tmp_path: Path, tmp_pdf: Path) -> None:
        store = HistoryStore(tmp_path / "h.db")
        store.touch_recent(tmp_pdf, page=2, zoom=1.5, page_count=3)
        recents = store.recent()
        assert recents and recents[0].name == tmp_pdf.name
        assert store.position_of(tmp_pdf) == (2, 1.5)
        store.close()

    def test_sessions(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "h.db")
        store.save_session("last", {"tabs": ["a.pdf"], "active": 0})
        assert store.load_session("last")["tabs"] == ["a.pdf"]
        assert store.load_session("missing") is None
        store.close()

    def test_search_history(self, tmp_path: Path) -> None:
        store = HistoryStore(tmp_path / "h.db")
        store.add_search("invoice", 5)
        store.add_search("total", 2)
        assert "invoice" in store.recent_searches()
        store.close()


class TestAutosave:
    def test_autosave_and_recovery(self, tmp_path: Path, document: PdfDocument) -> None:
        service = AutosaveService(directory=tmp_path / "autosave")
        service.track(document)
        TextEditor(document).add(0, Rect(50, 400, 300, 440), "unsaved edit")
        record = service.save(document)
        assert record is not None and record.autosave_path.exists()

        manager = RecoveryManager(tmp_path / "autosave")
        pending = manager.pending()
        assert len(pending) == 1
        restored = manager.restore(pending[0])
        assert "unsaved edit" in restored.extract_text(0)
        restored.close()
        manager.discard_all()
        assert not manager.pending()

    def test_version_pruning(self, tmp_path: Path, document: PdfDocument) -> None:
        from pdfstudio.core.settings import AutosaveSettings

        service = AutosaveService(
            AutosaveSettings(keep_versions=2), directory=tmp_path / "autosave"
        )
        for i in range(4):
            document.mark_modified(f"edit {i}")
            service.save(document)
            time.sleep(1.05)  # timestamps have one-second resolution
        assert len(service.versions(document.id)) <= 2


class TestBatch:
    def test_pipeline(self, tmp_pdf: Path, tmp_path: Path) -> None:
        processor = (
            BatchProcessor(
                output_dir=tmp_path / "out", rename=RenameRule("{stem}-{counter:03d}")
            )
            .add(WatermarkOperation("BATCH"))
            .add(CompressOperation("screen"))
        )
        result = processor.run([tmp_pdf, tmp_pdf])
        assert len(result.succeeded) == 2
        assert not result.failed
        outputs = list((tmp_path / "out").glob("*.pdf"))
        assert len(outputs) == 2
        with PdfDocument.open(outputs[0]) as done:
            assert "BATCH" in done.extract_text(0)

    def test_error_isolation(self, tmp_pdf: Path, tmp_path: Path) -> None:
        broken = tmp_path / "broken.pdf"
        broken.write_bytes(b"not a pdf at all")
        processor = BatchProcessor(output_dir=tmp_path / "out").add(WatermarkOperation("X"))
        result = processor.run([tmp_pdf, broken])
        assert len(result.succeeded) == 1
        assert len(result.failed) == 1

    def test_merge_and_split_helpers(self, tmp_pdf: Path, tmp_path: Path) -> None:
        merged = merge_files([tmp_pdf, tmp_pdf], tmp_path / "merged.pdf")
        with PdfDocument.open(merged) as document:
            assert document.page_count == 6
        parts = split_file(merged, tmp_path / "parts", pages_per_file=2)
        assert len(parts) == 3

    def test_rename_tokens(self, tmp_pdf: Path, tmp_path: Path) -> None:
        processor = BatchProcessor(
            output_dir=tmp_path / "out", rename=RenameRule("{stem}-{pages}p")
        )
        result = processor.run([tmp_pdf])
        assert result.succeeded[0].output.name == "sample-3p.pdf"


# --------------------------------------------------------------------------- #
# Plugins
# --------------------------------------------------------------------------- #
class TestPlugins:
    def test_builtin_plugins_load(self) -> None:
        from pdfstudio.plugins.manager import PluginManager

        manager = PluginManager()
        loaded = manager.load_all()
        identifiers = {p.identifier for p in loaded}
        assert "org.pdfstudio.page-numbers" in identifiers
        assert "org.pdfstudio.quick-redact" in identifiers
        assert manager.commands()
        manager.shutdown()

    def test_command_execution(self, document: PdfDocument) -> None:
        from pdfstudio.plugins.manager import PluginManager

        class Host:
            def active_document(self) -> PdfDocument:
                return document

            def open_document(self, path: str) -> PdfDocument:
                return PdfDocument.open(path)

            def notify(self, message: str, *, level: str = "info") -> None:
                pass

            def ask(self, question: str, options: list[str]) -> str:
                return options[0]

        manager = PluginManager(host=Host())
        manager.load_all()
        manager.execute("org.pdfstudio.page-numbers.insert")
        assert "Page 1 of 3" in document.extract_text(0)
        manager.shutdown()

    def test_quick_redact_finds_personal_data(self, blank_document: PdfDocument) -> None:
        from pdfstudio.plugins.api import PluginContext
        from pdfstudio.plugins.builtin.quick_redact import QuickRedactPlugin

        TextEditor(blank_document).add(
            0,
            Rect(50, 50, 545, 200),
            "Contact ada@example.com or 4111 1111 1111 1111 for details.",
            TextStyle(size=12),
        )

        class Host:
            def active_document(self) -> PdfDocument:
                return blank_document

            def open_document(self, path: str) -> PdfDocument:  # pragma: no cover
                raise NotImplementedError

            def notify(self, message: str, *, level: str = "info") -> None:
                pass

            def ask(self, question: str, options: list[str]) -> str:  # pragma: no cover
                return options[0]

        plugin = QuickRedactPlugin()
        context = PluginContext(plugin.metadata, host=Host())
        plugin.activate(context)
        findings = plugin.find(context)
        kinds = {f["kind"] for f in findings}
        assert "e-mail address" in kinds
        assert "credit card" in kinds

    def test_reload_and_disable(self) -> None:
        from pdfstudio.plugins.manager import PluginManager

        manager = PluginManager()
        manager.load_all()
        identifier = "org.pdfstudio.page-numbers"
        assert manager.disable(identifier)
        assert not manager.get(identifier).enabled
        assert manager.enable(identifier)
        reloaded = manager.reload(identifier)
        assert reloaded.enabled
        manager.shutdown()

    def test_unknown_command_raises(self) -> None:
        from pdfstudio.plugins.manager import PluginManager

        manager = PluginManager()
        with pytest.raises(PluginError):
            manager.execute("does.not.exist")
        manager.shutdown()
