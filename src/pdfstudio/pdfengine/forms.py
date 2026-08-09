"""AcroForm engine: create, edit, fill, validate, calculate, import/export.

Supports every widget type in ISO 32000-1: text fields (single/multi-line,
comb, password), check boxes, radio groups, combo boxes, list boxes, push
buttons, signature fields, plus higher-level helpers for date pickers,
barcodes and QR codes which are rendered as read-only widget appearances.

Data interchange is provided for FDF, XFDF, JSON and CSV so forms can be
round-tripped with Acrobat and processed in bulk.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pymupdf as fitz

from pdfstudio.core.exceptions import DependencyMissingError, ValidationError
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.undo import Command
from pdfstudio.pdfengine.content import PageSnapshotCommand
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import BLACK, Color, FieldType, FormField, Rect

log = get_logger("forms")

_WIDGET_TYPE_MAP: dict[int, FieldType] = {
    fitz.PDF_WIDGET_TYPE_TEXT: FieldType.TEXT,
    fitz.PDF_WIDGET_TYPE_CHECKBOX: FieldType.CHECKBOX,
    fitz.PDF_WIDGET_TYPE_RADIOBUTTON: FieldType.RADIO,
    fitz.PDF_WIDGET_TYPE_COMBOBOX: FieldType.COMBOBOX,
    fitz.PDF_WIDGET_TYPE_LISTBOX: FieldType.LISTBOX,
    fitz.PDF_WIDGET_TYPE_BUTTON: FieldType.PUSHBUTTON,
    fitz.PDF_WIDGET_TYPE_SIGNATURE: FieldType.SIGNATURE,
}
_REVERSE_TYPE_MAP = {v: k for k, v in _WIDGET_TYPE_MAP.items()}


@dataclass(slots=True)
class FieldStyle:
    """Appearance of a form widget."""

    font: str = "Helv"
    font_size: float = 11.0
    text_color: Color = BLACK
    fill_color: Color | None = None
    border_color: Color | None = None
    border_width: float = 1.0
    border_style: str = "S"  # S solid, D dashed, B beveled, I inset, U underline
    alignment: int = 0  # 0 left, 1 centre, 2 right


@dataclass(slots=True)
class FieldSpec:
    """Declarative description used to create a new field."""

    name: str
    type: FieldType
    page: int
    rect: Rect
    value: Any = None
    options: list[str] = field(default_factory=list)
    export_values: list[str] = field(default_factory=list)
    tooltip: str = ""
    required: bool = False
    readonly: bool = False
    multiline: bool = False
    password: bool = False
    comb: bool = False
    max_length: int = 0
    multiselect: bool = False
    editable: bool = False  # editable combobox
    style: FieldStyle = field(default_factory=FieldStyle)
    format_script: str = ""
    validate_script: str = ""
    calculate_script: str = ""
    action_script: str = ""


class CreateFieldCommand(PageSnapshotCommand):
    """Add a widget to a page."""

    def __init__(self, doc: PdfDocument, spec: FieldSpec) -> None:
        super().__init__(doc, spec.page, f"Add {spec.type.value} field")
        self.spec = spec

    def apply(self) -> None:
        spec = self.spec
        with self.doc.locked() as handle:
            page = handle[spec.page]
            widget = fitz.Widget()
            widget.field_name = spec.name
            widget.field_type = _REVERSE_TYPE_MAP[spec.type]
            widget.rect = fitz.Rect(*spec.rect)
            widget.field_label = spec.tooltip or spec.name
            widget.text_font = spec.style.font
            widget.text_fontsize = spec.style.font_size
            widget.text_color = spec.style.text_color.to_rgb_tuple()
            widget.border_width = spec.style.border_width
            widget.text_maxlen = spec.max_length
            if spec.style.fill_color:
                widget.fill_color = spec.style.fill_color.to_rgb_tuple()
            if spec.style.border_color:
                widget.border_color = spec.style.border_color.to_rgb_tuple()
            widget.text_format = 0

            flags = 0
            if spec.required:
                flags |= fitz.PDF_FIELD_IS_REQUIRED
            if spec.readonly:
                flags |= fitz.PDF_FIELD_IS_READ_ONLY
            if spec.type is FieldType.TEXT:
                if spec.multiline:
                    flags |= fitz.PDF_TX_FIELD_IS_MULTILINE
                if spec.password:
                    flags |= fitz.PDF_TX_FIELD_IS_PASSWORD
                if spec.comb:
                    flags |= fitz.PDF_TX_FIELD_IS_COMB
            if spec.type in (FieldType.COMBOBOX, FieldType.LISTBOX):
                widget.choice_values = spec.options
                if spec.editable and spec.type is FieldType.COMBOBOX:
                    flags |= fitz.PDF_CH_FIELD_IS_EDIT
                if spec.multiselect and spec.type is FieldType.LISTBOX:
                    flags |= fitz.PDF_CH_FIELD_IS_MULTI_SELECT
            widget.field_flags = flags

            if spec.value is not None:
                widget.field_value = spec.value
            if spec.type is FieldType.CHECKBOX and spec.value is None:
                widget.field_value = False
            if spec.action_script:
                widget.script = spec.action_script
            if spec.format_script:
                widget.script_format = spec.format_script
            if spec.validate_script:
                widget.script_change = spec.validate_script
            if spec.calculate_script:
                widget.script_calc = spec.calculate_script
            page.add_widget(widget)


class CreateRadioGroupCommand(PageSnapshotCommand):
    """Create a radio-button group (a parent field with kid widgets)."""

    #: ``/Ff`` bit 16 (value 32768) marks a button field as a radio group.
    RADIO_FLAG = 1 << 15
    #: Bit 15 (16384) keeps exactly one button selected at all times.
    NO_TOGGLE_OFF_FLAG = 1 << 14

    def __init__(
        self,
        doc: PdfDocument,
        name: str,
        page: int,
        options: list[tuple[str, Rect]],
        selected: str = "",
    ) -> None:
        super().__init__(doc, page, f"Add radio group “{name}”")
        self.name = name
        self.options = options
        self.selected = selected

    def apply(self) -> None:
        with self.doc.locked() as handle:
            target = handle[self.page]
            kid_xrefs: list[int] = []

            for index, (_value, rect) in enumerate(self.options):
                widget = fitz.Widget()
                # A temporary unique name avoids PyMuPDF merging the widgets.
                widget.field_name = f"{self.name}__kid{index}"
                widget.field_type = fitz.PDF_WIDGET_TYPE_CHECKBOX
                widget.rect = fitz.Rect(*rect)
                widget.field_value = False
                widget.border_width = 1
                kid_xrefs.append(target.add_widget(widget).xref)

            parent = handle.get_new_xref()
            kids = " ".join(f"{xref} 0 R" for xref in kid_xrefs)
            flags = self.RADIO_FLAG | self.NO_TOGGLE_OFF_FLAG
            chosen = self.selected if self.selected else "Off"
            handle.update_object(
                parent,
                f"<< /FT /Btn /Ff {flags} /T ({_escape(self.name)}) "
                f"/V /{_pdf_name(chosen)} /DV /{_pdf_name(chosen)} /Kids [{kids}] >>",
            )

            for xref, (value, _rect) in zip(kid_xrefs, self.options, strict=True):
                state = _pdf_name(value)
                # The kid inherits /FT and /T from the parent.
                handle.xref_set_key(xref, "Parent", f"{parent} 0 R")
                handle.xref_set_key(xref, "T", "null")
                handle.xref_set_key(xref, "FT", "null")
                handle.xref_set_key(xref, "Ff", "null")
                # Rename the "on" appearance stream to the export value so the
                # widget reports that value when selected.
                kind, appearance = handle.xref_get_key(xref, "AP/N")
                if kind == "dict":
                    for candidate in ("Yes", "On", "1"):
                        marker = f"/{candidate} "
                        if marker in appearance:
                            handle.xref_set_key(
                                xref,
                                f"AP/N/{state}",
                                appearance.split(marker)[1].split("/")[0].strip().rstrip(">"),
                            )
                            break
                handle.xref_set_key(
                    xref, "AS", f"/{state}" if value == self.selected else "/Off"
                )
                handle.xref_set_key(
                    xref, "V", f"/{state}" if value == self.selected else "/Off"
                )
            _register_acroform_field(handle, parent, replace=kid_xrefs)


def _pdf_name(value: str) -> str:
    """Escape a string for use as a PDF name (``/Foo#20Bar``)."""
    return "".join(c if c.isalnum() or c in "-_" else f"#{ord(c):02X}" for c in value) or "Off"


def _register_acroform_field(
    handle: fitz.Document, xref: int, *, replace: Sequence[int] = ()
) -> None:
    """Add ``xref`` to ``/AcroForm/Fields`` and drop the widgets it adopted."""
    try:
        root = handle.pdf_catalog()
        kind, fields = handle.xref_get_key(root, "AcroForm/Fields")
        entries = (
            []
            if kind != "array"
            else [token for token in fields.strip("[]").split(" 0 R") if token.strip()]
        )
        keep = [
            f"{int(token)} 0 R"
            for token in (t.strip() for t in entries)
            if token.isdigit() and int(token) not in set(replace)
        ]
        keep.append(f"{xref} 0 R")
        handle.xref_set_key(root, "AcroForm/Fields", "[" + " ".join(keep) + "]")
    except Exception:
        log.debug("Could not update /AcroForm/Fields for xref {}", xref)


class SetFieldValueCommand(Command):
    """Change a field's value (merges rapid keystrokes into one undo step)."""

    merge_id = 0x701

    def __init__(self, doc: PdfDocument, name: str, value: Any) -> None:
        super().__init__(f"Set “{name}”")
        self.doc = doc
        self.name = name
        self.value = value
        self._old: Any = None
        self._captured = False

    def _widget(self, handle: fitz.Document) -> tuple[fitz.Page, fitz.Widget] | None:
        for page in handle:
            for widget in page.widgets():
                if widget.field_name == self.name:
                    return page, widget
        return None

    @staticmethod
    def _assign_radio(handle: fitz.Document, name: str, value: Any) -> bool:
        """Select ``value`` in a radio group. Returns ``True`` when handled."""
        state = _pdf_name(str(value)) if value not in (None, "", False) else "Off"
        handled = False
        for page in handle:
            for widget in page.widgets():
                if (
                    widget.field_name != name
                    or widget.field_type != fitz.PDF_WIDGET_TYPE_RADIOBUTTON
                ):
                    continue
                handled = True
                kind, parent = handle.xref_get_key(widget.xref, "Parent")
                if kind == "xref":
                    handle.xref_set_key(int(parent.split()[0]), "V", f"/{state}")
                # Only the kid owning that appearance state turns on.
                appearance_kind, appearance = handle.xref_get_key(widget.xref, "AP/N")
                owns = appearance_kind == "dict" and f"/{state}" in appearance
                handle.xref_set_key(widget.xref, "AS", f"/{state}" if owns else "/Off")
        return handled

    @staticmethod
    def _assign(handle: fitz.Document, widget: fitz.Widget, value: Any) -> None:
        """Write ``value`` to a widget.

        PyMuPDF silently ignores assignment of an empty string to a text field,
        so clearing a value is done by writing ``/V`` directly before letting
        ``update()`` regenerate the appearance stream.
        """
        if value in (None, "") and widget.field_type in (
            fitz.PDF_WIDGET_TYPE_TEXT,
            fitz.PDF_WIDGET_TYPE_COMBOBOX,
            fitz.PDF_WIDGET_TYPE_LISTBOX,
        ):
            handle.xref_set_key(widget.xref, "V", "()")
            refreshed = next(
                (w for w in handle[widget.parent.number].widgets() if w.xref == widget.xref),
                None,
            )
            (refreshed or widget).update()
            return
        widget.field_value = value
        widget.update()

    def execute(self) -> None:
        with self.doc.locked() as handle:
            found = self._widget(handle)
            if found is None:
                raise ValidationError(f"No field named {self.name!r}")
            _, widget = found
            if not self._captured:
                self._old = (
                    self._radio_value(handle, self.name)
                    if widget.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON
                    else widget.field_value
                )
                self._captured = True
            if widget.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                self._assign_radio(handle, self.name, self.value)
            else:
                self._assign(handle, widget, self.value)
        self.doc.mark_modified("form-value")

    @staticmethod
    def _radio_value(handle: fitz.Document, name: str) -> str:
        """The currently selected export value of a radio group."""
        for page in handle:
            for widget in page.widgets():
                if widget.field_name == name and widget.field_value not in (None, "", "Off"):
                    return str(widget.field_value)
        return "Off"

    def undo(self) -> None:
        with self.doc.locked() as handle:
            found = self._widget(handle)
            if found is None:
                return
            _, widget = found
            if widget.field_type == fitz.PDF_WIDGET_TYPE_RADIOBUTTON:
                self._assign_radio(handle, self.name, self._old)
            else:
                self._assign(handle, widget, self._old)
        self.doc.mark_modified("form-value")

    def merge_with(self, other: Command) -> bool:
        if (
            isinstance(other, SetFieldValueCommand)
            and other.name == self.name
            and other.timestamp - self.timestamp < 1.5
        ):
            self.value = other.value
            self.timestamp = other.timestamp
            return True
        return False


class FormService:
    """All AcroForm operations for one document."""

    def __init__(self, document: PdfDocument) -> None:
        self.doc = document

    # -- inspection --------------------------------------------------------- #
    def fields(self, page: int | None = None) -> list[FormField]:
        """Every field (optionally restricted to one page)."""
        pages = [page] if page is not None else range(self.doc.page_count)
        out: list[FormField] = []
        with self.doc.locked() as handle:
            for index in pages:
                for widget in handle[index].widgets():
                    ftype = _WIDGET_TYPE_MAP.get(widget.field_type, FieldType.UNKNOWN)
                    out.append(
                        FormField(
                            name=widget.field_name or "",
                            type=ftype,
                            page=index,
                            rect=Rect(*widget.rect),
                            value=widget.field_value,
                            default_value=widget.field_value,
                            options=list(widget.choice_values or []),
                            required=bool(widget.field_flags & fitz.PDF_FIELD_IS_REQUIRED),
                            readonly=bool(widget.field_flags & fitz.PDF_FIELD_IS_READ_ONLY),
                            multiline=bool(widget.field_flags & fitz.PDF_TX_FIELD_IS_MULTILINE),
                            max_length=int(widget.text_maxlen or 0),
                            tooltip=widget.field_label or "",
                            font=widget.text_font or "Helv",
                            font_size=float(widget.text_fontsize or 0),
                            text_color=Color(*(widget.text_color or (0, 0, 0))[:3]),
                            format_script=widget.script_format or "",
                            validate_script=widget.script_change or "",
                            calculate_script=widget.script_calc or "",
                        )
                    )
        return out

    def field(self, name: str) -> FormField | None:
        return next((f for f in self.fields() if f.name == name), None)

    def names(self) -> list[str]:
        return [f.name for f in self.fields()]

    def values(self) -> dict[str, Any]:
        """Current form data as a plain dictionary.

        A radio group appears once per kid widget; the selected export value
        wins so the dictionary reflects what the user actually chose.
        """
        out: dict[str, Any] = {}
        for item in self.fields():
            if not item.name:
                continue
            already_set = item.name in out and item.type is FieldType.RADIO
            if already_set and str(item.value) in ("", "Off", "None"):
                continue
            out[item.name] = item.value
        return out

    def has_forms(self) -> bool:
        return self.doc.is_form and bool(self.fields())

    # -- creation ----------------------------------------------------------- #
    def create(self, spec: FieldSpec) -> None:
        """Add a field described by ``spec`` (undoable)."""
        if not spec.name:
            raise ValidationError("Field name is required.")
        self.doc.undo_stack.push(CreateFieldCommand(self.doc, spec))

    def create_text(
        self,
        name: str,
        page: int,
        rect: Rect,
        *,
        value: str = "",
        multiline: bool = False,
        **kwargs: Any,
    ) -> None:
        self.create(
            FieldSpec(
                name=name,
                type=FieldType.TEXT,
                page=page,
                rect=rect,
                value=value,
                multiline=multiline,
                **kwargs,
            )
        )

    def create_checkbox(
        self, name: str, page: int, rect: Rect, *, checked: bool = False, **kwargs: Any
    ) -> None:
        self.create(
            FieldSpec(
                name=name,
                type=FieldType.CHECKBOX,
                page=page,
                rect=rect,
                value=checked,
                **kwargs,
            )
        )

    def create_radio_group(
        self,
        name: str,
        page: int,
        options: Sequence[tuple[str, Rect]],
        *,
        selected: str = "",
    ) -> None:
        """Create a radio group; ``options`` is ``[(export_value, rect), …]``.

        A radio group is one *field* with several *kid* widgets sharing a
        parent, which PyMuPDF cannot express through :class:`fitz.Widget`
        alone. The kids are therefore created as check boxes and then rewired
        into a proper ``/Btn`` radio field at the object level.
        """
        if not options:
            raise ValidationError("A radio group needs at least one option.")
        self.doc.undo_stack.push(
            CreateRadioGroupCommand(self.doc, name, page, list(options), selected)
        )

    def create_dropdown(
        self,
        name: str,
        page: int,
        rect: Rect,
        options: Sequence[str],
        *,
        value: str = "",
        editable: bool = False,
        **kwargs: Any,
    ) -> None:
        self.create(
            FieldSpec(
                name=name,
                type=FieldType.COMBOBOX,
                page=page,
                rect=rect,
                options=list(options),
                value=value or (options[0] if options else ""),
                editable=editable,
                **kwargs,
            )
        )

    def create_listbox(
        self,
        name: str,
        page: int,
        rect: Rect,
        options: Sequence[str],
        *,
        multiselect: bool = False,
        **kwargs: Any,
    ) -> None:
        self.create(
            FieldSpec(
                name=name,
                type=FieldType.LISTBOX,
                page=page,
                rect=rect,
                options=list(options),
                multiselect=multiselect,
                **kwargs,
            )
        )

    def create_date_picker(
        self,
        name: str,
        page: int,
        rect: Rect,
        *,
        fmt: str = "dd/mm/yyyy",
        value: str = "",
    ) -> None:
        """Text field with Acrobat date format/keystroke scripts attached."""
        self.create(
            FieldSpec(
                name=name,
                type=FieldType.TEXT,
                page=page,
                rect=rect,
                value=value,
                tooltip=f"Date ({fmt})",
                format_script=f'AFDate_FormatEx("{fmt}");',
                validate_script=f'AFDate_KeystrokeEx("{fmt}");',
            )
        )

    def create_signature_field(self, name: str, page: int, rect: Rect) -> None:
        self.create(FieldSpec(name=name, type=FieldType.SIGNATURE, page=page, rect=rect))

    def create_button(
        self, name: str, page: int, rect: Rect, caption: str, script: str
    ) -> None:
        self.create(
            FieldSpec(
                name=name,
                type=FieldType.PUSHBUTTON,
                page=page,
                rect=rect,
                value=caption,
                action_script=script,
            )
        )

    def create_barcode(
        self,
        name: str,
        page: int,
        rect: Rect,
        data: str,
        *,
        symbology: str = "qr",
        error_correction: str = "M",
    ) -> None:
        """Render a barcode/QR code and place it as a read-only widget image.

        Requires the optional ``qrcode`` (QR) or ``python-barcode`` (1-D) package.
        """
        image = _render_barcode(data, symbology, error_correction)

        class _Barcode(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    handle[page].insert_image(fitz.Rect(*rect), stream=image)

        self.doc.undo_stack.push(_Barcode(self.doc, page, f"Add {symbology} code"))
        self.create_text(
            f"{name}_data",
            page,
            Rect(rect.x0, rect.y1, rect.x1, rect.y1 + 1),
            value=data,
            readonly=True,
        )

    # -- filling ------------------------------------------------------------ #
    def set_value(self, name: str, value: Any) -> None:
        """Set one field (undoable, merges consecutive edits)."""
        self.doc.undo_stack.push(SetFieldValueCommand(self.doc, name, value))

    def fill(self, data: dict[str, Any], *, strict: bool = False) -> int:
        """Fill many fields at once. Returns the number of fields set."""
        known = set(self.names())
        filled = 0
        with self.doc.undo_stack.macro("Fill form"):
            for name, value in data.items():
                if name not in known:
                    if strict:
                        raise ValidationError(f"Unknown field {name!r}")
                    log.warning("Skipping unknown field {!r}", name)
                    continue
                self.set_value(name, value)
                filled += 1
        return filled

    def reset(self) -> None:
        """Clear every field back to its default/empty value."""
        with self.doc.undo_stack.macro("Reset form"):
            for f in self.fields():
                if f.type is FieldType.CHECKBOX:
                    self.set_value(f.name, False)
                elif f.type in (FieldType.COMBOBOX, FieldType.LISTBOX):
                    self.set_value(f.name, f.options[0] if f.options else "")
                elif f.type is FieldType.RADIO:
                    self.set_value(f.name, "Off")
                elif f.type is FieldType.TEXT:
                    self.set_value(f.name, "")

    def flatten(self, pages: Sequence[int] | None = None) -> None:
        """Render field values into page content and remove the widgets."""
        targets = list(pages) if pages is not None else range(self.doc.page_count)
        with self.doc.undo_stack.macro("Flatten form"):
            for index in targets:

                class _Flatten(PageSnapshotCommand):
                    def apply(inner, i: int = index) -> None:  # noqa: N805
                        with self.doc.locked() as handle:
                            page = handle[i]
                            for widget in list(page.widgets()):
                                try:
                                    pix = widget._annot.get_pixmap(dpi=150, alpha=True)
                                    rect = widget.rect
                                    page.delete_widget(widget)
                                    if pix.width and pix.height:
                                        page.insert_image(rect, pixmap=pix, overlay=True)
                                except Exception:
                                    page.delete_widget(widget)

                self.doc.undo_stack.push(
                    _Flatten(self.doc, index, f"Flatten fields on page {index + 1}")
                )

    def delete_field(self, name: str) -> None:
        """Remove every widget belonging to ``name``."""
        target = self.field(name)
        if target is None:
            raise ValidationError(f"No field named {name!r}")

        class _Delete(PageSnapshotCommand):
            def apply(inner) -> None:  # noqa: N805
                with self.doc.locked() as handle:
                    page = handle[target.page]
                    for widget in list(page.widgets()):
                        if widget.field_name == name:
                            page.delete_widget(widget)

        self.doc.undo_stack.push(_Delete(self.doc, target.page, f"Delete “{name}”"))

    # -- validation & calculation ------------------------------------------- #
    def validate(self) -> list[tuple[str, str]]:
        """Check required fields, lengths and formats.

        Returns:
            ``[(field_name, problem), …]`` — empty when the form is valid.
        """
        problems: list[tuple[str, str]] = []
        for f in self.fields():
            value = f.value
            text = "" if value is None else str(value)
            if f.required and not text.strip() and f.type is not FieldType.CHECKBOX:
                problems.append((f.name, "This field is required."))
            if f.max_length and len(text) > f.max_length:
                problems.append(
                    (f.name, f"Longer than the maximum of {f.max_length} characters.")
                )
            if f.type is FieldType.COMBOBOX and f.options and text and text not in f.options:
                problems.append((f.name, f"“{text}” is not one of the allowed values."))
            looks_like_date = bool(re.match(r"^[\d]{1,4}[-/.][\d]{1,2}[-/.][\d]{1,4}$", text))
            if "AFDate" in f.format_script and text and not looks_like_date:
                problems.append((f.name, "Not a valid date."))
            if "AFNumber" in f.format_script and text:
                try:
                    float(text.replace(",", ""))
                except ValueError:
                    problems.append((f.name, "Not a valid number."))
        return problems

    def calculate(self, formulas: dict[str, str] | None = None) -> dict[str, Any]:
        """Evaluate simple field calculations.

        ``formulas`` maps a field name to an expression using other field names,
        e.g. ``{"total": "price * quantity"}``.  Only arithmetic on numeric
        fields is permitted — the expression is evaluated with an empty
        builtins namespace, so no arbitrary code can run.
        """
        env: dict[str, float] = {}
        for f in self.fields():
            try:
                env[_safe_name(f.name)] = float(str(f.value).replace(",", "") or 0)
            except (TypeError, ValueError):
                continue
        results: dict[str, Any] = {}
        for target, expression in (formulas or {}).items():
            safe_expr = expression
            for name in sorted(env, key=len, reverse=True):
                safe_expr = safe_expr.replace(name, str(env[name]))
            if not re.fullmatch(r"[\d\s+\-*/().,%]*", safe_expr):
                raise ValidationError(f"Unsafe formula for {target!r}: {expression!r}")
            try:
                value = eval(safe_expr, {"__builtins__": {}}, {})  # noqa: S307
            except Exception as exc:
                raise ValidationError(f"Cannot evaluate {expression!r}") from exc
            results[target] = value
            if target in self.names():
                self.set_value(target, f"{value:g}")
        return results

    # -- data interchange --------------------------------------------------- #
    def export_json(self, path: str | Path | None = None) -> str:
        data = json.dumps(self.values(), indent=2, default=str)
        if path:
            Path(path).write_text(data, "utf-8")
        return data

    def import_json(self, source: str | Path) -> int:
        raw = (
            Path(source).read_text("utf-8")
            if isinstance(source, Path)
            or (len(str(source)) < 4096 and Path(str(source)).exists())
            else str(source)
        )
        return self.fill(json.loads(raw))

    def export_csv(self, path: str | Path | None = None) -> str:
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["field", "type", "page", "value"])
        for f in self.fields():
            writer.writerow([f.name, f.type.value, f.page + 1, f.value])
        text = buffer.getvalue()
        if path:
            Path(path).write_text(text, "utf-8")
        return text

    def export_fdf(self, path: str | Path | None = None) -> str:
        """Export values as FDF (Acrobat's native form-data format)."""
        entries = "".join(
            f"<< /T ({_escape(name)}) /V ({_escape(str(value))}) >>\n"
            for name, value in self.values().items()
            if value not in (None, "")
        )
        doc_ref = f"/F ({_escape(str(self.doc.path or self.doc.display_name))})"
        fdf = (
            "%FDF-1.2\n1 0 obj\n<< /FDF << /Fields [\n"
            f"{entries}] {doc_ref} >> >>\nendobj\n"
            "trailer\n<< /Root 1 0 R >>\n%%EOF\n"
        )
        if path:
            Path(path).write_text(fdf, "utf-8")
        return fdf

    def import_fdf(self, source: str | Path) -> int:
        """Import values from an FDF file."""
        text = (
            Path(source).read_text("utf-8", errors="replace")
            if isinstance(source, Path)
            or (len(str(source)) < 4096 and Path(str(source)).exists())
            else str(source)
        )
        pattern = re.compile(r"/T\s*\((?P<name>[^)]*)\)\s*/V\s*\((?P<value>[^)]*)\)")
        data = {m.group("name"): m.group("value") for m in pattern.finditer(text)}
        return self.fill(data)

    def export_xfdf(self, path: str | Path | None = None) -> str:
        root = ET.Element("xfdf", xmlns="http://ns.adobe.com/xfdf/")
        fields_el = ET.SubElement(root, "fields")
        for name, value in self.values().items():
            fel = ET.SubElement(fields_el, "field", name=name)
            vel = ET.SubElement(fel, "value")
            vel.text = "" if value is None else str(value)
        text = ET.tostring(root, encoding="unicode")
        if path:
            Path(path).write_text(text, "utf-8")
        return text

    def import_xfdf(self, source: str | Path) -> int:
        text = (
            Path(source).read_text("utf-8")
            if isinstance(source, Path)
            or (len(str(source)) < 4096 and Path(str(source)).exists())
            else str(source)
        )
        root = ET.fromstring(text)
        data: dict[str, Any] = {}
        for fel in root.iter():
            if fel.tag.split("}")[-1] != "field":
                continue
            name = fel.get("name")
            vel = next((c for c in fel if c.tag.split("}")[-1] == "value"), None)
            if name:
                data[name] = vel.text if vel is not None else ""
        return self.fill(data)

    def fill_many(
        self, rows: Iterable[dict[str, Any]], output_dir: str | Path, *, prefix: str = "form"
    ) -> list[Path]:
        """Mail-merge: produce one filled PDF per row of data."""
        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        for i, row in enumerate(rows, 1):
            clone = self.doc.copy()
            FormService(clone).fill(row)
            target = out_dir / f"{prefix}-{i:04d}.pdf"
            clone.save_as(target)
            clone.close()
            written.append(target)
        log.info("Generated {} filled forms in {}", len(written), out_dir)
        return written


def _safe_name(name: str) -> str:
    return re.sub(r"\W", "_", name)


def _escape(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _render_barcode(data: str, symbology: str, error_correction: str) -> bytes:
    """Render a QR code or 1-D barcode to PNG bytes."""
    if symbology.lower() in ("qr", "qrcode"):
        try:
            import qrcode
            from qrcode.constants import (
                ERROR_CORRECT_H,
                ERROR_CORRECT_L,
                ERROR_CORRECT_M,
                ERROR_CORRECT_Q,
            )
        except ImportError as exc:
            raise DependencyMissingError("qrcode", "QR code fields") from exc
        levels = {
            "L": ERROR_CORRECT_L,
            "M": ERROR_CORRECT_M,
            "Q": ERROR_CORRECT_Q,
            "H": ERROR_CORRECT_H,
        }
        qr = qrcode.QRCode(
            error_correction=levels.get(error_correction.upper(), ERROR_CORRECT_M),
            box_size=8,
            border=2,
        )
        qr.add_data(data)
        qr.make(fit=True)
        buffer = io.BytesIO()
        qr.make_image(fill_color="black", back_color="white").save(buffer, format="PNG")
        return buffer.getvalue()

    try:
        import barcode
        from barcode.writer import ImageWriter
    except ImportError as exc:
        raise DependencyMissingError("python-barcode", "Barcode fields") from exc
    cls = barcode.get_barcode_class(symbology.lower())
    buffer = io.BytesIO()
    cls(data, writer=ImageWriter()).write(buffer)
    return buffer.getvalue()
