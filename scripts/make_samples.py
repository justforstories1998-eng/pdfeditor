#!/usr/bin/env python3
"""Generate the sample PDFs used for manual testing and demonstrations.

    python scripts/make_samples.py [--output samples/]

Produces a small corpus that exercises every part of the application:
text, images, vectors, forms, annotations, bookmarks, layers, attachments,
encryption, a scanned page needing OCR, and a large document for performance
work.
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

from pdfstudio.pdfengine.annotations import AnnotationService, AnnotationStyle
from pdfstudio.pdfengine.content import (
    ImageEditor,
    ShapeStyle,
    TextEditor,
    TextStyle,
    VectorEditor,
)
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.forms import FormService
from pdfstudio.pdfengine.security import (
    EncryptionSettings,
    PermissionSet,
    SecurityService,
)
from pdfstudio.pdfengine.types import (
    AnnotationType,
    Bookmark,
    Color,
    EncryptionMethod,
    Point,
    Rect,
)

BLUE = Color(0.15, 0.35, 0.75)
GREY = Color(0.35, 0.35, 0.4)


def _image(width: int, height: int, colour: tuple[int, int, int]) -> bytes:
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), colour)
    draw = ImageDraw.Draw(image)
    for i in range(0, width, 40):
        draw.line([(i, 0), (i, height)], fill=(255, 255, 255), width=1)
    draw.ellipse(
        [width * 0.25, height * 0.25, width * 0.75, height * 0.75],
        outline=(255, 255, 255), width=6,
    )
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def sample_report(path: Path) -> Path:
    """A realistic multi-page report: headings, body text, chart, table."""
    document = PdfDocument.create(pages=4)
    text = TextEditor(document)
    vectors = VectorEditor(document)

    text.add(0, Rect(60, 70, 535, 130), "Quarterly Energy Report", TextStyle(size=28, bold=True))
    text.add(0, Rect(60, 140, 535, 170), "Financial year 2026 · Q1", TextStyle(size=13, color=GREY))
    text.add(
        0, Rect(60, 200, 535, 250), "Executive summary", TextStyle(size=17, bold=True)
    )
    text.add(
        0,
        Rect(60, 260, 535, 470),
        "Renewable generation rose 18 percent across the portfolio during the "
        "quarter. Solar capacity reached 4.2 gigawatts while wind contributed "
        "6.8 gigawatts. Operating costs fell by 7 percent following improved "
        "maintenance scheduling, and grid reliability remained above 99.9 "
        "percent throughout the reporting period. Invoice 88123 for 4500.00 GBP "
        "remains outstanding with Acme Ltd.",
        TextStyle(size=11.5, line_height=1.5),
    )

    # a simple bar chart drawn with vectors
    text.add(
        0, Rect(60, 500, 535, 522), "Generation by quarter (GWh)",
        TextStyle(size=10, bold=True, color=GREY),
    )
    baseline = 700.0
    for i, value in enumerate([70, 110, 95, 150, 130]):
        x = 80 + i * 70
        vectors.draw(
            0, "rect", [Point(x, baseline - value), Point(x + 44, baseline)],
            ShapeStyle(fill=BLUE, stroke=None),
        )
        text.add(
            0, Rect(x, baseline + 5, x + 44, baseline + 25), f"Q{i + 1}",
            TextStyle(size=9, align="center"),
        )

    text.add(1, Rect(60, 70, 535, 110), "Financial overview", TextStyle(size=20, bold=True))
    text.add(
        1,
        Rect(60, 130, 535, 300),
        "Revenue totalled 14.6 billion pounds, an increase of 9 percent year on "
        "year. Net profit was 2.1 billion pounds and the dividend was raised to "
        "32 pence per share. The board approved a further 2 billion pound "
        "investment programme for 2027.",
        TextStyle(size=11.5, line_height=1.5),
    )
    table_html = """
    <table><tr><th>Metric</th><th>2025</th><th>2026</th></tr>
    <tr><td>Revenue</td><td>13.4bn</td><td>14.6bn</td></tr>
    <tr><td>Net profit</td><td>1.9bn</td><td>2.1bn</td></tr>
    <tr><td>Dividend</td><td>29p</td><td>32p</td></tr></table>
    """
    text.add_rich_text(1, Rect(60, 320, 535, 470), table_html)

    text.add(2, Rect(60, 70, 535, 110), "Operations", TextStyle(size=20, bold=True))
    text.add(
        2, Rect(60, 130, 535, 260),
        "Fleet availability averaged 94 percent. Two turbines were taken offline "
        "for scheduled blade inspection. Contact operations@example.com or call "
        "+44 20 7946 0958 for site access.",
        TextStyle(size=11.5, line_height=1.5),
    )
    ImageEditor(document).insert(2, Rect(60, 290, 380, 530), _image(640, 480, (30, 90, 160)))

    text.add(3, Rect(60, 70, 535, 110), "Appendix", TextStyle(size=20, bold=True))
    text.add(
        3, Rect(60, 130, 535, 400),
        "Definitions, methodology and assurance statements. All figures are "
        "unaudited unless stated otherwise.",
        TextStyle(size=11.5, line_height=1.5),
    )

    document.set_bookmarks([
        Bookmark("Quarterly Energy Report", 0, children=[
            Bookmark("Executive summary", 0, level=2),
            Bookmark("Financial overview", 1, level=2),
            Bookmark("Operations", 2, level=2),
        ]),
        Bookmark("Appendix", 3),
    ])
    metadata = document.metadata()
    metadata.title = "Quarterly Energy Report"
    metadata.author = "Ada Lovelace"
    metadata.subject = "FY2026 Q1 results"
    metadata.keywords = "energy, renewables, quarterly, report"
    document.set_metadata(metadata)

    SecurityService(document).header_footer(
        header_left="{title}", footer_center="Page {page} of {pages}", footer_right="{date}"
    )
    document.save_as(path, SaveOptions.optimized())
    document.close()
    return path


def sample_annotated(path: Path) -> Path:
    """A review copy covered in every annotation type."""
    document = PdfDocument.create(pages=2)
    text = TextEditor(document)
    text.add(0, Rect(60, 60, 535, 100), "Draft for review", TextStyle(size=22, bold=True))
    text.add(
        0, Rect(60, 120, 535, 260),
        "This paragraph contains deliberate errors and questionable claims that "
        "reviewers are expected to mark up during the review cycle.",
        TextStyle(size=12, line_height=1.5),
    )

    service = AnnotationService(document, author="Ada Lovelace")
    service.highlight_text(0, "deliberate errors")
    service.markup(0, [Rect(60, 200, 300, 218)], AnnotationType.UNDERLINE)
    service.sticky_note(0, Point(500, 130), "Please verify this claim.")
    service.free_text(0, Rect(60, 300, 300, 350), "Typewriter comment")
    service.callout(0, Rect(330, 300, 535, 360), "Look at this figure", Point(300, 250))
    service.ink(0, [[Point(70, 400), Point(140, 430), Point(210, 395), Point(280, 425)]])
    service.shape(0, Rect(320, 390, 520, 460), AnnotationType.SQUARE,
                  style=AnnotationStyle(color=Color(0.9, 0.2, 0.2), cloud_intensity=2))
    service.stamp(0, Rect(330, 620, 520, 690), "Draft")
    service.measure_distance(1, Point(80, 120), Point(380, 120), scale=0.5, unit="cm")
    service.measure_area(1, [Point(80, 200), Point(300, 200), Point(300, 340), Point(80, 340)])

    reviewer = AnnotationService(document, author="Grace Hopper")
    reviewer.sticky_note(1, Point(450, 420), "Second reviewer: agreed.")

    document.save_as(path, SaveOptions.optimized())
    document.close()
    return path


def sample_form(path: Path) -> Path:
    """An interactive application form with every widget type."""
    document = PdfDocument.create()
    text = TextEditor(document)
    forms = FormService(document)

    text.add(0, Rect(60, 60, 535, 100), "Membership application", TextStyle(size=20, bold=True))
    labels = [
        ("Full name", "name", "text"),
        ("E-mail", "email", "text"),
        ("Date of birth", "dob", "date"),
        ("Country", "country", "dropdown"),
        ("Interests", "interests", "listbox"),
    ]
    y = 140.0
    for label, name, kind in labels:
        text.add(0, Rect(60, y, 200, y + 22), label + ":", TextStyle(size=11))
        box = Rect(210, y - 4, 500, y + 22)
        match kind:
            case "text":
                forms.create_text(name, 0, box)
            case "date":
                forms.create_date_picker(name, 0, box)
            case "dropdown":
                forms.create_dropdown(
                    name, 0, box, ["United Kingdom", "Ireland", "France", "Germany"]
                )
            case "listbox":
                forms.create_listbox(
                    name, 0, Rect(210, y - 4, 500, y + 76),
                    ["Solar", "Wind", "Hydro", "Storage"], multiselect=True,
                )
                y += 56
        y += 46

    text.add(0, Rect(60, y, 500, y + 22), "I agree to the terms:", TextStyle(size=11))
    forms.create_checkbox("agree", 0, Rect(210, y - 2, 228, y + 16))
    y += 50
    text.add(0, Rect(60, y, 500, y + 22), "Membership type:", TextStyle(size=11))
    forms.create_radio_group(
        "membership", 0,
        [("standard", Rect(210, y - 2, 228, y + 16)), ("premium", Rect(320, y - 2, 338, y + 16))],
    )
    text.add(0, Rect(232, y, 310, y + 20), "Standard", TextStyle(size=10))
    text.add(0, Rect(342, y, 420, y + 20), "Premium", TextStyle(size=10))
    y += 60
    forms.create_signature_field("signature", 0, Rect(210, y, 500, y + 70))
    text.add(0, Rect(60, y + 24, 200, y + 46), "Signature:", TextStyle(size=11))

    document.save_as(path, SaveOptions.optimized())
    document.close()
    return path


def sample_scanned(path: Path) -> Path:
    """An image-only page: the OCR test case."""
    source = PdfDocument.create()
    TextEditor(source).add(
        0,
        Rect(70, 80, 525, 400),
        "SCANNED INVOICE\n\nInvoice number: 2026-0042\nDate: 14 March 2026\n"
        "Customer: Ada Lovelace\nTotal amount due: 1250.00 GBP\n\n"
        "Payment terms: 30 days net",
        TextStyle(size=19, line_height=1.7),
    )
    with source.locked() as handle:
        pixels = handle[0].get_pixmap(dpi=150).tobytes("png")
    source.close()

    scan = PdfDocument.create()
    ImageEditor(scan).insert(0, Rect(0, 0, 595, 842), pixels)
    scan.save_as(path, SaveOptions.optimized())
    scan.close()
    return path


def sample_encrypted(path: Path) -> Path:
    """AES-256 protected, read-only permissions. Password: ``secret``."""
    document = PdfDocument.create()
    TextEditor(document).add(
        0, Rect(60, 60, 535, 200),
        "Confidential\n\nThis document is encrypted with AES-256.\n"
        "The open password is: secret",
        TextStyle(size=15, line_height=1.6),
    )
    SecurityService(document).encrypt_to(
        path,
        EncryptionSettings(
            method=EncryptionMethod.AES_256,
            user_password="secret",
            owner_password="owner",
            permissions=PermissionSet.read_only(),
        ),
    )
    document.close()
    return path


def sample_large(path: Path, pages: int = 500) -> Path:
    """A large document for performance testing."""
    document = PdfDocument.create(pages=pages)
    text = TextEditor(document)
    for index in range(0, pages, 5):
        text.add(
            index,
            Rect(60, 60, 535, 260),
            f"Page {index + 1}\nSection {index // 10}\n"
            f"Reference {index:05d}. The keyword benchmark appears on this page.",
            TextStyle(size=12, line_height=1.5),
        )
    document.set_bookmarks(
        [Bookmark(f"Section {i}", i * 10) for i in range(pages // 10)]
    )
    document.save_as(path, SaveOptions.fast())
    document.close()
    return path


def sample_attachments(path: Path) -> Path:
    """A document carrying embedded files and a hidden layer."""
    document = PdfDocument.create()
    TextEditor(document).add(
        0, Rect(60, 60, 535, 160),
        "This document has attachments.\nOpen the Attachments panel to see them.",
        TextStyle(size=14, line_height=1.6),
    )
    document.add_attachment(
        "data.csv", b"quarter,generation\nQ1,70\nQ2,110\nQ3,95\nQ4,150\n",
        description="Source data for the chart",
    )
    document.add_attachment(
        "notes.txt", b"Internal notes that should be removed before publishing.\n",
        description="Internal notes",
    )
    document.save_as(path, SaveOptions.optimized())
    document.close()
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate sample PDFs")
    parser.add_argument("--output", type=Path, default=Path(__file__).parent.parent / "samples")
    parser.add_argument("--large-pages", type=int, default=500)
    parser.add_argument("--skip-large", action="store_true")
    args = parser.parse_args()

    out = args.output
    out.mkdir(parents=True, exist_ok=True)

    builders = [
        ("report.pdf", "multi-page report with charts and bookmarks", sample_report),
        ("annotated.pdf", "every annotation type, two reviewers", sample_annotated),
        ("form.pdf", "interactive AcroForm", sample_form),
        ("scanned.pdf", "image-only page for OCR", sample_scanned),
        ("encrypted.pdf", "AES-256, password 'secret'", sample_encrypted),
        ("attachments.pdf", "embedded files", sample_attachments),
    ]
    for filename, description, builder in builders:
        target = builder(out / filename)
        size = target.stat().st_size / 1024
        print(f"  ✓ {filename:<18} {size:>7.1f} KB  {description}")

    if not args.skip_large:
        target = sample_large(out / "large.pdf", args.large_pages)
        print(
            f"  ✓ {'large.pdf':<18} {target.stat().st_size / 1024:>7.1f} KB  "
            f"{args.large_pages} pages for performance testing"
        )

    print(f"\nSamples written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
