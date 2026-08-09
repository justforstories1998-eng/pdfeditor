# API reference

The head-less API — everything below works without a GUI, in scripts, plugins,
notebooks and servers.

```python
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
```

---

## `PdfDocument`

The central object. Thread-safe; always use it as a context manager or call
`close()`.

### Opening and creating

| Method | Description |
| --- | --- |
| `PdfDocument.open(path, password=None)` | Open a file. Raises `PasswordRequiredError`, `DocumentError` |
| `PdfDocument.from_bytes(data, name=, password=)` | Open an in-memory PDF |
| `PdfDocument.create(width=595, height=842, pages=1)` | New blank document |
| `doc.copy()` | Deep copy through serialisation |
| `doc.close()` | Release resources |

### Properties

`page_count`, `path`, `display_name`, `is_pdf`, `is_encrypted`,
`encryption_method`, `is_form`, `is_portfolio`, `is_linearized`, `is_tagged`,
`is_modified`, `file_size`, `pdf_version`, `undo_stack`.

### Pages

```python
doc.page_info(0)          # PageInfo: size, rotation, label, counts
doc.pages_info()          # every page
doc.page_size(0)          # (width, height) in points
doc.page_rotation(0)      # 0 / 90 / 180 / 270
doc.set_page_rotation(0, 90)
doc.page_label(0)         # "iv", "A-3", …
doc.set_page_labels([{"startpage": 0, "style": "r"}])
```

### Content

```python
doc.extract_text(0)                 # plain text
doc.extract_text(0, mode="html")    # any PyMuPDF mode
doc.extract_all_text()
doc.extract_blocks(0)               # [TextBlock] → lines → spans (font, size, colour)
doc.extract_words(0)                # [(Rect, word)] for selection and hit-testing
doc.page_images(0)                  # [ImageInfo]
doc.extract_image(xref)             # (bytes, extension)
doc.page_drawings(0)                # [DrawingPath]
doc.page_annotations(0)             # [Annotation]
doc.all_annotations()
doc.page_links(0)                   # [Link]
doc.needs_ocr()                     # heuristic for scans
```

### Structure and metadata

```python
doc.bookmarks()                     # tree of Bookmark
doc.set_bookmarks([...])
doc.metadata()                      # DocumentMetadata (info dict + XMP)
doc.set_metadata(metadata)
doc.clear_metadata()
doc.layers()                        # [Layer] (OCGs)
doc.set_layer_visible(xref, True)
doc.attachments() / add_attachment() / extract_attachment() / delete_attachment()
doc.javascript() / has_javascript()
doc.statistics()                    # everything, as a dict
```

### Saving

```python
doc.save()                                  # in place
doc.save_as("out.pdf", SaveOptions.optimized())
doc.to_bytes()                              # serialise to memory

SaveOptions.fast()        # quickest, no cleanup
SaveOptions.optimized()   # maximum size reduction
SaveOptions.web()         # optimised for byte-serving
SaveOptions(incremental=True)               # append a revision
SaveOptions(encryption=EncryptionMethod.AES_256, user_password="pw")
```

---

## Services

Each service wraps one document and pushes undoable commands.

### `PageService` — `pdfengine.pages`

```python
service = PageService(doc)
service.delete([1, 2]); service.insert_blank(0, count=2, size="A4")
service.move([0], 3); service.rotate([0], 90); service.crop([0], rect)
service.resize([0], "A5"); service.duplicate([0]); service.replace(0, other)
service.extract([0, 2])                 # → new PdfDocument
service.split_by_count(10)
service.split_by_bookmarks(level=1)
service.export_pages_to_files("out/")
```

Module functions: `merge_documents(sources)`, `n_up(doc, 2, 2)`,
`make_booklet(doc)`.

### `TextEditor` / `ImageEditor` / `VectorEditor` — `pdfengine.content`

