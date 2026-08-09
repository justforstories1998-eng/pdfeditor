"""GUI tests exercising the widgets, controller and main window.

These run head-less through Qt's ``offscreen`` platform plug-in, so they work
in CI without a display server.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

from pdfstudio.pdfengine.content import TextStyle  # noqa: E402
from pdfstudio.pdfengine.document import PdfDocument  # noqa: E402
from pdfstudio.pdfengine.types import (  # noqa: E402
    AnnotationType,
    PageLayout,
    Point,
    Rect,
    ZoomMode,
)


def pump(app: object, cycles: int = 12, delay: float = 0.01) -> None:
    """Run the event loop briefly so queued work completes."""
    for _ in range(cycles):
        app.processEvents()  # type: ignore[attr-defined]
        time.sleep(delay)


def wait_for(app: object, predicate, timeout: float = 10.0) -> bool:
    """Spin the event loop until ``predicate()`` is true or the timeout passes."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        app.processEvents()  # type: ignore[attr-defined]
        time.sleep(0.02)
    return predicate()


# --------------------------------------------------------------------------- #
# Theme
# --------------------------------------------------------------------------- #
class TestTheme:
    def test_builtin_themes(self, qapp) -> None:
        from pdfstudio.ui.theme import ThemeManager

        manager = ThemeManager()
        identifiers = {t.identifier for t in manager.themes()}
        assert {"dark", "light", "high-contrast", "sepia"} <= identifiers

    def test_apply_every_theme(self, qapp) -> None:
        from pdfstudio.ui.theme import ThemeManager

        manager = ThemeManager()
        for theme in manager.themes():
            applied = manager.apply(theme.identifier, qapp)
            assert applied.identifier == theme.identifier
            assert qapp.styleSheet()

    def test_translucent_colours_are_valid_css(self, qapp) -> None:
        from pdfstudio.ui.theme import BUILTIN_THEMES, _css_color, build_stylesheet

        # Qt cannot parse 8-digit hex; it must be converted to rgba().
        assert _css_color("#ffffff14").startswith("rgba(")
        stylesheet = build_stylesheet(BUILTIN_THEMES["dark"])
        import re

        assert not re.search(r"#[0-9a-fA-F]{8}\b", stylesheet)

    def test_custom_theme_persistence(self, qapp, tmp_path: Path) -> None:
        from pdfstudio.ui.theme import Theme, ThemeManager

        manager = ThemeManager()
        custom = Theme.from_dict(
            {"name": "Test", "identifier": "test-theme", "palette": {"accent": "#123456"}}
        )
        manager.add(custom)
        assert ThemeManager().get("test-theme").palette.accent == "#123456"


# --------------------------------------------------------------------------- #
# Ribbon
# --------------------------------------------------------------------------- #
class TestRibbon:
    def test_structure(self, qapp) -> None:
        from pdfstudio.ui.widgets.ribbon import RibbonBar, build_default_tabs

        tabs = build_default_tabs()
        assert len(tabs) >= 9
        total = sum(len(g.items) for t in tabs for g in t.groups)
        assert total > 120

        ribbon = RibbonBar()
        assert ribbon.tabs.count() == len(tabs)
        assert ribbon.action("file.open") is not None

    def test_commands_emit(self, qapp) -> None:
        from pdfstudio.ui.widgets.ribbon import RibbonBar

        ribbon = RibbonBar()
        received: list[str] = []
        ribbon.command.connect(received.append)
        ribbon._buttons["file.open"].click()
        assert received == ["file.open"]

    def test_tool_buttons_are_exclusive(self, qapp) -> None:
        from pdfstudio.ui.widgets.ribbon import RibbonBar

        ribbon = RibbonBar()
        ribbon._buttons["tool.pan"].setChecked(True)
        assert not ribbon._buttons["tool.select"].isChecked()
        ribbon._buttons["tool.select"].setChecked(True)
        assert not ribbon._buttons["tool.pan"].isChecked()

    def test_zoom_widget(self, qapp) -> None:
        from pdfstudio.ui.widgets.ribbon import RibbonBar

        ribbon = RibbonBar()
        values: list[float] = []
        ribbon.zoom_requested.connect(values.append)
        ribbon._emit_zoom("150%")
        assert values == [1.5]
        ribbon.set_zoom_display(0.75)
        assert ribbon.zoom_combo.currentText() == "75%"


