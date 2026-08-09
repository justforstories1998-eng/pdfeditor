"""Tests for the PDF engine: documents, pages, content, annotations, forms."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdfstudio.core.exceptions import (
    PasswordRequiredError,
    PdfStudioError,
    ValidationError,
)
from pdfstudio.pdfengine.annotations import AnnotationService
from pdfstudio.pdfengine.content import (
    ImageAdjustments,
    ImageEditor,
    ShapeStyle,
    TextEditor,
    TextStyle,
    VectorEditor,
)
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.forms import FieldSpec, FormService
from pdfstudio.pdfengine.pages import PageService, make_booklet, merge_documents, n_up
from pdfstudio.pdfengine.search import SearchService
from pdfstudio.pdfengine.types import (
    AnnotationType,
    Bookmark,
    Color,
    EncryptionMethod,
    FieldType,
    Point,
    Rect,
    SearchQuery,
    format_page_ranges,
    parse_page_ranges,
)


# --------------------------------------------------------------------------- #
# Types
# --------------------------------------------------------------------------- #
class TestTypes:
    def test_rect_geometry(self) -> None:
        rect = Rect(10, 20, 110, 220)
        assert rect.width == 100 and rect.height == 200
        assert rect.area == 20000
        assert rect.center == Point(60, 120)
        assert rect.contains(Point(50, 50))
        assert rect.intersects(Rect(100, 200, 200, 300))
        assert not rect.intersects(Rect(500, 500, 600, 600))
        assert rect.translated(5, 5) == Rect(15, 25, 115, 225)
        assert Rect(0, 0, 10, 10).united(Rect(5, 5, 20, 20)) == Rect(0, 0, 20, 20)

    def test_colour_conversions(self) -> None:
        assert Color.from_hex("#ff0000").to_rgb_tuple() == (1.0, 0.0, 0.0)
        assert Color.from_hex("#f00").to_hex() == "#ff0000"
        assert Color.from_hex("#00ff0080").a == pytest.approx(0.502, abs=0.01)
        assert Color.from_bytes(255, 128, 0).to_hex() == "#ff8000"
        with pytest.raises(ValueError):
            Color.from_hex("nonsense")

    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("all", [0, 1, 2, 3, 4]),
            ("1-3", [0, 1, 2]),
            ("2,4", [1, 3]),
            ("3-", [2, 3, 4]),
            ("-2", [0, 1]),
            ("even", [1, 3]),
            ("odd", [0, 2, 4]),
            ("5-2", [1, 2, 3, 4]),
            ("1,1,2", [0, 1]),
        ],
    )
    def test_parse_page_ranges(self, spec: str, expected: list[int]) -> None:
        assert parse_page_ranges(spec, 5) == expected

    def test_format_page_ranges(self) -> None:
        assert format_page_ranges([0, 1, 2, 5, 7, 8]) == "1-3,6,8-9"
        assert format_page_ranges([]) == ""


# --------------------------------------------------------------------------- #
# Document
# --------------------------------------------------------------------------- #
class TestDocument:
    def test_create_and_properties(self) -> None:
        with PdfDocument.create(pages=3) as document:
            assert document.page_count == 3
            assert document.is_pdf
            assert not document.is_encrypted
            assert document.page_size(0) == (595.0, 842.0)

    def test_open_missing_file(self) -> None:
        with pytest.raises(PdfStudioError):
            PdfDocument.open("/definitely/not/here.pdf")

    def test_round_trip_bytes(self, document: PdfDocument) -> None:
        data = document.to_bytes()
        clone = PdfDocument.from_bytes(data)
        assert clone.page_count == document.page_count
        clone.close()

    def test_text_extraction(self, document: PdfDocument) -> None:
        assert "Page 1 heading" in document.extract_text(0)
        assert "Invoice" in document.extract_all_text()
        blocks = document.extract_blocks(0)
        assert blocks and blocks[0].lines[0].spans[0].size > 0
        words = document.extract_words(0)
        assert any(word == "Invoice" for _, word in words)

    def test_metadata_round_trip(self, blank_document: PdfDocument) -> None:
        metadata = blank_document.metadata()
        metadata.title = "Test title"
        metadata.author = "Ada"
        blank_document.set_metadata(metadata)
        assert blank_document.metadata().title == "Test title"
        blank_document.clear_metadata()
        assert blank_document.metadata().title == ""

    def test_bookmarks_round_trip(self, blank_document: PdfDocument) -> None:
        blank_document.raw.new_page()
        blank_document.set_bookmarks(
            [Bookmark("Chapter 1", 0, children=[Bookmark("Section", 1, level=2)])]
        )
        roots = blank_document.bookmarks()
        assert roots[0].title == "Chapter 1"
        assert roots[0].children[0].title == "Section"

    def test_attachments(self, blank_document: PdfDocument) -> None:
        blank_document.add_attachment("data.csv", b"a,b\n1,2\n", description="test")
        names = [a.name for a in blank_document.attachments()]
        assert "data.csv" in names
        assert blank_document.extract_attachment("data.csv") == b"a,b\n1,2\n"
        blank_document.delete_attachment("data.csv")
        assert not blank_document.attachments()

    def test_encryption_round_trip(self, document: PdfDocument, tmp_path: Path) -> None:
        options = SaveOptions(
            encryption=EncryptionMethod.AES_256, user_password="secret", owner_password="owner"
        )
        target = tmp_path / "encrypted.pdf"
        document.save_as(target, options)
        with pytest.raises(PasswordRequiredError):
            PdfDocument.open(target)
        with PdfDocument.open(target, "secret") as opened:
            assert opened.is_encrypted
            assert "Page 1 heading" in opened.extract_text(0)

    def test_statistics(self, document: PdfDocument) -> None:
        stats = document.statistics()
        assert stats["pages"] == 3
        assert stats["encrypted"] is False
        assert "permissions" in stats

    def test_modified_flag(self, blank_document: PdfDocument) -> None:
        blank_document.mark_saved()
        assert not blank_document.is_modified
        TextEditor(blank_document).add(0, Rect(10, 10, 200, 40), "hi")
        assert blank_document.is_modified


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
class TestPages:
    def test_delete_and_undo(self, document: PdfDocument) -> None:
        service = PageService(document)
        original = document.extract_text(1)
        service.delete([1])
        assert document.page_count == 2
        document.undo_stack.undo()
        assert document.page_count == 3
        assert document.extract_text(1) == original

    def test_cannot_delete_every_page(self, blank_document: PdfDocument) -> None:
        with pytest.raises(ValidationError):
            PageService(blank_document).delete([0])

    def test_insert_blank(self, document: PdfDocument) -> None:
        PageService(document).insert_blank(1, count=2, size="A5")
        assert document.page_count == 5
        assert document.page_size(1) == (420.0, 595.0)
        document.undo_stack.undo()
        assert document.page_count == 3

    def test_move_and_undo(self, document: PdfDocument) -> None:
        first = document.extract_text(0)
        PageService(document).move([0], 3)
        assert document.extract_text(2) == first
        document.undo_stack.undo()
        assert document.extract_text(0) == first

    def test_rotate(self, document: PdfDocument) -> None:
        PageService(document).rotate([0], 90)
        assert document.page_rotation(0) == 90
        document.undo_stack.undo()
        assert document.page_rotation(0) == 0

    def test_duplicate(self, document: PdfDocument) -> None:
        PageService(document).duplicate([0])
        assert document.page_count == 4
        assert document.extract_text(0) == document.extract_text(1)

    def test_crop(self, document: PdfDocument) -> None:
        PageService(document).crop([0], Rect(50, 50, 400, 700))
        width, height = document.page_size(0)
        assert width == pytest.approx(350) and height == pytest.approx(650)
        document.undo_stack.undo()
        assert document.page_size(0) == (595.0, 842.0)

    def test_resize(self, document: PdfDocument) -> None:
        PageService(document).resize([0], "A5")
        assert document.page_size(0) == (420.0, 595.0)
        document.undo_stack.undo()
        assert document.page_size(0) == (595.0, 842.0)

    def test_extract_and_split(self, document: PdfDocument) -> None:
        service = PageService(document)
        extracted = service.extract([0, 2])
        assert extracted.page_count == 2
        extracted.close()
        parts = service.split_by_count(2)
        assert [p.page_count for p in parts] == [2, 1]
        for part in parts:
            part.close()

    def test_merge(self, tmp_pdf: Path) -> None:
        merged = merge_documents([tmp_pdf, tmp_pdf])
        assert merged.page_count == 6
        assert len(merged.bookmarks()) == 2
        merged.close()

    def test_n_up_and_booklet(self, document: PdfDocument) -> None:
        result = n_up(document, 2, 2)
        assert result.page_count == 1
        result.close()
        booklet = make_booklet(document)
        assert booklet.page_count == 2
        booklet.close()


# --------------------------------------------------------------------------- #
# Content editing
# --------------------------------------------------------------------------- #
class TestContent:
    def test_add_text(self, blank_document: PdfDocument) -> None:
        TextEditor(blank_document).add(
            0, Rect(50, 50, 400, 120), "Hello world", TextStyle(size=16)
        )
        assert "Hello world" in blank_document.extract_text(0)
        blank_document.undo_stack.undo()
        assert "Hello world" not in blank_document.extract_text(0)

    def test_replace_all(self, document: PdfDocument) -> None:
        count = TextEditor(document).replace_all("Invoice", "Receipt")
        assert count == 3
        assert "Receipt" in document.extract_text(0)
        assert "Invoice" not in document.extract_text(0)

    def test_regex_replace(self, document: PdfDocument) -> None:
        changed = TextEditor(document).replace_all(r"\d{3,}", "XXX", regex=True)
        assert changed >= 1
        assert "1000" not in document.extract_text(0)

    def test_rich_text(self, blank_document: PdfDocument) -> None:
        TextEditor(blank_document).add_rich_text(
            0, Rect(50, 50, 400, 200), "<b>Bold</b> and <i>italic</i>"
        )
        text = blank_document.extract_text(0)
        assert "Bold" in text and "italic" in text

    def test_font_listing(self, document: PdfDocument) -> None:
        fonts = TextEditor(document).fonts()
        assert fonts and "name" in fonts[0]

    def test_insert_and_extract_image(
        self, blank_document: PdfDocument, sample_image: Path
    ) -> None:
        editor = ImageEditor(blank_document)
        editor.insert(0, Rect(50, 50, 350, 275), str(sample_image))
        images = editor.list(0)
        assert len(images) == 1 and images[0].width == 640
        extracted = editor.extract_all(0)
        assert extracted and len(extracted[0][1]) > 100

    def test_image_adjustments(self, sample_image: Path) -> None:
        data = sample_image.read_bytes()
        adjusted = ImageAdjustments(brightness=1.5, grayscale=True).apply(data)
        assert adjusted[:4] == b"\x89PNG"
        assert adjusted != data

    def test_image_compression(self, blank_document: PdfDocument, sample_image: Path) -> None:
        editor = ImageEditor(blank_document)
        editor.insert(0, Rect(0, 0, 595, 450), str(sample_image))
        xref = editor.list(0)[0].xref
        saved = editor.resample(0, xref, max_pixels=10000)
        assert saved > 0

    def test_vector_drawing(self, blank_document: PdfDocument) -> None:
        editor = VectorEditor(blank_document)
        editor.draw(
            0,
            "rect",
            [Point(50, 50), Point(200, 150)],
            ShapeStyle(fill=Color(1, 0, 0)),
        )
        assert len(editor.paths(0)) >= 1
        blank_document.undo_stack.undo()
        assert len(editor.paths(0)) == 0

    def test_gradient(self, blank_document: PdfDocument) -> None:
        VectorEditor(blank_document).draw_gradient(
            0, Rect(50, 50, 300, 200), Color(1, 0, 0), Color(0, 0, 1), steps=16
        )
        assert len(VectorEditor(blank_document).paths(0)) >= 10


# --------------------------------------------------------------------------- #
# Annotations
# --------------------------------------------------------------------------- #
class TestAnnotations:
    def test_highlight_text(self, document: PdfDocument) -> None:
        service = AnnotationService(document, author="Tester")
        assert service.highlight_text(0, "Invoice") == 1
        annotations = document.page_annotations(0)
        assert annotations[0].type is AnnotationType.HIGHLIGHT
        assert annotations[0].author == "Tester"

    def test_sticky_note_and_undo(self, blank_document: PdfDocument) -> None:
        service = AnnotationService(blank_document)
        service.sticky_note(0, Point(100, 100), "A note")
        assert len(blank_document.page_annotations(0)) == 1
        blank_document.undo_stack.undo()
        assert len(blank_document.page_annotations(0)) == 0

    def test_ink_and_shapes(self, blank_document: PdfDocument) -> None:
        service = AnnotationService(blank_document)
        service.ink(0, [[Point(10, 10), Point(50, 50), Point(90, 20)]])
        service.shape(0, Rect(100, 100, 200, 200), AnnotationType.CIRCLE)
        service.polygon(0, [Point(10, 300), Point(80, 320), Point(50, 380)])
        types = {a.type for a in blank_document.page_annotations(0)}
        assert AnnotationType.INK in types
        assert AnnotationType.CIRCLE in types

    def test_measurements(self, blank_document: PdfDocument) -> None:
        service = AnnotationService(blank_document)
        distance = service.measure_distance(0, Point(0, 0), Point(100, 0))
        assert distance == pytest.approx(100)
        area = service.measure_area(
            0, [Point(0, 0), Point(100, 0), Point(100, 50), Point(0, 50)]
        )
        assert area == pytest.approx(5000)

    def test_redaction_removes_text(self, document: PdfDocument) -> None:
        service = AnnotationService(document)
        assert service.redact_text("Invoice") >= 1
        service.apply_redactions()
        assert "Invoice" not in document.extract_text(0)

    def test_json_round_trip(self, document: PdfDocument) -> None:
        service = AnnotationService(document, author="Ada")
        service.sticky_note(0, Point(50, 50), "Check this")
        service.highlight_text(0, "Invoice")
        payload = service.export_json()

        target = PdfDocument.create(pages=3)
        imported = AnnotationService(target).import_json(payload)
        assert imported == 2
        assert len(target.all_annotations()) == 2
        target.close()

    def test_xfdf_round_trip(self, document: PdfDocument) -> None:
        service = AnnotationService(document, author="Ada")
        service.sticky_note(0, Point(50, 50), "Note text")
        xfdf = service.export_xfdf()
        assert "<xfdf" in xfdf

        target = PdfDocument.create(pages=3)
        assert AnnotationService(target).import_xfdf(xfdf) == 1
        target.close()

    def test_replies_and_states(self, blank_document: PdfDocument) -> None:
        service = AnnotationService(blank_document, author="Ada")
        service.sticky_note(0, Point(50, 50), "Parent")
        xref = blank_document.page_annotations(0)[0].extra["xref"]
        service.reply(0, xref, "Child reply")
        assert len(blank_document.page_annotations(0)) == 2
        service.resolve(0, xref)

    def test_flatten(self, blank_document: PdfDocument) -> None:
        service = AnnotationService(blank_document)
        service.shape(0, Rect(50, 50, 200, 150), AnnotationType.SQUARE)
        service.flatten()
        assert len(blank_document.page_annotations(0)) == 0

    def test_summary(self, document: PdfDocument) -> None:
        service = AnnotationService(document, author="Ada")
        service.highlight_text(0, "Invoice")
        summary = service.summary()
        assert summary["total"] == 1
        assert summary["authors"] == ["Ada"]


# --------------------------------------------------------------------------- #
# Forms
# --------------------------------------------------------------------------- #
class TestForms:
    def test_create_and_read_fields(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create_text("name", 0, Rect(100, 100, 300, 125), value="Ada")
        service.create_checkbox("agree", 0, Rect(100, 150, 118, 168), checked=True)
        service.create_dropdown("country", 0, Rect(100, 200, 300, 225), ["UK", "US"])
        fields = {f.name: f for f in service.fields()}
        assert fields["name"].value == "Ada"
        assert fields["name"].type is FieldType.TEXT
        assert fields["country"].options == ["UK", "US"]

    def test_fill_and_reset(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create_text("name", 0, Rect(100, 100, 300, 125))
        service.create_text("email", 0, Rect(100, 150, 300, 175))
        assert service.fill({"name": "Grace", "email": "g@example.com"}) == 2
        assert service.values()["name"] == "Grace"
        service.reset()
        assert service.values()["name"] == ""

    def test_unknown_field_is_rejected_in_strict_mode(
        self, blank_document: PdfDocument
    ) -> None:
        service = FormService(blank_document)
        service.create_text("name", 0, Rect(10, 10, 100, 30))
        with pytest.raises(ValidationError):
            service.fill({"nope": "x"}, strict=True)

    def test_validation(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create(
            FieldSpec(
                name="required_field",
                type=FieldType.TEXT,
                page=0,
                rect=Rect(100, 100, 300, 125),
                required=True,
            )
        )
        problems = service.validate()
        assert problems and problems[0][0] == "required_field"

    def test_calculation(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create_text("price", 0, Rect(100, 100, 200, 125), value="10")
        service.create_text("qty", 0, Rect(100, 150, 200, 175), value="4")
        service.create_text("total", 0, Rect(100, 200, 200, 225))
        assert service.calculate({"total": "price * qty"})["total"] == 40
        assert service.values()["total"] == "40"

    def test_unsafe_formula_is_rejected(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create_text("total", 0, Rect(10, 10, 100, 30))
        with pytest.raises(ValidationError):
            service.calculate({"total": "__import__('os').system('ls')"})

    def test_data_interchange(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create_text("name", 0, Rect(100, 100, 300, 125), value="Ada")
        for exporter, importer in (
            (service.export_json, service.import_json),
            (service.export_fdf, service.import_fdf),
            (service.export_xfdf, service.import_xfdf),
        ):
            payload = exporter()
            target = PdfDocument.create()
            other = FormService(target)
            other.create_text("name", 0, Rect(100, 100, 300, 125))
            assert importer.__self__ is service  # type: ignore[attr-defined]
            assert getattr(other, importer.__name__)(payload) == 1
            assert other.values()["name"] == "Ada"
            target.close()

    def test_flatten(self, blank_document: PdfDocument) -> None:
        service = FormService(blank_document)
        service.create_text("name", 0, Rect(100, 100, 300, 125), value="Ada")
        service.flatten()
        assert service.fields() == []


# --------------------------------------------------------------------------- #
# Search
# --------------------------------------------------------------------------- #
class TestSearch:
    def test_plain_search(self, document: PdfDocument) -> None:
        hits = SearchService(document).search("Invoice")
        assert len(hits) == 3
        assert hits[0].page == 0
        assert hits[0].context

    def test_case_sensitivity(self, document: PdfDocument) -> None:
        service = SearchService(document)
        assert len(service.search(SearchQuery("invoice"))) == 3
        assert len(service.search(SearchQuery("invoice", case_sensitive=True))) == 0
        assert len(service.search(SearchQuery("Invoice", case_sensitive=True))) == 3

    def test_regex(self, document: PdfDocument) -> None:
        hits = SearchService(document).search(SearchQuery(r"\d+\.00", regex=True))
        assert len(hits) >= 3

    def test_invalid_regex(self, document: PdfDocument) -> None:
        with pytest.raises(ValidationError):
            SearchService(document).search(SearchQuery("(unclosed", regex=True))

    def test_boolean(self, document: PdfDocument) -> None:
        service = SearchService(document)
        assert service.search(SearchQuery("Invoice AND heading", boolean=True))
        assert not service.search(SearchQuery("Invoice AND zzz", boolean=True))
        assert service.search(SearchQuery("zzz OR Invoice", boolean=True))

    def test_whole_words(self, document: PdfDocument) -> None:
        service = SearchService(document)
        assert service.search(SearchQuery("Invoice", whole_words=True))
        assert not service.search(SearchQuery("nvoic", whole_words=True))

    def test_page_range_filter(self, document: PdfDocument) -> None:
        hits = SearchService(document).search(SearchQuery("Invoice", pages=(0, 0)))
        assert len(hits) == 1

    def test_annotation_and_bookmark_search(self, document: PdfDocument) -> None:
        AnnotationService(document).sticky_note(0, Point(10, 10), "special marker")
        document.set_bookmarks([Bookmark("Special chapter", 0)])
        service = SearchService(document)
        annotations = [
            h
            for h in service.search(SearchQuery("special", include_annotations=True))
            if h.source == "annotation"
        ]
        bookmarks = [
            h
            for h in service.search(SearchQuery("special", include_bookmarks=True))
            if h.source == "bookmark"
        ]
        assert annotations and bookmarks


# --------------------------------------------------------------------------- #
# Text wrapping and line-level editing
# --------------------------------------------------------------------------- #
class TestTextWrapping:
    """Regression tests for text that used to run off the page."""

    def test_wrap_respects_width(self) -> None:
        import pymupdf as fitz

        from pdfstudio.pdfengine.content import wrap_text

        style = TextStyle(size=11)
        text = (
            "Renewable generation rose 18 percent across the portfolio during "
            "2026 and the board approved a further two billion pound programme."
        )
        lines = wrap_text(text, 300, style)
        font = fitz.Font("helv")
        assert len(lines) > 1
        assert all(font.text_length(line, 11) <= 300 for line in lines)

    def test_hard_newlines_are_kept(self) -> None:
        from pdfstudio.pdfengine.content import wrap_text

        assert wrap_text("one\ntwo\n\nthree", 400, TextStyle(size=11)) == [
            "one",
            "two",
            "",
            "three",
        ]

    def test_unbreakable_word_is_split(self) -> None:
        import pymupdf as fitz

        from pdfstudio.pdfengine.content import wrap_text

        lines = wrap_text("x" * 300, 100, TextStyle(size=11))
        font = fitz.Font("helv")
        assert len(lines) > 1
        assert all(font.text_length(line, 11) <= 100 for line in lines)

    def test_font_shrinks_when_box_is_small(self) -> None:
        from pdfstudio.pdfengine.content import fit_text

        style = TextStyle(size=11)
        # Long enough to need several lines at this width.
        text = (
            "A sentence long enough that it cannot possibly fit on a single "
            "line at this width, forcing the layout to wrap several times."
        )
        roomy = fit_text(text, width=300, height=200, style=style)
        cramped = fit_text(text, width=300, height=16, style=style)
        assert roomy.font_size == 11
        assert len(roomy.lines) > 1
        assert cramped.font_size < 11
        assert cramped.height <= 16 + 1

    def test_replacement_never_overflows_the_page(self, blank_document: PdfDocument) -> None:
        editor = TextEditor(blank_document)
        editor.add(0, Rect(60, 60, 520, 120), "Short line.", TextStyle(size=13))
        line = blank_document.extract_blocks(0)[0].lines[0]
        rect, _text, style = editor.edit_region(0, line.rect.center)
        editor.replace(
            0,
            rect,
            "A replacement sentence that is far too long to sit on a single "
            "line and must therefore wrap instead of running off the page.",
            style,
        )
        page_width, _height = blank_document.page_size(0)
        for block in blank_document.extract_blocks(0):
            for text_line in block.lines:
                assert text_line.rect.x1 <= page_width - 4


class TestLineLevelEditing:
    """Editing one line must not disturb its neighbours."""

    @pytest.fixture
    def paragraph(self, blank_document: PdfDocument) -> PdfDocument:
        TextEditor(blank_document).add(
            0,
            Rect(60, 60, 520, 220),
            "Alpha line one.\nBeta line two.\nGamma line three.",
            TextStyle(size=13),
        )
        return blank_document

    def test_line_at_returns_one_line(self, paragraph: PdfDocument) -> None:
        editor = TextEditor(paragraph)
        second = paragraph.extract_blocks(0)[0].lines[1]
        found = editor.line_at(0, second.rect.center)
        assert found is not None
        assert found.text == "Beta line two."

    def test_edit_region_line_versus_paragraph(self, paragraph: PdfDocument) -> None:
        editor = TextEditor(paragraph)
        point = paragraph.extract_blocks(0)[0].lines[1].rect.center
        _rect, line_text, _style = editor.edit_region(0, point)
        _rect2, block_text, _style2 = editor.edit_region(0, point, whole_paragraph=True)
        assert line_text == "Beta line two."
        assert block_text.count("\n") == 2

    def test_editing_one_line_keeps_the_others(self, paragraph: PdfDocument) -> None:
        editor = TextEditor(paragraph)
        point = paragraph.extract_blocks(0)[0].lines[1].rect.center
        rect, _text, style = editor.edit_region(0, point)
        editor.replace(0, rect, "BETA REPLACED", style)
        text = paragraph.extract_text(0)
        assert "Alpha line one." in text
        assert "Gamma line three." in text
        assert "BETA REPLACED" in text
        assert "Beta line two." not in text

    def test_short_edit_stays_on_one_line(self, paragraph: PdfDocument) -> None:
        editor = TextEditor(paragraph)
        point = paragraph.extract_blocks(0)[0].lines[1].rect.center
        rect, _text, style = editor.edit_region(0, point)
        editor.replace(0, rect, "BETA REPLACED", style)
        lines = [line.text for block in paragraph.extract_blocks(0) for line in block.lines]
        assert "BETA REPLACED" in lines  # not split across two lines

    def test_edit_width_uses_the_column_not_the_glyphs(self, paragraph: PdfDocument) -> None:
        editor = TextEditor(paragraph)
        line = paragraph.extract_blocks(0)[0].lines[1]
        rect, _text, _style = editor.edit_region(0, line.rect.center)
        # The line's own box is narrow; the editable region spans the column.
        assert rect.width > line.rect.width * 2

    def test_column_boundary_is_respected(self, blank_document: PdfDocument) -> None:
        import io

        from PIL import Image

        from pdfstudio.pdfengine.content import ImageEditor

        TextEditor(blank_document).add(
            0, Rect(60, 60, 280, 120), "Left column text.", TextStyle(size=11)
        )
        buffer = io.BytesIO()
        Image.new("RGB", (200, 200), (200, 60, 10)).save(buffer, "PNG")
        ImageEditor(blank_document).insert(0, Rect(320, 50, 520, 250), buffer.getvalue())
        line = blank_document.extract_blocks(0)[0].lines[0]
        rect, _text, _style = TextEditor(blank_document).edit_region(0, line.rect.center)
        assert rect.x1 <= 320  # must not flow underneath the image

    def test_undo_restores_the_original_line(self, paragraph: PdfDocument) -> None:
        editor = TextEditor(paragraph)
        original = paragraph.extract_text(0)
        point = paragraph.extract_blocks(0)[0].lines[1].rect.center
        rect, _text, style = editor.edit_region(0, point)
        editor.replace(0, rect, "Something else entirely", style)
        paragraph.undo_stack.undo()
        assert paragraph.extract_text(0) == original

    def test_free_space_ignores_own_paragraph(self, paragraph: PdfDocument) -> None:
        from pdfstudio.pdfengine.content import _free_space_below

        line = paragraph.extract_blocks(0)[0].lines[1]
        # Siblings must not cap growth, or a line could never wrap.
        assert _free_space_below(paragraph, 0, line.rect) > line.rect.y1


# --------------------------------------------------------------------------- #
# Movable page objects
# --------------------------------------------------------------------------- #
class TestPageObjects:
    """Selecting and repositioning anything drawn on a page."""

    @pytest.fixture
    def laid_out(self, blank_document: PdfDocument) -> PdfDocument:
        """Text, a horizontal rule and an image — a resume-like page."""
        import io

        import pymupdf as fitz
        from PIL import Image

        from pdfstudio.pdfengine.content import ImageEditor

        TextEditor(blank_document).add(
            0, Rect(60, 60, 520, 100), "Test Automation: Playwright", TextStyle(size=11)
        )
        TextEditor(blank_document).add(
            0, Rect(60, 170, 520, 200), "PROFESSIONAL EXPERIENCE", TextStyle(size=13)
        )
        with blank_document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(60, 165), fitz.Point(535, 165))
            shape.finish(width=1.2, color=(0, 0, 0))
            shape.commit()
        buffer = io.BytesIO()
        Image.new("RGB", (120, 80), (30, 110, 200)).save(buffer, "PNG")
        ImageEditor(blank_document).insert(0, Rect(300, 300, 420, 380), buffer.getvalue())
        blank_document.undo_stack.clear()
        return blank_document

    def _objects(self, document: PdfDocument, kind):
        from pdfstudio.pdfengine.objects import ObjectService

        return [o for o in ObjectService(document).objects(0) if o.kind is kind]

    def _text_block(self, document: PdfDocument):
        """The "Test Automation" text block, freshly resolved."""
        from pdfstudio.pdfengine.objects import ObjectKind

        return next(o for o in self._objects(document, ObjectKind.TEXT) if "Test" in o.label)

    def test_enumerates_every_kind(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        kinds = {o.kind for o in ObjectService(laid_out).objects(0)}
        assert ObjectKind.TEXT in kinds
        assert ObjectKind.DRAWING in kinds
        assert ObjectKind.IMAGE in kinds

    def test_hit_test_prefers_the_smaller_object(self, laid_out: PdfDocument) -> None:
        """Clicking a hairline rule must not select the text block behind it."""
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(laid_out)
        rule = self._objects(laid_out, ObjectKind.DRAWING)[0]
        found = service.object_at(0, rule.rect.center)
        assert found is not None
        assert found.kind is ObjectKind.DRAWING

    def test_move_drawing(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        rule = self._objects(laid_out, ObjectKind.DRAWING)[0]
        before = rule.rect.y0
        ObjectService(laid_out).move(rule, 0, -12)
        after = self._objects(laid_out, ObjectKind.DRAWING)
        assert len(after) == 1
        assert after[0].rect.y0 == pytest.approx(before - 12, abs=1.0)

    def test_move_drawing_keeps_nearby_text(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        rule = self._objects(laid_out, ObjectKind.DRAWING)[0]
        ObjectService(laid_out).move(rule, 0, -12)
        assert "PROFESSIONAL EXPERIENCE" in laid_out.extract_text(0)
        assert "Test Automation" in laid_out.extract_text(0)

    def test_move_text_block(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        block = self._text_block(laid_out)
        ObjectService(laid_out).move(block, 40, 0)
        moved = self._text_block(laid_out)
        assert moved.rect.x0 == pytest.approx(block.rect.x0 + 40, abs=1.5)

    def test_move_image_does_not_duplicate(self, laid_out: PdfDocument) -> None:
        """Regression: delete_image() leaves a stub, so images multiplied."""
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        image = self._objects(laid_out, ObjectKind.IMAGE)[0]
        ObjectService(laid_out).move(image, -100, 20)
        after = self._objects(laid_out, ObjectKind.IMAGE)
        assert len(after) == 1
        assert after[0].rect.x0 == pytest.approx(image.rect.x0 - 100, abs=1.5)

    def test_repeated_moves_accumulate(self, laid_out: PdfDocument) -> None:
        """Regression: a stale payload replayed the original geometry."""
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(laid_out)
        rule = self._objects(laid_out, ObjectKind.DRAWING)[0]
        start = rule.rect.y0
        for _ in range(4):
            service.move(rule, 0, -2)
            resolved = service.resolve(
                rule, Rect(rule.rect.x0, rule.rect.y0 - 2, rule.rect.x1, rule.rect.y1 - 2)
            )
            assert resolved is not None
            rule = resolved
        rules = self._objects(laid_out, ObjectKind.DRAWING)
        assert len(rules) == 1
        assert rules[0].rect.y0 == pytest.approx(start - 8, abs=1.5)

    def test_align_left(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        block = self._text_block(laid_out)
        service = ObjectService(laid_out)
        service.move(block, 120, 0)
        shifted = service.resolve(
            block, Rect(block.rect.x0 + 120, block.rect.y0, block.rect.x1 + 120, block.rect.y1)
        )
        service.align_left(shifted, 60.0)
        final = self._text_block(laid_out)
        assert final.rect.x0 == pytest.approx(60, abs=1.5)

    def test_move_is_undoable(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        rule = self._objects(laid_out, ObjectKind.DRAWING)[0]
        before = rule.rect.y0
        ObjectService(laid_out).move(rule, 0, -20)
        laid_out.undo_stack.undo()
        restored = self._objects(laid_out, ObjectKind.DRAWING)[0]
        assert restored.rect.y0 == pytest.approx(before, abs=0.5)

    def test_delete_object(self, laid_out: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        image = self._objects(laid_out, ObjectKind.IMAGE)[0]
        ObjectService(laid_out).delete(image)
        assert not self._objects(laid_out, ObjectKind.IMAGE)
        laid_out.undo_stack.undo()
        assert self._objects(laid_out, ObjectKind.IMAGE)


class TestIndentControl:
    """Hanging indents let a wrapped line be pulled back to the margin."""

    def test_wrap_indent_shifts_continuation_lines(self) -> None:
        from pdfstudio.pdfengine.content import wrap_text

        text = (
            "Test Automation: Playwright, Java, Selenium WebDriver, TypeScript, "
            "Python, Page Object Model, Data-Driven Testing, BDD"
        )
        plain = wrap_text(text, 475, TextStyle(size=10.5))
        indented = wrap_text(text, 475, TextStyle(size=10.5, wrap_indent=90))
        # The indent narrows continuation lines, so it may need more of them.
        assert len(indented) >= len(plain)

    def test_indent_never_overflows(self, blank_document: PdfDocument) -> None:
        editor = TextEditor(blank_document)
        editor.add(
            0,
            Rect(60, 60, 535, 120),
            "A long sentence that needs to wrap at least once inside the column.",
            TextStyle(size=11, wrap_indent=80),
        )
        width, _height = blank_document.page_size(0)
        for block in blank_document.extract_blocks(0):
            for line in block.lines:
                assert line.rect.x1 <= width - 4

    def test_zero_indent_is_flush_left(self, blank_document: PdfDocument) -> None:
        editor = TextEditor(blank_document)
        editor.add(
            0,
            Rect(60, 60, 535, 140),
            "A deliberately long sentence that has to wrap onto at least a "
            "second visual line inside this column width so the alignment of "
            "every wrapped line can be compared.",
            TextStyle(size=11, wrap_indent=0),
        )
        lefts = [
            round(line.rect.x0)
            for block in blank_document.extract_blocks(0)
            for line in block.lines
        ]
        assert len(lefts) >= 2
        assert len(set(lefts)) == 1  # every line starts at the same margin


class TestNoWhiteLayer:
    """Edits must not paint an opaque patch over the page background.

    Every redaction used to pass ``fill=(1, 1, 1)``, which stamps a white
    rectangle into the content stream. On a tinted CV, a letterhead or any
    page with artwork that showed up as a white box the user then had to
    clean up by hand.
    """

    @pytest.fixture
    def tinted(self, blank_document: PdfDocument) -> PdfDocument:
        """A page with a coloured background and two lines of text."""
        import pymupdf as fitz

        with blank_document.locked() as handle:
            page = handle[0]
            page.draw_rect(
                fitz.Rect(0, 0, page.rect.width, page.rect.height),
                color=None,
                fill=(0.2, 0.4, 0.8),
            )
        TextEditor(blank_document).add(
            0, Rect(60, 60, 500, 90), "First line of text", TextStyle(size=12)
        )
        TextEditor(blank_document).add(
            0, Rect(60, 200, 500, 230), "Second line of text", TextStyle(size=12)
        )
        blank_document.undo_stack.clear()
        return blank_document

    @staticmethod
    def _pixel(document: PdfDocument, x: int, y: int) -> tuple[int, int, int]:
        with document.locked() as handle:
            pix = handle[0].get_pixmap()
        return pix.pixel(x, y)[:3]

    def _is_white(self, document: PdfDocument, x: int, y: int) -> bool:
        return all(channel > 240 for channel in self._pixel(document, x, y))

    def test_replacing_text_keeps_the_background(self, tinted: PdfDocument) -> None:
        TextEditor(tinted).replace(0, Rect(60, 60, 500, 90), "Replacement", TextStyle(size=12))
        # Inside the edited rectangle but clear of the new glyphs.
        assert not self._is_white(tinted, 450, 75)

    def test_deleting_text_keeps_the_background(self, tinted: PdfDocument) -> None:
        TextEditor(tinted).delete(0, Rect(60, 60, 500, 90))
        assert not self._is_white(tinted, 300, 75)
        assert "First line" not in tinted.extract_text(0)

    def test_moving_text_keeps_the_background(self, tinted: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(tinted)
        block = next(
            o for o in service.objects(0) if o.kind is ObjectKind.TEXT and "First" in o.label
        )
        service.move(block, 0, 40)
        assert not self._is_white(tinted, 200, 72)

    def test_an_explicit_background_is_still_honoured(self, tinted: PdfDocument) -> None:
        """Asking for a fill must still paint one — only the default changed."""
        from pdfstudio.pdfengine.types import Color

        TextEditor(tinted).delete(0, Rect(60, 60, 500, 90), background=Color(1, 1, 1))
        assert self._is_white(tinted, 300, 75)


class TestObjectClipboard:
    """Copying and pasting objects of every kind."""

    @pytest.fixture
    def populated(self, blank_document: PdfDocument) -> PdfDocument:
        import io

        import pymupdf as fitz
        from PIL import Image

        from pdfstudio.pdfengine.content import ImageEditor

        TextEditor(blank_document).add(
            0, Rect(60, 60, 400, 90), "Copy me please", TextStyle(size=12)
        )
        with blank_document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(60, 160), fitz.Point(520, 160))
            shape.finish(width=1.5, color=(0.1, 0.1, 0.1))
            shape.commit()
            annot = handle[0].add_rect_annot(fitz.Rect(400, 400, 480, 450))
            annot.set_colors(stroke=(1, 0, 0))
            annot.update()
        buffer = io.BytesIO()
        Image.new("RGB", (90, 60), (200, 40, 40)).save(buffer, "PNG")
        ImageEditor(blank_document).insert(0, Rect(300, 250, 390, 310), buffer.getvalue())
        blank_document.undo_stack.clear()
        return blank_document

    @staticmethod
    def _of_kind(document: PdfDocument, kind):
        from pdfstudio.pdfengine.objects import ObjectService

        return [o for o in ObjectService(document).objects(0) if o.kind is kind]

    def test_copy_paste_text(self, populated: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        block = self._of_kind(populated, ObjectKind.TEXT)[0]
        service.paste(service.copy(block), 0, at=Point(60, 500))
        labels = [o.label for o in self._of_kind(populated, ObjectKind.TEXT)]
        assert labels.count("Copy me please") == 2

    def test_copy_paste_image_is_a_real_second_placement(self, populated: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        image = self._of_kind(populated, ObjectKind.IMAGE)[0]
        service.paste(service.copy(image), 0, at=Point(80, 600))
        assert len(self._of_kind(populated, ObjectKind.IMAGE)) == 2

    def test_copy_paste_drawing(self, populated: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        rule = next(
            o for o in self._of_kind(populated, ObjectKind.DRAWING) if "rule" in o.label
        )
        service.paste(service.copy(rule), 0, at=Point(60, 600))
        rules = [o for o in self._of_kind(populated, ObjectKind.DRAWING) if "rule" in o.label]
        assert len(rules) == 2

    def test_copy_paste_annotation(self, populated: PdfDocument) -> None:
        """Regression: there is no copy_annot(), so /Annots is edited by hand."""
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        annotation = self._of_kind(populated, ObjectKind.ANNOTATION)[0]
        service.paste(service.copy(annotation), 0, at=Point(400, 600))
        copies = self._of_kind(populated, ObjectKind.ANNOTATION)
        assert len(copies) == 2
        # Pasted at the requested spot, not mirrored into bottom-up space.
        assert any(abs(o.rect.y0 - 600) < 3 for o in copies)

    def test_paste_is_undoable(self, populated: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        before = len(self._of_kind(populated, ObjectKind.TEXT))
        service.paste(service.copy(self._of_kind(populated, ObjectKind.TEXT)[0]), 0)
        assert populated.undo_stack.undo()
        assert len(self._of_kind(populated, ObjectKind.TEXT)) == before

    def test_cut_removes_the_original(self, populated: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        clip = service.cut(self._of_kind(populated, ObjectKind.TEXT)[0])
        assert "Copy me please" not in populated.extract_text(0)
        service.paste(clip, 0, at=Point(60, 400))
        assert "Copy me please" in populated.extract_text(0)

    def test_duplicate_offsets_the_copy(self, populated: PdfDocument) -> None:
        """A copy hidden exactly behind its original looks like nothing happened."""
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        block = self._of_kind(populated, ObjectKind.TEXT)[0]
        rect = service.duplicate(block)
        assert rect.x0 > block.rect.x0
        assert rect.y0 > block.rect.y0

    def test_paste_is_clamped_to_the_page(self, populated: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        service = ObjectService(populated)
        width, height = populated.page_size(0)
        block = self._of_kind(populated, ObjectKind.TEXT)[0]
        rect = service.paste(service.copy(block), 0, at=Point(width + 200, height + 200))
        assert rect.x1 <= width
        assert rect.y1 <= height


class TestBackgroundObjectsAreNotGrabbable:
    """A page-sized shape must not be selected by clicking empty space.

    Dragging the page background drops an opaque sheet over everything else,
    which is what users described as "a white layer on top of the text".
    """

    @pytest.fixture
    def tinted(self, blank_document: PdfDocument) -> PdfDocument:
        import pymupdf as fitz

        with blank_document.locked() as handle:
            page = handle[0]
            page.draw_rect(
                fitz.Rect(0, 0, page.rect.width, page.rect.height),
                color=None,
                fill=(0.9, 0.93, 1.0),
            )
            shape = page.new_shape()
            shape.draw_line(fitz.Point(60, 300), fitz.Point(500, 300))
            shape.finish(width=1.0, color=(0, 0, 0))
            shape.commit()
        blank_document.undo_stack.clear()
        return blank_document

    def test_clicking_empty_space_selects_nothing(self, tinted: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        assert ObjectService(tinted).object_at(0, Point(450, 650)) is None

    def test_real_content_is_still_selectable(self, tinted: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        found = ObjectService(tinted).object_at(0, Point(300, 300))
        assert found is not None
        assert found.kind is ObjectKind.DRAWING
        assert found.label == "Horizontal rule"

    def test_background_can_still_be_picked_deliberately(self, tinted: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        service = ObjectService(tinted)
        found = service.object_at(0, Point(450, 650), include_background=True)
        assert found is not None
        assert service.is_background(found)

    def test_solid_line_dashes_are_not_the_string_none(self, tinted: PdfDocument) -> None:
        """MuPDF reports dashes=None; str() made it the literal "None",

        which was then written into the content stream and broke the parser.
        """
        for path in tinted.page_drawings(0):
            assert path.dashes != "None"


class TestObjectAlignment:
    """Aligning an object to each page edge."""

    @pytest.fixture
    def with_rule(self, blank_document: PdfDocument) -> PdfDocument:
        import pymupdf as fitz

        with blank_document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(100, 300), fitz.Point(300, 300))
            shape.finish(width=1.0, color=(0, 0, 0))
            shape.commit()
        blank_document.undo_stack.clear()
        return blank_document

    def _rule(self, document: PdfDocument):
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        return next(
            o for o in ObjectService(document).objects(0) if o.kind is ObjectKind.DRAWING
        )

    @pytest.mark.parametrize("edge", ["left", "right", "center", "top", "bottom", "middle"])
    def test_every_edge_moves_the_object(self, with_rule: PdfDocument, edge: str) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        service = ObjectService(with_rule)
        expected = service.align(self._rule(with_rule), edge)
        moved = self._rule(with_rule)
        assert moved.rect.x0 == pytest.approx(expected.x0, abs=1.5)
        assert moved.rect.y0 == pytest.approx(expected.y0, abs=1.5)

    def test_align_left_snaps_to_the_margin(self, with_rule: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        ObjectService(with_rule).align(self._rule(with_rule), "left")
        assert self._rule(with_rule).rect.x0 == pytest.approx(60.0, abs=1.5)

    def test_align_centre_is_symmetric(self, with_rule: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        width, _height = with_rule.page_size(0)
        ObjectService(with_rule).align(self._rule(with_rule), "center")
        rect = self._rule(with_rule).rect
        assert rect.x0 == pytest.approx(width - rect.x1, abs=2.0)

    def test_unknown_edge_is_rejected(self, with_rule: PdfDocument) -> None:
        from pdfstudio.pdfengine.objects import ObjectService

        with pytest.raises(ValueError):
            ObjectService(with_rule).align(self._rule(with_rule), "diagonally")


class TestReflowKeepsRowsTogether:
    """Growing a paragraph must move whole rows, not just one column."""

    def test_labels_travel_with_their_values(self, blank_document: PdfDocument) -> None:
        editor = TextEditor(blank_document)
        for row, label in enumerate(("Alpha:", "Beta:", "Gamma:")):
            y = 100 + row * 20
            editor.add(0, Rect(60, y, 140, y + 16), label, TextStyle(size=10))
            editor.add(0, Rect(150, y, 520, y + 16), f"value {row}", TextStyle(size=10))
        blank_document.undo_stack.clear()

        editor.replace(
            0,
            Rect(150, 100, 520, 116),
            "A replacement long enough to wrap onto a second and probably a "
            "third line inside this column, which pushes everything below it "
            "further down the page.",
            TextStyle(size=10),
        )

        rows: dict[int, list[str]] = {}
        for block in blank_document.extract_blocks(0):
            for line in block.lines:
                rows.setdefault(round(line.rect.y0 / 4), []).append(line.text)
        # Each label must still share a baseline with a value.
        for label in ("Beta:", "Gamma:"):
            band = next(texts for texts in rows.values() if any(label in t for t in texts))
            assert any("value" in text for text in band), f"{label} lost its value"


class TestParagraphSelection:
    """Selecting a whole paragraph must stop at real boundaries.

    Regression: ``_paragraph_group`` measured "leading" from the whole block
    height, so a three-line paragraph reported ~45 pt of line spacing and the
    blank gap before the next paragraph looked like ordinary leading. Clicking
    any body line selected the heading, the paragraph and the list below it.
    """

    @pytest.fixture
    def structured(self, blank_document: PdfDocument) -> PdfDocument:
        """Heading, a three-line paragraph, then a three-line list."""
        editor = TextEditor(blank_document)
        editor.add(0, Rect(60, 50, 520, 80), "PROJECT REPORT", TextStyle(size=15, bold=True))
        editor.add(
            0,
            Rect(60, 100, 520, 150),
            "Renewable generation rose sharply.\n"
            "Costs fell across the region.\n"
            "Wind led the new capacity.",
            TextStyle(size=11),
        )
        # Only ~20 pt below the paragraph: a realistic gap that the old
        # grouping rule (0.9 x whole-block-height) treated as ordinary line
        # spacing, swallowing this list into the paragraph above.
        editor.add(
            0,
            Rect(60, 165, 520, 215),
            "First item about timelines.\nSecond item about budget.\nThird item about staffing.",
            TextStyle(size=11),
        )
        blank_document.undo_stack.clear()
        return blank_document

    def test_body_paragraph_stops_at_its_own_lines(self, structured: PdfDocument) -> None:
        editor = TextEditor(structured)
        anchor = next(
            block.rect.center
            for block in structured.extract_blocks(0)
            if "Renewable" in block.text
        )
        region = editor.edit_region(0, anchor, whole_paragraph=True)
        assert region is not None
        assert "PROJECT REPORT" not in region[1]
        assert "First item" not in region[1]
        assert region[1].count("\n") == 2  # exactly three lines

    def test_heading_is_its_own_paragraph(self, structured: PdfDocument) -> None:
        """A larger font marks a boundary even when spacing is tight."""
        editor = TextEditor(structured)
        anchor = next(
            block.rect.center
            for block in structured.extract_blocks(0)
            if "PROJECT REPORT" in block.text
        )
        region = editor.edit_region(0, anchor, whole_paragraph=True)
        assert region is not None
        assert region[1].strip() == "PROJECT REPORT"

    def test_list_is_separate_from_the_paragraph(self, structured: PdfDocument) -> None:
        editor = TextEditor(structured)
        anchor = next(
            block.rect.center
            for block in structured.extract_blocks(0)
            if "First item" in block.text
        )
        region = editor.edit_region(0, anchor, whole_paragraph=True)
        assert region is not None
        assert "Renewable" not in region[1]
        assert "First item" in region[1]


class TestLineReordering:
    """Moving a line up or down within its paragraph."""

    @pytest.fixture
    def listed(self, blank_document: PdfDocument) -> PdfDocument:
        TextEditor(blank_document).add(
            0,
            Rect(60, 100, 520, 160),
            "Alpha line here.\nBeta line here.\nGamma line here.",
            TextStyle(size=11),
        )
        blank_document.undo_stack.clear()
        return blank_document

    @staticmethod
    def _line_point(document: PdfDocument, index: int) -> Point:
        """Centre of line ``index`` — never a guessed coordinate."""
        lines = TextEditor(document).paragraph_lines(0, Point(90, 105))
        return lines[index].rect.center

    @classmethod
    def _texts(cls, document: PdfDocument) -> list[str]:
        return [ln.text for ln in TextEditor(document).paragraph_lines(0, Point(90, 105))]

    def test_move_down_swaps_with_the_next_line(self, listed: PdfDocument) -> None:
        editor = TextEditor(listed)
        assert editor.move_line(0, self._line_point(listed, 0), 1) is True
        assert self._texts(listed)[:2] == ["Beta line here.", "Alpha line here."]

    def test_move_up_swaps_with_the_previous_line(self, listed: PdfDocument) -> None:
        editor = TextEditor(listed)
        before = self._texts(listed)
        assert editor.move_line(0, self._line_point(listed, 1), -1) is True
        after = self._texts(listed)
        assert after[0] == before[1]
        assert after[1] == before[0]

    def test_first_line_cannot_move_up(self, listed: PdfDocument) -> None:
        assert TextEditor(listed).move_line(0, self._line_point(listed, 0), -1) is False

    def test_last_line_cannot_move_down(self, listed: PdfDocument) -> None:
        assert TextEditor(listed).move_line(0, self._line_point(listed, -1), 1) is False

    def test_move_is_undoable(self, listed: PdfDocument) -> None:
        before = self._texts(listed)
        TextEditor(listed).move_line(0, self._line_point(listed, 0), 1)
        assert listed.undo_stack.undo()
        assert self._texts(listed) == before

    def test_no_text_is_lost(self, listed: PdfDocument) -> None:
        TextEditor(listed).move_line(0, self._line_point(listed, 0), 1)
        text = listed.extract_text(0)
        for word in ("Alpha", "Beta", "Gamma"):
            assert word in text

    def test_bad_direction_is_rejected(self, listed: PdfDocument) -> None:
        with pytest.raises(ValidationError):
            TextEditor(listed).move_line(0, self._line_point(listed, 0), 3)


class TestWordProcessorFormatting:
    """Lists, case conversion, spacing and line editing."""

    @pytest.fixture
    def listed(self, blank_document: PdfDocument) -> PdfDocument:
        TextEditor(blank_document).add(
            0,
            Rect(60, 100, 420, 160),
            "First item about timelines.\nSecond item about budget.\nThird item about staffing.",
            TextStyle(size=11),
        )
        blank_document.undo_stack.clear()
        return blank_document

    @staticmethod
    def _anchor(document: PdfDocument) -> Point:
        """A point inside the first line, wherever it actually sits."""
        lines = TextEditor(document).paragraph_lines(0, Point(70, 105))
        return lines[0].rect.center

    @classmethod
    def _texts(cls, document: PdfDocument) -> list[str]:
        return [
            ln.text for ln in TextEditor(document).paragraph_lines(0, cls._anchor(document))
        ]

    def test_bullets_are_added(self, listed: PdfDocument) -> None:
        assert TextEditor(listed).set_list_style(0, self._anchor(listed), "bullet") is True
        # The base-14 fonts have no U+2022, so PDF text extraction reports the
        # substituted middle dot. Either glyph means a bullet was written.
        assert all(
            line.lstrip().startswith(("\u2022", "\u00b7")) for line in self._texts(listed)
        )

    def test_numbering_counts_items_not_wrapped_lines(self, listed: PdfDocument) -> None:
        """Regression: markers widened the text so lines wrapped, and the
        wrapped fragment was then numbered as its own item."""
        editor = TextEditor(listed)
        editor.set_list_style(0, self._anchor(listed), "bullet")
        editor.set_list_style(0, self._anchor(listed), "number")
        texts = self._texts(listed)
        assert len(texts) == 3
        assert texts[0].startswith("1.")
        assert texts[1].startswith("2.")
        assert texts[2].startswith("3.")

    def test_markers_do_not_accumulate(self, listed: PdfDocument) -> None:
        editor = TextEditor(listed)
        editor.set_list_style(0, self._anchor(listed), "bullet")
        editor.set_list_style(0, self._anchor(listed), "bullet")
        assert not any("\u2022 \u2022" in line for line in self._texts(listed))

    def test_list_can_be_removed(self, listed: PdfDocument) -> None:
        editor = TextEditor(listed)
        editor.set_list_style(0, self._anchor(listed), "number")
        editor.set_list_style(0, self._anchor(listed), "none")
        assert self._texts(listed)[0].startswith("First item")

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("upper", "FIRST ITEM ABOUT TIMELINES."),
            ("lower", "first item about timelines."),
            ("title", "First Item About Timelines."),
        ],
    )
    def test_case_conversion(self, listed: PdfDocument, mode: str, expected: str) -> None:
        assert TextEditor(listed).transform_case(0, self._anchor(listed), mode) is True
        assert self._texts(listed)[0] == expected

    def test_case_conversion_keeps_line_count(self, listed: PdfDocument) -> None:
        """Upper case is wider; without room the lines would wrap and double."""
        TextEditor(listed).transform_case(0, self._anchor(listed), "upper")
        assert len(self._texts(listed)) == 3

    def test_sentence_case_handles_full_stops(self) -> None:
        from pdfstudio.pdfengine.content import _convert_case

        assert _convert_case("HELLO THERE. BYE NOW", "sentence") == "Hello there. Bye now"

    def test_title_case_keeps_apostrophes(self) -> None:
        from pdfstudio.pdfengine.content import _convert_case

        assert _convert_case("don't stop", "title") == "Don't Stop"

    def test_unknown_case_mode_is_rejected(self, listed: PdfDocument) -> None:
        with pytest.raises(ValidationError):
            TextEditor(listed).transform_case(0, self._anchor(listed), "sideways")

    def test_line_spacing_moves_lines_apart(self, listed: PdfDocument) -> None:
        editor = TextEditor(listed)
        before = editor.paragraph_lines(0, self._anchor(listed))
        gap_before = before[1].rect.y0 - before[0].rect.y0
        editor.set_line_spacing(0, self._anchor(listed), 2.0)
        after = editor.paragraph_lines(0, self._anchor(listed))
        assert after[1].rect.y0 - after[0].rect.y0 > gap_before

    def test_zero_spacing_is_rejected(self, listed: PdfDocument) -> None:
        with pytest.raises(ValidationError):
            TextEditor(listed).set_line_spacing(0, self._anchor(listed), 0)

    def test_duplicate_line(self, listed: PdfDocument) -> None:
        assert TextEditor(listed).duplicate_line(0, self._anchor(listed)) is True
        texts = self._texts(listed)
        assert len(texts) == 4
        assert texts[0] == texts[1]

    def test_delete_line(self, listed: PdfDocument) -> None:
        assert TextEditor(listed).delete_line(0, self._anchor(listed)) is True
        texts = self._texts(listed)
        assert len(texts) == 2
        assert not any("First item" in t for t in texts)

    def test_delete_line_is_undoable(self, listed: PdfDocument) -> None:
        before = self._texts(listed)
        TextEditor(listed).delete_line(0, self._anchor(listed))
        assert listed.undo_stack.undo()
        assert self._texts(listed) == before

    def test_format_painter_applies_a_style(self, listed: PdfDocument) -> None:
        editor = TextEditor(listed)
        wanted = TextStyle(size=16, bold=True)
        assert editor.apply_style_to_paragraph(0, self._anchor(listed), wanted) is True
        spans = [
            sp
            for block in listed.extract_blocks(0)
            for line in block.lines
            for sp in line.spans
            if sp.text.strip()
        ]
        assert any(round(sp.size) == 16 for sp in spans)


class TestTextDecorations:
    """Underline and strike-through are drawn, since PDF has no attribute."""

    def test_underline_draws_a_rule(self, blank_document: PdfDocument) -> None:
        TextEditor(blank_document).add(
            0, Rect(60, 60, 400, 90), "Underlined", TextStyle(size=14, underline=True)
        )
        assert len(blank_document.page_drawings(0)) == 1

    def test_strikethrough_draws_a_rule(self, blank_document: PdfDocument) -> None:
        TextEditor(blank_document).add(
            0, Rect(60, 60, 400, 90), "Struck", TextStyle(size=14, strikethrough=True)
        )
        assert len(blank_document.page_drawings(0)) == 1

    def test_both_draw_two_rules(self, blank_document: PdfDocument) -> None:
        TextEditor(blank_document).add(
            0,
            Rect(60, 60, 400, 90),
            "Both",
            TextStyle(size=14, underline=True, strikethrough=True),
        )
        assert len(blank_document.page_drawings(0)) == 2

    def test_plain_text_draws_nothing_extra(self, blank_document: PdfDocument) -> None:
        TextEditor(blank_document).add(0, Rect(60, 60, 400, 90), "Plain", TextStyle(size=14))
        assert blank_document.page_drawings(0) == []

    def test_the_text_is_still_extractable(self, blank_document: PdfDocument) -> None:
        """Decorations must not be drawn over the glyphs as images."""
        TextEditor(blank_document).add(
            0, Rect(60, 60, 400, 90), "Readable", TextStyle(size=14, underline=True)
        )
        assert "Readable" in blank_document.extract_text(0)