```python
TextEditor(doc).add(0, rect, "text", TextStyle(size=12, bold=True))
TextEditor(doc).replace(0, rect, "new text")
TextEditor(doc).replace_all("old", "new", regex=False)
TextEditor(doc).add_rich_text(0, rect, "<b>HTML</b>")
TextEditor(doc).fonts()                  # embedding status per font

ImageEditor(doc).insert(0, rect, "logo.png", adjustments=ImageAdjustments(...))
ImageEditor(doc).replace(0, xref, data); .crop(0, xref, box); .adjust(...)
ImageEditor(doc).resample(0, xref)       # → bytes saved
ImageEditor(doc).extract_all()

VectorEditor(doc).draw(0, "rect", [p1, p2], ShapeStyle(fill=Color(1, 0, 0)))
VectorEditor(doc).draw_gradient(0, rect, start, end)
VectorEditor(doc).import_svg(0, rect, svg_text)
```

### `AnnotationService` — `pdfengine.annotations`

```python
service = AnnotationService(doc, author="Ada")
service.markup(0, quads, AnnotationType.HIGHLIGHT)
service.highlight_text(0, "invoice")        # → count
service.sticky_note(0, point, "text"); service.free_text(0, rect, "text")
service.callout(0, rect, "text", target); service.ink(0, strokes)
service.line(0, a, b, arrow=True); service.shape(0, rect, AnnotationType.CIRCLE)
service.polygon(0, points); service.stamp(0, rect, "Approved")
service.measure_distance(0, a, b, scale=1.0, unit="mm")
service.measure_area(0, points); service.measure_perimeter(0, points)
service.mark_redaction(0, rect); service.redact_text("secret")
service.apply_redactions()                  # irreversible
service.reply(0, xref, "text"); service.resolve(0, xref)
service.export_xfdf() / import_xfdf() / export_json() / import_json()
service.flatten(); service.delete_all(); service.summary()
```

### `FormService` — `pdfengine.forms`

```python
service = FormService(doc)
service.create_text("name", 0, rect, multiline=False)
service.create_checkbox / create_radio_group / create_dropdown / create_listbox
service.create_date_picker / create_signature_field / create_barcode
service.fields(); service.values(); service.set_value("name", "Ada")
service.fill({"name": "Ada"}); service.reset(); service.flatten()
service.validate()                          # [(field, problem)]
service.calculate({"total": "price * qty"}) # safe arithmetic only
service.export_json / export_fdf / export_xfdf / export_csv (+ imports)
service.fill_many(rows, "out/")             # mail-merge
```

### `SecurityService` — `pdfengine.security`

```python
service = SecurityService(doc)
service.encrypt_to("out.pdf", EncryptionSettings(
    method=EncryptionMethod.AES_256,
    user_password="pw",
    permissions=PermissionSet.read_only(),
))
service.decrypt_to("open.pdf")
service.sanitize(javascript=True, metadata=True, embedded_files=True)
service.remove_hidden_text(); service.secure_delete_file(path)
service.watermark_text("DRAFT", opacity=0.25, tile=True)
service.watermark_image("logo.png")
service.bates_numbering(prefix="ACME-", start=1)
service.header_footer(footer_center="Page {page} of {pages}")
service.background(Color(0.98, 0.98, 0.95))
service.draw_signature_appearance(0, rect, name="Ada")
service.sign("signed.pdf", pkcs12_file="key.p12", pkcs12_password="pw")
service.validate_signatures(); service.security_report()
```

### `SearchService` — `pdfengine.search`

```python
hits = SearchService(doc).search(SearchQuery(
    text="invoice",
    regex=False, boolean=False, case_sensitive=False, whole_words=False,
    include_annotations=True, include_bookmarks=True, include_forms=True,
    pages=(0, 9),
))
# SearchHit: page, rect, text, context, source
search_documents([doc1, doc2], "term")      # across open documents
```

### `Optimizer`, `DocumentComparer`, `AccessibilityChecker` — `pdfengine.optimize`

```python
Optimizer(doc).analyze()                    # where the bytes are
Optimizer(doc).optimize(OptimizeProfile.screen(), output="small.pdf")

report = DocumentComparer(left, right).compare()
report.identical; report.changed_pages; report.summary(); report.as_dict()
DocumentComparer(left, right).build_report_pdf(report, "diff.pdf")

checker = AccessibilityChecker(doc)
checker.check()                             # [AccessibilityIssue]
checker.auto_fix(language="en-GB")
checker.set_alt_text(page, xref, "A chart"); checker.reading_order(0)
```

