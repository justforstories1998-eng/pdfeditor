"""Head-less command-line interface — every core feature without a GUI.

Examples::

    pdfstudio info report.pdf
    pdfstudio merge a.pdf b.pdf -o merged.pdf
    pdfstudio split report.pdf --pages-per-file 10 -o parts/
    pdfstudio ocr scan.pdf -o searchable.pdf --lang eng+fra
    pdfstudio compress big.pdf -o small.pdf --profile screen
    pdfstudio encrypt secret.pdf -o locked.pdf --password hunter2
    pdfstudio watermark in.pdf -o out.pdf --text DRAFT --opacity 0.2
    pdfstudio search "invoice" *.pdf --regex
    pdfstudio batch in/*.pdf -o out/ --ops ocr,watermark,compress

Designed for scripting and CI: every command prints machine-readable output
with ``--json`` and returns a meaningful exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pdfstudio import APP_NAME, __version__

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


def _print(data: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, indent=2, default=str))
    elif isinstance(data, dict):
        width = max((len(str(k)) for k in data), default=0)
        for key, value in data.items():
            print(f"{str(key).ljust(width)} : {value}")
    elif isinstance(data, list):
        for item in data:
            print(item)
    else:
        print(data)


def _progress(name: str) -> Any:
    """Console progress reporting for long CLI operations."""
    from pdfstudio.core.events import Topic, bus

    state = {"last": -1}

    def handler(event: Any) -> None:
        progress = event.get("progress")
        if progress is None:
            return
        percent = progress.percent
        if percent != state["last"]:
            state["last"] = percent
            bar = "█" * (percent // 4) + "░" * (25 - percent // 4)
            sys.stderr.write(f"\r{name} [{bar}] {percent:3d}%  {progress.message[:48]:<48}")
            sys.stderr.flush()

    return bus().subscribe(Topic.JOB_PROGRESS, handler)


def _done() -> None:
    sys.stderr.write("\n")
    sys.stderr.flush()


class _Context:
    """Runs a callable with a job context so progress is reported."""

    def __init__(self, name: str, quiet: bool) -> None:
        self.name = name
        self.quiet = quiet
        self._unsubscribe: Any = None

    def __enter__(self) -> Any:
        import threading

        from pdfstudio.core.jobs import JobContext

        if not self.quiet:
            self._unsubscribe = _progress(self.name)
        return JobContext("cli", self.name, threading.Event())

    def __exit__(self, *exc: Any) -> bool:
        if self._unsubscribe:
            self._unsubscribe()
            _done()
        return False


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #
def cmd_info(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument

    with PdfDocument.open(args.input, args.password) as document:
        stats = document.statistics()
        if args.verbose:
            metadata = document.metadata()
            stats["metadata"] = metadata.as_dict()
            stats["bookmarks"] = sum(len(b.flatten()) for b in document.bookmarks())
            stats["fonts"] = sorted(
                {
                    f[3]
                    for page in range(document.page_count)
                    for f in document.raw[page].get_fonts()
                }
            )
        _print(stats, args.json)
    return EXIT_OK


def cmd_merge(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import SaveOptions
    from pdfstudio.pdfengine.pages import merge_documents

    with _Context("Merging", args.quiet) as ctx:
        merged = merge_documents(args.inputs, keep_bookmarks=not args.no_bookmarks, ctx=ctx)
    try:
        merged.save_as(args.output, SaveOptions.optimized())
    finally:
        merged.close()
    _print({"output": str(args.output), "inputs": len(args.inputs)}, args.json)
    return EXIT_OK


def cmd_split(args: argparse.Namespace) -> int:
    from pdfstudio.services.batch import split_file

    with _Context("Splitting", args.quiet) as ctx:
        written = split_file(
            args.input,
            args.output,
            pages_per_file=args.pages_per_file,
            password=args.password,
            ctx=ctx,
        )
    _print([str(p) for p in written], args.json)
    return EXIT_OK


def cmd_extract(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.pages import PageService
    from pdfstudio.pdfengine.types import parse_page_ranges

    with PdfDocument.open(args.input, args.password) as document:
        pages = parse_page_ranges(args.pages, document.page_count)
        extracted = PageService(document).extract(pages)
        extracted.save_as(args.output)
        extracted.close()
    _print({"output": str(args.output), "pages": len(pages)}, args.json)
    return EXIT_OK


def cmd_convert(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.convert import (
        Exporter,
        ExportOptions,
        Importer,
    )
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.types import ConformanceLevel

    source = Path(args.input)
    target = Path(args.output)
    fmt = args.to or target.suffix.lstrip(".").lower()

    if source.suffix.lower() != ".pdf":
        with _Context("Importing", args.quiet) as ctx:
            document = Importer().import_file(source, ctx=ctx)
        document.save_as(target)
        document.close()
        _print({"output": str(target)}, args.json)
        return EXIT_OK

    with PdfDocument.open(source, args.password) as document:
        exporter = Exporter(document)
        options = ExportOptions(dpi=args.dpi)
        with _Context(f"Exporting {fmt}", args.quiet):
            match fmt:
                case "txt" | "text":
                    target.write_text(exporter.to_text(), "utf-8")
                case "html":
                    target.write_text(exporter.to_html(), "utf-8")
                case "md" | "markdown":
                    target.write_text(exporter.to_markdown(), "utf-8")
                case "docx":
                    exporter.to_docx(target)
                case "pptx":
                    exporter.to_pptx(target)
                case "tiff" | "tif":
                    exporter.to_multipage_tiff(target, options)
                case "svg":
                    target.write_text(exporter.to_svg(0), "utf-8")
                case "png" | "jpg" | "jpeg" | "webp" | "bmp":
                    target.mkdir(parents=True, exist_ok=True)
                    exporter.to_images(target, fmt, options)  # type: ignore[arg-type]
                case "pdfa" | "pdf/a":
                    exporter.to_conformance(target, ConformanceLevel.PDF_A_2B)
                case "pdfx" | "pdf/x":
                    exporter.to_conformance(target, ConformanceLevel.PDF_X_4)
                case "pdfua" | "pdf/ua":
                    exporter.to_conformance(target, ConformanceLevel.PDF_UA_1)
                case _:
                    print(f"Unsupported target format: {fmt}", file=sys.stderr)
                    return EXIT_USAGE
    _print({"output": str(target), "format": fmt}, args.json)
    return EXIT_OK


def cmd_ocr(args: argparse.Namespace) -> int:
    from pdfstudio.core.settings import OcrSettings
    from pdfstudio.ocr.engine import OcrService
    from pdfstudio.pdfengine.document import PdfDocument

    config = OcrSettings(
        engine=args.engine,
        languages=args.lang.split("+"),
        dpi=args.dpi,
        force=args.force,
        deskew=not args.no_deskew,
        denoise=not args.no_denoise,
    )
    with PdfDocument.open(args.input, args.password) as document:
        service = OcrService(document, config)
        with _Context("OCR", args.quiet) as ctx:
            results = service.run(ctx=ctx)
        document.save_as(args.output or args.input)
        report = service.confidence_report(results)
    _print(report, args.json)
    return EXIT_OK


def cmd_compress(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.optimize import OptimizeProfile, Optimizer

    profiles = {
        "screen": OptimizeProfile.screen,
        "ebook": OptimizeProfile.ebook,
        "print": OptimizeProfile.print_quality,
        "prepress": OptimizeProfile.prepress,
        "maximum": OptimizeProfile.maximum,
    }
    with PdfDocument.open(args.input, args.password) as document:
        optimizer = Optimizer(document)
        if args.analyze:
            _print(optimizer.analyze(), args.json)
            return EXIT_OK
        with _Context("Compressing", args.quiet) as ctx:
            report = optimizer.optimize(profiles[args.profile](), output=args.output, ctx=ctx)
    _print(
        {
            "original": report.original_bytes,
            "optimized": report.optimized_bytes,
            "saved_percent": round(report.percent_saved, 1),
            "summary": report.summary(),
            "details": report.details,
        },
        args.json,
    )
    return EXIT_OK


def cmd_encrypt(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.security import (
        EncryptionSettings,
        PermissionSet,
        SecurityService,
    )
    from pdfstudio.pdfengine.types import EncryptionMethod

    with PdfDocument.open(args.input, args.open_password) as document:
        config = EncryptionSettings(
            method=EncryptionMethod(args.method),
            user_password=args.password,
            owner_password=args.owner_password or args.password,
            permissions=PermissionSet.read_only() if args.read_only else PermissionSet(),
        )
        SecurityService(document).encrypt_to(args.output, config)
    _print({"output": str(args.output), "method": args.method}, args.json)
    return EXIT_OK


def cmd_decrypt(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.security import SecurityService

    with PdfDocument.open(args.input, args.password) as document:
        SecurityService(document).decrypt_to(args.output)
    _print({"output": str(args.output)}, args.json)
    return EXIT_OK


def cmd_watermark(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
    from pdfstudio.pdfengine.security import SecurityService
    from pdfstudio.pdfengine.types import Color

    with PdfDocument.open(args.input, args.password) as document:
        service = SecurityService(document)
        if args.image:
            service.watermark_image(args.image, opacity=args.opacity)
        else:
            service.watermark_text(
                args.text,
                opacity=args.opacity,
                font_size=args.size,
                rotate=args.rotation,
                color=Color.from_hex(args.color),
                tile=args.tile,
                position=args.position,
            )
        document.save_as(args.output, SaveOptions.optimized())
    _print({"output": str(args.output)}, args.json)
    return EXIT_OK


def cmd_bates(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
    from pdfstudio.pdfengine.security import SecurityService

    with PdfDocument.open(args.input, args.password) as document:
        stamps = SecurityService(document).bates_numbering(
            prefix=args.prefix, suffix=args.suffix, start=args.start, digits=args.digits
        )
        document.save_as(args.output, SaveOptions.optimized())
    _print({"output": str(args.output), "first": stamps[0], "last": stamps[-1]}, args.json)
    return EXIT_OK


def cmd_search(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.search import SearchService
    from pdfstudio.pdfengine.types import SearchQuery

    query = SearchQuery(
        text=args.query,
        case_sensitive=args.case_sensitive,
        whole_words=args.whole_words,
        regex=args.regex,
        boolean=args.boolean,
        include_annotations=args.annotations,
        include_bookmarks=args.bookmarks,
    )
    results: list[dict[str, Any]] = []
    for path in args.inputs:
        with PdfDocument.open(path, args.password) as document:
            for hit in SearchService(document).search(query):
                results.append(
                    {
                        "file": str(path),
                        "page": hit.page + 1,
                        "source": hit.source,
                        "text": hit.text,
                        "context": hit.context,
                    }
                )
    if args.json:
        _print(results, True)
    else:
        for hit in results:
            print(f"{hit['file']}:{hit['page']}: {hit['context'] or hit['text']}")
        print(f"\n{len(results)} match(es)", file=sys.stderr)
    return EXIT_OK if results else EXIT_ERROR


def cmd_compare(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.optimize import DocumentComparer

    with PdfDocument.open(args.left) as left, PdfDocument.open(args.right) as right:
        comparer = DocumentComparer(left, right)
        with _Context("Comparing", args.quiet) as ctx:
            report = comparer.compare(ctx=ctx)
        if args.output:
            comparer.build_report_pdf(report, args.output)
        _print(report.as_dict() if args.json else report.summary(), args.json)
    return EXIT_OK if report.identical else EXIT_ERROR


def cmd_text(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument
    from pdfstudio.pdfengine.types import parse_page_ranges

    with PdfDocument.open(args.input, args.password) as document:
        pages = parse_page_ranges(args.pages, document.page_count)
        text = "\n\f\n".join(document.extract_text(p) for p in pages)
    if args.output:
        Path(args.output).write_text(text, "utf-8")
    else:
        print(text)
    return EXIT_OK


def cmd_images(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.content import ImageEditor
    from pdfstudio.pdfengine.document import PdfDocument

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    with PdfDocument.open(args.input, args.password) as document:
        for n, (info, data) in enumerate(ImageEditor(document).extract_all(), 1):
            target = out_dir / f"image-{n:04d}.{info.ext}"
            target.write_bytes(data)
            written.append(str(target))
    _print(written, args.json)
    return EXIT_OK


def cmd_forms(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
    from pdfstudio.pdfengine.forms import FormService

    with PdfDocument.open(args.input, args.password) as document:
        service = FormService(document)
        if args.fill:
            data = json.loads(Path(args.fill).read_text("utf-8"))
            count = service.fill(data)
            document.save_as(args.output or args.input, SaveOptions.optimized())
            _print({"filled": count}, args.json)
        elif args.flatten:
            service.flatten()
            document.save_as(args.output or args.input, SaveOptions.optimized())
            _print({"flattened": True}, args.json)
        else:
            _print(
                [
                    {"name": f.name, "type": f.type.value, "page": f.page + 1, "value": f.value}
                    for f in service.fields()
                ],
                args.json,
            )
    return EXIT_OK


def cmd_sanitize(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
    from pdfstudio.pdfengine.security import SecurityService

    with PdfDocument.open(args.input, args.password) as document:
        removed = SecurityService(document).sanitize(
            annotations=args.annotations, forms=args.forms
        )
        document.save_as(args.output or args.input, SaveOptions.optimized())
    _print(removed, args.json)
    return EXIT_OK


def cmd_rotate(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
    from pdfstudio.pdfengine.pages import PageService
    from pdfstudio.pdfengine.types import parse_page_ranges

    with PdfDocument.open(args.input, args.password) as document:
        pages = parse_page_ranges(args.pages, document.page_count)
        PageService(document).rotate(pages, args.degrees)
        document.save_as(args.output or args.input, SaveOptions.optimized())
    _print({"rotated": len(pages), "degrees": args.degrees}, args.json)
    return EXIT_OK


def cmd_delete_pages(args: argparse.Namespace) -> int:
    from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
    from pdfstudio.pdfengine.pages import PageService
    from pdfstudio.pdfengine.types import parse_page_ranges

    with PdfDocument.open(args.input, args.password) as document:
        pages = parse_page_ranges(args.pages, document.page_count)
        PageService(document).delete(pages)
        document.save_as(args.output or args.input, SaveOptions.optimized())
        # Read the count before the context manager closes the document.
        remaining = document.page_count
    _print({"deleted": len(pages), "remaining": remaining}, args.json)
    return EXIT_OK


def cmd_batch(args: argparse.Namespace) -> int:
    from pdfstudio.services.batch import (
        BatchProcessor,
        BatesOperation,
        CompressOperation,
        ConvertOperation,
        EncryptOperation,
        ExtractOperation,
        OcrOperation,
        RenameRule,
        SanitizeOperation,
        WatermarkOperation,
    )

    factory = {
        "ocr": lambda: OcrOperation(args.lang.split("+")),
        "watermark": lambda: WatermarkOperation(args.text),
        "compress": lambda: CompressOperation(args.profile),
        "encrypt": lambda: EncryptOperation(user_password=args.password or ""),
        "sanitize": SanitizeOperation,
        "bates": lambda: BatesOperation(prefix=args.prefix),
        "convert": lambda: ConvertOperation(args.to or "png"),
        "extract-text": lambda: ExtractOperation("text"),
        "extract-images": lambda: ExtractOperation("images"),
    }
    processor = BatchProcessor(
        output_dir=args.output, rename=RenameRule(args.rename), continue_on_error=True
    )
    for name in args.ops.split(","):
        key = name.strip().lower()
        if key not in factory:
            print(f"Unknown operation: {key}", file=sys.stderr)
            return EXIT_USAGE
        processor.add(factory[key]())

    with _Context("Batch", args.quiet) as ctx:
        result = processor.run(args.inputs, ctx=ctx)
    if args.json:
        print(result.to_json())
    else:
        print(result.summary())
        for item in result.failed:
            print(f"  FAILED {item.source.name}: {item.error}", file=sys.stderr)
    return EXIT_OK if not result.failed else EXIT_ERROR


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def _global_options(*, with_password: bool = True) -> argparse.ArgumentParser:
    """Options accepted both before and after the sub-command name."""
    shared = argparse.ArgumentParser(add_help=False)
    shared.add_argument("--json", action="store_true", help="machine-readable output")
    shared.add_argument("--quiet", "-q", action="store_true", help="suppress progress")
    if with_password:
        shared.add_argument(
            "--password", default=None, help="password for the encrypted input file"
        )
    return shared


def build_parser() -> argparse.ArgumentParser:
    shared = _global_options()
    parser = argparse.ArgumentParser(
        prog="pdfstudio",
        description=f"{APP_NAME} command-line interface",
        epilog="Run without a sub-command to launch the graphical editor.",
        parents=[shared],
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ``encrypt`` defines its own --password (the *new* one), so it opts out of
    # the shared input-password option to avoid an argparse conflict.
    shared_no_password = _global_options(with_password=False)

    def add_command(
        name: str, help_text: str, *, own_password: bool = False
    ) -> argparse.ArgumentParser:
        return subparsers.add_parser(
            name,
            help=help_text,
            parents=[shared_no_password if own_password else shared],
        )

    info = add_command("info", "show document information")
    info.add_argument("input", type=Path)
    info.add_argument("--verbose", "-v", action="store_true")
    info.set_defaults(func=cmd_info)

    merge = add_command("merge", "merge PDFs")
    merge.add_argument("inputs", nargs="+", type=Path)
    merge.add_argument("--output", "-o", required=True, type=Path)
    merge.add_argument("--no-bookmarks", action="store_true")
    merge.set_defaults(func=cmd_merge)

    split = add_command("split", "split a PDF")
    split.add_argument("input", type=Path)
    split.add_argument("--output", "-o", required=True, type=Path)
    split.add_argument("--pages-per-file", type=int, default=1)
    split.set_defaults(func=cmd_split)

    extract = add_command("extract", "extract pages")
    extract.add_argument("input", type=Path)
    extract.add_argument("--output", "-o", required=True, type=Path)
    extract.add_argument("--pages", default="all", help="e.g. 1-3,7,10-")
    extract.set_defaults(func=cmd_extract)

    convert = add_command("convert", "convert to/from PDF")
    convert.add_argument("input", type=Path)
    convert.add_argument("--output", "-o", required=True, type=Path)
    convert.add_argument("--to", default=None, help="docx, pptx, txt, html, md, png, pdfa…")
    convert.add_argument("--dpi", type=int, default=200)
    convert.set_defaults(func=cmd_convert)

    ocr = add_command("ocr", "add a searchable text layer")
    ocr.add_argument("input", type=Path)
    ocr.add_argument("--output", "-o", type=Path, default=None)
    ocr.add_argument("--lang", default="eng")
    ocr.add_argument(
        "--engine", default="auto", choices=["auto", "tesseract", "easyocr", "mupdf"]
    )
    ocr.add_argument("--dpi", type=int, default=300)
    ocr.add_argument("--force", action="store_true")
    ocr.add_argument("--no-deskew", action="store_true")
    ocr.add_argument("--no-denoise", action="store_true")
    ocr.set_defaults(func=cmd_ocr)

    compress = add_command("compress", "reduce the file size")
    compress.add_argument("input", type=Path)
    compress.add_argument("--output", "-o", type=Path, default=None)
    compress.add_argument(
        "--profile",
        default="ebook",
        choices=["screen", "ebook", "print", "prepress", "maximum"],
    )
    compress.add_argument("--analyze", action="store_true", help="report only")
    compress.set_defaults(func=cmd_compress)

    encrypt = add_command("encrypt", "password protect", own_password=True)
    encrypt.add_argument("input", type=Path)
    encrypt.add_argument("--output", "-o", required=True, type=Path)
    encrypt.add_argument("--password", required=True, help="new open password")
    encrypt.add_argument("--owner-password", default="")
    encrypt.add_argument("--open-password", default=None, help="password of the input")
    encrypt.add_argument(
        "--method", default="AES-256", choices=["AES-256", "AES-128", "RC4-128", "RC4-40"]
    )
    encrypt.add_argument("--read-only", action="store_true")
    encrypt.set_defaults(func=cmd_encrypt)

    decrypt = add_command("decrypt", "remove encryption")
    decrypt.add_argument("input", type=Path)
    decrypt.add_argument("--output", "-o", required=True, type=Path)
    decrypt.set_defaults(func=cmd_decrypt)

    watermark = add_command("watermark", "stamp a watermark")
    watermark.add_argument("input", type=Path)
    watermark.add_argument("--output", "-o", required=True, type=Path)
    watermark.add_argument("--text", default="CONFIDENTIAL")
    watermark.add_argument("--image", default=None)
    watermark.add_argument("--opacity", type=float, default=0.25)
    watermark.add_argument("--size", type=float, default=48)
    watermark.add_argument("--rotation", type=float, default=45)
    watermark.add_argument("--color", default="#999999")
    watermark.add_argument("--position", default="center")
    watermark.add_argument("--tile", action="store_true")
    watermark.set_defaults(func=cmd_watermark)

    bates = add_command("bates", "apply Bates numbering")
    bates.add_argument("input", type=Path)
    bates.add_argument("--output", "-o", required=True, type=Path)
    bates.add_argument("--prefix", default="")
    bates.add_argument("--suffix", default="")
    bates.add_argument("--start", type=int, default=1)
    bates.add_argument("--digits", type=int, default=6)
    bates.set_defaults(func=cmd_bates)

    search = add_command("search", "search documents")
    search.add_argument("query")
    search.add_argument("inputs", nargs="+", type=Path)
    search.add_argument("--regex", action="store_true")
    search.add_argument("--boolean", action="store_true")
    search.add_argument("--case-sensitive", action="store_true")
    search.add_argument("--whole-words", action="store_true")
    search.add_argument("--annotations", action="store_true")
    search.add_argument("--bookmarks", action="store_true")
    search.set_defaults(func=cmd_search)

    compare = add_command("compare", "compare two PDFs")
    compare.add_argument("left", type=Path)
    compare.add_argument("right", type=Path)
    compare.add_argument("--output", "-o", type=Path, default=None, help="report PDF")
    compare.set_defaults(func=cmd_compare)

    text = add_command("text", "extract text")
    text.add_argument("input", type=Path)
    text.add_argument("--output", "-o", type=Path, default=None)
    text.add_argument("--pages", default="all")
    text.set_defaults(func=cmd_text)

    images = add_command("images", "extract embedded images")
    images.add_argument("input", type=Path)
    images.add_argument("--output", "-o", required=True, type=Path)
    images.set_defaults(func=cmd_images)

    forms = add_command("forms", "list, fill or flatten form fields")
    forms.add_argument("input", type=Path)
    forms.add_argument("--output", "-o", type=Path, default=None)
    forms.add_argument("--fill", type=Path, default=None, help="JSON data file")
    forms.add_argument("--flatten", action="store_true")
    forms.set_defaults(func=cmd_forms)

    sanitize = add_command("sanitize", "remove hidden data")
    sanitize.add_argument("input", type=Path)
    sanitize.add_argument("--output", "-o", type=Path, default=None)
    sanitize.add_argument("--annotations", action="store_true")
    sanitize.add_argument("--forms", action="store_true")
    sanitize.set_defaults(func=cmd_sanitize)

    rotate = add_command("rotate", "rotate pages")
    rotate.add_argument("input", type=Path)
    rotate.add_argument("--output", "-o", type=Path, default=None)
    rotate.add_argument("--pages", default="all")
    rotate.add_argument("--degrees", type=int, default=90, choices=[90, 180, 270, -90])
    rotate.set_defaults(func=cmd_rotate)

    delete = add_command("delete-pages", "delete pages")
    delete.add_argument("input", type=Path)
    delete.add_argument("--output", "-o", type=Path, default=None)
    delete.add_argument("--pages", required=True)
    delete.set_defaults(func=cmd_delete_pages)

    batch = add_command("batch", "run a pipeline over many files", own_password=True)
    batch.add_argument("inputs", nargs="+", type=Path)
    batch.add_argument("--output", "-o", required=True, type=Path)
    batch.add_argument(
        "--ops",
        required=True,
        help="comma separated: ocr,watermark,compress,encrypt,sanitize,bates,convert,"
        "extract-text,extract-images",
    )
    batch.add_argument("--rename", default="{stem}")
    batch.add_argument("--text", default="CONFIDENTIAL")
    batch.add_argument("--lang", default="eng")
    batch.add_argument("--profile", default="ebook")
    batch.add_argument("--prefix", default="")
    batch.add_argument(
        "--encrypt-password",
        dest="password",
        default=None,
        help="password used by the encrypt operation",
    )
    batch.add_argument("--to", default=None)
    batch.set_defaults(func=cmd_batch)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    from pdfstudio.core.logging_setup import setup_logging

    parser = build_parser()
    args = parser.parse_args(argv)
    setup_logging(level="WARNING" if args.quiet else "INFO", console=not args.json)

    from pdfstudio.core.exceptions import PdfStudioError

    try:
        return int(args.func(args))
    except PdfStudioError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except KeyboardInterrupt:
        print("\nCancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"unexpected error: {exc}", file=sys.stderr)
        from pdfstudio.core.logging_setup import get_logger

        get_logger("cli").opt(exception=exc).error("CLI command failed")
        return EXIT_ERROR
    finally:
        from pdfstudio.core.jobs import jobs

        jobs().shutdown()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
