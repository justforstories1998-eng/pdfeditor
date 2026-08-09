"""OCR subsystem: pluggable engines, pre-processing and searchable-PDF output.

Engines
-------
``TesseractEngine``  Tesseract via ``pytesseract`` or the CLI. Best all-round
                     accuracy, 100+ languages, gives word boxes and confidence.
``EasyOCREngine``    Neural OCR with optional GPU; strong on noisy scans and
                     non-Latin scripts.
``MuPdfEngine``      MuPDF's built-in Tesseract binding — zero extra Python
                     dependencies, used as the fallback.

All engines return :class:`OcrResult` objects, so the rest of the application
never cares which one produced the text.  Recognised text is written back as an
*invisible text layer* (render mode 3) aligned to the recognised word boxes,
producing a fully searchable, selectable, copy-able PDF that looks unchanged.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import tempfile
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pymupdf as fitz

from pdfstudio.core.exceptions import DependencyMissingError, OcrError
from pdfstudio.core.jobs import JobContext
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.settings import OcrSettings
from pdfstudio.pdfengine.content import PageSnapshotCommand
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import Rect

log = get_logger("ocr")


@dataclass(slots=True)
class OcrWord:
    """One recognised word with its page-space box and confidence."""

    text: str
    rect: Rect
    confidence: float = 0.0
    line: int = 0
    block: int = 0


@dataclass(slots=True)
class OcrResult:
    """Everything an engine recognised on one page."""

    page: int
    words: list[OcrWord] = field(default_factory=list)
    text: str = ""
    engine: str = ""
    language: str = ""
    rotation: int = 0
    duration_ms: float = 0.0

    @property
    def confidence(self) -> float:
        scored = [w.confidence for w in self.words if w.confidence > 0]
        return sum(scored) / len(scored) if scored else 0.0

    @property
    def word_count(self) -> int:
        return len(self.words)


@dataclass(slots=True)
class PreprocessOptions:
    """Image clean-up applied before recognition — the biggest accuracy lever."""

    deskew: bool = True
    denoise: bool = True
    binarize: bool = False
    auto_contrast: bool = True
    remove_borders: bool = False
    scale: float = 1.0
    dpi: int = 300


class OcrEngine(ABC):
    """Interface every OCR back-end implements."""

    name: str = "engine"

    @abstractmethod
    def is_available(self) -> bool:
        """``True`` when the engine's dependencies are installed."""

    @abstractmethod
    def languages(self) -> list[str]:
        """Language codes this installation can recognise."""

    @abstractmethod
    def recognize(
        self, image: bytes, *, languages: Sequence[str], scale: float = 1.0
    ) -> list[OcrWord]:
        """Recognise ``image`` (PNG bytes); boxes are scaled by ``1/scale``."""