# --------------------------------------------------------------------------- #
# Page view
# --------------------------------------------------------------------------- #
class TestPageView:
    def test_layout_and_zoom(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(900, 700)
        view.set_document(document)
        assert len(view._geometry) == 3

        view.set_zoom_mode(ZoomMode.FIT_WIDTH)
        fit_width = view.zoom
        view.set_zoom_mode(ZoomMode.FIT_PAGE)
        assert view.zoom < fit_width

        view.set_zoom(2.0)
        assert view.zoom == 2.0
        view.zoom_in()
        assert view.zoom > 2.0

    def test_layout_modes(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(1000, 700)
        view.set_document(document)
        for layout in PageLayout:
            view.set_layout(layout)
            assert len(view._geometry) == 3

    def test_navigation(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(800, 600)
        view.set_document(document)
        pages: list[int] = []
        view.page_changed.connect(pages.append)
        view.go_to_page(2, animate=False)
        assert view.current_page == 2
        assert 2 in pages
        view.first_page()
        assert view.current_page == 0

    def test_text_selection(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(800, 600)
        view.set_document(document)
        view.select_all_on_page(0)
        assert "Invoice" in view.selected_text
        view.clear_selection()
        assert view.selected_text == ""

    def test_rendering_populates_images(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(900, 700)
        view.set_document(document)
        view.show()
        pump(qapp)
        assert wait_for(qapp, lambda: bool(view._images))
        image = next(iter(view._images.values()))
        assert not image.isNull()

    def test_search_hits(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.pdfengine.search import SearchService
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(800, 600)
        view.set_document(document)
        hits = SearchService(document).search("Invoice")
        view.set_search_hits(hits)
        view.show_hit(1)
        assert view._active_hit == 1
        view.clear_search_hits()
        assert not view._search_hits

    def test_presentation_mode(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(800, 600)
        view.set_document(document)
        view.set_presentation_mode(True)
        assert view.is_presentation
        assert view._layout is PageLayout.SINGLE
        view.set_presentation_mode(False)
        assert not view.is_presentation

    def test_coordinate_mapping(self, qapp, document: PdfDocument) -> None:
        from PySide6.QtCore import QPointF

        from pdfstudio.ui.widgets.page_view import PageView

        view = PageView()
        view.resize(900, 900)
        view.set_document(document)
        view.set_zoom(1.0)
        rect = view.page_rect_in_view(0)
        assert rect is not None
        centre = QPointF(rect.x() + 100, rect.y() + 100)
        hit = view._viewport_to_document(centre)
        assert hit is not None
        page, point = hit
        assert page == 0
        assert point.x == pytest.approx(100, abs=1)


# --------------------------------------------------------------------------- #
# Panels
# --------------------------------------------------------------------------- #
class TestPanels:
    def test_thumbnails(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.render.renderer import PageRenderer
        from pdfstudio.ui.panels.thumbnails import ThumbnailPanel

        panel = ThumbnailPanel()
        panel.resize(250, 700)
        panel.set_document(document, PageRenderer(document))
        panel.show()
        assert panel.list.count() == 3
        assert panel.list.iconSize().width() > 32  # real thumbnails, not 32px icons
        pump(qapp, 30)

    def test_bookmarks(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.pdfengine.types import Bookmark
        from pdfstudio.ui.panels.side_panels import BookmarkPanel

        document.set_bookmarks(
            [Bookmark("Chapter", 0, children=[Bookmark("Section", 1, level=2)])]
        )
        panel = BookmarkPanel()
        panel.set_document(document)
        assert panel.tree.topLevelItemCount() == 1
        collected = panel.collect()
        assert collected[0].title == "Chapter"
        assert collected[0].children[0].title == "Section"

    def test_comments_filtering(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.pdfengine.annotations import AnnotationService
        from pdfstudio.ui.panels.side_panels import CommentsPanel

        AnnotationService(document, author="Ada").sticky_note(0, Point(50, 50), "First note")
        AnnotationService(document, author="Bob").sticky_note(1, Point(50, 50), "Second note")
        panel = CommentsPanel()
        panel.set_document(document)
        assert panel.list.count() == 2
        panel._author_filter.setCurrentText("Bob")
        assert panel.list.count() == 1
        panel._author_filter.setCurrentText(panel.ALL_AUTHORS)
        panel._search.setText("First")
        assert panel.list.count() == 1

    def test_attachments(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.panels.side_panels import AttachmentsPanel

        document.add_attachment("notes.txt", b"hello")
        panel = AttachmentsPanel()
        panel.set_document(document)
        assert panel.table.rowCount() == 1
        panel.table.selectRow(0)
        assert panel.current_name() == "notes.txt"

    def test_properties(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.panels.side_panels import PropertiesPanel

        panel = PropertiesPanel()
        panel.set_document(document)
        panel.reload(0)
        assert panel._form.rowCount() > 10

    def test_search_panel_query(self, qapp) -> None:
        from pdfstudio.ui.panels.side_panels import SearchPanel

        panel = SearchPanel()
        panel.input.setText("invoice")
        panel.regex_box.setChecked(True)
        query = panel.build_query()
        assert query.text == "invoice" and query.regex
        assert not panel.boolean_box.isEnabled()  # mutually exclusive


# --------------------------------------------------------------------------- #
# Main window and controller
# --------------------------------------------------------------------------- #
@pytest.fixture
def window(qapp):
    """A main window wired to a plugin manager, closed automatically."""
    from pdfstudio.plugins.manager import PluginManager
    from pdfstudio.ui.main_window import MainWindow
    from pdfstudio.ui.theme import ThemeManager

    themes = ThemeManager()
    themes.apply("dark", qapp)
    plugins = PluginManager()
    win = MainWindow(theme_manager=themes, plugin_manager=plugins)
    plugins.host = win
    plugins.load_all()
    win.discard_on_close = True
    win.resize(1400, 900)
    win.show()
    pump(qapp)
    yield win
    win.close()
    # close() only hides the window: a MainWindow keeps roughly eighteen
    # child top-level widgets (docks, menus, dialogs) alive, and with one
    # window per test they accumulate for the whole session until the suite
    # slows to a crawl. deleteLater() plus a pump actually frees them.
    win.deleteLater()
    pump(qapp, 3)


class TestMainWindow:
    def test_constructs_with_all_panels(self, qapp, window) -> None:
        assert set(window._docks) == {
            "thumbnails",
            "bookmarks",
            "comments",
            "layers",
            "attachments",
            "search",
            "properties",
        }
        assert window.tabs.count() == 1  # welcome tab

    def test_open_and_close_document(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        assert window.document is not None
        assert window.document.page_count == 3
        assert window.thumbnail_panel.list.count() == 3
        window.close_tab(window.tabs.currentIndex(), discard=True)
        assert window.document is None

    def test_reopening_activates_existing_tab(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        assert len(window.tabs_list()) == 1

    def test_edit_commands_and_undo(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        document = window.document
        window.controller.page_action("rotate-right", [0])
        assert document.page_rotation(0) == 90
        window.controller.undo()
        assert document.page_rotation(0) == 0
        window.controller.redo()
        assert document.page_rotation(0) == 90

    def test_annotation_from_canvas_payload(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.controller.create_annotation(
            {"type": AnnotationType.SQUARE, "page": 0, "rect": Rect(50, 50, 200, 150)}
        )
        assert len(window.document.page_annotations(0)) == 1
        assert window.comments_panel.list.count() == 1

    def test_search_runs_on_a_background_thread(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.search_panel.input.setText("Invoice")
        window.controller.run_search(window.search_panel.build_query())
        assert wait_for(qapp, lambda: window.search_panel.results.count() > 0)
        assert window.search_panel.results.count() == 3
        assert len(window.view._search_hits) == 3

    def test_tool_selection(self, qapp, window, tmp_pdf: Path) -> None:
        from pdfstudio.ui.widgets.page_view import Tool

        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.dispatch("tool.pan")
        assert window.view.tool is Tool.PAN
        window.dispatch("annot.highlight")
        assert window.view.tool is Tool.HIGHLIGHT

    def test_toolbar_mode_switch(self, qapp, window) -> None:
        window.set_toolbar_mode("classic")
        assert window.classic_toolbar.isVisible()
        window.set_toolbar_mode("ribbon")
        assert not window.classic_toolbar.isVisible()

    def test_unknown_command_is_ignored(self, qapp, window) -> None:
        window.dispatch("no.such.command")  # must not raise

    def test_plugin_commands_available(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.dispatch("org.pdfstudio.page-numbers.insert")
        assert "Page 1 of 3" in window.document.extract_text(0)

    def test_session_round_trip(self, qapp, window, tmp_pdf: Path) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.view.go_to_page(2, animate=False)
        window.save_session()
        session = window.history.load_session("last")
        assert session["tabs"][0]["path"] == str(tmp_pdf)


class TestDialogs:
    def test_metadata_dialog(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.dialogs.metadata_dialog import MetadataDialog

        dialog = MetadataDialog(document)
        dialog.title.setText("New title")
        dialog._save()
        assert document.metadata().title == "New title"

    def test_security_dialog(self, qapp) -> None:
        from pdfstudio.ui.dialogs.security_dialog import SecurityDialog

        dialog = SecurityDialog()
        dialog.user_password.setText("hunter2")
        dialog.checks["copy"].setChecked(False)
        config = dialog.result_settings()
        assert config.user_password == "hunter2"
        assert not config.permissions.copy

    def test_watermark_dialog_preview(self, qapp) -> None:
        from pdfstudio.ui.dialogs.watermark_dialog import WatermarkDialog

        dialog = WatermarkDialog()
        dialog.text.setText("SAMPLE")
        dialog.tile.setChecked(True)
        values = dialog.values()
        assert values["text"] == "SAMPLE" and values["tile"]
        assert dialog.preview.pixmap() is not None

    def test_print_dialog_ranges(self, qapp, document: PdfDocument) -> None:
        from pdfstudio.ui.dialogs.print_dialog import PrintDialog

        dialog = PrintDialog(document)
        dialog.range_edit.setText("1,3")
        assert dialog.selected_pages() == [0, 2]

    def test_script_console(self, qapp, window, tmp_pdf: Path) -> None:
        from pdfstudio.ui.dialogs.console import ScriptConsole

        window.controller.open_path(tmp_pdf)
        pump(qapp)
        console = ScriptConsole(window)
        console.editor.setPlainText("print(doc.page_count)")
        console.run_script()
        assert "3" in console.output.toPlainText()

    def test_console_reports_errors(self, qapp, window) -> None:
        from pdfstudio.ui.dialogs.console import ScriptConsole

        console = ScriptConsole(window)
        console.editor.setPlainText("1 / 0")
        console.run_script()
        assert "ZeroDivisionError" in console.output.toPlainText()

    def test_preferences_saves(self, qapp, window) -> None:
        from pdfstudio.ui.dialogs.preferences import PreferencesDialog

        dialog = PreferencesDialog(window)
        dialog.font_size.setValue(12)
        dialog._save()
        from pdfstudio.core.settings import settings

        assert settings().data.ui.font_size == 12

    def test_plugin_dialog(self, qapp, window) -> None:
        from pdfstudio.ui.dialogs.plugin_dialog import PluginDialog

        dialog = PluginDialog(window.plugins, window)
        assert dialog.table.rowCount() >= 2
        dialog._show_details()


# --------------------------------------------------------------------------- #
# Inline (on-page) text editing
# --------------------------------------------------------------------------- #
class TestInlineTextEditing:
    """The editor must appear on the page itself, never in a dialog."""

    @pytest.fixture
    def edited(self, qapp, window, tmp_path):
        """A window showing a three-line paragraph, ready to edit."""
        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.document import PdfDocument as Doc

        document = Doc.create()
        Editor(document).add(
            0,
            Rect(60, 60, 520, 220),
            "Alpha line one.\nBeta line two.\nGamma line three.",
            TextStyle(size=13),
        )
        path = tmp_path / "para.pdf"
        document.save_as(path)
        document.close()
        window.controller.open_path(path)
        pump(qapp, 20)
        return window

    def test_editor_opens_on_the_page(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        editor = edited.view.inline_editor
        assert editor is not None
        # It lives inside the canvas, not in a separate window.
        assert editor.parentWidget() is edited.view.viewport()
        assert editor.toPlainText() == "Beta line two."

    def test_only_the_clicked_line_is_loaded(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        text = edited.view.inline_editor.toPlainText()
        assert "Alpha" not in text
        assert "Gamma" not in text

    def test_paragraph_mode_loads_every_line(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, True)
        pump(qapp)
        text = edited.view.inline_editor.toPlainText()
        assert "Alpha" in text and "Gamma" in text

    def test_commit_updates_only_that_line(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        edited.view.inline_editor.setPlainText("BETA REPLACED")
        edited.view.inline_editor.commit()
        pump(qapp, 20)
        text = document.extract_text(0)
        assert "BETA REPLACED" in text
        assert "Alpha line one." in text
        assert "Gamma line three." in text
        assert edited.view.inline_editor is None

    def test_escape_cancels_without_changing_anything(self, qapp, edited) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        document = edited.document
        before = document.extract_text(0)
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        editor = edited.view.inline_editor
        editor.setPlainText("discard me")
        editor.keyPressEvent(
            QKeyEvent(
                QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
            )
        )
        pump(qapp, 15)
        assert document.extract_text(0) == before
        assert edited.view.inline_editor is None

    def test_editor_grows_as_text_wraps(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        editor = edited.view.inline_editor
        start_height = editor.height()
        editor.setPlainText(
            "A replacement that is deliberately long so that it has to wrap "
            "onto several lines inside the original column width."
        )
        pump(qapp, 12)
        assert editor.height() > start_height

    def test_committed_text_does_not_leave_the_page(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        edited.view.inline_editor.setPlainText(
            "An extremely long replacement sentence which in the old build "
            "would have continued straight past the right hand edge of the "
            "sheet instead of wrapping onto the following line."
        )
        edited.view.inline_editor.commit()
        pump(qapp, 20)
        width, _height = document.page_size(0)
        for block in document.extract_blocks(0):
            for text_line in block.lines:
                assert text_line.rect.x1 <= width - 4

    def test_format_toolbar_is_shown(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        editor = edited.view.inline_editor
        assert editor._toolbar is not None
        assert editor._toolbar.isVisible()
        style = editor._toolbar.current_style()
        assert style.size == pytest.approx(13, abs=0.51)

    def test_toolbar_bold_applies_to_the_commit(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        editor = edited.view.inline_editor
        editor._toolbar.bold.setChecked(True)
        pump(qapp)
        assert editor.style.bold
        editor.setPlainText("Bold beta")
        editor.commit()
        pump(qapp, 20)
        assert "Bold beta" in document.extract_text(0)

    def test_editor_follows_zoom(self, qapp, edited) -> None:
        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        editor = edited.view.inline_editor
        before = editor.geometry()
        edited.view.set_zoom(edited.view.zoom * 2)
        pump(qapp, 12)
        assert editor.geometry() != before

    def test_double_click_starts_an_edit(self, qapp, edited) -> None:
        """Double-clicking text with the Select tool edits it in place."""
        from pdfstudio.ui.widgets.page_view import Tool

        document = edited.document
        edited.view.set_tool(Tool.SELECT)
        line = document.extract_blocks(0)[0].lines[0]
        edited.view._begin_text_edit(0, line.rect.center)
        pump(qapp, 12)
        assert edited.view.inline_editor is not None

    def test_click_on_empty_space_creates_a_text_box(self, qapp, edited) -> None:
        document = edited.document
        edited.controller.edit_text_block(0, None, Point(200, 600), False)
        pump(qapp)
        editor = edited.view.inline_editor
        assert editor is not None and editor.toPlainText() == ""
        editor.setPlainText("Brand new text")
        editor.commit()
        pump(qapp, 20)
        assert "Brand new text" in document.extract_text(0)

    def test_no_modal_dialog_is_used(self, qapp, edited) -> None:
        """Regression: editing must not block on QInputDialog."""
        from PySide6.QtWidgets import QDialog

        document = edited.document
        line = document.extract_blocks(0)[0].lines[1]
        edited.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp)
        modal = [w for w in qapp.topLevelWidgets() if isinstance(w, QDialog) and w.isVisible()]
        assert not modal


class TestCommandCoverage:
    """Every ribbon button must actually do something when clicked."""

    def test_no_ribbon_command_is_dead(self, qapp, window) -> None:
        """Regression: Edit ▸ Edit text silently did nothing.

        A command with no handler prints "not available yet" and leaves the
        user thinking the feature is broken, so the whole surface is checked.
        """
        from pdfstudio.ui.main_window import TOOL_MAP
        from pdfstudio.ui.widgets.ribbon import build_default_tabs

        handled = set(window.controller._handlers)
        panels = {f"panel.{key}" for key in window._docks}
        window_local = {
            "app.next_tab",
            "app.prev_tab",
            "edit.find",
            "view.toolbar_mode",
            "view.fullscreen",
            "help.logs",
            "help.plugins_folder",
        }
        toggles = {
            "view.single",
            "view.continuous",
            "view.facing",
            "view.book",
            "view.invert",
            "view.annotations",
            "a11y.high_contrast",
        }

        dead = [
            item.identifier
            for tab in build_default_tabs()
            for group in tab.groups
            for item in group.items
            if item.identifier
            and not item.widget
            and item.identifier not in handled
            and item.identifier not in TOOL_MAP
            and item.identifier not in panels
            and item.identifier not in window_local
            and item.identifier not in toggles
        ]
        assert not dead, f"ribbon commands with no handler: {dead}"

    def test_edit_tab_edit_text_arms_the_tool(self, qapp, window, tmp_pdf) -> None:
        from pdfstudio.ui.widgets.page_view import Tool

        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.dispatch("content.edit_text")
        pump(qapp)
        assert window.view.tool is Tool.EDIT_TEXT

    def test_both_edit_text_buttons_agree(self, qapp, window, tmp_pdf) -> None:
        """The Home tool toggle and the Edit tab button do the same thing."""
        from pdfstudio.ui.widgets.page_view import Tool

        window.controller.open_path(tmp_pdf)
        pump(qapp)
        window.dispatch("tool.edit_text")
        pump(qapp)
        assert window.view.tool is Tool.EDIT_TEXT
        window.dispatch("tool.select")
        pump(qapp)
        window.dispatch("content.edit_text")
        pump(qapp)
        assert window.view.tool is Tool.EDIT_TEXT


class TestEditingEntryPoints:
    """Every documented way of reaching the in-place editor."""

    @pytest.fixture
    def ready(self, qapp, window, tmp_pdf):
        window.controller.open_path(tmp_pdf)
        pump(qapp, 20)
        return window

    def _click_point(self, window):
        document = window.document
        line = document.extract_blocks(0)[0].lines[0]
        return line.rect.center

    def test_edit_text_tool_click(self, qapp, ready) -> None:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        from pdfstudio.ui.widgets.page_view import Tool

        ready.view.set_tool(Tool.EDIT_TEXT)
        point = self._click_point(ready)
        page_rect = ready.view.page_rect_in_view(0)
        zoom = ready.view.zoom
        x = page_rect.x() + point.x * zoom
        y = page_rect.y() + point.y * zoom
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonPress,
            QPointF(x, y),
            ready.view.viewport().mapToGlobal(QPoint(int(x), int(y))),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        ready.view.mousePressEvent(event)
        pump(qapp, 15)
        assert ready.view.inline_editor is not None

    @pytest.mark.parametrize("tool_name", ["SELECT", "PAN", "TEXT_SELECT", "EDIT_TEXT"])
    def test_double_click_from_any_navigation_tool(self, qapp, ready, tool_name: str) -> None:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        from pdfstudio.ui.widgets.page_view import Tool

        ready.view.set_tool(getattr(Tool, tool_name))
        point = self._click_point(ready)
        page_rect = ready.view.page_rect_in_view(0)
        zoom = ready.view.zoom
        x = page_rect.x() + point.x * zoom
        y = page_rect.y() + point.y * zoom
        event = QMouseEvent(
            QMouseEvent.Type.MouseButtonDblClick,
            QPointF(x, y),
            ready.view.viewport().mapToGlobal(QPoint(int(x), int(y))),
            Qt.MouseButton.LeftButton,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        ready.view.mouseDoubleClickEvent(event)
        pump(qapp, 15)
        assert ready.view.inline_editor is not None
        ready.view.finish_inline_edit(commit=False)

    def test_context_menu_edits_at_the_click(self, qapp, ready) -> None:
        point = self._click_point(ready)
        ready.controller.edit_text_block(0, None, point, False)
        pump(qapp, 12)
        assert ready.view.inline_editor is not None

    def test_style_buttons_target_the_live_editor(self, qapp, ready) -> None:
        point = self._click_point(ready)
        ready.controller.edit_text_block(0, None, point, False)
        pump(qapp, 12)
        editor = ready.view.inline_editor
        assert not editor.style.bold
        ready.dispatch("style.bold")
        pump(qapp)
        assert editor.style.bold


class TestObjectMoving:
    """Drag, nudge and align objects on the page."""

    @pytest.fixture
    def arranged(self, qapp, window, tmp_path):
        """A page with text, a rule and an image, opened in the window."""
        import pymupdf as fitz

        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.document import PdfDocument as Doc

        document = Doc.create()
        Editor(document).add(
            0, Rect(60, 60, 520, 100), "Test Automation: Playwright", TextStyle(size=11)
        )
        Editor(document).add(
            0, Rect(60, 170, 520, 200), "PROFESSIONAL EXPERIENCE", TextStyle(size=13)
        )
        with document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(60, 165), fitz.Point(535, 165))
            shape.finish(width=1.2, color=(0, 0, 0))
            shape.commit()
        path = tmp_path / "arranged.pdf"
        document.save_as(path)
        document.close()
        window.controller.open_path(path)
        pump(qapp, 20)
        return window

    @staticmethod
    def _rule(window):
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        return next(
            o for o in ObjectService(window.document).objects(0) if o.kind is ObjectKind.DRAWING
        )

    def test_move_tool_is_selectable(self, qapp, arranged) -> None:
        from pdfstudio.ui.widgets.page_view import Tool

        arranged.dispatch("tool.move_object")
        pump(qapp)
        assert arranged.view.tool is Tool.MOVE_OBJECT

    def test_both_move_buttons_check_together(self, qapp, arranged) -> None:
        arranged.dispatch("tool.move_object")
        pump(qapp)
        buttons = arranged.ribbon._buttons
        assert buttons["tool.move_object"].isChecked()
        assert buttons["object.move"].isChecked()

    def test_clicking_selects_an_object(self, qapp, arranged) -> None:
        from PySide6.QtCore import QPoint, QPointF, Qt
        from PySide6.QtGui import QMouseEvent

        arranged.dispatch("tool.move_object")
        pump(qapp)
        rule = self._rule(arranged)
        page_rect = arranged.view.page_rect_in_view(0)
        zoom = arranged.view.zoom
        x = page_rect.x() + rule.rect.center.x * zoom
        y = page_rect.y() + rule.rect.center.y * zoom
        arranged.view.mousePressEvent(
            QMouseEvent(
                QMouseEvent.Type.MouseButtonPress,
                QPointF(x, y),
                arranged.view.viewport().mapToGlobal(QPoint(int(x), int(y))),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        pump(qapp)
        selected = arranged.view.selected_object
        assert selected is not None
        assert selected.kind.value == "drawing"

    def test_arrow_keys_nudge_repeatedly(self, qapp, arranged) -> None:
        """Regression: only the first nudge used to take effect."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        arranged.dispatch("tool.move_object")
        pump(qapp)
        rule = self._rule(arranged)
        start = rule.rect.y0
        arranged.view.set_selected_object(rule)
        pump(qapp)
        for _ in range(3):
            arranged.view.keyPressEvent(
                QKeyEvent(
                    QKeyEvent.Type.KeyPress, Qt.Key.Key_Up, Qt.KeyboardModifier.NoModifier
                )
            )
            pump(qapp, 10)
        moved = self._rule(arranged)
        assert moved.rect.y0 == pytest.approx(start - 6, abs=1.5)

    def test_backspace_pulls_the_object_left(self, qapp, arranged) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        arranged.dispatch("tool.move_object")
        pump(qapp)
        block = next(
            o
            for o in ObjectService(arranged.document).objects(0)
            if o.kind is ObjectKind.TEXT and "Test" in o.label
        )
        arranged.controller.move_object(block, 60, 0)
        pump(qapp, 12)
        shifted = arranged.view.selected_object
        assert shifted.rect.x0 == pytest.approx(120, abs=2)
        arranged.view.keyPressEvent(
            QKeyEvent(
                QKeyEvent.Type.KeyPress,
                Qt.Key.Key_Backspace,
                Qt.KeyboardModifier.NoModifier,
            )
        )
        pump(qapp, 12)
        assert arranged.view.selected_object.rect.x0 < shifted.rect.x0

    def test_align_left_snaps_to_the_margin(self, qapp, arranged) -> None:
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        arranged.dispatch("tool.move_object")
        pump(qapp)
        block = next(
            o
            for o in ObjectService(arranged.document).objects(0)
            if o.kind is ObjectKind.TEXT and "Test" in o.label
        )
        arranged.controller.move_object(block, 90, 0)
        pump(qapp, 12)
        arranged.dispatch("object.align_left")
        pump(qapp, 12)
        final = next(
            o
            for o in ObjectService(arranged.document).objects(0)
            if o.kind is ObjectKind.TEXT and "Test" in o.label
        )
        assert final.rect.x0 == pytest.approx(60, abs=2)

    def test_escape_clears_the_selection(self, qapp, arranged) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        arranged.dispatch("tool.move_object")
        pump(qapp)
        arranged.view.set_selected_object(self._rule(arranged))
        pump(qapp)
        arranged.view.keyPressEvent(
            QKeyEvent(
                QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier
            )
        )
        pump(qapp)
        assert arranged.view.selected_object is None

    def test_moving_the_rule_leaves_the_heading(self, qapp, arranged) -> None:
        """The reported collision: move the rule, keep the heading."""
        arranged.dispatch("tool.move_object")
        pump(qapp)
        rule = self._rule(arranged)
        arranged.controller.move_object(rule, 0, -14)
        pump(qapp, 15)
        text = arranged.document.extract_text(0)
        assert "PROFESSIONAL EXPERIENCE" in text
        assert "Test Automation" in text

    def test_paragraph_indent_commands(self, qapp, arranged) -> None:
        """Outdent pulls a wrapped line back towards the margin."""
        document = arranged.document
        line = document.extract_blocks(0)[0].lines[0]
        arranged.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp, 12)
        editor = arranged.view.inline_editor
        assert editor is not None
        arranged.dispatch("text.indent")
        pump(qapp)
        assert editor.style.wrap_indent == pytest.approx(12.0)
        arranged.dispatch("text.outdent")
        pump(qapp)
        assert editor.style.wrap_indent == pytest.approx(0.0)

    def test_alignment_commands_reach_the_editor(self, qapp, arranged) -> None:
        document = arranged.document
        line = document.extract_blocks(0)[0].lines[0]
        arranged.controller.edit_text_block(0, None, line.rect.center, False)
        pump(qapp, 12)
        editor = arranged.view.inline_editor
        arranged.dispatch("text.align_center")
        pump(qapp)
        assert editor.style.align == "center"
        arranged.dispatch("text.align_left")
        pump(qapp)
        assert editor.style.align == "left"


class TestObjectClipboardUi:
    """Copy, cut, paste and duplicate objects through the real UI path."""

    @pytest.fixture
    def arranged(self, qapp, window, tmp_path):
        """A tinted page with a rule and a heading, opened in the window."""
        import pymupdf as fitz

        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.document import PdfDocument as Doc

        document = Doc.create()
        with document.locked() as handle:
            page = handle[0]
            page.draw_rect(
                fitz.Rect(0, 0, page.rect.width, page.rect.height),
                color=None,
                fill=(0.9, 0.93, 1.0),
            )
        Editor(document).add(
            0, Rect(60, 170, 520, 200), "PROFESSIONAL EXPERIENCE", TextStyle(size=13)
        )
        with document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(60, 165), fitz.Point(535, 165))
            shape.finish(width=1.2, color=(0, 0, 0))
            shape.commit()
        path = tmp_path / "clip.pdf"
        document.save_as(path)
        document.close()
        window.controller.open_path(path)
        pump(qapp, 20)
        return window

    @staticmethod
    def _rules(window):
        from pdfstudio.pdfengine.objects import ObjectService

        return [
            o for o in ObjectService(window.document).objects(0) if o.label == "Horizontal rule"
        ]

    def _select_rule(self, qapp, window):
        window.dispatch("tool.move_object")
        pump(qapp)
        window.view.set_selected_object(self._rules(window)[0])
        pump(qapp)

    def test_copy_then_paste_adds_a_second_object(self, qapp, arranged) -> None:
        self._select_rule(qapp, arranged)
        arranged.controller.copy_object()
        assert arranged.controller.has_object_clip
        arranged.controller.paste_object()
        pump(qapp, 20)
        assert len(self._rules(arranged)) == 2

    def test_paste_selects_the_new_copy(self, qapp, arranged) -> None:
        """A paste that lands with nothing selected feels like it failed."""
        self._select_rule(qapp, arranged)
        arranged.controller.copy_object()
        arranged.controller.paste_object()
        pump(qapp, 20)
        assert arranged.view.selected_object is not None

    def test_duplicate_command(self, qapp, arranged) -> None:
        self._select_rule(qapp, arranged)
        arranged.dispatch("object.duplicate")
        pump(qapp, 20)
        assert len(self._rules(arranged)) == 2

    def test_cut_removes_and_paste_restores(self, qapp, arranged) -> None:
        self._select_rule(qapp, arranged)
        arranged.controller.cut_object()
        pump(qapp, 20)
        assert len(self._rules(arranged)) == 0
        arranged.controller.paste_object()
        pump(qapp, 20)
        assert len(self._rules(arranged)) == 1

    def test_ctrl_c_and_ctrl_v_on_the_canvas(self, qapp, arranged) -> None:
        """The keyboard path, not just the controller call."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        self._select_rule(qapp, arranged)
        for key in (Qt.Key.Key_C, Qt.Key.Key_V):
            arranged.view.keyPressEvent(
                QKeyEvent(QKeyEvent.Type.KeyPress, key, Qt.KeyboardModifier.ControlModifier)
            )
            pump(qapp, 20)
        assert len(self._rules(arranged)) == 2

    def test_ctrl_d_duplicates(self, qapp, arranged) -> None:
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        self._select_rule(qapp, arranged)
        arranged.view.keyPressEvent(
            QKeyEvent(
                QKeyEvent.Type.KeyPress,
                Qt.Key.Key_D,
                Qt.KeyboardModifier.ControlModifier,
            )
        )
        pump(qapp, 20)
        assert len(self._rules(arranged)) == 2

    def test_paste_is_undoable_from_the_ui(self, qapp, arranged) -> None:
        self._select_rule(qapp, arranged)
        arranged.controller.copy_object()
        arranged.controller.paste_object()
        pump(qapp, 20)
        arranged.controller.undo()
        pump(qapp, 20)
        assert len(self._rules(arranged)) == 1

    def test_copy_without_a_selection_is_harmless(self, qapp, arranged) -> None:
        arranged.view.set_selected_object(None)
        arranged.dispatch("edit.copy")
        pump(qapp)
        assert not arranged.controller.has_object_clip


class TestNoWhiteLayerInTheUi:
    """Selecting or editing must never drop an opaque patch on the page."""

    @pytest.fixture
    def tinted(self, qapp, window, tmp_path):
        import pymupdf as fitz

        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.document import PdfDocument as Doc

        document = Doc.create()
        with document.locked() as handle:
            page = handle[0]
            page.draw_rect(
                fitz.Rect(0, 0, page.rect.width, page.rect.height),
                color=None,
                fill=(0.2, 0.4, 0.8),
            )
        Editor(document).add(0, Rect(60, 60, 500, 90), "Editable line", TextStyle(size=12))
        path = tmp_path / "tinted.pdf"
        document.save_as(path)
        document.close()
        window.controller.open_path(path)
        pump(qapp, 20)
        return window

    def test_clicking_blank_space_selects_nothing(self, qapp, tinted) -> None:
        """The page background must not be grabbable by an empty-space click."""
        from pdfstudio.pdfengine.types import Point as DocPoint

        tinted.dispatch("tool.move_object")
        pump(qapp)
        assert tinted.view._object_at(0, DocPoint(450, 650)) is None

    def test_editing_blank_space_paints_no_mask(self, qapp, tinted) -> None:
        """A new text box has nothing underneath to hide."""
        from pdfstudio.pdfengine.types import Point as DocPoint

        tinted.controller.edit_text_block(0, None, DocPoint(300, 600), False)
        pump(qapp, 20)
        assert tinted.view._edit_mask is not None
        assert tinted.view._edit_mask[2] is False

    def test_editing_real_text_does_mask_it(self, qapp, tinted) -> None:
        from pdfstudio.pdfengine.types import Point as DocPoint

        tinted.controller.edit_text_block(0, None, DocPoint(120, 72), False)
        pump(qapp, 20)
        assert tinted.view._edit_mask is not None
        assert tinted.view._edit_mask[2] is True

    def test_edit_keeps_the_page_colour(self, qapp, tinted) -> None:
        from pdfstudio.pdfengine.content import TextEditor as Editor

        Editor(tinted.document).replace(
            0, Rect(60, 60, 500, 90), "Replaced", TextStyle(size=12)
        )
        pump(qapp, 20)
        with tinted.document.locked() as handle:
            pixel = handle[0].get_pixmap().pixel(450, 75)[:3]
        assert not all(channel > 240 for channel in pixel)


class TestAlignmentUi:
    """Alignment commands must actually change what is on screen."""

    @pytest.fixture
    def arranged(self, qapp, window, tmp_path):
        import pymupdf as fitz

        from pdfstudio.pdfengine.document import PdfDocument as Doc

        document = Doc.create()
        with document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(100, 300), fitz.Point(300, 300))
            shape.finish(width=1.0, color=(0, 0, 0))
            shape.commit()
        path = tmp_path / "align.pdf"
        document.save_as(path)
        document.close()
        window.controller.open_path(path)
        pump(qapp, 20)
        return window

    @staticmethod
    def _rule(window):
        from pdfstudio.pdfengine.objects import ObjectKind, ObjectService

        return next(
            o for o in ObjectService(window.document).objects(0) if o.kind is ObjectKind.DRAWING
        )

    def test_align_left_command_moves_the_object(self, qapp, arranged) -> None:
        arranged.dispatch("tool.move_object")
        arranged.view.set_selected_object(self._rule(arranged))
        pump(qapp)
        arranged.dispatch("object.align_left")
        pump(qapp, 20)
        assert self._rule(arranged).rect.x0 == pytest.approx(60.0, abs=1.5)

    def test_align_right_command_moves_the_object(self, qapp, arranged) -> None:
        arranged.dispatch("tool.move_object")
        arranged.view.set_selected_object(self._rule(arranged))
        pump(qapp)
        width, _height = arranged.document.page_size(0)
        arranged.dispatch("object.align_right")
        pump(qapp, 20)
        assert self._rule(arranged).rect.x1 == pytest.approx(width - 60.0, abs=1.5)

    def test_align_centre_command_moves_the_object(self, qapp, arranged) -> None:
        arranged.dispatch("tool.move_object")
        arranged.view.set_selected_object(self._rule(arranged))
        pump(qapp)
        width, _height = arranged.document.page_size(0)
        arranged.dispatch("object.align_center")
        pump(qapp, 20)
        rect = self._rule(arranged).rect
        assert rect.x0 == pytest.approx(width - rect.x1, abs=2.0)

    def test_text_align_falls_back_to_the_object(self, qapp, arranged) -> None:
        """With no editor open, "align left" should align the selection."""
        arranged.dispatch("tool.move_object")
        arranged.view.set_selected_object(self._rule(arranged))
        pump(qapp)
        arranged.dispatch("text.align_left")
        pump(qapp, 20)
        assert self._rule(arranged).rect.x0 == pytest.approx(60.0, abs=1.5)

    def test_toolbar_alignment_updates_the_editor(self, qapp, arranged) -> None:
        """The on-screen text must move, not just the style that gets written."""
        from PySide6.QtCore import Qt

        from pdfstudio.pdfengine.types import Point as DocPoint

        arranged.controller.edit_text_block(0, None, DocPoint(200, 400), False)
        pump(qapp, 20)
        editor = arranged.view.inline_editor
        assert editor is not None
        arranged.dispatch("text.align_center")
        pump(qapp, 20)
        assert editor.style.align == "center"
        assert editor.document().defaultTextOption().alignment() & Qt.AlignmentFlag.AlignHCenter

    def test_indent_survives_a_font_change(self, qapp, arranged) -> None:
        """Rebuilding the style from the toolbar used to reset the indent."""
        from pdfstudio.pdfengine.types import Point as DocPoint

        arranged.controller.edit_text_block(0, None, DocPoint(200, 400), False)
        pump(qapp, 20)
        editor = arranged.view.inline_editor
        arranged.dispatch("text.indent")
        pump(qapp, 20)
        assert editor.style.wrap_indent == pytest.approx(12.0)
        editor._toolbar.size.setCurrentText("16")
        pump(qapp, 20)
        assert editor.style.wrap_indent == pytest.approx(12.0)


class TestStaleRenderRegression:
    """The canvas must show the document as it is now, not as it was."""

    def test_generations_do_not_repeat_across_sessions(self) -> None:
        """The disk cache outlives the process; the counter must not restart.

        Two runs both starting at generation 0 produced identical cache keys
        for different page states, so the second run served the first run's
        stale pixels.
        """
        from pdfstudio.render.cache import GenerationTracker

        first = GenerationTracker()
        second = GenerationTracker()
        assert first.current("doc-1") != second.current("doc-1")

    def test_bump_still_increases(self) -> None:
        from pdfstudio.render.cache import GenerationTracker

        tracker = GenerationTracker()
        before = tracker.current("doc-1")
        assert tracker.bump("doc-1") == before + 1
        assert tracker.current("doc-1") == before + 1

    def test_an_edit_makes_earlier_renders_stale(self, qapp, window, tmp_pdf) -> None:
        """Any render started before an edit must be recognisable as old.

        ``_request_render`` captures the generation in its callback and drops
        results older than the renderer's current one; that guard is only
        sound if an edit really does advance the counter.
        """
        window.controller.open_path(tmp_pdf)
        pump(qapp, 20)
        renderer = window.view._renderer
        before = renderer.generation
        renderer.invalidate(0)  # what refresh() does after an edit
        assert before < renderer.generation

    def test_the_canvas_matches_the_document_after_a_move(self, qapp, window, tmp_path) -> None:
        """End-to-end: the shown pixels must be the post-edit pixels."""
        import pymupdf as fitz

        from pdfstudio.pdfengine.document import PdfDocument as Doc
        from pdfstudio.pdfengine.objects import ObjectService
        from pdfstudio.render.renderer import RenderRequest
        from pdfstudio.ui.widgets.page_view import _to_qimage

        document = Doc.create()
        with document.locked() as handle:
            shape = handle[0].new_shape()
            shape.draw_line(fitz.Point(60, 300), fitz.Point(500, 300))
            shape.finish(width=1.5, color=(0, 0, 0))
            shape.commit()
        path = tmp_path / "moved.pdf"
        document.save_as(path)
        document.close()

        window.controller.open_path(path)
        pump(qapp, 25)
        view = window.view
        rule = next(o for o in ObjectService(window.document).objects(0) if "rule" in o.label)
        window.controller.move_object(rule, 0, -40)
        pump(qapp, 40)

        shown = view._images.get(0)
        assert shown is not None
        # Render the current document with a generation no cache can hold.
        expected = _to_qimage(
            view._renderer.render(
                RenderRequest(
                    document_id=window.document.id,
                    page=0,
                    zoom=view._zoom,
                    generation=view._renderer.generation + 10_000,
                )
            )
        )
        assert shown.size() == expected.size()
        assert shown == expected


class TestRibbonFitsTheWindow:
    """The ribbon must never force the window wider than the screen.

    Regression: the Edit tab grew to ~1765 px when the Arrange and Paragraph
    groups were added. A tab page's full width became its
    ``minimumSizeHint``, ``QTabWidget`` reported the widest tab as the minimum
    for the whole bar, and that became a hard floor on the window. On a
    1568 px display the window could not shrink to fit, so Qt clipped the
    right-hand edge and "Cut" and "Move object" simply disappeared, with no
    scrollbar to reveal them.
    """

    #: Comfortably narrower than any tab's natural width.
    NARROW = 1100

    def test_the_bar_does_not_impose_a_width_floor(self, qapp) -> None:
        from pdfstudio.ui.widgets.ribbon import RibbonBar

        bar = RibbonBar()
        assert bar.minimumSizeHint().width() < 400

    def test_no_tab_imposes_a_width_floor(self, qapp) -> None:
        from pdfstudio.ui.widgets.ribbon import RibbonBar

        bar = RibbonBar()
        for index in range(bar.tabs.count()):
            page = bar.tabs.widget(index)
            assert page.minimumSizeHint().width() < 400, bar.tabs.tabText(index)

    def test_window_honours_a_narrow_size(self, qapp, window) -> None:
        window.resize(self.NARROW, 900)
        pump(qapp, 30)
        assert window.width() == self.NARROW

    def test_every_command_is_reachable_when_narrow(self, qapp, window) -> None:
        """Scrolled off-screen is fine; unreachable is not."""
        from PySide6.QtWidgets import QToolButton

        window.resize(self.NARROW, 900)
        pump(qapp, 30)
        ribbon = window.ribbon
        unreachable: list[tuple[str, str]] = []
        for index in range(ribbon.tabs.count()):
            ribbon.tabs.setCurrentIndex(index)
            pump(qapp, 10)
            scroller = ribbon.tabs.currentWidget()
            page = scroller.widget()
            for button in page.findChildren(QToolButton):
                if not button.visibleRegion().isEmpty():
                    continue
                scroller.ensureWidgetVisible(button)
                pump(qapp, 6)
                if button.visibleRegion().isEmpty():
                    unreachable.append((ribbon.tabs.tabText(index), button.text()))
        assert unreachable == []

    def test_no_tab_is_vertically_clipped(self, qapp, window) -> None:
        """Three-row groups must fit inside the ribbon's fixed height.

        The height also has to allow for a horizontal scrollbar, which eats
        into the viewport on any tab wider than the window.
        """
        window.resize(self.NARROW, 900)
        pump(qapp, 30)
        ribbon = window.ribbon
        for index in range(ribbon.tabs.count()):
            ribbon.tabs.setCurrentIndex(index)
            pump(qapp, 10)
            scroller = ribbon.tabs.currentWidget()
            page = scroller.widget()
            assert page.sizeHint().height() <= scroller.viewport().height(), (
                ribbon.tabs.tabText(index)
            )

    @pytest.mark.parametrize("identifier", ["edit.cut", "tool.move_object", "edit.copy"])
    def test_home_tab_commands_are_visible(self, qapp, window, identifier: str) -> None:
        """The exact buttons the user reported missing from Home."""
        window.resize(1568, 900)
        pump(qapp, 30)
        window.ribbon.tabs.setCurrentIndex(0)
        pump(qapp, 15)
        button = window.ribbon._buttons[identifier]
        assert not button.visibleRegion().isEmpty()

    def test_group_labels_are_never_cut_off_vertically(self, qapp, window) -> None:
        """A label may scroll off the side, but must never be sliced in half.

        Vertical clipping would mean the ribbon's fixed height is too small
        for its contents, which is a layout bug; horizontal scrolling is the
        intended behaviour on a narrow window.
        """
        from PySide6.QtWidgets import QLabel

        window.resize(1568, 900)
        pump(qapp, 30)
        ribbon = window.ribbon
        for index in range(ribbon.tabs.count()):
            ribbon.tabs.setCurrentIndex(index)
            pump(qapp, 10)
            scroller = ribbon.tabs.currentWidget()
            page = scroller.widget()
            for label in page.findChildren(QLabel, "RibbonGroupLabel"):
                bottom = label.mapTo(page, label.rect().bottomLeft()).y()
                assert bottom <= scroller.viewport().height(), (
                    f"{ribbon.tabs.tabText(index)} / {label.text()}"
                )


class TestDialogsNeverBlock:
    """Every modal must honour ``PDFSTUDIO_NO_DIALOGS``.

    A raw ``QMessageBox.exec()`` waits forever when there is nobody to click
    it. That made the command unreachable from tests and headless automation
    and hid any bug behind it — ``a11y.autofix`` hung a full-surface sweep
    indefinitely.
    """

    def test_inform_does_not_block(self, qapp, window) -> None:
        window.inform("Title", "Message")
        pump(qapp)
        assert "Message" in window._status_label.text()

    def test_warn_does_not_block(self, qapp, window) -> None:
        window.warn("Careful", "Something to note")
        pump(qapp)
        assert "Something to note" in window._status_label.text()

    def test_confirm_defaults_to_no(self, qapp, window) -> None:
        """Destructive actions must not proceed unattended."""
        assert window.confirm("Delete", "Really?") is False

    def test_confirm_can_default_to_yes(self, qapp, window) -> None:
        assert window.confirm("Proceed", "OK?", default_yes=True) is True

    def test_controller_has_no_raw_message_boxes(self) -> None:
        """Enforced statically so a new call site cannot reintroduce a hang."""
        import inspect

        from pdfstudio.ui import controller

        source = inspect.getsource(controller)
        for banned in (
            "QMessageBox.information(",
            "QMessageBox.warning(",
            "QMessageBox.question(",
            "QMessageBox.critical(",
        ):
            assert banned not in source, f"use window.inform/warn/confirm instead of {banned}"

    def test_autofix_completes_without_a_user(self, qapp, window, tmp_pdf) -> None:
        """The command that originally hung the sweep."""
        window.controller.open_path(tmp_pdf)
        pump(qapp, 20)
        window.dispatch("a11y.autofix")
        pump(qapp, 20)  # would never return if the dialog blocked


class TestEveryCommandSurvivesDispatch:
    """Dispatch the whole ribbon and assert nothing raises.

    Most commands end in a modal, so before the dialog helpers existed they
    could not be exercised at all. This is the sweep that found the
    ``clipboard.mimeData()`` crash.
    """

    @staticmethod
    def _neutralise_modals(monkeypatch) -> None:
        from PySide6.QtGui import QColor
        from PySide6.QtWidgets import (
            QColorDialog,
            QDialog,
            QFileDialog,
            QInputDialog,
        )

        monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("t", False))
        monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (1, False))
        monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("", False))
        monkeypatch.setattr(QInputDialog, "getMultiLineText", lambda *a, **k: ("", False))
        monkeypatch.setattr(QInputDialog, "getDouble", lambda *a, **k: (1.0, False))
        monkeypatch.setattr(QFileDialog, "getOpenFileName", lambda *a, **k: ("", ""))
        monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([], ""))
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
        monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: "")
        monkeypatch.setattr(QColorDialog, "getColor", lambda *a, **k: QColor("#ff0000"))
        monkeypatch.setattr(QDialog, "exec", lambda self: 0)

    @staticmethod
    def _identifiers() -> list[str]:
        from pdfstudio.ui.widgets.ribbon import build_default_tabs

        return sorted(
            {
                item.identifier
                for tab in build_default_tabs()
                for group in tab.groups
                for item in group.items
                if not item.widget
            }
            - {"file.close"}  # would tear down the fixture mid-sweep
        )

    def test_with_a_document(self, qapp, window, tmp_pdf, monkeypatch) -> None:
        self._neutralise_modals(monkeypatch)
        window.controller.open_path(tmp_pdf)
        pump(qapp, 20)
        failures: list[tuple[str, str]] = []
        for identifier in self._identifiers():
            try:
                window.dispatch(identifier)
                pump(qapp, 3)
            except Exception as exc:
                failures.append((identifier, f"{type(exc).__name__}: {exc}"))
        assert failures == []

    def test_without_a_document(self, qapp, window, monkeypatch) -> None:
        """Commands must degrade gracefully, not crash, with nothing open."""
        self._neutralise_modals(monkeypatch)
        failures: list[tuple[str, str]] = []
        for identifier in self._identifiers():
            try:
                window.dispatch(identifier)
                pump(qapp, 3)
            except Exception as exc:
                failures.append((identifier, f"{type(exc).__name__}: {exc}"))
        assert failures == []


class TestPasteWithoutAClipboard:
    """Regression: ``clipboard.mimeData()`` is None in a headless session."""

    def test_paste_reports_instead_of_crashing(self, qapp, window, tmp_pdf) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp, 20)
        window.controller.paste()  # must not raise
        pump(qapp)

    def test_paste_command_survives(self, qapp, window, tmp_pdf) -> None:
        window.controller.open_path(tmp_pdf)
        pump(qapp, 20)
        window.dispatch("edit.paste")
        pump(qapp)


class TestTabStripIsVisible:
    """The ribbon tab strip must read as tabs in every theme.

    Regression: the tabs were laid out correctly but effectively invisible.
    Several palettes set ``tab_inactive`` to exactly ``surface``, so unselected
    tabs had no edge against the background, and the selected tab's only cue
    was a 2 px underline — no enclosing shape. The strip looked like a row of
    loose words, so the tabs appeared "missing".

    These assertions are on the palette rather than on rendered pixels: the
    contrast ratios are what actually decide whether a user can see the tabs,
    and they hold regardless of platform font rendering.
    """

    @staticmethod
    def _luminance(colour: str) -> float:
        text = colour.lstrip("#")[:6]
        channels = [int(text[i : i + 2], 16) / 255 for i in (0, 2, 4)]
        linear = [c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
        return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]

    @classmethod
    def _contrast(cls, first: str, second: str) -> float:
        a, b = cls._luminance(first), cls._luminance(second)
        return (max(a, b) + 0.05) / (min(a, b) + 0.05)

    @staticmethod
    def _themes():
        from pdfstudio.ui.theme import BUILTIN_THEMES

        return BUILTIN_THEMES

    def test_inactive_tabs_differ_from_their_strip(self) -> None:
        """A tab the same colour as the rail behind it is invisible."""
        for name, theme in self._themes().items():
            palette = theme.palette
            assert palette.tab_inactive != palette.tab_strip, name
            assert self._contrast(palette.tab_inactive, palette.tab_strip) >= 1.10, name

    def test_selected_tab_differs_from_unselected(self) -> None:
        for name, theme in self._themes().items():
            palette = theme.palette
            assert palette.tab_active != palette.tab_inactive, name
            assert self._contrast(palette.tab_active, palette.tab_inactive) >= 1.10, name

    def test_tab_labels_are_readable(self) -> None:
        """4.5:1 is the WCAG AA minimum for body text."""
        for name, theme in self._themes().items():
            palette = theme.palette
            assert self._contrast(palette.text_muted, palette.tab_inactive) >= 4.5, name
            assert self._contrast(palette.text, palette.tab_active) >= 4.5, name

    def test_every_theme_defines_a_tab_strip_colour(self) -> None:
        for name, theme in self._themes().items():
            assert theme.palette.tab_strip, name

    def test_stylesheet_gives_the_selected_tab_a_shape(self) -> None:
        """Not just an underline: the tab needs a border and a background."""
        from pdfstudio.ui.theme import BUILTIN_THEMES, build_stylesheet

        sheet = build_stylesheet(BUILTIN_THEMES["dark"])
        selected = sheet.split("QTabBar::tab:selected")[1].split("}")[0]
        assert "background" in selected
        assert "border-top" in selected

    def test_a_user_theme_without_tab_strip_still_loads(self) -> None:
        """Old theme JSON predates the new key and must not break."""
        from pdfstudio.ui.theme import Theme

        theme = Theme.from_dict(
            {"name": "Legacy", "identifier": "legacy", "palette": {"surface": "#123456"}}
        )
        assert theme.palette.tab_strip  # falls back to the dataclass default

    @pytest.mark.parametrize("identifier", ["dark", "light", "sepia", "high-contrast"])
    def test_tabs_are_laid_out_with_the_theme_applied(self, qapp, identifier: str) -> None:
        """Build the window the way ``app.py`` does — theme first.

        Constructing ``MainWindow`` without applying a theme (as the other
        tests do) exercises Qt's default style, which is not what a user ever
        sees and hid this bug.
        """
        from pdfstudio.ui.main_window import MainWindow
        from pdfstudio.ui.theme import ThemeManager

        manager = ThemeManager()
        manager.apply(identifier, qapp)
        window = MainWindow(theme_manager=manager)
        window.show()
        window.resize(1568, 900)
        pump(qapp, 30)
        try:
            bar = window.ribbon.tabs.tabBar()
            assert bar.count() == 10
            assert not bar.visibleRegion().isEmpty()
            for index in range(bar.count()):
                rect = bar.tabRect(index)
                assert rect.width() > 20, bar.tabText(index)
                assert rect.height() > 10, bar.tabText(index)
        finally:
            # deleteLater() as well as close(): a closed MainWindow keeps ~18
            # child top-levels alive, and these parametrised cases build a
            # fresh window each time.
            window.close()
            window.deleteLater()
            pump(qapp, 5)
            manager.apply("dark", qapp)


class TestCaptionsAreNotElided:
    """Large ribbon buttons must fit their own caption after theming.

    Regression: the minimum width was measured once at construction, but a
    theme is applied *after* the ribbon is built and uses a larger font, so
    "Edit text" rendered as "Edi...ext".
    """

    def test_large_buttons_grow_with_the_font(self, qapp) -> None:
        from PySide6.QtGui import QFont

        from pdfstudio.ui.widgets.ribbon import RibbonBar

        bar = RibbonBar()
        button = bar._buttons["tool.edit_text"]
        before = button.sizeHint().width()
        font = QFont(button.font())
        font.setPointSizeF(font.pointSizeF() + 6)
        button.setFont(font)
        pump(qapp, 5)
        assert button.sizeHint().width() > before

    def test_no_caption_is_clipped_in_any_tab(self, qapp) -> None:
        from PySide6.QtWidgets import QToolButton

        from pdfstudio.ui.main_window import MainWindow
        from pdfstudio.ui.theme import ThemeManager

        manager = ThemeManager()
        manager.apply("dark", qapp)
        window = MainWindow(theme_manager=manager)
        window.show()
        window.resize(1568, 900)
        pump(qapp, 30)
        clipped: list[tuple[str, str, int, int]] = []
        try:
            ribbon = window.ribbon
            for index in range(ribbon.tabs.count()):
                # Each tab must be shown before Qt gives its children a real
                # geometry; measuring a tab that was never displayed reports a
                # meaningless default width.
                ribbon.tabs.setCurrentIndex(index)
                pump(qapp, 12)
                page = ribbon.tabs.currentWidget().widget()
                for button in page.findChildren(QToolButton):
                    if not button.text():
                        continue
                    # Compare against the width Qt itself says the button
                    # needs. A bare text-advance comparison is not enough:
                    # ToolButtonTextUnderIcon also reserves padding, so a
                    # button can be wider than its text and still elide.
                    needed = button.sizeHint().width()
                    if needed > button.width() + 1:
                        clipped.append(
                            (
                                ribbon.tabs.tabText(index),
                                button.text(),
                                button.width(),
                                needed,
                            )
                        )
        finally:
            window.close()
            window.deleteLater()
            pump(qapp, 5)
        assert clipped == []


class TestStartupIsNotImmersive:
    """The app must never launch straight into full screen or presentation.

    Regression: ``saveGeometry()`` encodes the full-screen flag, so once a
    user pressed F11 or F5 the window came back full screen on *every*
    subsequent launch — chrome hidden, with no obvious way out.
    """

    def test_save_geometry_round_trip_drops_fullscreen(self, qapp) -> None:
        """The heart of the bug, isolated from the rest of the window."""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QMainWindow

        source = QMainWindow()
        source.resize(1200, 800)
        source.show()
        pump(qapp, 5)
        source.setWindowState(source.windowState() | Qt.WindowState.WindowFullScreen)
        pump(qapp, 5)
        blob = source.saveGeometry()
        source.close()

        restored = QMainWindow()
        restored.restoreGeometry(blob)
        # Qt really does bring the flag back; this is why the fix is needed.
        assert bool(restored.windowState() & Qt.WindowState.WindowFullScreen)
        restored.close()

    def test_window_clears_fullscreen_on_restore(self, qapp, window) -> None:
        from PySide6.QtCore import Qt

        window.setWindowState(window.windowState() | Qt.WindowState.WindowFullScreen)
        pump(qapp, 5)
        window._leave_immersive_modes()
        pump(qapp, 5)
        assert not bool(window.windowState() & Qt.WindowState.WindowFullScreen)

    def test_restore_puts_the_ribbon_back(self, qapp, window) -> None:
        """Presentation mode hides the chrome, and saveState() records that."""
        window._ribbon_dock.hide()
        pump(qapp, 5)
        window._leave_immersive_modes()
        pump(qapp, 5)
        assert not window._ribbon_dock.isHidden()

    def test_maximised_is_still_remembered(self, qapp, window) -> None:
        """Only full screen is stripped — maximised is a normal window mode."""
        from PySide6.QtCore import Qt

        window.setWindowState(window.windowState() | Qt.WindowState.WindowMaximized)
        pump(qapp, 5)
        window._leave_immersive_modes()
        pump(qapp, 5)
        assert bool(window.windowState() & Qt.WindowState.WindowMaximized)

    def test_a_fresh_window_is_not_fullscreen(self, qapp, window) -> None:
        assert not window.isFullScreen()
        view = window.view
        assert view is None or not view.is_presentation


class TestParagraphCommandsInTheUi:
    """Paragraph selection, line reordering and Word-style formatting."""

    @pytest.fixture
    def edited(self, qapp, window, tmp_path):
        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.document import PdfDocument as Doc

        document = Doc.create()
        Editor(document).add(
            0,
            Rect(60, 100, 420, 170),
            "Alpha item about timelines.\nBeta item about budget.\nGamma item about staffing.",
            TextStyle(size=11),
        )
        path = tmp_path / "para.pdf"
        document.save_as(path)
        document.close()
        window.controller.open_path(path)
        pump(qapp, 20)
        return window

    @staticmethod
    def _lines(window) -> list[str]:
        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.types import Point as DocPoint

        editor = Editor(window.document)
        anchor = editor.paragraph_lines(0, DocPoint(70, 105))
        if not anchor:
            return []
        return [ln.text for ln in editor.paragraph_lines(0, anchor[0].rect.center)]

    def _place_cursor(self, window, index: int = 0) -> None:
        from pdfstudio.pdfengine.content import TextEditor as Editor
        from pdfstudio.pdfengine.types import Point as DocPoint

        lines = Editor(window.document).paragraph_lines(0, DocPoint(70, 105))
        window.view.set_last_text_point(0, lines[index].rect.center)

    def test_move_line_down_command(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        before = self._lines(edited)
        edited.dispatch("text.move_line_down")
        pump(qapp, 20)
        after = self._lines(edited)
        assert after[0] == before[1]
        assert after[1] == before[0]

    def test_move_line_up_command(self, qapp, edited) -> None:
        self._place_cursor(edited, 1)
        before = self._lines(edited)
        edited.dispatch("text.move_line_up")
        pump(qapp, 20)
        after = self._lines(edited)
        assert after[0] == before[1]

    def test_alt_arrow_moves_a_line(self, qapp, edited) -> None:
        """The real keyboard path, not just the command."""
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QKeyEvent

        self._place_cursor(edited, 0)
        before = self._lines(edited)
        edited.view.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Down, Qt.KeyboardModifier.AltModifier)
        )
        pump(qapp, 20)
        assert self._lines(edited)[0] == before[1]

    def test_repeated_moves_follow_the_same_line(self, qapp, edited) -> None:
        """The cursor must travel with the line, or the second press undoes it."""
        self._place_cursor(edited, 0)
        first = self._lines(edited)[0]
        edited.dispatch("text.move_line_down")
        pump(qapp, 20)
        edited.dispatch("text.move_line_down")
        pump(qapp, 20)
        assert self._lines(edited)[2] == first

    def test_edit_paragraph_command_opens_the_editor(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        edited.dispatch("text.edit_paragraph")
        pump(qapp, 20)
        editor = edited.view.inline_editor
        assert editor is not None
        # All three lines, not just the one under the cursor.
        assert editor.toPlainText().count("\n") == 2

    def test_bullets_command(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        edited.dispatch("text.bullets")
        pump(qapp, 20)
        assert all(
            line.lstrip().startswith(("\u2022", "\u00b7")) for line in self._lines(edited)
        )

    def test_numbering_command(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        edited.dispatch("text.numbering")
        pump(qapp, 20)
        lines = self._lines(edited)
        assert len(lines) == 3
        assert lines[0].startswith("1.")

    def test_case_command(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        edited.dispatch("text.case_upper")
        pump(qapp, 20)
        assert self._lines(edited)[0].isupper()

    def test_duplicate_and_delete_line(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        edited.dispatch("text.duplicate_line")
        pump(qapp, 20)
        assert len(self._lines(edited)) == 4
        self._place_cursor(edited, 0)
        edited.dispatch("text.delete_line")
        pump(qapp, 20)
        assert len(self._lines(edited)) == 3

    def test_format_painter_round_trip(self, qapp, edited) -> None:
        self._place_cursor(edited, 0)
        edited.dispatch("text.copy_format")
        pump(qapp, 10)
        assert edited.controller.has_format_clip
        edited.dispatch("text.paste_format")
        pump(qapp, 20)
        assert "Formatting applied" in edited._status_label.text()

    def test_word_count_reports_totals(self, qapp, edited) -> None:
        edited.dispatch("review.word_count")
        pump(qapp, 10)
        assert "words" in edited._status_label.text()

    def test_commands_without_a_cursor_do_not_crash(self, qapp, window) -> None:
        """No document, no click: must report rather than raise."""
        for command in (
            "text.move_line_up",
            "text.bullets",
            "text.case_upper",
            "text.duplicate_line",
            "text.paste_format",
        ):
            window.dispatch(command)
            pump(qapp, 3)
