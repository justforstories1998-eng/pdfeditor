"""Security: encryption, permissions, sanitisation, watermarks, signatures.

Encryption uses MuPDF's writer (RC4-40/128, AES-128, AES-256).  Sanitisation
removes JavaScript, embedded files, launch actions, hidden layers, metadata and
off-page content — the "Remove hidden information" feature of Acrobat.

Digital signatures are delegated to :mod:`pyhanko` when installed, providing
PKCS#12/PKCS#11 signing, RFC 3161 timestamps and full validation.  A visible
appearance is drawn by PDF Studio so signatures look right in every viewer.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import string
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pymupdf as fitz

from pdfstudio.core.exceptions import (
    DependencyMissingError,
    PdfStudioError,
    ValidationError,
)
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.pdfengine.content import PageSnapshotCommand
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.types import (
    BLACK,
    Color,
    EncryptionMethod,
    Permission,
    Point,
    Rect,
)

log = get_logger("security")


# --------------------------------------------------------------------------- #
# Encryption & permissions
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class PermissionSet:
    """Human friendly wrapper over the PDF permission bit-field."""

    printing: bool = True
    high_quality_printing: bool = True
    modify: bool = True
    copy: bool = True
    annotate: bool = True
    fill_forms: bool = True
    accessibility: bool = True
    assemble: bool = True

    def to_bits(self) -> int:
        bits = 0
        if self.printing:
            bits |= int(Permission.PRINT)
        if self.high_quality_printing:
            bits |= int(Permission.PRINT_HIGH_RES)
        if self.modify:
            bits |= int(Permission.MODIFY)
        if self.copy:
            bits |= int(Permission.COPY)
        if self.annotate:
            bits |= int(Permission.ANNOTATE)
        if self.fill_forms:
            bits |= int(Permission.FILL_FORMS)
        if self.accessibility:
            bits |= int(Permission.ACCESSIBILITY)
        if self.assemble:
            bits |= int(Permission.ASSEMBLE)
        return bits

    @classmethod
    def read_only(cls) -> PermissionSet:
        """Allow viewing, printing and accessibility only."""
        return cls(modify=False, copy=False, annotate=False, fill_forms=False, assemble=False)

    @classmethod
    def none(cls) -> PermissionSet:
        return cls(
            printing=False,
            high_quality_printing=False,
            modify=False,
            copy=False,
            annotate=False,
            fill_forms=False,
            accessibility=False,
            assemble=False,
        )


@dataclass(slots=True)
class EncryptionSettings:
    """Everything needed to encrypt a document on save."""

    method: EncryptionMethod = EncryptionMethod.AES_256
    user_password: str = ""
    owner_password: str = ""
    permissions: PermissionSet = field(default_factory=PermissionSet)

    def to_save_options(self, base: SaveOptions | None = None) -> SaveOptions:
        opts = base or SaveOptions()
        opts.encryption = self.method
        opts.user_password = self.user_password
        opts.owner_password = self.owner_password or self.user_password
        opts.permissions = self.permissions.to_bits()
        return opts


class SecurityService:
    """Encryption, sanitisation and integrity operations for one document."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    # -- encryption --------------------------------------------------------- #
    def encrypt_to(
        self, path: str | Path, settings: EncryptionSettings, *, optimize: bool = True
    ) -> Path:
        """Save an encrypted copy to ``path``."""
        if not settings.user_password and not settings.owner_password:
            raise ValidationError("At least one password is required.")
        base = SaveOptions.optimized() if optimize else SaveOptions()
        return self.doc.save_as(path, settings.to_save_options(base))

    def decrypt_to(self, path: str | Path) -> Path:
        """Save an unencrypted copy (requires the document to be authenticated)."""
        opts = SaveOptions.optimized()
        opts.encryption = EncryptionMethod.NONE
        return self.doc.save_as(path, opts)

    def change_permissions(
        self, permissions: PermissionSet, owner_password: str
    ) -> EncryptionSettings:
        """Build settings that keep the file readable but restrict actions."""
        return EncryptionSettings(
            method=EncryptionMethod.AES_256,
            user_password="",
            owner_password=owner_password,
            permissions=permissions,
        )

    def security_report(self) -> dict[str, Any]:
        """Summary shown in the Security panel of the Properties dialog."""
        return {
            "encrypted": self.doc.is_encrypted,
            "permissions": self.doc.permissions(),
            "has_javascript": self.doc.has_javascript(),
            "attachments": len(self.doc.attachments()),
            "signed": bool(self.signature_fields()),
            "metadata_present": any(self.doc.metadata().as_dict().values()),
            "hidden_layers": [layer.name for layer in self.doc.layers() if not layer.visible],
        }

    # -- sanitisation ------------------------------------------------------- #
    def sanitize(
        self,
        *,
        javascript: bool = True,
        embedded_files: bool = True,
        metadata: bool = True,
        annotations: bool = False,
        links: bool = False,
        forms: bool = False,
        hidden_layers: bool = True,
        off_page_content: bool = True,
    ) -> dict[str, int]:
        """Remove hidden or risky content. Returns counts of what was removed."""
        removed = {
            "javascript": 0,
            "embedded_files": 0,
            "annotations": 0,
            "links": 0,
            "form_fields": 0,
            "layers": 0,
            "metadata": 0,
        }
        with self.doc.undo_stack.macro("Sanitise document"):
            if javascript and self.doc.has_javascript():
                removed["javascript"] = self._strip_javascript()
            if embedded_files:
                for att in self.doc.attachments():
                    self.doc.delete_attachment(att.name)
                    removed["embedded_files"] += 1
            if metadata:
                self.doc.clear_metadata()
                removed["metadata"] = 1
            if annotations:
                from pdfstudio.pdfengine.annotations import AnnotationService

                removed["annotations"] = AnnotationService(self.doc).delete_all()
            if links:
                removed["links"] = self._strip_links()
            if forms:
                from pdfstudio.pdfengine.forms import FormService

                service = FormService(self.doc)
                removed["form_fields"] = len(service.fields())
                service.flatten()
            if hidden_layers:
                removed["layers"] = self._remove_hidden_layers()
            if off_page_content:
                self._clip_to_page()
        log.info("Sanitised document: {}", removed)
        self.doc.mark_modified("sanitize")
        return removed

    def _strip_javascript(self) -> int:
        count = 0
        with self.doc.locked() as handle:
            catalog = handle.pdf_catalog()
            try:
                handle.xref_set_key(catalog, "Names/JavaScript", "null")
                handle.xref_set_key(catalog, "OpenAction", "null")
                handle.xref_set_key(catalog, "AA", "null")
                count += 1
            except Exception:
                log.debug("No document-level JavaScript to remove")
            for page in handle:
                try:
                    handle.xref_set_key(page.xref, "AA", "null")
                except Exception:
                    continue
                for widget in page.widgets():
                    try:
                        handle.xref_set_key(widget.xref, "A", "null")
                        handle.xref_set_key(widget.xref, "AA", "null")
                        count += 1
                    except Exception:
                        continue
        return count

    def _strip_links(self) -> int:
        count = 0
        with self.doc.locked() as handle:
            for page in handle:
                for link in page.get_links():
                    page.delete_link(link)
                    count += 1
        return count

    def _remove_hidden_layers(self) -> int:
        hidden = [layer for layer in self.doc.layers() if not layer.visible]
        for layer in hidden:
            with self.doc.locked() as handle:
                try:
                    handle.set_layer(-1, off=[layer.xref])
                except Exception:
                    continue
        return len(hidden)

    def _clip_to_page(self) -> None:
        """Set the CropBox as the MediaBox so off-page content disappears."""
        with self.doc.locked() as handle:
            for page in handle:
                crop = page.cropbox
                if crop != page.mediabox:
                    page.set_mediabox(crop)

    def remove_hidden_text(self, pages: Sequence[int] | None = None) -> int:
        """Delete invisible (render-mode 3) text used to hide information."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        removed = 0
        with self.doc.locked() as handle:
            for index in targets:
                page = handle[index]
                raw = page.get_text("rawdict")
                for block in raw.get("blocks", []):
                    for line in block.get("lines", []):
                        for span in line.get("spans", []):
                            if int(span.get("flags", 0)) & 8:  # hidden/invisible
                                page.add_redact_annot(fitz.Rect(span["bbox"]))
                                removed += 1
                if removed:
                    page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)
        if removed:
            self.doc.mark_modified("remove-hidden-text")
        return removed

    def secure_delete_file(self, path: str | Path, passes: int = 3) -> None:
        """Overwrite and unlink a file so its contents cannot be recovered."""
        target = Path(path)
        if not target.exists():
            return
        size = target.stat().st_size
        with target.open("r+b", buffering=0) as fh:
            for _ in range(passes):
                fh.seek(0)
                fh.write(secrets.token_bytes(size))
                fh.flush()
                os.fsync(fh.fileno())
            fh.seek(0)
            fh.write(b"\x00" * size)
            fh.flush()
            os.fsync(fh.fileno())
        target.unlink()
        log.info("Securely deleted {}", target)

    # -- watermarks --------------------------------------------------------- #
    def watermark_text(
        self,
        text: str,
        *,
        pages: Sequence[int] | None = None,
        font_size: float = 48.0,
        color: Color | None = None,
        opacity: float = 0.25,
        rotate: float = 45.0,
        font: str = "helv",
        position: str = "center",
        tile: bool = False,
        foreground: bool = True,
    ) -> None:
        """Stamp a text watermark on the selected pages (undoable)."""
        color = color if color is not None else Color(0.6, 0.6, 0.6)
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        with self.doc.undo_stack.macro("Add watermark"):
            for index in targets:

                class _Watermark(PageSnapshotCommand):
                    def apply(inner, i: int = index) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            rect = page.rect
                            spots = (
                                _tile_positions(rect, font_size * 6, font_size * 3)
                                if tile
                                else [_anchor(rect, position)]
                            )
                            for spot in spots:
                                _draw_rotated_text(
                                    page,
                                    text,
                                    spot,
                                    font=font,
                                    size=font_size,
                                    color=color,
                                    opacity=opacity,
                                    rotate=rotate,
                                    overlay=foreground,
                                )

                self.doc.undo_stack.push(
                    _Watermark(self.doc, index, f"Watermark page {index + 1}")
                )

    def watermark_image(
        self,
        image: bytes | str,
        *,
        pages: Sequence[int] | None = None,
        opacity: float = 0.2,
        scale: float = 0.5,
        position: str = "center",
        foreground: bool = False,
    ) -> None:
        """Stamp an image watermark (logo, "DRAFT" graphic, …)."""
        from pdfstudio.pdfengine.content import (
            ImageAdjustments,
            _load_image_bytes,
        )

        payload = ImageAdjustments(opacity=opacity).apply(_load_image_bytes(image))
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        with self.doc.undo_stack.macro("Add image watermark"):
            for index in targets:

                class _ImageWatermark(PageSnapshotCommand):
                    def apply(inner, i: int = index) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            r = page.rect
                            w, h = r.width * scale, r.height * scale
                            centre = _anchor(r, position)
                            box = fitz.Rect(
                                centre.x - w / 2,
                                centre.y - h / 2,
                                centre.x + w / 2,
                                centre.y + h / 2,
                            )
                            page.insert_image(
                                box, stream=payload, overlay=foreground, keep_proportion=True
                            )

                self.doc.undo_stack.push(
                    _ImageWatermark(self.doc, index, f"Image watermark p{index + 1}")
                )

    def bates_numbering(
        self,
        *,
        prefix: str = "",
        suffix: str = "",
        start: int = 1,
        digits: int = 6,
        position: str = "bottom-right",
        font_size: float = 9.0,
        color: Color = BLACK,
        margin: float = 28.0,
        pages: Sequence[int] | None = None,
    ) -> list[str]:
        """Apply Bates numbering; returns the stamps applied, in order."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        stamps: list[str] = []
        with self.doc.undo_stack.macro("Bates numbering"):
            for n, index in enumerate(targets):
                label = f"{prefix}{start + n:0{digits}d}{suffix}"
                stamps.append(label)

                class _Bates(PageSnapshotCommand):
                    def apply(inner, i: int = index, text: str = label) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            point = _margin_anchor(page.rect, position, margin)
                            # insert_text() draws left-to-right from the given
                            # point, so centre/right anchors must be shifted by
                            # the rendered width or the stamp runs off the page.
                            width = fitz.get_text_length(
                                text, fontname="helv", fontsize=font_size
                            )
                            x = point.x
                            if position.endswith("center"):
                                x -= width / 2
                            elif position.endswith("right"):
                                x -= width
                            page.insert_text(
                                fitz.Point(x, point.y),
                                text,
                                fontsize=font_size,
                                fontname="helv",
                                color=color.to_rgb_tuple(),
                            )

                self.doc.undo_stack.push(_Bates(self.doc, index, f"Bates stamp p{index + 1}"))
        return stamps

    def header_footer(
        self,
        *,
        header_left: str = "",
        header_center: str = "",
        header_right: str = "",
        footer_left: str = "",
        footer_center: str = "",
        footer_right: str = "",
        font_size: float = 9.0,
        color: Color = BLACK,
        margin: float = 24.0,
        pages: Sequence[int] | None = None,
        start_number: int = 1,
    ) -> None:
        """Add headers/footers supporting the tokens ``{page}``, ``{pages}``,
        ``{date}``, ``{time}``, ``{filename}``, ``{title}`` and ``{author}``."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        meta = self.doc.metadata()
        now = datetime.now()
        total = self.doc.page_count

        def expand(template: str, index: int) -> str:
            return (
                template.replace("{page}", str(start_number + index))
                .replace("{pages}", str(total))
                .replace("{date}", now.strftime("%Y-%m-%d"))
                .replace("{time}", now.strftime("%H:%M"))
                .replace("{filename}", self.doc.display_name)
                .replace("{title}", meta.title)
                .replace("{author}", meta.author)
            )

        slots = [
            ("top-left", header_left),
            ("top-center", header_center),
            ("top-right", header_right),
            ("bottom-left", footer_left),
            ("bottom-center", footer_center),
            ("bottom-right", footer_right),
        ]
        with self.doc.undo_stack.macro("Headers and footers"):
            for n, index in enumerate(targets):

                class _HF(PageSnapshotCommand):
                    def apply(inner, i: int = index, ordinal: int = n) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            for position, template in slots:
                                if not template:
                                    continue
                                text = expand(template, ordinal)
                                point = _margin_anchor(page.rect, position, margin)
                                width = fitz.get_text_length(
                                    text, fontname="helv", fontsize=font_size
                                )
                                x = point.x
                                if position.endswith("center"):
                                    x -= width / 2
                                elif position.endswith("right"):
                                    x -= width
                                page.insert_text(
                                    fitz.Point(x, point.y),
                                    text,
                                    fontsize=font_size,
                                    fontname="helv",
                                    color=color.to_rgb_tuple(),
                                )

                self.doc.undo_stack.push(_HF(self.doc, index, f"Header/footer p{index + 1}"))

    def background(
        self,
        color: Color,
        *,
        pages: Sequence[int] | None = None,
    ) -> None:
        """Paint a solid background behind existing page content."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        with self.doc.undo_stack.macro("Add background"):
            for index in targets:

                class _Background(PageSnapshotCommand):
                    def apply(inner, i: int = index) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            shape = page.new_shape()
                            shape.draw_rect(page.rect)
                            shape.finish(fill=color.to_rgb_tuple(), color=None, width=0)
                            shape.commit(overlay=False)

                self.doc.undo_stack.push(
                    _Background(self.doc, index, f"Background p{index + 1}")
                )

    # -- signatures --------------------------------------------------------- #
    def signature_fields(self) -> list[dict[str, Any]]:
        """List signature fields and whether each is signed."""
        out: list[dict[str, Any]] = []
        with self.doc.locked() as handle:
            for page in handle:
                for widget in page.widgets():
                    if widget.field_type != fitz.PDF_WIDGET_TYPE_SIGNATURE:
                        continue
                    signed = bool(widget.field_value)
                    out.append(
                        {
                            "name": widget.field_name,
                            "page": page.number,
                            "rect": Rect(*widget.rect),
                            "signed": signed,
                            "xref": widget.xref,
                        }
                    )
        return out

    def draw_signature_appearance(
        self,
        page: int,
        rect: Rect,
        *,
        name: str,
        reason: str = "",
        location: str = "",
        image: bytes | None = None,
        date: datetime | None = None,
    ) -> None:
        """Draw a visible signature block (does not sign cryptographically)."""
        stamp_date = (date or datetime.now(UTC)).strftime("%Y-%m-%d %H:%M %Z")

        class _Appearance(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    p = handle[page]
                    box = fitz.Rect(*rect)
                    shape = p.new_shape()
                    shape.draw_rect(box)
                    shape.finish(color=(0.2, 0.3, 0.6), fill=(0.96, 0.97, 1.0), width=1)
                    shape.commit()
                    if image:
                        p.insert_image(
                            fitz.Rect(
                                box.x0 + 4, box.y0 + 4, box.x0 + box.width * 0.42, box.y1 - 4
                            ),
                            stream=image,
                            keep_proportion=True,
                        )
                        text_x = box.x0 + box.width * 0.45
                    else:
                        text_x = box.x0 + 6
                    lines = [f"Signed by: {name}", f"Date: {stamp_date}"]
                    if reason:
                        lines.append(f"Reason: {reason}")
                    if location:
                        lines.append(f"Location: {location}")
                    p.insert_textbox(
                        fitz.Rect(text_x, box.y0 + 4, box.x1 - 4, box.y1 - 4),
                        "\n".join(lines),
                        fontsize=min(8.5, box.height / (len(lines) + 1)),
                        fontname="helv",
                        color=(0.1, 0.1, 0.25),
                    )

        self.doc.undo_stack.push(_Appearance(self.doc, page, "Add signature block"))

    def sign(
        self,
        output: str | Path,
        *,
        pkcs12_file: str | Path,
        pkcs12_password: str,
        field_name: str = "Signature1",
        page: int = 0,
        rect: Rect | None = None,
        reason: str = "",
        location: str = "",
        contact: str = "",
        timestamp_url: str = "",
        subfilter: Literal[
            "adbe.pkcs7.detached", "ETSI.CAdES.detached"
        ] = "ETSI.CAdES.detached",
    ) -> Path:
        """Apply a cryptographic signature using pyHanko.

        Args:
            output: Destination file (signing always writes a new revision).
            pkcs12_file: PKCS#12 (.p12/.pfx) key store holding key + chain.
            pkcs12_password: Pass-phrase protecting the key store.
            rect: Visible appearance rectangle; ``None`` signs invisibly.
            timestamp_url: RFC 3161 TSA endpoint for a trusted timestamp.

        Raises:
            DependencyMissingError: pyHanko is not installed.
        """
        try:
            from pyhanko import stamp
            from pyhanko.pdf_utils.incremental_writer import (
                IncrementalPdfFileWriter,
            )
            from pyhanko.sign import fields, signers
            from pyhanko.sign.timestamps import HTTPTimeStamper
        except ImportError as exc:
            raise DependencyMissingError("pyHanko", "Digital signatures") from exc

        source = Path(output).with_suffix(".unsigned.pdf")
        self.doc.save_as(source, SaveOptions())
        signer = signers.SimpleSigner.load_pkcs12(
            pfx_file=str(pkcs12_file), passphrase=pkcs12_password.encode()
        )
        if signer is None:
            raise PdfStudioError("Could not load the PKCS#12 key store.")

        target = Path(output)
        with source.open("rb") as fh:
            writer = IncrementalPdfFileWriter(fh)
            if rect is not None:
                fields.append_signature_field(
                    writer,
                    fields.SigFieldSpec(
                        sig_field_name=field_name,
                        on_page=page,
                        box=(rect.x0, rect.y0, rect.x1, rect.y1),
                    ),
                )
            meta = signers.PdfSignatureMetadata(
                field_name=field_name,
                reason=reason or None,
                location=location or None,
                contact_info=contact or None,
                subfilter=fields.SigSeedSubFilter(subfilter),
            )
            pdf_signer = signers.PdfSigner(
                meta,
                signer=signer,
                timestamper=HTTPTimeStamper(timestamp_url) if timestamp_url else None,
                stamp_style=stamp.TextStampStyle(
                    stamp_text="Signed by: %(signer)s\nTime: %(ts)s"
                )
                if rect is not None
                else None,
            )
            with target.open("wb") as out_fh:
                pdf_signer.sign_pdf(writer, output=out_fh)
        source.unlink(missing_ok=True)
        log.info("Signed document written to {}", target)
        return target

    def validate_signatures(self, path: str | Path | None = None) -> list[dict[str, Any]]:
        """Validate every signature; returns one report per signature."""
        try:
            from pyhanko.pdf_utils.reader import PdfFileReader
            from pyhanko.sign.validation import validate_pdf_signature
            from pyhanko_certvalidator import ValidationContext
        except ImportError as exc:
            raise DependencyMissingError("pyHanko", "Signature validation") from exc

        target = Path(path) if path else self.doc.path
        if target is None:
            raise ValidationError("Save the document before validating signatures.")
        reports: list[dict[str, Any]] = []
        context = ValidationContext(allow_fetching=False)
        with Path(target).open("rb") as fh:
            reader = PdfFileReader(fh)
            for sig in reader.embedded_signatures:
                status = validate_pdf_signature(sig, context)
                reports.append(
                    {
                        "field": sig.field_name,
                        "signer": status.signing_cert.subject.human_friendly
                        if status.signing_cert
                        else "",
                        "intact": status.intact,
                        "valid": status.valid,
                        "trusted": status.trusted,
                        "coverage": str(status.coverage),
                        "modified": not status.intact,
                        "timestamp": str(status.timestamp_validity)
                        if status.timestamp_validity
                        else "",
                        "summary": status.summary(),
                    }
                )
        return reports

    def document_hash(self, algorithm: str = "sha256") -> str:
        """Cryptographic digest of the saved bytes (integrity checking)."""
        digest = hashlib.new(algorithm)
        digest.update(self.doc.to_bytes(SaveOptions.fast()))
        return digest.hexdigest()


def generate_password(length: int = 20, *, symbols: bool = True) -> str:
    """Generate a cryptographically strong password for encryption dialogs."""
    alphabet = string.ascii_letters + string.digits + ("!@#$%^&*-_=+" if symbols else "")
    return "".join(secrets.choice(alphabet) for _ in range(length))


def password_strength(password: str) -> tuple[int, str]:
    """Score a password 0-100 with a human readable verdict."""
    score = min(40, len(password) * 3)
    score += 15 if any(c.islower() for c in password) else 0
    score += 15 if any(c.isupper() for c in password) else 0
    score += 15 if any(c.isdigit() for c in password) else 0
    score += 15 if any(not c.isalnum() for c in password) else 0
    if len(set(password)) < max(4, len(password) // 3):
        score -= 20
    score = max(0, min(100, score))
    verdict = (
        "Very weak"
        if score < 30
        else "Weak"
        if score < 50
        else "Fair"
        if score < 70
        else "Strong"
        if score < 90
        else "Very strong"
    )
    return score, verdict


# --------------------------------------------------------------------------- #
# Placement helpers
# --------------------------------------------------------------------------- #
def _anchor(rect: fitz.Rect, position: str) -> Point:
    """Centre point for a named position within ``rect``."""
    x = {
        "left": rect.x0 + rect.width * 0.25,
        "center": rect.x0 + rect.width / 2,
        "right": rect.x1 - rect.width * 0.25,
    }
    y = {
        "top": rect.y0 + rect.height * 0.25,
        "center": rect.y0 + rect.height / 2,
        "middle": rect.y0 + rect.height / 2,
        "bottom": rect.y1 - rect.height * 0.25,
    }
    parts = position.lower().split("-")
    vertical = next((p for p in parts if p in y), "center")
    horizontal = next((p for p in parts if p in x), "center")
    return Point(x[horizontal], y[vertical])


def _margin_anchor(rect: fitz.Rect, position: str, margin: float) -> Point:
    parts = position.lower().split("-")
    vertical = "top" if "top" in parts else "bottom"
    horizontal = "left" if "left" in parts else "right" if "right" in parts else "center"
    y = rect.y0 + margin if vertical == "top" else rect.y1 - margin
    x = {
        "left": rect.x0 + margin,
        "center": rect.x0 + rect.width / 2,
        "right": rect.x1 - margin,
    }[horizontal]
    return Point(x, y)


def _tile_positions(rect: fitz.Rect, step_x: float, step_y: float) -> list[Point]:
    points: list[Point] = []
    y = rect.y0 + step_y / 2
    while y < rect.y1:
        x = rect.x0 + step_x / 2
        while x < rect.x1:
            points.append(Point(x, y))
            x += step_x
        y += step_y
    return points


def _draw_rotated_text(
    page: fitz.Page,
    text: str,
    centre: Point,
    *,
    font: str,
    size: float,
    color: Color,
    opacity: float,
    rotate: float,
    overlay: bool,
) -> None:
    """Draw ``text`` rotated about ``centre`` using a text writer + morph."""
    writer = fitz.TextWriter(page.rect, opacity=opacity, color=color.to_rgb_tuple())
    font_obj = fitz.Font(font)
    width = font_obj.text_length(text, size)
    writer.append(
        fitz.Point(centre.x - width / 2, centre.y + size / 3),
        text,
        font=font_obj,
        fontsize=size,
    )
    matrix = fitz.Matrix(1, 1).prerotate(rotate)
    writer.write_text(page, morph=(fitz.Point(centre.x, centre.y), matrix), overlay=overlay)