class TesseractEngine(OcrEngine):
    """Tesseract via ``pytesseract`` if present, otherwise the ``tesseract`` CLI."""

    name = "tesseract"

    def __init__(self, binary: str | None = None) -> None:
        self._binary = binary or shutil.which("tesseract")

    def is_available(self) -> bool:
        if self._binary:
            return True
        try:
            import pytesseract  # noqa: F401

            return True
        except ImportError:
            return False

    def languages(self) -> list[str]:
        try:
            import pytesseract

            return list(pytesseract.get_languages(config=""))
        except Exception:
            pass
        if not self._binary:
            return []
        try:
            proc = subprocess.run(  # noqa: S603
                [self._binary, "--list-langs"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            return [line.strip() for line in proc.stdout.splitlines()[1:] if line.strip()]
        except (subprocess.SubprocessError, OSError):
            return []

    def recognize(
        self, image: bytes, *, languages: Sequence[str], scale: float = 1.0
    ) -> list[OcrWord]:
        lang = "+".join(languages) or "eng"
        try:
            import pytesseract
            from PIL import Image

            data = pytesseract.image_to_data(
                Image.open(io.BytesIO(image)),
                lang=lang,
                output_type=pytesseract.Output.DICT,
            )
            return _words_from_tsv_dict(data, scale)
        except ImportError:
            pass
        except Exception as exc:
            raise OcrError("Tesseract recognition failed", detail=str(exc)) from exc

        if not self._binary:
            raise DependencyMissingError("pytesseract", "Tesseract OCR")
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "page.png"
            src.write_bytes(image)
            out_base = Path(tmpdir) / "out"
            try:
                subprocess.run(  # noqa: S603
                    [self._binary, str(src), str(out_base), "-l", lang, "tsv"],
                    check=True,
                    capture_output=True,
                    timeout=300,
                )
            except (subprocess.SubprocessError, OSError) as exc:
                raise OcrError("Tesseract CLI failed", detail=str(exc)) from exc
            tsv = out_base.with_suffix(".tsv")
            return _words_from_tsv_text(tsv.read_text("utf-8", "replace"), scale)


class EasyOCREngine(OcrEngine):
    """EasyOCR (PyTorch); supports GPU and many non-Latin scripts."""

    name = "easyocr"

    def __init__(self, *, gpu: bool = False) -> None:
        self.gpu = gpu
        self._reader: Any = None
        self._reader_langs: tuple[str, ...] = ()

    def is_available(self) -> bool:
        try:
            import easyocr  # noqa: F401

            return True
        except ImportError:
            return False

    def languages(self) -> list[str]:
        return [
            "en",
            "de",
            "fr",
            "es",
            "it",
            "pt",
            "nl",
            "pl",
            "tr",
            "ru",
            "uk",
            "ar",
            "fa",
            "hi",
            "bn",
            "ta",
            "te",
            "th",
            "vi",
            "id",
            "ms",
            "ja",
            "ko",
            "ch_sim",
            "ch_tra",
        ]

    def _get_reader(self, languages: Sequence[str]) -> Any:
        try:
            import easyocr
        except ImportError as exc:
            raise DependencyMissingError("easyocr", "EasyOCR recognition") from exc
        key = tuple(languages)
        if self._reader is None or key != self._reader_langs:
            self._reader = easyocr.Reader(list(languages) or ["en"], gpu=self.gpu)
            self._reader_langs = key
        return self._reader

    def recognize(
        self, image: bytes, *, languages: Sequence[str], scale: float = 1.0
    ) -> list[OcrWord]:
        import numpy as np

        try:
            from PIL import Image
        except ImportError as exc:
            raise DependencyMissingError("Pillow", "EasyOCR recognition") from exc

        mapped = [_map_language_easyocr(code) for code in languages] or ["en"]
        reader = self._get_reader(mapped)
        array = np.array(Image.open(io.BytesIO(image)).convert("RGB"))
        words: list[OcrWord] = []
        for n, (box, text, confidence) in enumerate(reader.readtext(array)):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            words.append(
                OcrWord(
                    text=text,
                    rect=Rect(
                        min(xs) / scale, min(ys) / scale, max(xs) / scale, max(ys) / scale
                    ),
                    confidence=float(confidence) * 100,
                    line=n,
                )
            )
        return words


class MuPdfEngine(OcrEngine):
    """MuPDF's built-in Tesseract binding (needs ``TESSDATA_PREFIX``)."""

    name = "mupdf"

    def is_available(self) -> bool:
        return bool(os.environ.get("TESSDATA_PREFIX")) and shutil.which("tesseract") is not None

    def languages(self) -> list[str]:
        return TesseractEngine().languages()

    def recognize(
        self, image: bytes, *, languages: Sequence[str], scale: float = 1.0
    ) -> list[OcrWord]:
        lang = "+".join(languages) or "eng"
        try:
            pix = fitz.Pixmap(image)
            pdf_bytes = pix.pdfocr_tobytes(language=lang)
        except Exception as exc:
            raise OcrError("MuPDF OCR failed", detail=str(exc)) from exc
        words: list[OcrWord] = []
        with fitz.open("pdf", pdf_bytes) as doc:
            for w in doc[0].get_text("words"):
                words.append(
                    OcrWord(
                        text=w[4],
                        rect=Rect(w[0] / scale, w[1] / scale, w[2] / scale, w[3] / scale),
                        confidence=0.0,
                        block=int(w[5]),
                        line=int(w[6]),
                    )
                )
        return words


def available_engines(*, gpu: bool = False) -> list[OcrEngine]:
    """All engines installed on this machine, best first."""
    candidates = [TesseractEngine(), EasyOCREngine(gpu=gpu), MuPdfEngine()]
    return [e for e in candidates if e.is_available()]


def get_engine(name: str = "auto", *, gpu: bool = False) -> OcrEngine:
    """Resolve an engine by name, or pick the best available with ``"auto"``."""
    mapping: dict[str, OcrEngine] = {
        "tesseract": TesseractEngine(),
        "easyocr": EasyOCREngine(gpu=gpu),
        "mupdf": MuPdfEngine(),
    }
    if name != "auto":
        engine = mapping.get(name)
        if engine is None:
            raise OcrError(f"Unknown OCR engine {name!r}")
        if not engine.is_available():
            raise DependencyMissingError(name, f"{name} OCR")
        return engine
    for engine in available_engines(gpu=gpu):
        return engine
    raise DependencyMissingError(
        "pytesseract (and the tesseract binary)", "Optical character recognition"
    )


# --------------------------------------------------------------------------- #
# Pre-processing
# --------------------------------------------------------------------------- #
def preprocess(image: bytes, options: PreprocessOptions) -> tuple[bytes, float]:
    """Clean an image before recognition. Returns ``(png_bytes, rotation)``."""
    try:
        import numpy as np
        from PIL import Image, ImageFilter, ImageOps
    except ImportError as exc:  # pragma: no cover
        raise DependencyMissingError("Pillow and NumPy", "OCR pre-processing") from exc

    img = Image.open(io.BytesIO(image)).convert("L")
    angle = 0.0

    if options.auto_contrast:
        img = ImageOps.autocontrast(img)
    if options.denoise:
        img = img.filter(ImageFilter.MedianFilter(3))
    if options.deskew:
        angle = _estimate_skew(np.array(img))
        if abs(angle) > 0.15:
            img = img.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    if options.remove_borders:
        img = _crop_borders(img)
    if options.binarize:
        array = np.array(img)
        threshold = _otsu_threshold(array)
        img = Image.fromarray(((array > threshold) * 255).astype("uint8"))
    if options.scale != 1.0:
        img = img.resize(
            (int(img.width * options.scale), int(img.height * options.scale)),
            Image.LANCZOS,
        )
    buffer = io.BytesIO()
    img.save(buffer, format="PNG")
    return buffer.getvalue(), angle


def _estimate_skew(array: Any, limit: float = 8.0, step: float = 0.4) -> float:
    """Estimate page skew by maximising row-variance of the projection profile."""
    import numpy as np
    from PIL import Image

    small = np.array(
        Image.fromarray(array).resize((min(720, array.shape[1]), min(720, array.shape[0])))
    )
    binary = (small < small.mean()).astype("float32")
    best_angle, best_score = 0.0, -1.0
    angle = -limit
    while angle <= limit:
        rotated = np.array(
            Image.fromarray((binary * 255).astype("uint8")).rotate(
                angle, resample=Image.NEAREST, fillcolor=0
            )
        )
        profile = rotated.sum(axis=1, dtype="float64")
        score = float(((profile[1:] - profile[:-1]) ** 2).sum())
        if score > best_score:
            best_score, best_angle = score, angle
        angle += step
    return best_angle


def _otsu_threshold(array: Any) -> int:
    """Otsu's method for automatic binarisation thresholding."""
    import numpy as np

    histogram = np.bincount(array.ravel(), minlength=256).astype("float64")
    total = histogram.sum()
    sum_total = float((np.arange(256) * histogram).sum())
    sum_bg, weight_bg, best, threshold = 0.0, 0.0, -1.0, 127
    for i in range(256):
        weight_bg += histogram[i]
        if weight_bg == 0:
            continue
        weight_fg = total - weight_bg
        if weight_fg == 0:
            break
        sum_bg += i * histogram[i]
        mean_bg = sum_bg / weight_bg
        mean_fg = (sum_total - sum_bg) / weight_fg
        variance = weight_bg * weight_fg * (mean_bg - mean_fg) ** 2
        if variance > best:
            best, threshold = variance, i
    return threshold


def _crop_borders(img: Any, tolerance: int = 24) -> Any:
    """Trim dark scanner borders."""
    import numpy as np
    from PIL import Image

    array = np.array(img)
    mask = array < (255 - tolerance)
    rows = np.where(mask.any(axis=1))[0]
    cols = np.where(mask.any(axis=0))[0]
    if rows.size == 0 or cols.size == 0:
        return img
    return Image.fromarray(array[rows[0] : rows[-1] + 1, cols[0] : cols[-1] + 1])


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #
class OcrService:
    """Runs OCR over a document and writes a searchable text layer."""

    def __init__(
        self,
        document: PdfDocument,
        settings: OcrSettings | None = None,
        *,
        engine: OcrEngine | None = None,
    ) -> None:
        self.doc = document
        self.settings = settings or OcrSettings()
        self._engine = engine

    @property
    def engine(self) -> OcrEngine:
        if self._engine is None:
            self._engine = get_engine(self.settings.engine, gpu=self.settings.gpu)
        return self._engine

    def pages_needing_ocr(self) -> list[int]:
        """Pages with no extractable text (or all pages when forcing)."""
        if self.settings.force:
            return list(range(self.doc.page_count))
        return [i for i in range(self.doc.page_count) if not self.doc.page_has_text(i)]

    def recognize_page(self, index: int) -> OcrResult:
        """OCR a single page without modifying the document."""
        import time

        started = time.perf_counter()
        scale = self.settings.dpi / 72.0
        with self.doc.locked() as handle:
            pix = handle[index].get_pixmap(dpi=self.settings.dpi, annots=False)
            image = pix.tobytes("png")

        options = PreprocessOptions(
            deskew=self.settings.deskew,
            denoise=self.settings.denoise,
            dpi=self.settings.dpi,
        )
        processed, angle = preprocess(image, options)
        words = self.engine.recognize(processed, languages=self.settings.languages, scale=scale)
        return OcrResult(
            page=index,
            words=words,
            text=" ".join(w.text for w in words),
            engine=self.engine.name,
            language="+".join(self.settings.languages),
            rotation=int(angle),
            duration_ms=(time.perf_counter() - started) * 1000,
        )

    def apply_text_layer(self, result: OcrResult, *, visible: bool = False) -> None:
        """Write recognised words onto the page as invisible, selectable text."""
        if not result.words:
            return
        doc = self.doc

        class _TextLayer(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with doc.locked() as handle:
                    page = handle[result.page]
                    writer = fitz.TextWriter(page.rect)
                    font = fitz.Font("helv")
                    for word in result.words:
                        text = word.text.strip()
                        if not text:
                            continue
                        height = max(4.0, word.rect.height * 0.82)
                        length = font.text_length(text, height) or 1.0
                        # Squeeze the glyphs to match the recognised box width.
                        size = height * min(2.5, max(0.3, word.rect.width / length))
                        writer.append(
                            fitz.Point(word.rect.x0, word.rect.y1 - height * 0.18),
                            text,
                            font=font,
                            fontsize=size,
                        )
                    writer.write_text(page, render_mode=0 if visible else 3, overlay=True)

        doc.undo_stack.push(_TextLayer(doc, result.page, f"OCR page {result.page + 1}"))

    def run(
        self,
        pages: Sequence[int] | None = None,
        *,
        ctx: JobContext | None = None,
        apply: bool = True,
    ) -> list[OcrResult]:
        """OCR the requested pages and (optionally) add the text layer.

        Returns one :class:`OcrResult` per processed page.
        """
        targets = list(pages) if pages is not None else self.pages_needing_ocr()
        if not targets:
            log.info("No pages require OCR")
            return []
        if ctx:
            ctx.set_total(len(targets))
        log.info(
            "OCR starting: {} page(s), engine={}, languages={}",
            len(targets),
            self.engine.name,
            self.settings.languages,
        )
        results: list[OcrResult] = []
        with self.doc.undo_stack.macro("OCR document"):
            for n, index in enumerate(targets, 1):
                if ctx:
                    ctx.raise_if_cancelled()
                try:
                    result = self.recognize_page(index)
                except OcrError as exc:
                    log.error("OCR failed on page {}: {}", index + 1, exc)
                    continue
                results.append(result)
                if apply:
                    self.apply_text_layer(result)
                if ctx:
                    ctx.progress(
                        n,
                        f"Page {index + 1}: {result.word_count} words "
                        f"({result.confidence:.0f}% confidence)",
                    )
        log.info(
            "OCR finished: {} page(s), {} words",
            len(results),
            sum(r.word_count for r in results),
        )
        return results

    def make_searchable(self, output: str | Path, *, ctx: JobContext | None = None) -> Path:
        """OCR every image-only page and save a searchable copy."""
        self.run(ctx=ctx)
        return self.doc.save_as(output)

    def correct_word(self, page: int, rect: Rect, corrected: str) -> None:
        """Fix an OCR mistake by replacing the invisible text in ``rect``."""
        from pdfstudio.pdfengine.content import TextEditor

        TextEditor(self.doc).replace(page, rect, corrected)

    def confidence_report(self, results: Sequence[OcrResult]) -> dict[str, Any]:
        """Quality summary shown after a batch OCR run."""
        words = [w for r in results for w in r.words]
        low = [w for w in words if 0 < w.confidence < 70]
        return {
            "pages": len(results),
            "words": len(words),
            "mean_confidence": round(sum(w.confidence for w in words) / len(words), 1)
            if words
            else 0.0,
            "low_confidence_words": len(low),
            "engine": results[0].engine if results else "",
            "suspect": [w.text for w in low[:50]],
        }


def _words_from_tsv_dict(data: dict[str, list[Any]], scale: float) -> list[OcrWord]:
    words: list[OcrWord] = []
    for i in range(len(data.get("text", []))):
        text = str(data["text"][i]).strip()
        confidence = float(data.get("conf", [0])[i] or 0)
        if not text or confidence < 0:
            continue
        left, top = float(data["left"][i]), float(data["top"][i])
        width, height = float(data["width"][i]), float(data["height"][i])
        words.append(
            OcrWord(
                text=text,
                rect=Rect(
                    left / scale, top / scale, (left + width) / scale, (top + height) / scale
                ),
                confidence=confidence,
                line=int(data.get("line_num", [0])[i] or 0),
                block=int(data.get("block_num", [0])[i] or 0),
            )
        )
    return words


def _words_from_tsv_text(tsv: str, scale: float) -> list[OcrWord]:
    words: list[OcrWord] = []
    for line in tsv.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 12:
            continue
        text = parts[11].strip()
        if not text:
            continue
        try:
            left, top, width, height = (float(parts[i]) for i in (6, 7, 8, 9))
            confidence = float(parts[10])
        except ValueError:
            continue
        words.append(
            OcrWord(
                text=text,
                rect=Rect(
                    left / scale, top / scale, (left + width) / scale, (top + height) / scale
                ),
                confidence=confidence,
                block=int(parts[2] or 0),
                line=int(parts[4] or 0),
            )
        )
    return words


def _map_language_easyocr(code: str) -> str:
    """Translate Tesseract language codes to EasyOCR's."""
    return {
        "eng": "en",
        "deu": "de",
        "fra": "fr",
        "spa": "es",
        "ita": "it",
        "por": "pt",
        "nld": "nl",
        "pol": "pl",
        "tur": "tr",
        "rus": "ru",
        "ukr": "uk",
        "ara": "ar",
        "fas": "fa",
        "hin": "hi",
        "ben": "bn",
        "tam": "ta",
        "tel": "te",
        "tha": "th",
        "vie": "vi",
        "ind": "id",
        "jpn": "ja",
        "kor": "ko",
        "chi_sim": "ch_sim",
        "chi_tra": "ch_tra",
    }.get(code, code)
