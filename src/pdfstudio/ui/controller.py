"""Application controller — the bridge between UI commands and engine services.

Every command identifier emitted by the ribbon, menu, toolbar or a keyboard
shortcut ends up in :meth:`DocumentController.execute`.  The controller:

* resolves the active document,
* gathers any parameters via dialogs,
* performs the work through the head-less engine services (off the GUI thread
  when it may be slow),
* pushes undoable commands, and
* asks the window to refresh the affected views.

Keeping this logic out of :class:`~pdfstudio.ui.main_window.MainWindow` means
the same operations can be driven from tests, the CLI and plugins.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QColorDialog,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMessageBox,
)

from pdfstudio.core.exceptions import (
    DependencyMissingError,
    PasswordRequiredError,
    PdfStudioError,
    ValidationError,
)
from pdfstudio.core.jobs import Job, JobContext, jobs
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.settings import settings
from pdfstudio.pdfengine.annotations import AnnotationService, AnnotationStyle
from pdfstudio.pdfengine.content import (
    ImageEditor,
    TextEditor,
    TextStyle,
)
from pdfstudio.pdfengine.convert import Exporter, ExportOptions, Importer
from pdfstudio.pdfengine.document import PdfDocument, SaveOptions
from pdfstudio.pdfengine.forms import FormService
from pdfstudio.pdfengine.optimize import (
    AccessibilityChecker,
    DocumentComparer,
    OptimizeProfile,
    Optimizer,
)
from pdfstudio.pdfengine.pages import PageService, make_booklet, merge_documents, n_up
from pdfstudio.pdfengine.search import SearchService
from pdfstudio.pdfengine.security import (
    SecurityService,
)
from pdfstudio.pdfengine.types import (
    AnnotationType,
    Bookmark,
    Color,
    ConformanceLevel,
    PageLayout,
    Point,
    Rect,
    SearchQuery,
    ZoomMode,
)

if TYPE_CHECKING:
    from pdfstudio.ui.main_window import DocumentTab, MainWindow

log = get_logger("ui.controller")

PDF_FILTER = "PDF documents (*.pdf)"
ALL_FILTER = "All supported files (*.pdf *.docx *.pptx *.xlsx *.txt *.md *.html *.rtf *.epub *.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.svg);;PDF documents (*.pdf);;All files (*)"


class DocumentController(QObject):
    """Executes application commands against the active document."""

    #: Emitted from worker threads to run a callable on the GUI thread.
    #: ``QTimer.singleShot`` cannot be used for this: a timer created in a
    #: thread without an event loop never fires, so job results were silently
    #: dropped. A queued signal connection is the correct Qt mechanism.
    _marshal = Signal(object)

    def __init__(self, window: MainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.settings = settings()
        #: Last copied page object. Separate from the system clipboard because
        #: a vector path or annotation dictionary has no text/bitmap flavour.
        self._object_clip: Any = None
        #: Style remembered by the format painter.
        self._format_clip: Any = None
        self._marshal.connect(self._run_on_gui_thread, Qt.ConnectionType.QueuedConnection)
        self._handlers: dict[str, Callable[[], Any]] = self._build_handlers()

    @Slot(object)
    def _run_on_gui_thread(self, fn: Callable[[], Any]) -> None:
        """Execute ``fn`` on the GUI thread (target of :attr:`_marshal`)."""
        try:
            fn()
        except Exception as exc:
            log.opt(exception=exc).error("Deferred callback failed")

    def call_on_gui_thread(self, fn: Callable[[], Any]) -> None:
        """Schedule ``fn`` on the GUI thread from any thread."""
        self._marshal.emit(fn)

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    @property
    def document(self) -> PdfDocument | None:
        return self.window.document

    def require_document(self) -> PdfDocument:
        document = self.window.document
        if document is None:
            raise ValidationError("Open a document first.")
        return document

    def _current_pages(self) -> list[int]:
        """Pages selected in the thumbnail panel, else the visible page."""
        selected = self.window.thumbnail_panel.selected_pages()
        if selected:
            return selected
        view = self.window.view
        return [view.current_page] if view else [0]

    def _refresh(self, page: int | None = None) -> None:
        self.window.refresh_after_edit(page)

    def _status(self, message: str) -> None:
        self.window.show_status(message)

    def _run_job(
        self,
        name: str,
        function: Callable[..., Any],
        *args: Any,
        on_done: Callable[[Any], None] | None = None,
        with_context: bool = True,
        **kwargs: Any,
    ) -> Job[Any]:
        """Run slow work off the GUI thread and marshal the result back."""
        job = jobs().submit(name, function, *args, with_context=with_context, **kwargs)

        def finished(completed: Job[Any]) -> None:
            # This runs on a worker thread — everything touching widgets must
            # be marshalled onto the GUI thread.
            if completed.future.cancelled():
                return
            error = completed.future.exception()
            if error is not None:
                self.call_on_gui_thread(lambda: self.window.show_error(error))
                return
            result = completed.future.result()
            if on_done is not None:
                self.call_on_gui_thread(lambda: on_done(result))

        job.add_done_callback(finished)
        return job

    # ------------------------------------------------------------------ #
    # Dispatch table
    # ------------------------------------------------------------------ #
    def _build_handlers(self) -> dict[str, Callable[[], Any]]:
        w = self.window
        view = lambda: w.view  # noqa: E731 - late binding is intentional
        return {
            # file
            "file.new": self.new_document,
            "file.open": self.open_dialog,
            "file.save": self.save,
            "file.save_as": self.save_as,
            "file.print": self.print_document,
            "file.close": lambda: w.close_tab(w.tabs.currentIndex()),
            # edit
            "edit.undo": self.undo,
            "edit.redo": self.redo,
            # Copy/cut/paste act on the selected object when there is one and
            # fall back to text, so one shortcut covers both.
            "edit.copy": self.smart_copy,
            "edit.cut": self.smart_cut,
            "edit.paste": self.smart_paste,
            "edit.duplicate": self.duplicate_object,
            "object.copy": lambda: self.copy_object(),
            "object.cut": lambda: self.cut_object(),
            "object.paste": lambda: self.paste_object(),
            "object.duplicate": lambda: self.duplicate_object(),
            "edit.replace": self.find_replace,
            # view
            "view.zoom_in": lambda: view() and view().zoom_in(),
            "view.zoom_out": lambda: view() and view().zoom_out(),
            "view.fit_page": lambda: view() and view().set_zoom_mode(ZoomMode.FIT_PAGE),
            "view.fit_width": lambda: view() and view().set_zoom_mode(ZoomMode.FIT_WIDTH),
            "view.rotate_left": lambda: view() and view().rotate_view(-90),
            "view.rotate_right": lambda: view() and view().rotate_view(90),
            "view.single": lambda: view() and view().set_layout(PageLayout.SINGLE),
            "view.continuous": lambda: view() and view().set_layout(PageLayout.CONTINUOUS),
            "view.facing": lambda: view() and view().set_layout(PageLayout.FACING),
            "view.book": lambda: view() and view().set_layout(PageLayout.BOOK),
            "view.presentation": self.toggle_presentation,
            "view.theme": self.choose_theme,
            # pages
            "pages.insert": lambda: self.page_action("insert-after", self._current_pages()),
            "pages.delete": lambda: self.page_action("delete", self._current_pages()),
            "pages.extract": lambda: self.page_action("extract", self._current_pages()),
            "pages.duplicate": lambda: self.page_action("duplicate", self._current_pages()),
            "pages.rotate_left": lambda: self.page_action("rotate-left", self._current_pages()),
            "pages.rotate_right": lambda: self.page_action(
                "rotate-right", self._current_pages()
            ),
            "pages.crop": lambda: self.page_action("crop", self._current_pages()),
            "pages.resize": lambda: self.page_action("resize", self._current_pages()),
            "pages.replace": self.replace_pages,
            "pages.labels": self.edit_page_labels,
            "pages.merge": self.merge_documents,
            "pages.split": self.split_document,
            "pages.nup": self.make_n_up,
            "pages.booklet": self.make_booklet,
            # annotations
            "annot.stamp": self.add_stamp,
            "annot.export": self.export_comments,
            "annot.import": self.import_comments,
            "annot.flatten": self.flatten_annotations,
            "annot.delete_all": self.delete_all_annotations,
            # content
            "content.edit_text": self.activate_text_editing,
            # arrange (move objects)
            "object.align_left": lambda: self.align_selected_object("left"),
            "object.align_center": lambda: self.align_selected_object("center"),
            "object.align_right": lambda: self.align_selected_object("right"),
            "object.align_top": lambda: self.align_selected_object("top"),
            "object.align_middle": lambda: self.align_selected_object("middle"),
            "object.align_bottom": lambda: self.align_selected_object("bottom"),
            "object.nudge_left": lambda: self.nudge_selected_object(-2.0, 0.0),
            "object.nudge_right": lambda: self.nudge_selected_object(2.0, 0.0),
            "object.nudge_up": lambda: self.nudge_selected_object(0.0, -2.0),
            "object.nudge_down": lambda: self.nudge_selected_object(0.0, 2.0),
            "object.delete": self.delete_selected_object,
            # paragraph alignment / indent of the text being edited
            "text.align_left": lambda: self.set_text_alignment("left"),
            "text.align_center": lambda: self.set_text_alignment("center"),
            "text.align_right": lambda: self.set_text_alignment("right"),
            "text.indent": lambda: self.change_wrap_indent(12.0),
            "text.outdent": lambda: self.change_wrap_indent(-12.0),
            # paragraph selection and reordering
            "text.edit_paragraph": self.edit_paragraph_at_cursor,
            "text.select_paragraph": self.select_paragraph_at_cursor,
            "text.move_line_up": lambda: self.move_line(-1),
            "text.move_line_down": lambda: self.move_line(1),
            # Word-style formatting
            "text.underline": lambda: self.toggle_text_style("underline"),
            "text.strikethrough": lambda: self.toggle_text_style("strikethrough"),
            "text.bullets": lambda: self.set_list_style("bullet"),
            "text.numbering": lambda: self.set_list_style("number"),
            "text.list_none": lambda: self.set_list_style("none"),
            "text.case_upper": lambda: self.change_case("upper"),
            "text.case_lower": lambda: self.change_case("lower"),
            "text.case_title": lambda: self.change_case("title"),
            "text.case_sentence": lambda: self.change_case("sentence"),
            "text.spacing_single": lambda: self.set_line_spacing(1.0),
            "text.spacing_15": lambda: self.set_line_spacing(1.5),
            "text.spacing_double": lambda: self.set_line_spacing(2.0),
            "text.duplicate_line": self.duplicate_line,
            "text.delete_line": self.delete_line,
            "text.copy_format": self.copy_formatting,
            "text.paste_format": self.paste_formatting,
            "review.word_count": self.show_word_count,
            "content.add_text": self.add_text_box,
            "content.add_image": self.add_image,
            "content.edit_image": self.edit_image,
            "content.link": self.add_link,
            "content.draw": lambda: self.window.dispatch("annot.rectangle"),
            # text style (applies to the live inline editor, or the next edit)
            "style.bold": lambda: self.toggle_text_style("bold"),
            "style.italic": lambda: self.toggle_text_style("italic"),
            "style.color": self.choose_text_color,
            # pages
            "pages.move": self.move_pages_dialog,
            # forms
            "form.text": lambda: self.add_form_field("text"),
            "form.checkbox": lambda: self.add_form_field("checkbox"),
            "form.radio": lambda: self.add_form_field("radio"),
            "form.dropdown": lambda: self.add_form_field("combobox"),
            "form.listbox": lambda: self.add_form_field("listbox"),
            "form.date": lambda: self.add_form_field("date"),
            "form.signature": lambda: self.add_form_field("signature"),
            "form.barcode": lambda: self.add_form_field("barcode"),
            "form.import": self.import_form_data,
            "form.export": self.export_form_data,
            "form.reset": self.reset_form,
            "form.flatten": self.flatten_form,
            "form.validate": self.validate_form,
            # security
            "secure.encrypt": self.encrypt_document,
            "secure.decrypt": self.decrypt_document,
            "secure.permissions": self.edit_permissions,
            "secure.sanitize": self.sanitize_document,
            "secure.find_redact": self.find_and_redact,
            "secure.apply_redaction": self.apply_redactions,
            "sign.sign": self.sign_document,
            "sign.validate": self.validate_signatures,
            "sign.certificates": self.manage_certificates,
            "mark.watermark": self.add_watermark,
            "mark.bates": self.add_bates,
            "mark.header_footer": self.add_header_footer,
            "mark.background": self.add_background,
            # convert
            "convert.from_file": self.import_file,
            "convert.from_images": self.import_images,
            "convert.from_scanner": self.import_from_scanner,
            "convert.to_docx": lambda: self.export_as("docx"),
            "convert.to_pptx": lambda: self.export_as("pptx"),
            "convert.to_images": lambda: self.export_as("images"),
            "convert.to_text": lambda: self.export_as("txt"),
            "convert.to_html": lambda: self.export_as("html"),
            "convert.to_markdown": lambda: self.export_as("md"),
            "convert.pdfa": lambda: self.export_conformance(ConformanceLevel.PDF_A_2B),
            "convert.pdfx": lambda: self.export_conformance(ConformanceLevel.PDF_X_4),
            "convert.pdfua": lambda: self.export_conformance(ConformanceLevel.PDF_UA_1),
            # optimise / OCR / AI
            "optimize.compress": self.compress_document,
            "optimize.analyze": self.analyze_size,
            "ocr.run": self.run_ocr,
            "ocr.settings": lambda: self.show_preferences(page="OCR"),
            "ai.summarize": self.ai_summarize,
            "ai.chat": self.ai_chat,
            "ai.translate": self.ai_translate,
            "ai.bookmarks": self.ai_bookmarks,
            "ai.tables": self.ai_tables,
            "ai.metadata": self.ai_metadata,
            "compare.documents": self.compare_documents,
            "batch.run": self.batch_process,
            "batch.merge": self.merge_documents,
            "batch.split": self.split_document,
            # accessibility
            "a11y.check": self.accessibility_check,
            "a11y.autofix": self.accessibility_fix,
            "a11y.language": self.set_language,
            "a11y.tags": self.show_tag_editor,
            "a11y.reading_order": self.show_reading_order,
            "a11y.alt_text": self.edit_alt_text,
            "a11y.read_aloud": self.read_aloud,
            # document
            "document.metadata": self.edit_metadata,
            "document.properties": self.show_properties,
            "search.highlight_all": self.highlight_search_results,
            # app
            "app.preferences": self.show_preferences,
            "plugins.manage": self.manage_plugins,
            "script.console": self.script_console,
            "script.macro": self.record_macro,
            "help.about": self.show_about,
            "help.manual": self.show_manual,
            "help.shortcuts": self.show_shortcuts,
        }

    def execute(self, command: str) -> Any:
        """Run the handler registered for ``command``."""
        handler = self._handlers.get(command)
        if handler is None:
            log.debug("Unhandled command {}", command)
            self._status(f"“{command}” is not available yet.")
            return None
        return handler()

    # ------------------------------------------------------------------ #
    # File operations
    # ------------------------------------------------------------------ #
    def new_document(self) -> None:
        document = PdfDocument.create()
        self.window.add_document(document)
        self._status("Created a new document")

    def open_dialog(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self.window, "Open", str(Path.home()), ALL_FILTER
        )
        for path in paths:
            self.open_path(Path(path))

    def open_path(
        self,
        path: str | Path,
        *,
        activate: bool = True,
        password: str | None = None,
    ) -> DocumentTab | None:
        """Open a file, converting non-PDF formats and prompting for passwords.

        Args:
            password: Supplied up-front by scripts, plugins or session restore;
                when omitted the user is prompted if the file is protected.
        """
        target = Path(path)
        for tab in self.window.tabs_list():
            if tab.document.path and tab.document.path == target.resolve():
                self.window.tabs.setCurrentWidget(tab)
                return tab
        try:
            if target.suffix.lower() == ".pdf":
                document = PdfDocument.open(target, password)
            else:
                self._status(f"Converting {target.name}…")
                document = Importer().import_file(target)
        except PasswordRequiredError:
            if self.window.suppress_dialogs:
                # Automated runs must not block on a modal prompt.
                self.window.show_status(f"{target.name} is password protected")
                return None
            password, ok = QInputDialog.getText(
                self.window,
                "Password required",
                f"“{target.name}” is protected.\n\nEnter the password:",
                QLineEdit.EchoMode.Password,
            )
            if not ok:
                return None
            try:
                document = PdfDocument.open(target, password)
            except PasswordRequiredError:
                self.window.show_error(
                    PdfStudioError("Incorrect password.", detail="Please try again.")
                )
                return None
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return None

        tab = self.window.add_document(document, activate=activate)
        position = self.window.history.position_of(target)
        if position and self.settings.data.ui.restore_session:
            tab.view.go_to_page(position[0], animate=False)
        self._status(f"Opened {document.display_name} ({document.page_count} pages)")
        return tab

    def save(self) -> bool:
        document = self.document
        if document is None:
            return False
        if document.path is None:
            return self.save_as()
        try:
            document.save(SaveOptions(incremental=False))
            self.window.refresh_after_edit()
            self._status(f"Saved {document.display_name}")
            self.window.autosave.discard(document.id)
            return True
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return False

    def save_as(self) -> bool:
        document = self.document
        if document is None:
            return False
        suggestion = str(document.path or Path.home() / document.display_name)
        path, _ = QFileDialog.getSaveFileName(self.window, "Save as", suggestion, PDF_FILTER)
        if not path:
            return False
        try:
            document.save_as(Path(path), SaveOptions.optimized())
            self.window.history.touch_recent(path, page_count=document.page_count)
            self.window.refresh_after_edit()
            self._status(f"Saved {Path(path).name}")
            return True
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return False

    def print_document(self) -> None:
        from pdfstudio.ui.dialogs.print_dialog import PrintDialog

        document = self.require_document()
        dialog = PrintDialog(document, self.window)
        dialog.exec()

    def import_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Create PDF from file", str(Path.home()), ALL_FILTER
        )
        if path:
            self.open_path(Path(path))

    def import_images(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self.window,
            "Create PDF from images",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.tif *.tiff *.bmp *.gif *.webp *.heic)",
        )
        if not paths:
            return
        document = Importer().import_images([Path(p) for p in paths])
        self.window.add_document(document)
        self._status(f"Created a PDF from {len(paths)} image(s)")

    # ------------------------------------------------------------------ #
    # Undo / clipboard
    # ------------------------------------------------------------------ #
    def undo(self) -> None:
        document = self.document
        if document and document.undo_stack.undo():
            self._refresh()
            self._status(f"Undid: {document.undo_stack.redo_label}")

    def redo(self) -> None:
        document = self.document
        if document and document.undo_stack.redo():
            self._refresh()
            self._status(f"Redid: {document.undo_stack.undo_label}")

    def paste(self) -> None:
        """Paste an image or text from the clipboard onto the current page."""
        document = self.require_document()
        view = self.window.view
        if view is None:
            return
        clipboard = QGuiApplication.clipboard()
        page = view.current_page
        width, height = document.page_size(page)
        # mimeData() is None when no clipboard is available — a headless
        # session, or a platform where another process holds it open. Calling
        # hasImage() on that crashed the command outright.
        mime = clipboard.mimeData() if clipboard is not None else None
        if mime is not None and mime.hasImage():
            from PySide6.QtCore import QBuffer

            image = clipboard.image()
            buffer = QBuffer()
            buffer.open(QBuffer.OpenModeFlag.WriteOnly)
            image.save(buffer, "PNG")
            data = bytes(buffer.data())
            box = Rect(
                72,
                72,
                min(width - 72, 72 + image.width()),
                min(height - 72, 72 + image.height()),
            )
            ImageEditor(document).insert(page, box, data)
            self._refresh(page)
            self._status("Pasted image")
        elif clipboard is not None and clipboard.text():
            TextEditor(document).add(
                page, Rect(72, 72, width - 72, 240), clipboard.text(), TextStyle()
            )
            self._refresh(page)
            self._status("Pasted text")
        else:
            self._status("Nothing to paste")

    # ------------------------------------------------------------------ #
    # Pages
    # ------------------------------------------------------------------ #
    @Slot(str, list)
    def page_action(self, action: str, pages: Sequence[int]) -> None:
        """Handle every page-level operation from the ribbon or thumbnails."""
        document = self.require_document()
        service = PageService(document)
        pages = list(pages) or self._current_pages()
        try:
            match action:
                case "delete":
                    if len(pages) >= document.page_count:
                        raise ValidationError("A document must keep at least one page.")
                    if not self.window.confirm(
                        "Delete pages",
                        f"Delete {len(pages)} page(s)? This can be undone.",
                    ):
                        return
                    service.delete(pages)
                case "rotate-left":
                    service.rotate(pages, -90)
                case "rotate-right":
                    service.rotate(pages, 90)
                case "duplicate":
                    service.duplicate(pages)
                case "insert-before":
                    service.insert_blank(min(pages))
                case "insert-after":
                    service.insert_blank(max(pages) + 1)
                case "extract":
                    extracted = service.extract(pages)
                    self.window.add_document(extracted)
                    self._status(f"Extracted {len(pages)} page(s) into a new tab")
                    return
                case "export-image":
                    directory = QFileDialog.getExistingDirectory(
                        self.window, "Export pages as images", str(Path.home())
                    )
                    if not directory:
                        return
                    written = Exporter(document).to_images(
                        directory, "png", ExportOptions(dpi=200, pages=pages)
                    )
                    self._status(f"Exported {len(written)} image(s)")
                    return
                case "crop":
                    self._crop_dialog(pages)
                    return
                case "resize":
                    size, ok = QInputDialog.getItem(
                        self.window,
                        "Resize pages",
                        "Paper size:",
                        ["A3", "A4", "A5", "Letter", "Legal", "Tabloid"],
                        1,
                        False,
                    )
                    if not ok:
                        return
                    service.resize(pages, size)
                case _:
                    log.debug("Unknown page action {}", action)
                    return
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return
        self._refresh()
        self._status(f"{action.replace('-', ' ').title()}: {len(pages)} page(s)")

    @Slot(list, int)
    def move_pages(self, pages: Sequence[int], destination: int) -> None:
        document = self.require_document()
        PageService(document).move(list(pages), destination)
        self._refresh()
        self._status(f"Moved {len(pages)} page(s)")

    def _crop_dialog(self, pages: Sequence[int]) -> None:
        document = self.require_document()
        width, height = document.page_size(pages[0])
        margin, ok = QInputDialog.getDouble(
            self.window, "Crop pages", "Margin to remove (points):", 18.0, 0.0, 400.0, 1
        )
        if not ok:
            return
        PageService(document).crop(pages, Rect(margin, margin, width - margin, height - margin))
        self._refresh()

    def replace_pages(self) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Replace with pages from", str(Path.home()), PDF_FILTER
        )
        if not path:
            return
        source = PdfDocument.open(Path(path))
        pages = self._current_pages()
        PageService(document).replace(pages[0], source, 0)
        source.close()
        self._refresh()
        self._status("Replaced page")

    def edit_page_labels(self) -> None:
        document = self.require_document()
        style, ok = QInputDialog.getItem(
            self.window,
            "Page labels",
            "Numbering style:",
            [
                "1, 2, 3 (decimal)",
                "i, ii, iii (roman)",
                "I, II, III (Roman)",
                "a, b, c (letters)",
                "A, B, C (Letters)",
            ],
            0,
            False,
        )
        if not ok:
            return
        code = {"1": "D", "i": "r", "I": "R", "a": "a", "A": "A"}[style[0]]
        document.set_page_labels(
            [{"startpage": 0, "prefix": "", "style": code, "firstpagenum": 1}]
        )
        self._refresh()
        self._status("Applied page labels")

    def merge_documents(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self.window, "Select PDFs to merge", str(Path.home()), PDF_FILTER
        )
        if len(paths) < 2:
            return

        def work(ctx: JobContext) -> PdfDocument:
            return merge_documents([Path(p) for p in paths], ctx=ctx)

        def done(document: PdfDocument) -> None:
            self.window.add_document(document)
            self._status(f"Merged {len(paths)} documents")

        self._run_job("Merging documents", work, on_done=done)

    def split_document(self) -> None:
        document = self.require_document()
        count, ok = QInputDialog.getInt(
            self.window, "Split", "Pages per file:", 1, 1, max(1, document.page_count)
        )
        if not ok:
            return
        directory = QFileDialog.getExistingDirectory(
            self.window, "Save split files to", str(Path.home())
        )
        if not directory:
            return

        def work(ctx: JobContext) -> list[Path]:
            parts = PageService(document).split_by_count(count)
            ctx.set_total(len(parts))
            written: list[Path] = []
            stem = Path(document.display_name).stem
            for n, part in enumerate(parts, 1):
                target = Path(directory) / f"{stem}-{n:03d}.pdf"
                part.save_as(target)
                part.close()
                written.append(target)
                ctx.progress(n, f"Wrote {target.name}")
            return written

        self._run_job(
            "Splitting document",
            work,
            on_done=lambda paths: self._status(f"Wrote {len(paths)} files to {directory}"),
        )

    def make_n_up(self) -> None:
        document = self.require_document()
        choice, ok = QInputDialog.getItem(
            self.window, "N-up", "Pages per sheet:", ["2", "4", "6", "9", "16"], 1, False
        )
        if not ok:
            return
        grid = {"2": (2, 1), "4": (2, 2), "6": (3, 2), "9": (3, 3), "16": (4, 4)}[choice]
        result = n_up(document, grid[0], grid[1], landscape=grid[0] > grid[1])
        self.window.add_document(result)
        self._status(f"Created a {choice}-up document")

    def make_booklet(self) -> None:
        document = self.require_document()
        self.window.add_document(make_booklet(document))
        self._status("Created booklet imposition")

    # ------------------------------------------------------------------ #
    # Annotations
    # ------------------------------------------------------------------ #
    @Slot(object)
    def create_annotation(self, payload: dict[str, Any]) -> None:
        """Create the annotation the canvas just described."""
        document = self.require_document()
        service = AnnotationService(document, author=self._author())
        kind = payload.get("type")
        page = int(payload.get("page", 0))
        style = AnnotationStyle(author=self._author())
        try:
            match kind:
                case (
                    AnnotationType.HIGHLIGHT
                    | AnnotationType.UNDERLINE
                    | AnnotationType.STRIKEOUT
                ):
                    quads = payload.get("quads") or []
                    if not quads:
                        return
                    service.markup(page, quads, kind, style=style)
                case AnnotationType.TEXT:
                    text, ok = QInputDialog.getMultiLineText(
                        self.window, "Sticky note", "Comment:"
                    )
                    if not ok or not text:
                        return
                    service.sticky_note(page, payload["point"], text, style=style)
                case AnnotationType.FREE_TEXT:
                    text, ok = QInputDialog.getMultiLineText(self.window, "Text box", "Text:")
                    if not ok or not text:
                        return
                    service.free_text(page, payload["rect"], text, style=style)
                case AnnotationType.INK:
                    service.ink(page, payload["strokes"], style=style)
                case AnnotationType.SQUARE:
                    service.shape(page, payload["rect"], AnnotationType.SQUARE, style=style)
                case AnnotationType.CIRCLE:
                    service.shape(page, payload["rect"], AnnotationType.CIRCLE, style=style)
                case AnnotationType.LINE:
                    service.line(
                        page,
                        payload["start"],
                        payload["end"],
                        style=style,
                        arrow=bool(payload.get("arrow")),
                    )
                case AnnotationType.DISTANCE:
                    unit = self.settings.data.ui.units
                    factor = {"mm": 25.4 / 72, "cm": 2.54 / 72, "in": 1 / 72, "pt": 1.0}[unit]
                    value = service.measure_distance(
                        page, payload["start"], payload["end"], scale=factor, unit=unit
                    )
                    self._status(f"Distance: {value:.2f} {unit}")
                case AnnotationType.REDACT:
                    service.mark_redaction(page, payload["rect"])
                    self._status("Marked for redaction — use Protect ▸ Apply to remove")
                case "crop":
                    PageService(document).crop([page], payload["rect"])
                case _:
                    return
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return
        self._refresh(page)

    @Slot(int, object, object, bool)
    def edit_text_block(
        self,
        page: int,
        rect: Rect | None,
        point: Point | None = None,
        whole_paragraph: bool = False,
    ) -> None:
        """Start editing text **on the page** (no dialog).

        Resolves the single line under ``point`` — or the whole paragraph when
        ``whole_paragraph`` is set — and opens an inline editor over it. A
        click on empty space starts a new text box there instead.
        """
        document = self.require_document()
        view = self.window.view
        if view is None:
            return

        text_editor = TextEditor(document)
        target_rect: Rect
        current = ""
        style = TextStyle()
        new_text = False

        if point is not None:
            region = text_editor.edit_region(page, point, whole_paragraph=whole_paragraph)
            if region is None:
                if rect is None:
                    # Empty space: offer a fresh text box the width of the margin.
                    width, _height = document.page_size(page)
                    target_rect = Rect(
                        point.x, point.y - 7, min(width - 54, point.x + 320), point.y + 15
                    )
                    new_text = True
                else:
                    target_rect = rect
            else:
                target_rect, current, style = region
        elif rect is not None:
            spans = text_editor.spans_in(page, rect)
            current = " ".join(s.text for s in spans).strip()
            style = text_editor.style_at(page, rect.center) or TextStyle()
            target_rect = rect
        else:
            return

        # Let the paragraph reflow into whatever room is free underneath.
        from pdfstudio.pdfengine.content import _free_space_below

        max_height = max(
            target_rect.height,
            _free_space_below(document, page, target_rect) - target_rect.y0,
        )

        inline = view.begin_inline_edit(
            page,
            target_rect,
            current,
            style,
            caret_at=point if not new_text else None,
            select_all=whole_paragraph and bool(current),
            max_height=max_height,
            # Nothing to hide when starting a fresh box on empty space.
            mask_existing=not new_text,
        )
        if inline is None:
            return

        def commit(text: str, edited_style: TextStyle) -> None:
            view.finish_inline_edit(commit=False)
            if new_text:
                if text.strip():
                    text_editor.add(page, target_rect, text, edited_style)
            else:
                text_editor.replace(page, target_rect, text, edited_style)
            self._refresh(page)
            self._status("Text updated")

        def cancelled() -> None:
            view.finish_inline_edit(commit=False)
            view.refresh(page)

        inline.committed.connect(commit)
        inline.cancelled.connect(cancelled)
        self._status("Editing text — Enter to apply, Shift+Enter for a new line, Esc to cancel")

    @Slot(str, object)
    def comment_action(self, action: str, annotation: Any) -> None:
        document = self.require_document()
        service = AnnotationService(document, author=self._author())
        xref = annotation.extra.get("xref", 0)
        match action:
            case "goto":
                self.window._goto_point(
                    annotation.page, Point(annotation.rect.x0, annotation.rect.y0)
                )
                return
            case "reply":
                text, ok = QInputDialog.getMultiLineText(self.window, "Reply", "Reply:")
                if ok and text:
                    service.reply(annotation.page, xref, text)
            case "edit":
                text, ok = QInputDialog.getMultiLineText(
                    self.window, "Edit comment", "Text:", annotation.contents
                )
                if ok:
                    service.update(annotation.page, xref, content=text)
            case "resolve":
                service.resolve(annotation.page, xref, resolved=True)
            case "unresolve":
                service.resolve(annotation.page, xref, resolved=False)
            case "copy":
                QGuiApplication.clipboard().setText(annotation.contents)
                self._status("Comment copied")
                return
            case "delete":
                service.delete(annotation.page, [xref])
            case _:
                return
        self._refresh(annotation.page)

    def add_stamp(self) -> None:
        from pdfstudio.pdfengine.annotations import STANDARD_STAMPS

        document = self.require_document()
        view = self.window.view
        name, ok = QInputDialog.getItem(
            self.window, "Stamp", "Choose a stamp:", list(STANDARD_STAMPS), 0, False
        )
        if not ok or view is None:
            return
        page = view.current_page
        width, _height = document.page_size(page)
        AnnotationService(document, author=self._author()).stamp(
            page, Rect(width - 260, 60, width - 60, 120), name
        )
        self._refresh(page)

    def export_comments(self) -> None:
        document = self.require_document()
        path, selected = QFileDialog.getSaveFileName(
            self.window,
            "Export comments",
            str(Path.home() / f"{Path(document.display_name).stem}-comments.xfdf"),
            "XFDF (*.xfdf);;JSON (*.json)",
        )
        if not path:
            return
        service = AnnotationService(document)
        if path.lower().endswith(".json") or "JSON" in selected:
            service.export_json(path)
        else:
            service.export_xfdf(path)
        self._status(f"Exported comments to {Path(path).name}")

    def import_comments(self) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Import comments", str(Path.home()), "Comments (*.xfdf *.json)"
        )
        if not path:
            return
        service = AnnotationService(document, author=self._author())
        count = (
            service.import_json(Path(path))
            if path.lower().endswith(".json")
            else service.import_xfdf(Path(path))
        )
        self._refresh()
        self._status(f"Imported {count} comment(s)")

    def flatten_annotations(self) -> None:
        document = self.require_document()
        if not self.window.confirm(
            "Flatten comments",
            "Comments will be merged into the page content and become uneditable.\n\nContinue?",
        ):
            return
        AnnotationService(document).flatten()
        self._refresh()
        self._status("Comments flattened")

    def delete_all_annotations(self) -> None:
        document = self.require_document()
        if not self.window.confirm("Delete comments", "Delete every comment in this document?"):
            return
        removed = AnnotationService(document).delete_all()
        self._refresh()
        self._status(f"Deleted {removed} comment(s)")

    def highlight_search_results(self) -> None:
        document = self.require_document()
        query = self.window.search_panel.build_query()
        service = SearchService(document)
        hits = service.search(query)
        service.highlight_all(hits)
        self._refresh()
        self._status(f"Highlighted {len(hits)} result(s)")

    # ------------------------------------------------------------------ #
    # Content
    # ------------------------------------------------------------------ #
    def activate_text_editing(self) -> None:
        """Turn on the Edit Text tool (Edit ▸ Edit text).

        The ribbon has two entries that read "Edit text": the tool toggle on
        the Home tab and this button on the Edit tab. Both must arm the same
        tool, otherwise clicking the Edit-tab one appears to do nothing.
        """
        from pdfstudio.ui.widgets.page_view import Tool

        view = self.window.view
        if view is None:
            self._status("Open a document to edit its text.")
            return
        view.set_tool(Tool.EDIT_TEXT)
        self.window.ribbon.set_checked("tool.edit_text", True)
        self._status(
            "Edit text: click any text to change it in place "
            "(Alt+click for the whole paragraph)"
        )

    @Slot(object, float, float)
    def move_object(self, target: Any, dx: float, dy: float) -> None:
        """Apply a drag or nudge to a page object (undoable)."""
        from pdfstudio.pdfengine.objects import ObjectService

        document = self.document
        if document is None or target is None:
            return
        service = ObjectService(document)
        service.move(target, dx, dy)
        self._refresh(target.page)
        # Re-resolve from the rewritten page: a fabricated copy would keep the
        # old payload and replay the original geometry on the next nudge.
        view = self.window.view
        if view is not None:
            expected = Rect(
                target.rect.x0 + dx,
                target.rect.y0 + dy,
                target.rect.x1 + dx,
                target.rect.y1 + dy,
            )
            view.set_selected_object(service.resolve(target, expected))
        self._status(f"Moved {target.kind.value} by {dx:+.0f}, {dy:+.0f} pt")

    @Slot(object)
    def delete_object(self, target: Any) -> None:
        """Delete a page object (undoable)."""
        from pdfstudio.pdfengine.objects import ObjectService

        document = self.document
        if document is None or target is None:
            return
        ObjectService(document).delete(target)
        view = self.window.view
        if view is not None:
            view.set_selected_object(None)
        self._refresh(target.page)
        self._status(f"Deleted {target.kind.value}")

    def _selected_object(self) -> Any:
        view = self.window.view
        target = view.selected_object if view is not None else None
        if target is None:
            self._status("Pick the Move tool and click an object first (Home ▸ Move object)")
        return target

    def nudge_selected_object(self, dx: float, dy: float) -> None:
        target = self._selected_object()
        if target is not None:
            self.move_object(target, dx, dy)

    def delete_selected_object(self) -> None:
        target = self._selected_object()
        if target is not None:
            self.delete_object(target)

    def align_selected_object(self, edge: str = "left") -> None:
        """Snap the selected object to a page edge, or centre it.

        Accepts ``left``, ``right``, ``center``/``centre`` (horizontal) and
        ``top``, ``bottom``, ``middle`` (vertical). The geometry lives in
        :meth:`ObjectService.align` so the CLI, plugins and the UI cannot
        disagree about where "centred" is.
        """
        from pdfstudio.pdfengine.objects import ObjectService

        target = self._selected_object()
        if target is None:
            return
        document = self.require_document()
        service = ObjectService(document)
        try:
            expected = service.align(target, edge)
        except ValueError:
            self._status(f"Unknown alignment: {edge}")
            return
        self._refresh(target.page)
        view = self.window.view
        if view is not None:
            view.set_selected_object(service.resolve(target, expected))
        self._status(f"Aligned {target.kind.value} {edge}")

    def set_text_alignment(self, align: str) -> None:
        """Set the alignment of the text being edited, or of a selected object.

        With the inline editor open this re-aligns the text inside its box.
        With an object selected instead, "align left/centre/right" is the more
        useful reading — snap the whole object to that side of the page — so
        the command falls through to :meth:`align_selected_object` rather than
        reporting that nothing is being edited.
        """
        view = self.window.view
        editor = view.inline_editor if view is not None else None
        if editor is not None and editor._toolbar is not None:
            editor._toolbar.set_alignment(align)
            self._status(f"Alignment: {align}")
            return
        if view is not None and view.selected_object is not None:
            self.align_selected_object(align)
            return
        self._status("Click text with the Edit Text tool, or select an object, first")

    def change_wrap_indent(self, delta: float) -> None:
        """Indent or outdent the wrapped lines of the text being edited.

        This is what pulls a wrapped continuation line back to the left margin
        when it has inherited an indent from the original layout. With no
        editor open but an object selected, it nudges that object sideways
        instead — the same button then means "move this left/right", which is
        what someone with a rule or an image selected expects.
        """
        view = self.window.view
        editor = view.inline_editor if view is not None else None
        if editor is None:
            if view is not None and view.selected_object is not None:
                self.nudge_selected_object(delta, 0.0)
                return
            self._status("Click text with the Edit Text tool, or select an object, first")
            return
        style = editor.style
        updated = replace(style, wrap_indent=max(0.0, style.wrap_indent + delta))
        editor.apply_style(updated)
        # Keep the floating toolbar's own copy in step, or the next change it
        # emits would revert the indent to the value it still remembers.
        if editor._toolbar is not None:
            editor._toolbar._style = updated
        self._status(f"Wrapped-line indent: {updated.wrap_indent:.0f} pt")

    # ------------------------------------------------------------------ #
    # Word-style paragraph editing
    # ------------------------------------------------------------------ #
    def _text_cursor(self) -> tuple[int, Point] | None:
        """The page and point the paragraph commands should act on.

        Prefers the open inline editor, then the last place the user clicked,
        so a ribbon button works without forcing them to click again.
        """
        view = self.window.view
        if view is None or self.document is None:
            self._status("Open a document first")
            return None
        editor = view.inline_editor
        if editor is not None and view.inline_page is not None:
            rect = editor.document_rect
            return view.inline_page, rect.center
        anchor = view.last_text_point
        if anchor is not None:
            return anchor
        self._status("Click the text you want to change first")
        return None

    def edit_paragraph_at_cursor(self) -> None:
        """Open the inline editor over the whole paragraph."""
        cursor = self._text_cursor()
        if cursor is None:
            return
        page, point = cursor
        view = self.window.view
        if view is not None and view.inline_editor is not None:
            # Committing first means the paragraph is re-read from the page
            # including whatever was just typed.
            view.finish_inline_edit(commit=True)
        self.edit_text_block(page, None, point, True)

    def select_paragraph_at_cursor(self) -> None:
        """Select all text in the paragraph under the cursor."""
        cursor = self._text_cursor()
        if cursor is None:
            return
        page, point = cursor
        view = self.window.view
        if view is not None:
            view.select_paragraph_at(page, point)
            self._status("Paragraph selected — press Enter to edit, or use the format toolbar")

    def move_line(self, direction: int) -> None:
        """Move the line under the cursor up or down within its paragraph."""
        cursor = self._text_cursor()
        if cursor is None:
            return
        page, point = cursor
        view = self.window.view
        if view is not None and view.inline_editor is not None:
            view.finish_inline_edit(commit=True)
        document = self.require_document()
        editor = TextEditor(document)
        # Where the line will land: the neighbour's top edge, captured before
        # the swap rewrites the page.
        before = editor.paragraph_lines(page, point)
        current = editor.line_at(page, point)
        destination: Point | None = None
        if current is not None and before:
            index = min(
                range(len(before)),
                key=lambda i: abs(before[i].rect.y0 - current.rect.y0),
            )
            neighbour = index + direction
            if 0 <= neighbour < len(before):
                target = before[neighbour].rect
                destination = Point(point.x, target.y0 + current.rect.height / 2)
        try:
            moved = editor.move_line(page, point, direction)
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return
        if not moved:
            where = "up" if direction < 0 else "down"
            self._status(f"This line cannot move {where} any further")
            return
        self._refresh(page)
        # Follow the line so repeated presses keep moving the same text
        # instead of whatever has taken its place.
        if view is not None and destination is not None:
            view.set_last_text_point(page, destination)
        self._status(f"Moved line {'up' if direction < 0 else 'down'}")

    def _paragraph_action(self, label: str, action: Callable[[int, Point], bool]) -> None:
        """Run a paragraph-level edit at the cursor and report the outcome."""
        cursor = self._text_cursor()
        if cursor is None:
            return
        page, point = cursor
        view = self.window.view
        if view is not None and view.inline_editor is not None:
            view.finish_inline_edit(commit=True)
        try:
            changed = action(page, point)
        except PdfStudioError as exc:
            self.window.show_error(exc)
            return
        if not changed:
            self._status(f"{label}: nothing to change here")
            return
        self._refresh(page)
        self._status(label)

    def duplicate_line(self) -> None:
        document = self.require_document()
        editor = TextEditor(document)
        self._paragraph_action("Line duplicated", editor.duplicate_line)

    def delete_line(self) -> None:
        document = self.require_document()
        editor = TextEditor(document)
        self._paragraph_action("Line deleted", editor.delete_line)

    def copy_formatting(self) -> None:
        """Format painter: remember the style under the cursor."""
        cursor = self._text_cursor()
        if cursor is None:
            return
        page, point = cursor
        document = self.require_document()
        style = TextEditor(document).style_at(page, point)
        if style is None:
            self._status("No text under the cursor to copy formatting from")
            return
        self._format_clip = style
        self._status(
            f"Copied formatting: {style.font} {style.size:g}pt — "
            "click another paragraph and choose Apply formatting"
        )

    def paste_formatting(self) -> None:
        """Format painter: apply the remembered style to this paragraph."""
        if self._format_clip is None:
            self._status("Use Copy formatting first")
            return
        document = self.require_document()
        editor = TextEditor(document)
        style = self._format_clip
        self._paragraph_action(
            "Formatting applied",
            lambda page, point: editor.apply_style_to_paragraph(page, point, style),
        )

    def show_word_count(self) -> None:
        """Word, character and page statistics for the document."""
        document = self.require_document()
        view = self.window.view
        page_index = view.current_page if view is not None else 0

        def count(text: str) -> tuple[int, int, int]:
            words = len(text.split())
            chars = len(text)
            no_spaces = len("".join(text.split()))
            return words, chars, no_spaces

        this_page = count(document.extract_text(page_index))
        whole = count("\n".join(document.extract_text(i) for i in range(document.page_count)))
        selected = view.selected_text if view is not None else ""
        lines = [
            f"Pages: {document.page_count}",
            "",
            f"This page — words: {this_page[0]}, characters: {this_page[1]} "
            f"({this_page[2]} without spaces)",
            f"Document  — words: {whole[0]}, characters: {whole[1]} "
            f"({whole[2]} without spaces)",
        ]
        if selected.strip():
            chosen = count(selected)
            lines.append(f"Selection — words: {chosen[0]}, characters: {chosen[1]}")
        self.window.inform("Word count", "\n".join(lines))

    def set_list_style(self, marker: str) -> None:
        document = self.require_document()
        editor = TextEditor(document)
        labels = {"bullet": "Bulleted list", "number": "Numbered list", "none": "List removed"}
        self._paragraph_action(
            labels.get(marker, "List"),
            lambda page, point: editor.set_list_style(page, point, marker),
        )

    def change_case(self, mode: str) -> None:
        document = self.require_document()
        editor = TextEditor(document)
        self._paragraph_action(
            f"Case: {mode}",
            lambda page, point: editor.transform_case(page, point, mode),
        )

    def set_line_spacing(self, spacing: float) -> None:
        document = self.require_document()
        editor = TextEditor(document)
        self._paragraph_action(
            f"Line spacing: {spacing:g}",
            lambda page, point: editor.set_line_spacing(page, point, spacing),
        )

    # ------------------------------------------------------------------ #
    # Object clipboard
    # ------------------------------------------------------------------ #
    def copy_object(self, target: Any = None, *, cut: bool = False) -> bool:
        """Copy (or cut) a page object to PDF Studio's object clipboard.

        Kept separate from the system clipboard: a PDF object is a vector
        path, an image XObject or an annotation dictionary, none of which
        survive a round-trip through plain text or a bitmap.
        """
        from pdfstudio.pdfengine.objects import ObjectService

        document = self.document
        target = target if target is not None else self._selected_object()
        if document is None or target is None:
            return False
        service = ObjectService(document)
        self._object_clip = service.cut(target) if cut else service.copy(target)
        if cut:
            view = self.window.view
            if view is not None:
                view.set_selected_object(None)
            self._refresh(target.page)
        self._status(f"{'Cut' if cut else 'Copied'} {self._object_clip.describe()}")
        return True

    def cut_object(self, target: Any = None) -> bool:
        return self.copy_object(target, cut=True)

    def paste_object(self, at: Point | None = None, page: int | None = None) -> bool:
        """Paste the object clipboard onto the page (undoable)."""
        from pdfstudio.pdfengine.objects import ObjectService

        clip = self._object_clip
        document = self.document
        view = self.window.view
        if clip is None or document is None or view is None:
            return False
        index = view.current_page if page is None else page
        service = ObjectService(document)
        rect = service.paste(clip, index, at=at)
        self._refresh(index)
        # Select the copy so it can be dragged into place straight away —
        # a paste that lands invisibly is the usual complaint otherwise.
        from pdfstudio.ui.widgets.page_view import Tool

        view.set_tool(Tool.MOVE_OBJECT)
        self.window.sync_tool_buttons(Tool.MOVE_OBJECT)
        pasted = service.object_at(index, rect.center)
        view.set_selected_object(pasted)
        self._status(f"Pasted {clip.kind.value} — drag or use the arrow keys to position it")
        return True

    def duplicate_object(self, target: Any = None) -> bool:
        """Copy and paste an object in one step (Ctrl+D)."""
        from pdfstudio.pdfengine.objects import ObjectService

        document = self.document
        target = target if target is not None else self._selected_object()
        if document is None or target is None:
            return False
        service = ObjectService(document)
        rect = service.duplicate(target)
        self._refresh(target.page)
        view = self.window.view
        if view is not None:
            view.set_selected_object(service.object_at(target.page, rect.center))
        self._status(f"Duplicated {target.kind.value}")
        return True

    @property
    def has_object_clip(self) -> bool:
        return self._object_clip is not None

    @property
    def has_format_clip(self) -> bool:
        return self._format_clip is not None

    def smart_copy(self) -> None:
        """Ctrl+C: copy the selected object, else the selected text.

        One shortcut covering both is what users expect; which one applies
        depends on what is currently selected.
        """
        view = self.window.view
        if view is not None and view.selected_object is not None:
            self.copy_object()
            return
        if view is not None:
            view.copy_selection()

    def smart_cut(self) -> None:
        """Ctrl+X: cut the selected object (text cutting needs an editor)."""
        view = self.window.view
        if view is not None and view.selected_object is not None:
            self.cut_object()
            return
        self._status("Select an object with the Move tool to cut it")

    def smart_paste(self) -> None:
        """Ctrl+V: paste an object if one was copied, else the system clipboard."""
        if self._object_clip is not None:
            self.paste_object()
            return
        self.paste()

    def toggle_text_style(self, attribute: str) -> None:
        """Toggle a character style on the text being edited.

        With no editor open the whole paragraph under the cursor is restyled
        instead, so the buttons are useful without entering edit mode first.
        """
        view = self.window.view
        editor = view.inline_editor if view is not None else None
        if editor is not None and editor._toolbar is not None:
            button = getattr(editor._toolbar, attribute, None)
            if button is not None:
                button.toggle()
                self._status(f"{attribute.title()} {'on' if button.isChecked() else 'off'}")
                return
            # Underline and strike-through have no toolbar button; apply them
            # to the live style directly.
            style = editor.style
            updated = replace(style, **{attribute: not getattr(style, attribute)})
            editor.apply_style(updated)
            if editor._toolbar is not None:
                editor._toolbar._style = updated
            self._status(
                f"{attribute.title()} {'on' if getattr(updated, attribute) else 'off'}"
            )
            return

        document = self.document
        if document is None:
            self._status("Open a document first")
            return
        text_editor = TextEditor(document)

        def apply(page: int, point: Point) -> bool:
            region = text_editor.edit_region(page, point, whole_paragraph=True)
            if region is None:
                return False
            rect, current, style = region
            updated = replace(style, **{attribute: not getattr(style, attribute)})
            text_editor.replace(page, rect, current, updated)
            return True

        self._paragraph_action(f"{attribute.title()} toggled", apply)

    def choose_text_color(self) -> None:
        """Pick a colour for the text currently being edited."""
        view = self.window.view
        editor = view.inline_editor if view is not None else None
        if editor is None or editor._toolbar is None:
            self._status("Select text with the Edit Text tool first.")
            return
        editor._toolbar._choose_color()

    def move_pages_dialog(self) -> None:
        """Move the selected pages to a new position."""
        document = self.require_document()
        pages = self._current_pages()
        destination, ok = QInputDialog.getInt(
            self.window,
            "Move pages",
            f"Move {len(pages)} page(s) before page:",
            min(pages) + 1,
            1,
            document.page_count + 1,
        )
        if not ok:
            return
        self.move_pages(pages, destination - 1)

    def manage_certificates(self) -> None:
        """Show the certificates used for signing and validation."""
        from pdfstudio.pdfengine.security import SecurityService

        document = self.document
        fields = SecurityService(document).signature_fields() if document else []
        if not fields:
            self.window.inform(
                "Certificates",
                "This document has no signature fields.\n\n"
                "Use Protect ▸ Sign document to sign it with a PKCS#12 "
                "certificate, then Validate to inspect the certificate chain.",
            )
            return
        lines = [
            f"• {f['name']} on page {f['page'] + 1}: {'signed' if f['signed'] else 'empty'}"
            for f in fields
        ]
        self.window.inform("Certificates", "\n".join(lines))

    def import_from_scanner(self) -> None:
        """Acquire pages from a scanner, when one is available."""
        import shutil

        scanner = shutil.which("scanimage")
        if scanner is None:
            self.window.inform(
                "Scanner",
                "No scanner interface was found.\n\n"
                "Install SANE (Linux), or use your scanner's own software and "
                "then Convert ▸ From images to build the PDF.",
            )
            return
        self._run_job(
            "Scanning",
            self._scan_pages,
            scanner,
            on_done=self._on_scan_finished,
            with_context=True,
        )

    @staticmethod
    def _scan_pages(ctx: JobContext, scanner: str) -> Path:
        """Capture one page via SANE and return the resulting image."""
        import subprocess
        import tempfile

        ctx.progress(0, "Acquiring page…")
        target = Path(tempfile.mkstemp(suffix=".png")[1])
        subprocess.run(  # noqa: S603
            [scanner, "--format=png", "--resolution", "300"],
            stdout=target.open("wb"),
            check=True,
            timeout=300,
        )
        return target

    def _on_scan_finished(self, image: Path) -> None:
        document = Importer().import_images([image])
        self.window.add_document(document)
        image.unlink(missing_ok=True)
        self._status("Scanned page imported")

    def record_macro(self) -> None:
        """Open the script console seeded with a macro template."""
        from pdfstudio.ui.dialogs.console import ScriptConsole

        console = ScriptConsole(self.window)
        console.editor.setPlainText(
            "# Macro template — runs against the open document.\n"
            "# Available: doc, pages, text, annots, forms, security, search, ai\n"
            "\n"
            "for page in range(doc.page_count):\n"
            "    pass  # your automation here\n"
            "\n"
            "window.refresh_after_edit()\n"
        )
        console.show()
        self._status("Write the macro, run it, then save it for re-use")

    def show_tag_editor(self) -> None:
        """Inspect the document's structure tags."""
        document = self.require_document()
        if not document.is_tagged():
            if self.window.confirm(
                "Tag editor",
                "This document has no structure tree, so screen readers "
                "cannot determine its reading order.\n\n"
                "Add basic tags now?",
            ):
                self.accessibility_fix()
            return
        self.window.inform(
            "Tag editor",
            "This document is tagged. Use Accessibility ▸ Full check to "
            "review the tags, alt text and reading order.",
        )

    def show_reading_order(self) -> None:
        """List the reading order of the current page."""
        from pdfstudio.pdfengine.optimize import AccessibilityChecker

        document = self.require_document()
        view = self.window.view
        page = view.current_page if view else 0
        order = AccessibilityChecker(document).reading_order(page)
        if not order:
            self._status("This page has no text to order")
            return
        blocks = document.extract_blocks(page)
        lookup = {(round(b.rect.y0), round(b.rect.x0)): b.text for b in blocks}
        lines = []
        for index, rect in enumerate(order[:25], 1):
            text = lookup.get((round(rect.y0), round(rect.x0)), "")
            lines.append(f"{index:>2}. {' '.join(text.split())[:70]}")
        self.window.inform(
            f"Reading order — page {page + 1}",
            "Screen readers will read the page in this order:\n\n" + "\n".join(lines),
        )

    def edit_alt_text(self) -> None:
        """Add alternative text to an image on the current page."""
        from pdfstudio.pdfengine.optimize import AccessibilityChecker

        document = self.require_document()
        view = self.window.view
        page = view.current_page if view else 0
        images = document.page_images(page)
        if not images:
            self._status("This page has no images")
            return
        choices = [
            f"{n}. {info.width}×{info.height} {info.colorspace}"
            for n, info in enumerate(images, 1)
        ]
        choice, ok = QInputDialog.getItem(
            self.window, "Alternative text", "Image:", choices, 0, False
        )
        if not ok:
            return
        info = images[choices.index(choice)]
        text, ok = QInputDialog.getText(
            self.window,
            "Alternative text",
            "Describe this image for screen-reader users:",
        )
        if ok and text:
            AccessibilityChecker(document).set_alt_text(page, info.xref, text)
            self._refresh(page)
            self._status("Alternative text added")

    def add_text_box(self) -> None:
        document = self.require_document()
        view = self.window.view
        if view is None:
            return
        text, ok = QInputDialog.getMultiLineText(self.window, "Add text", "Text:")
        if not ok or not text:
            return
        page = view.current_page
        width, _ = document.page_size(page)
        TextEditor(document).add(page, Rect(72, 72, width - 72, 220), text, TextStyle(size=12))
        self._refresh(page)

    def add_image(self) -> None:
        document = self.require_document()
        view = self.window.view
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Insert image",
            str(Path.home()),
            "Images (*.png *.jpg *.jpeg *.bmp *.gif *.webp *.tif *.tiff)",
        )
        if not path or view is None:
            return
        page = view.current_page
        width, height = document.page_size(page)
        ImageEditor(document).insert(
            page, Rect(72, 72, min(width - 72, 372), min(height - 72, 372)), path
        )
        self._refresh(page)

    def edit_image(self) -> None:
        from pdfstudio.ui.dialogs.image_dialog import ImageEditDialog

        document = self.require_document()
        view = self.window.view
        if view is None:
            return
        page = view.current_page
        images = document.page_images(page)
        if not images:
            self._status("This page has no images")
            return
        dialog = ImageEditDialog(document, page, images, self.window)
        if dialog.exec():
            self._refresh(page)

    def add_link(self) -> None:
        document = self.require_document()
        view = self.window.view
        if view is None:
            return
        target, ok = QInputDialog.getText(
            self.window, "Add link", "URL or page number:", text="https://"
        )
        if not ok or not target:
            return
        page = view.current_page
        rect = Rect(72, 72, 300, 96)
        with document.locked() as handle:
            if target.isdigit():
                handle[page].insert_link(
                    {
                        "kind": 1,
                        "from": _fitz_rect(rect),
                        "page": int(target) - 1,
                        "to": _fitz_point(0, 0),
                    }
                )
            else:
                handle[page].insert_link({"kind": 2, "from": _fitz_rect(rect), "uri": target})
        document.mark_modified("add-link")
        self._refresh(page)
        self._status("Link added")

    # ------------------------------------------------------------------ #
    # Forms
    # ------------------------------------------------------------------ #
    def add_form_field(self, kind: str) -> None:
        document = self.require_document()
        view = self.window.view
        if view is None:
            return
        name, ok = QInputDialog.getText(self.window, "Field name", "Name:", text=f"{kind}1")
        if not ok or not name:
            return
        page = view.current_page
        service = FormService(document)
        rect = Rect(100, 120, 340, 150)
        try:
            match kind:
                case "text":
                    service.create_text(name, page, rect)
                case "checkbox":
                    service.create_checkbox(name, page, Rect(100, 120, 120, 140))
                case "radio":
                    service.create_radio_group(
                        name,
                        page,
                        [("Yes", Rect(100, 120, 120, 140)), ("No", Rect(160, 120, 180, 140))],
                    )
                case "combobox" | "listbox":
                    text, ok2 = QInputDialog.getText(
                        self.window, "Options", "Comma-separated options:", text="One,Two,Three"
                    )
                    if not ok2:
                        return
                    options = [o.strip() for o in text.split(",") if o.strip()]
                    if kind == "combobox":
                        service.create_dropdown(name, page, rect, options)
                    else:
                        service.create_listbox(name, page, Rect(100, 120, 340, 210), options)
                case "date":
                    service.create_date_picker(name, page, rect)
                case "signature":
                    service.create_signature_field(name, page, Rect(100, 120, 340, 190))
                case "barcode":
                    data, ok2 = QInputDialog.getText(
                        self.window, "QR code", "Data to encode:", text="https://"
                    )
                    if not ok2 or not data:
                        return
                    service.create_barcode(name, page, Rect(100, 120, 220, 240), data)
        except DependencyMissingError as exc:
            self.window.show_error(exc)
            return
        self._refresh(page)
        self._status(f"Added {kind} field “{name}”")

    def import_form_data(self) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getOpenFileName(
            self.window,
            "Import form data",
            str(Path.home()),
            "Form data (*.fdf *.xfdf *.json)",
        )
        if not path:
            return
        service = FormService(document)
        suffix = Path(path).suffix.lower()
        count = (
            service.import_json(Path(path))
            if suffix == ".json"
            else service.import_xfdf(Path(path))
            if suffix == ".xfdf"
            else service.import_fdf(Path(path))
        )
        self._refresh()
        self._status(f"Filled {count} field(s)")

    def export_form_data(self) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "Export form data",
            str(Path.home() / f"{Path(document.display_name).stem}-data.json"),
            "JSON (*.json);;XFDF (*.xfdf);;FDF (*.fdf);;CSV (*.csv)",
        )
        if not path:
            return
        service = FormService(document)
        suffix = Path(path).suffix.lower()
        {
            ".json": service.export_json,
            ".xfdf": service.export_xfdf,
            ".fdf": service.export_fdf,
            ".csv": service.export_csv,
        }.get(suffix, service.export_json)(path)
        self._status(f"Exported form data to {Path(path).name}")

    def reset_form(self) -> None:
        document = self.require_document()
        FormService(document).reset()
        self._refresh()
        self._status("Form reset")

    def flatten_form(self) -> None:
        document = self.require_document()
        FormService(document).flatten()
        self._refresh()
        self._status("Form flattened")

    def validate_form(self) -> None:
        document = self.require_document()
        problems = FormService(document).validate()
        if not problems:
            self.window.inform("Validation", "The form is valid.")
            return
        detail = "\n".join(f"• {name}: {issue}" for name, issue in problems)
        self.window.warn("Validation", f"{len(problems)} problem(s) found:\n\n{detail}")

    # ------------------------------------------------------------------ #
    # Security
    # ------------------------------------------------------------------ #
    def encrypt_document(self) -> None:
        from pdfstudio.ui.dialogs.security_dialog import SecurityDialog

        document = self.require_document()
        dialog = SecurityDialog(self.window)
        if not dialog.exec():
            return
        config = dialog.result_settings()
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            "Save encrypted copy",
            str(
                (document.path or Path.home() / document.display_name).with_name(
                    f"{Path(document.display_name).stem}-protected.pdf"
                )
            ),
            PDF_FILTER,
        )
        if not path:
            return
        SecurityService(document).encrypt_to(path, config)
        self._status(f"Encrypted copy saved as {Path(path).name}")

    def decrypt_document(self) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getSaveFileName(
            self.window, "Save unprotected copy", str(Path.home()), PDF_FILTER
        )
        if path:
            SecurityService(document).decrypt_to(path)
            self._status("Saved without encryption")

    def edit_permissions(self) -> None:
        from pdfstudio.ui.dialogs.security_dialog import SecurityDialog

        dialog = SecurityDialog(self.window, permissions_only=True)
        if dialog.exec():
            self._status("Set permissions — save an encrypted copy to apply them")

    def sanitize_document(self) -> None:
        document = self.require_document()
        if not self.window.confirm(
            "Sanitise document",
            "Remove metadata, JavaScript, embedded files and hidden layers?",
        ):
            return
        removed = SecurityService(document).sanitize()
        self._refresh()
        summary = ", ".join(f"{v} {k}" for k, v in removed.items() if v)
        self._status(f"Sanitised: {summary or 'nothing to remove'}")

    def find_and_redact(self) -> None:
        document = self.require_document()
        text, ok = QInputDialog.getText(
            self.window, "Find text to redact", "Text or regular expression:"
        )
        if not ok or not text:
            return
        count = AnnotationService(document, author=self._author()).redact_text(text)
        self._refresh()
        self._status(f"Marked {count} occurrence(s) — use Apply to remove them")

    def apply_redactions(self) -> None:
        document = self.require_document()
        # Permanent and irreversible, so the default when dialogs are
        # suppressed must be "no".
        if not self.window.confirm(
            "Apply redactions",
            "Content under the redaction marks will be permanently removed "
            "when you save.\n\nContinue?",
        ):
            return
        pages = AnnotationService(document).apply_redactions()
        self._refresh()
        self._status(f"Redacted {pages} page(s)")

    def sign_document(self) -> None:
        from pdfstudio.ui.dialogs.signature_dialog import SignatureDialog

        document = self.require_document()
        dialog = SignatureDialog(document, self.window)
        if dialog.exec():
            self._refresh()

    def validate_signatures(self) -> None:
        document = self.require_document()
        try:
            reports = SecurityService(document).validate_signatures()
        except DependencyMissingError as exc:
            self.window.show_error(exc)
            return
        if not reports:
            self.window.inform("Signatures", "This document is not signed.")
            return
        lines = [
            f"• {r['field']}: {'valid' if r['valid'] else 'INVALID'}"
            f"{' (trusted)' if r['trusted'] else ' (untrusted certificate)'}"
            for r in reports
        ]
        self.window.inform("Signature validation", "\n".join(lines))

    def add_watermark(self) -> None:
        from pdfstudio.ui.dialogs.watermark_dialog import WatermarkDialog

        document = self.require_document()
        dialog = WatermarkDialog(self.window)
        if not dialog.exec():
            return
        config = dialog.values()
        SecurityService(document).watermark_text(
            config["text"],
            opacity=config["opacity"],
            font_size=config["size"],
            rotate=config["rotation"],
            color=config["color"],
            tile=config["tile"],
            position=config["position"],
        )
        self._refresh()
        self._status("Watermark applied")

    def add_bates(self) -> None:
        document = self.require_document()
        prefix, ok = QInputDialog.getText(self.window, "Bates numbering", "Prefix:", text="")
        if not ok:
            return
        start, ok2 = QInputDialog.getInt(self.window, "Bates numbering", "Start at:", 1, 0)
        if not ok2:
            return
        SecurityService(document).bates_numbering(prefix=prefix, start=start)
        self._refresh()
        self._status("Bates numbering applied")

    def add_header_footer(self) -> None:
        document = self.require_document()
        text, ok = QInputDialog.getText(
            self.window,
            "Header and footer",
            "Footer text (tokens: {page} {pages} {date} {filename} {title}):",
            text="Page {page} of {pages}",
        )
        if not ok:
            return
        SecurityService(document).header_footer(footer_center=text)
        self._refresh()
        self._status("Header/footer applied")

    def add_background(self) -> None:
        document = self.require_document()
        colour = QColorDialog.getColor(parent=self.window, title="Background colour")
        if not colour.isValid():
            return
        SecurityService(document).background(
            Color(colour.redF(), colour.greenF(), colour.blueF())
        )
        self._refresh()

    # ------------------------------------------------------------------ #
    # Export / optimise
    # ------------------------------------------------------------------ #
    def export_as(self, fmt: str) -> None:
        document = self.require_document()
        exporter = Exporter(document)
        stem = Path(document.display_name).stem
        if fmt == "images":
            directory = QFileDialog.getExistingDirectory(
                self.window, "Export images to", str(Path.home())
            )
            if not directory:
                return
            self._run_job(
                "Exporting images",
                lambda ctx: exporter.to_images(
                    directory, "png", ExportOptions(dpi=200), ctx=ctx
                ),
                on_done=lambda paths: self._status(f"Exported {len(paths)} image(s)"),
            )
            return

        filters = {
            "docx": ("Word document (*.docx)", ".docx"),
            "pptx": ("PowerPoint (*.pptx)", ".pptx"),
            "txt": ("Text file (*.txt)", ".txt"),
            "html": ("HTML (*.html)", ".html"),
            "md": ("Markdown (*.md)", ".md"),
        }
        label, suffix = filters[fmt]
        path, _ = QFileDialog.getSaveFileName(
            self.window, f"Export as {fmt.upper()}", str(Path.home() / f"{stem}{suffix}"), label
        )
        if not path:
            return

        def work(ctx: JobContext) -> Path:
            ctx.progress(0, f"Exporting {fmt.upper()}…")
            match fmt:
                case "docx":
                    return exporter.to_docx(path)
                case "pptx":
                    return exporter.to_pptx(path)
                case "txt":
                    Path(path).write_text(exporter.to_text(), "utf-8")
                case "html":
                    Path(path).write_text(exporter.to_html(), "utf-8")
                case "md":
                    Path(path).write_text(exporter.to_markdown(), "utf-8")
            return Path(path)

        self._run_job(
            f"Exporting {fmt.upper()}",
            work,
            on_done=lambda target: self._status(f"Exported {Path(target).name}"),
        )

    def export_conformance(self, level: ConformanceLevel) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getSaveFileName(
            self.window,
            f"Export as {level.value}",
            str(Path.home() / f"{Path(document.display_name).stem}-{level.name.lower()}.pdf"),
            PDF_FILTER,
        )
        if not path:
            return
        self._run_job(
            f"Converting to {level.value}",
            lambda ctx: Exporter(document).to_conformance(path, level, ctx=ctx),
            on_done=lambda target: self._status(f"Wrote {Path(target).name}"),
        )

    def compress_document(self) -> None:
        document = self.require_document()
        choice, ok = QInputDialog.getItem(
            self.window,
            "Compress",
            "Optimisation profile:",
            [
                "Screen (smallest)",
                "E-book (balanced)",
                "Print (high quality)",
                "Prepress (maximum quality)",
                "Maximum compression",
            ],
            1,
            False,
        )
        if not ok:
            return
        profile = {
            "S": OptimizeProfile.screen,
            "E": OptimizeProfile.ebook,
            "P": OptimizeProfile.print_quality,
            "M": OptimizeProfile.maximum,
        }[choice[0]]()
        if choice.startswith("Prepress"):
            profile = OptimizeProfile.prepress()

        def done(report: Any) -> None:
            self._refresh()
            self.window.inform(
                "Compression complete",
                f"{report.summary()}\n\n" + "\n".join(report.details),
            )

        self._run_job(
            "Compressing",
            lambda ctx: Optimizer(document).optimize(profile, ctx=ctx),
            on_done=done,
        )

    def analyze_size(self) -> None:
        document = self.require_document()
        info = Optimizer(document).analyze()
        largest = info["largest_images"][:5]
        lines = [
            f"Total size: {info['total_human']}",
            f"Pages: {info['pages']}",
            f"Images: {info['image_count']} ({info['image_share']}% of the file)",
            f"Embedded fonts: {info['embedded_fonts']} of {len(info['fonts'])}",
            f"Attachments: {info['attachments']}",
            "",
            "Largest images:",
            *[
                f"  • page {i['page'] + 1}: {i['pixels'] // 1000}k pixels, "
                f"{i['bytes'] // 1024} KB at {i['dpi']} dpi"
                for i in largest
            ],
        ]
        self.window.inform("Size analysis", "\n".join(lines))

    # ------------------------------------------------------------------ #
    # OCR & AI
    # ------------------------------------------------------------------ #
    def run_ocr(self) -> None:
        from pdfstudio.ocr.engine import OcrService, available_engines

        document = self.require_document()
        if not available_engines():
            self.window.show_error(
                DependencyMissingError(
                    "pytesseract + the tesseract binary", "Optical character recognition"
                )
            )
            return
        pages = OcrService(document, self.settings.data.ocr).pages_needing_ocr()
        if not pages and not self.window.confirm(
            "OCR",
            "Every page already contains text.\n\nRun OCR anyway (forced)?",
        ):
            return

        config = self.settings.data.ocr
        if not pages:
            config.force = True

        def work(ctx: JobContext) -> Any:
            service = OcrService(document, config)
            results = service.run(ctx=ctx)
            return service.confidence_report(results)

        def done(report: dict[str, Any]) -> None:
            self._refresh()
            self.window.inform(
                "OCR complete",
                f"Recognised {report['words']} words on {report['pages']} page(s).\n"
                f"Mean confidence: {report['mean_confidence']}%\n"
                f"Low-confidence words: {report['low_confidence_words']}",
            )

        self._run_job("Running OCR", work, on_done=done)

    def _assistant(self) -> Any:
        from pdfstudio.ai.assistant import AIAssistant

        return AIAssistant(self.require_document(), self.settings.data.ai)

    def ai_summarize(self) -> None:
        from pdfstudio.ui.dialogs.ai_dialog import AIResultDialog

        assistant = self._assistant()
        self._run_job(
            "Summarising",
            lambda ctx: assistant.summarize(ctx=ctx),
            on_done=lambda response: AIResultDialog(
                "Document summary", response.text, self.window
            ).exec(),
        )

    def ai_chat(self) -> None:
        from pdfstudio.ui.dialogs.ai_dialog import AIChatDialog

        dialog = AIChatDialog(self._assistant(), self.window)
        dialog.show()

    def ai_translate(self) -> None:
        from pdfstudio.ui.dialogs.ai_dialog import AIResultDialog

        language, ok = QInputDialog.getItem(
            self.window,
            "Translate",
            "Target language:",
            [
                "English",
                "French",
                "German",
                "Spanish",
                "Italian",
                "Portuguese",
                "Dutch",
                "Polish",
                "Japanese",
                "Chinese",
                "Arabic",
                "Hindi",
            ],
            0,
            True,
        )
        if not ok:
            return
        assistant = self._assistant()
        try:
            response = assistant.translate(language)
        except DependencyMissingError as exc:
            self.window.show_error(exc)
            return
        AIResultDialog(f"Translation ({language})", response.text, self.window).exec()

    def ai_bookmarks(self) -> None:
        assistant = self._assistant()
        count = assistant.apply_generated_bookmarks()
        self._refresh()
        self._status(f"Generated {count} bookmark(s)" if count else "No headings were detected")

    def ai_tables(self) -> None:
        document = self.require_document()
        tables = self._assistant().extract_tables()
        if not tables:
            self._status("No tables detected")
            return
        directory = QFileDialog.getExistingDirectory(
            self.window, "Save tables (CSV) to", str(Path.home())
        )
        if not directory:
            return
        written = Exporter(document).tables_to_csv(directory)
        self._status(f"Extracted {len(written)} table(s)")

    def ai_metadata(self) -> None:
        document = self.require_document()
        suggestion = self._assistant().generate_metadata()
        message = "\n".join(f"{k.title()}: {v}" for k, v in suggestion.items())
        if not self.window.confirm("Suggested metadata", f"{message}\n\nApply these values?"):
            return
        metadata = document.metadata()
        metadata.title = suggestion["title"]
        metadata.subject = suggestion["subject"]
        metadata.keywords = suggestion["keywords"]
        document.set_metadata(metadata)
        self._refresh()

    # ------------------------------------------------------------------ #
    # Compare, batch, accessibility
    # ------------------------------------------------------------------ #
    def compare_documents(self) -> None:
        document = self.require_document()
        path, _ = QFileDialog.getOpenFileName(
            self.window, "Compare with", str(Path.home()), PDF_FILTER
        )
        if not path:
            return
        other = PdfDocument.open(Path(path))

        def work(ctx: JobContext) -> Any:
            return DocumentComparer(document, other).compare(ctx=ctx)

        def done(report: Any) -> None:
            if self.window.confirm(
                "Comparison complete",
                f"{report.summary()}\n\nCreate a side-by-side report PDF?",
            ):
                target, _ = QFileDialog.getSaveFileName(
                    self.window, "Save report", str(Path.home() / "comparison.pdf"), PDF_FILTER
                )
                if target:
                    DocumentComparer(document, other).build_report_pdf(report, target)
                    self.open_path(target)
            other.close()

        self._run_job("Comparing documents", work, on_done=done)

    def batch_process(self) -> None:
        from pdfstudio.ui.dialogs.batch_dialog import BatchDialog

        BatchDialog(self.window).exec()

    def accessibility_check(self) -> None:
        document = self.require_document()
        issues = AccessibilityChecker(document).check()
        if not issues:
            self.window.inform("Accessibility", "No accessibility problems were found.")
            return
        text = "\n".join(f"• {issue}" for issue in issues[:40])
        self.window.warn(
            "Accessibility check",
            f"{len(issues)} issue(s) found:\n\n{text}\n\n"
            "Use “Fix automatically” to resolve the simple ones.",
        )

    def accessibility_fix(self) -> None:
        document = self.require_document()
        applied = AccessibilityChecker(document).auto_fix()
        self._refresh()
        self.window.inform(
            "Accessibility",
            "\n".join(f"• {item}" for item in applied) or "Nothing needed fixing.",
        )

    def set_language(self) -> None:
        document = self.require_document()
        language, ok = QInputDialog.getText(
            self.window, "Document language", "BCP-47 language tag:", text="en-GB"
        )
        if ok and language:
            AccessibilityChecker(document).set_language(language)
            self._status(f"Document language set to {language}")

    def read_aloud(self) -> None:
        """Speak the current page using the platform text-to-speech engine."""
        document = self.require_document()
        view = self.window.view
        if view is None:
            return
        text = view.selected_text or document.extract_text(view.current_page)
        if not text.strip():
            self._status("Nothing to read on this page")
            return
        try:
            from PySide6.QtTextToSpeech import QTextToSpeech
        except ImportError:
            self.window.show_error(DependencyMissingError("PySide6-Addons", "Read aloud"))
            return
        if not hasattr(self, "_tts"):
            self._tts = QTextToSpeech(self)
        self._tts.say(text[:6000])
        self._status("Reading aloud…")

    # ------------------------------------------------------------------ #
    # Search
    # ------------------------------------------------------------------ #
    @Slot(object)
    def run_search(self, query: SearchQuery) -> None:
        document = self.document
        if document is None:
            return
        panel = self.window.search_panel
        panel.show_progress()
        started = time.perf_counter()
        service = SearchService(document)

        def done(hits: list[Any]) -> None:
            elapsed = time.perf_counter() - started
            panel.set_results(hits, elapsed=elapsed)
            if self.window.view:
                self.window.view.set_search_hits(hits)
                if hits:
                    self.window.view.show_hit(0)
            self.window.history.add_search(query.text, len(hits))
            self._status(f"{len(hits)} result(s) for “{query.text}”")

        self._run_job(
            f"Searching “{query.text}”",
            lambda ctx: service.search(query, ctx=ctx),
            on_done=done,
        )

    # ------------------------------------------------------------------ #
    # Document-level dialogs
    # ------------------------------------------------------------------ #
    def edit_metadata(self) -> None:
        from pdfstudio.ui.dialogs.metadata_dialog import MetadataDialog

        document = self.require_document()
        dialog = MetadataDialog(document, self.window)
        if dialog.exec():
            self._refresh()
            self._status("Metadata updated")

    def show_properties(self) -> None:
        dock = self.window._docks["properties"]
        dock.show()
        dock.raise_()

    def find_replace(self) -> None:
        document = self.require_document()
        search, ok = QInputDialog.getText(self.window, "Replace", "Find:")
        if not ok or not search:
            return
        replacement, ok2 = QInputDialog.getText(self.window, "Replace", "Replace with:")
        if not ok2:
            return
        count = TextEditor(document).replace_all(search, replacement)
        self._refresh()
        self._status(f"Replaced {count} occurrence(s)")

    def toggle_presentation(self) -> None:
        view = self.window.view
        if view is None:
            return
        entering = not view.is_presentation
        view.set_presentation_mode(entering)
        if entering:
            self.window.showFullScreen()
            self.window._ribbon_dock.hide()
            for dock in self.window._docks.values():
                dock.hide()
        else:
            self.window.showNormal()
            self.window.set_toolbar_mode(self.settings.data.ui.toolbar_mode)
            self.window._docks["thumbnails"].show()

    def choose_theme(self) -> None:
        names = {t.name: t.identifier for t in self.window.themes.themes()}
        choice, ok = QInputDialog.getItem(
            self.window, "Theme", "Choose a theme:", list(names), 0, False
        )
        if ok:
            self.window.apply_theme(names[choice])

    def show_preferences(self, page: str = "") -> None:
        """Open Preferences, optionally on a named category."""
        from pdfstudio.ui.dialogs.preferences import PreferencesDialog

        dialog = PreferencesDialog(self.window)
        if page:
            matches = dialog.categories.findItems(page, Qt.MatchFlag.MatchExactly)
            if matches:
                dialog.categories.setCurrentItem(matches[0])
        if dialog.exec():
            self.window.apply_theme(self.settings.data.ui.theme)
            self._status("Preferences saved")

    def manage_plugins(self) -> None:
        from pdfstudio.ui.dialogs.plugin_dialog import PluginDialog

        if self.window.plugins is None:
            self._status("The plugin system is disabled")
            return
        PluginDialog(self.window.plugins, self.window).exec()

    def script_console(self) -> None:
        from pdfstudio.ui.dialogs.console import ScriptConsole

        console = ScriptConsole(self.window)
        console.show()

    @Slot(int, bool)
    def toggle_layer(self, xref: int, visible: bool) -> None:
        document = self.require_document()
        document.set_layer_visible(xref, visible)
        self._refresh()

    @Slot(str, str)
    def attachment_action(self, action: str, name: str) -> None:
        document = self.require_document()
        match action:
            case "add":
                path, _ = QFileDialog.getOpenFileName(
                    self.window, "Attach file", str(Path.home())
                )
                if path:
                    target = Path(path)
                    document.add_attachment(target.name, target.read_bytes())
                    self._refresh()
                    self._status(f"Attached {target.name}")
            case "extract":
                if not name:
                    return
                path, _ = QFileDialog.getSaveFileName(
                    self.window, "Save attachment", str(Path.home() / name)
                )
                if path:
                    Path(path).write_bytes(document.extract_attachment(name))
                    self._status(f"Extracted {name}")
            case "delete":
                if name:
                    document.delete_attachment(name)
                    self._refresh()
                    self._status(f"Deleted {name}")

    @Slot(list)
    def set_bookmarks(self, bookmarks: list[Bookmark]) -> None:
        document = self.document
        if document is not None:
            document.set_bookmarks(bookmarks)
            self._status("Bookmarks updated")

    def show_about(self) -> None:
        import sys

        import pymupdf

        from pdfstudio import __version__

        QMessageBox.about(
            self.window,
            "About PDF Studio",
            f"<h3>PDF Studio {__version__}</h3>"
            "<p>A professional PDF editor built with Python and PySide6.</p>"
            f"<p>Python {sys.version.split()[0]} · PyMuPDF {pymupdf.__doc__.split()[1] if pymupdf.__doc__ else ''}"
            f" · Qt via PySide6</p>"
            "<p>Licensed under the MIT licence.</p>",
        )

    def show_manual(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        docs = Path(__file__).resolve().parents[3] / "docs" / "user-manual.md"
        if docs.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(docs)))
        else:
            self._status("The user manual is not installed")

    def show_shortcuts(self) -> None:
        rows = [
            ("Ctrl+O", "Open"),
            ("Ctrl+S", "Save"),
            ("Ctrl+Shift+S", "Save as"),
            ("Ctrl+P", "Print"),
            ("Ctrl+W", "Close tab"),
            ("Ctrl+Z / Ctrl+Y", "Undo / redo"),
            ("Ctrl+F", "Find"),
            ("Ctrl+H", "Replace"),
            ("Ctrl++ / Ctrl+-", "Zoom"),
            ("Ctrl+0", "Fit page"),
            ("Ctrl+1", "Fit width"),
            ("F5", "Presentation"),
            ("F11", "Full screen"),
            ("V / H / T", "Select / pan / text tools"),
            ("Ctrl+Shift+H", "Highlight"),
            ("Space", "Scroll down"),
            ("Home / End", "First / last page"),
            ("Ctrl+Tab", "Next tab"),
        ]
        body = "".join(f"<tr><td><b>{k}</b></td><td>{v}</td></tr>" for k, v in rows)
        self.window.inform("Keyboard shortcuts", f"<table cellpadding=4>{body}</table>")

    def _author(self) -> str:
        import getpass

        try:
            return getpass.getuser()
        except Exception:
            return "PDF Studio user"


def _fitz_rect(rect: Rect) -> Any:
    import pymupdf as fitz

    return fitz.Rect(*rect)


def _fitz_point(x: float, y: float) -> Any:
    import pymupdf as fitz

    return fitz.Point(x, y)