### `Importer` / `Exporter` — `pdfengine.convert`

```python
Importer().import_file("report.docx")       # → PdfDocument
Importer().import_images([...]); Importer().import_text("...")

exporter = Exporter(doc)
exporter.to_text(layout=True); exporter.to_html(); exporter.to_markdown()
exporter.to_images("out/", "png", ExportOptions(dpi=200))
exporter.to_multipage_tiff("out.tiff"); exporter.to_svg(0)
exporter.to_docx("out.docx"); exporter.to_pptx("out.pptx")
exporter.tables_to_csv("tables/")
exporter.to_conformance("archive.pdf", ConformanceLevel.PDF_A_2B)
```

### `OcrService` — `ocr.engine`

```python
from pdfstudio.core.settings import OcrSettings

service = OcrService(doc, OcrSettings(languages=["eng", "fra"], dpi=300))
service.pages_needing_ocr()
results = service.run()                     # adds an invisible text layer
service.confidence_report(results)
service.make_searchable("out.pdf")
available_engines(); get_engine("tesseract")
```

### `AIAssistant` — `ai.assistant`

```python
ai = AIAssistant(doc)
ai.summarize(style="bullets"); ai.summarize_pages()
ai.ask("What is the total?")                # .text and .citations
ai.chat("And the due date?")
ai.generate_bookmarks(); ai.apply_generated_bookmarks()
ai.generate_title(); ai.generate_metadata(); ai.auto_tag()
ai.extract_tables(); ai.check_grammar(text); ai.generate_citation("apa")
ai.translate("French")                      # needs a remote provider
```

---

## Core infrastructure

```python
from pdfstudio.core import settings, jobs, bus, Topic, UndoStack, get_logger

settings().get("ui.theme"); settings().set("performance.render_dpi", 200)
settings().subscribe("ui.", callback)

job = jobs().submit("Work", fn, arg, with_context=True)
job.add_done_callback(lambda j: print(j.result()))
job.cancel()

bus().subscribe(Topic.DOCUMENT_SAVED, handler)
bus().publish(Topic.STATUS_MESSAGE, {"message": "Done"})

with doc.undo_stack.macro("Bulk edit"):
    ...                                      # one undo step
doc.undo_stack.undo(); doc.undo_stack.create_snapshot("before import")
```

---

## Batch processing — `services.batch`

```python
from pdfstudio.services.batch import (
    BatchProcessor, OcrOperation, WatermarkOperation, CompressOperation,
    EncryptOperation, RenameRule,
)

result = (
    BatchProcessor(output_dir="out/", rename=RenameRule("{stem}-{date}"))
    .add(OcrOperation(["eng"]))
    .add(WatermarkOperation("CONFIDENTIAL"))
    .add(CompressOperation("ebook"))
    .run(["a.pdf", "b.pdf"])
)
result.summary(); result.failed; result.to_json()
```

---

## Value types — `pdfengine.types`

`Point`, `Rect`, `Color`, `PageInfo`, `TextSpan`, `TextLine`, `TextBlock`,
`ImageInfo`, `DrawingPath`, `Annotation`, `FormField`, `Bookmark`, `Link`,
`Attachment`, `Layer`, `DocumentMetadata`, `SearchQuery`, `SearchHit`,
`PageSize`, plus the enums `AnnotationType`, `FieldType`, `EncryptionMethod`,
`Permission`, `ConformanceLevel`, `PageLayout`, `ZoomMode`, `BlendMode`,
`Rotation`.

Helpers: `parse_page_ranges("1-3,7,10-", total)` and
`format_page_ranges([0, 1, 2])`.

---

## Exceptions

All inherit `PdfStudioError` and carry `.message`, `.detail` and `.title`:
`DocumentError`, `PasswordRequiredError`, `PermissionDeniedError`,
`RenderError`, `OcrError`, `PluginError`, `DependencyMissingError`,
`ValidationError`.
