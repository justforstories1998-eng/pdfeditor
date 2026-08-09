"""Tests for the head-less command-line interface."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pdfstudio.cli import EXIT_ERROR, EXIT_OK, build_parser, main
from pdfstudio.pdfengine.document import PdfDocument


def run(*argv: str) -> int:
    """Invoke the CLI in-process and return its exit code."""
    return main(list(argv))


class TestParser:
    def test_every_command_is_registered(self) -> None:
        parser = build_parser()
        actions = [a for a in parser._actions if a.dest == "command"]
        assert actions
        names = set(actions[0].choices)
        assert {
            "info",
            "merge",
            "split",
            "extract",
            "convert",
            "ocr",
            "compress",
            "encrypt",
            "decrypt",
            "watermark",
            "search",
            "compare",
            "batch",
            "text",
            "images",
            "forms",
            "sanitize",
            "rotate",
            "delete-pages",
            "bates",
        } <= names

    def test_global_options_work_after_the_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["info", "x.pdf", "--json", "-q"])
        assert args.json and args.quiet

    def test_encrypt_password_is_the_new_password(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["encrypt", "a.pdf", "-o", "b.pdf", "--password", "pw"])
        assert args.password == "pw"


class TestCommands:
    def test_info(self, tmp_pdf: Path, capsys: pytest.CaptureFixture[str]) -> None:
        assert run("info", str(tmp_pdf), "--json", "-q") == EXIT_OK
        data = json.loads(capsys.readouterr().out)
        assert data["pages"] == 3
        assert data["encrypted"] is False

    def test_merge(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "merged.pdf"
        assert run("merge", str(tmp_pdf), str(tmp_pdf), "-o", str(target), "-q") == EXIT_OK
        with PdfDocument.open(target) as document:
            assert document.page_count == 6

    def test_split(self, tmp_pdf: Path, tmp_path: Path) -> None:
        out = tmp_path / "parts"
        assert (
            run("split", str(tmp_pdf), "-o", str(out), "--pages-per-file", "2", "-q") == EXIT_OK
        )
        assert len(list(out.glob("*.pdf"))) == 2

    def test_extract(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "ex.pdf"
        assert (
            run("extract", str(tmp_pdf), "-o", str(target), "--pages", "1,3", "-q") == EXIT_OK
        )
        with PdfDocument.open(target) as document:
            assert document.page_count == 2

    def test_rotate(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "rot.pdf"
        assert (
            run("rotate", str(tmp_pdf), "-o", str(target), "--degrees", "90", "-q") == EXIT_OK
        )
        with PdfDocument.open(target) as document:
            assert document.page_rotation(0) == 90

    def test_delete_pages(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "del.pdf"
        assert (
            run("delete-pages", str(tmp_pdf), "-o", str(target), "--pages", "2", "-q")
            == EXIT_OK
        )
        with PdfDocument.open(target) as document:
            assert document.page_count == 2

    def test_watermark(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "wm.pdf"
        assert (
            run("watermark", str(tmp_pdf), "-o", str(target), "--text", "DRAFT", "-q")
            == EXIT_OK
        )
        with PdfDocument.open(target) as document:
            assert "DRAFT" in document.extract_text(0)

    def test_bates(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "bates.pdf"
        assert (
            run("bates", str(tmp_pdf), "-o", str(target), "--prefix", "ACME-", "-q") == EXIT_OK
        )
        with PdfDocument.open(target) as document:
            assert "ACME-000001" in document.extract_text(0)

    def test_encrypt_and_decrypt(self, tmp_pdf: Path, tmp_path: Path) -> None:
        locked = tmp_path / "locked.pdf"
        assert (
            run("encrypt", str(tmp_pdf), "-o", str(locked), "--password", "pw", "-q") == EXIT_OK
        )
        with PdfDocument.open(locked, "pw") as document:
            assert document.is_encrypted

        unlocked = tmp_path / "open.pdf"
        assert (
            run("decrypt", str(locked), "-o", str(unlocked), "--password", "pw", "-q")
            == EXIT_OK
        )
        with PdfDocument.open(unlocked) as document:
            assert not document.is_encrypted

    def test_compress(self, tmp_pdf: Path, tmp_path: Path, capsys) -> None:
        target = tmp_path / "small.pdf"
        assert (
            run(
                "compress",
                str(tmp_pdf),
                "-o",
                str(target),
                "--profile",
                "screen",
                "--json",
                "-q",
            )
            == EXIT_OK
        )
        report = json.loads(capsys.readouterr().out)
        assert "summary" in report and target.exists()

    def test_compress_analyze_only(self, tmp_pdf: Path, capsys) -> None:
        assert run("compress", str(tmp_pdf), "--analyze", "--json", "-q") == EXIT_OK
        info = json.loads(capsys.readouterr().out)
        assert info["pages"] == 3

    def test_text_extraction(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "out.txt"
        assert run("text", str(tmp_pdf), "-o", str(target), "-q") == EXIT_OK
        assert "Invoice" in target.read_text("utf-8")

    def test_convert_markdown(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "out.md"
        assert run("convert", str(tmp_pdf), "-o", str(target), "-q") == EXIT_OK
        assert target.exists() and target.stat().st_size > 0

    def test_convert_images(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "images"
        assert run("convert", str(tmp_pdf), "-o", str(target), "--to", "png", "-q") == EXIT_OK
        assert len(list(target.glob("*.png"))) == 3

    def test_search_found_and_not_found(self, tmp_pdf: Path, capsys) -> None:
        assert run("search", "Invoice", str(tmp_pdf), "-q") == EXIT_OK
        assert "Invoice" in capsys.readouterr().out
        assert run("search", "zzz-not-here", str(tmp_pdf), "-q") == EXIT_ERROR

    def test_search_json(self, tmp_pdf: Path, capsys) -> None:
        assert run("search", "Invoice", str(tmp_pdf), "--json", "-q") == EXIT_OK
        results = json.loads(capsys.readouterr().out)
        assert len(results) == 3
        assert results[0]["page"] == 1

    def test_compare_identical_and_different(self, tmp_pdf: Path, tmp_path: Path) -> None:
        assert run("compare", str(tmp_pdf), str(tmp_pdf), "-q") == EXIT_OK
        modified = tmp_path / "modified.pdf"
        with PdfDocument.open(tmp_pdf) as document:
            from pdfstudio.pdfengine.pages import PageService

            PageService(document).delete([2])
            document.save_as(modified)
        assert run("compare", str(tmp_pdf), str(modified), "-q") == EXIT_ERROR

    def test_sanitize(self, tmp_pdf: Path, tmp_path: Path) -> None:
        target = tmp_path / "clean.pdf"
        assert run("sanitize", str(tmp_pdf), "-o", str(target), "-q") == EXIT_OK
        with PdfDocument.open(target) as document:
            assert document.metadata().author == ""

    def test_forms_listing(self, tmp_path: Path, capsys) -> None:
        from pdfstudio.pdfengine.forms import FormService
        from pdfstudio.pdfengine.types import Rect

        source = tmp_path / "form.pdf"
        document = PdfDocument.create()
        FormService(document).create_text("name", 0, Rect(50, 50, 250, 75), value="Ada")
        document.save_as(source)
        document.close()

        assert run("forms", str(source), "--json", "-q") == EXIT_OK
        fields = json.loads(capsys.readouterr().out)
        assert fields[0]["name"] == "name" and fields[0]["value"] == "Ada"

    def test_batch(self, tmp_pdf: Path, tmp_path: Path) -> None:
        out = tmp_path / "batch"
        assert (
            run(
                "batch",
                str(tmp_pdf),
                str(tmp_pdf),
                "-o",
                str(out),
                "--ops",
                "watermark,compress",
                "--text",
                "BATCH",
                "-q",
            )
            == EXIT_OK
        )
        produced = list(out.glob("*.pdf"))
        assert produced
        with PdfDocument.open(produced[0]) as document:
            assert "BATCH" in document.extract_text(0)

    def test_unknown_batch_operation(self, tmp_pdf: Path, tmp_path: Path) -> None:
        assert (
            run("batch", str(tmp_pdf), "-o", str(tmp_path / "x"), "--ops", "nonsense", "-q")
            == 2
        )

    def test_missing_input_file(self, capsys) -> None:
        assert run("info", "/definitely/missing.pdf", "-q") == EXIT_ERROR
        assert "error" in capsys.readouterr().err.lower()
