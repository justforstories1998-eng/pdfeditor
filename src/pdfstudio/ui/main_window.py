"""The main application window: tabs, docks, ribbon, status bar and commands.

This is the *view* in the MVVM split.  It owns no PDF logic; every command is
dispatched to :class:`~pdfstudio.ui.controller.DocumentController`, which
performs the operation through the engine services and pushes undoable
commands.  That separation keeps the window testable and lets the CLI, plugins
and the REST worker reuse exactly the same operations.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QByteArray,
    QPoint,
    QSettings,
    Qt,
    QTimer,
    QUrl,
    Slot,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QKeySequence,
    QShortcut,
)
from PySide6.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QStatusBar,
    QTabWidget,
    QToolButton,
    QWidget,
)

from pdfstudio import APP_NAME, __version__
from pdfstudio.core.events import Event, Topic, bus
from pdfstudio.core.exceptions import PdfStudioError
from pdfstudio.core.jobs import jobs
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.settings import settings
from pdfstudio.pdfengine.document import PdfDocument
from pdfstudio.pdfengine.types import PageLayout, Point, ZoomMode
from pdfstudio.plugins.manager import PluginManager
from pdfstudio.render.renderer import PageRenderer
from pdfstudio.services.autosave import AutosaveService, RecoveryManager
from pdfstudio.services.history import HistoryStore
from pdfstudio.ui.panels.side_panels import (
    AttachmentsPanel,
    BookmarkPanel,
    CommentsPanel,
    LayersPanel,
    PropertiesPanel,
    SearchPanel,
)
from pdfstudio.ui.panels.thumbnails import ThumbnailPanel
from pdfstudio.ui.theme import ThemeManager
from pdfstudio.ui.widgets.page_view import PageView, Tool
from pdfstudio.ui.widgets.ribbon import ClassicToolBar, RibbonBar

log = get_logger("ui.main")

#: Tool identifiers exposed by the ribbon, mapped to canvas tools.
TOOL_MAP: dict[str, Tool] = {
    "tool.select": Tool.SELECT,
    "tool.pan": Tool.PAN,
    "tool.text_select": Tool.TEXT_SELECT,
    "tool.zoom": Tool.ZOOM,
    "tool.edit_text": Tool.EDIT_TEXT,
    "tool.move_object": Tool.MOVE_OBJECT,
    "object.move": Tool.MOVE_OBJECT,
    "annot.highlight": Tool.HIGHLIGHT,
    "annot.underline": Tool.UNDERLINE,
    "annot.strikeout": Tool.STRIKEOUT,
    "annot.note": Tool.NOTE,
    "annot.textbox": Tool.TEXT_BOX,
    "annot.ink": Tool.INK,
    "annot.rectangle": Tool.RECTANGLE,
    "annot.ellipse": Tool.ELLIPSE,
    "annot.arrow": Tool.ARROW,
    "annot.measure": Tool.MEASURE,
    "secure.mark_redaction": Tool.REDACT,
}


def _tool_name(tool: Tool) -> str:
    return tool.value.replace("_", " ").title()


#: What each tool actually does, shown in the status bar when it is armed.
#: Picking a tool and being told only its name leaves the keyboard shortcuts
#: undiscoverable, which is the single most common source of "it does nothing".
TOOL_HINTS: dict[Tool, str] = {
    Tool.SELECT: "Select: double-click any text to edit it in place",
    Tool.PAN: "Pan: drag to scroll, or hold the middle mouse button anywhere",
    Tool.TEXT_SELECT: "Select text: drag to select, Ctrl+C to copy",
    Tool.ZOOM: "Zoom: drag a rectangle to zoom into it",
    Tool.EDIT_TEXT: (
        "Edit text: click a line to change it in place · Alt+click for the whole "
        "paragraph · Enter applies, Esc cancels"
    ),
    Tool.MOVE_OBJECT: (
        "Move object: click anything to select it · drag or use the arrow keys · "
        "Ctrl+C/Ctrl+V to copy · Ctrl+D to duplicate · Shift+Delete to remove"
    ),
    Tool.HIGHLIGHT: "Highlight: drag across the text you want to mark",
    Tool.NOTE: "Sticky note: click where the note should go",
    Tool.INK: "Draw: drag to draw freehand",
    Tool.REDACT: "Redact: drag over content, then Protect ▸ Apply redactions",
}


class DocumentTab(QWidget):
    """One open document: its view, renderer and per-document services."""

    def __init__(self, document: PdfDocument, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        from PySide6.QtWidgets import QVBoxLayout

        self.document = document
        self.renderer = PageRenderer(document)
        self.view = PageView(self)
        self.view.set_document(document, self.renderer)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)

    @property
    def title(self) -> str:
        marker = "•  " if self.document.is_modified else ""
        return f"{marker}{self.document.display_name}"

    def close_document(self) -> None:
        self.view.set_document(None)
        self.document.close()


class MainWindow(QMainWindow):
    """PDF Studio's main window."""

    def __init__(
        self,
        *,
        theme_manager: ThemeManager | None = None,
        plugin_manager: PluginManager | None = None,
    ) -> None:
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1560, 980)
        self.setAcceptDrops(True)
        self.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
        )

        self.settings = settings()
        self.themes = theme_manager or ThemeManager()
        self.history = HistoryStore()
        self.autosave = AutosaveService(self.settings.data.autosave)
        self.plugins = plugin_manager
        self._closing = False
        #: When ``True``, closing never prompts about unsaved changes
        #: (set by tests and by the ``PDFSTUDIO_DISCARD_ON_CLOSE`` variable).
        self.discard_on_close = bool(os.environ.get("PDFSTUDIO_DISCARD_ON_CLOSE"))
        #: When ``True``, errors go to the status bar instead of a modal box.
        self.suppress_dialogs = bool(os.environ.get("PDFSTUDIO_NO_DIALOGS"))

        # Deferred import avoids a cycle: controller imports UI types.
        from pdfstudio.ui.controller import DocumentController

        self.controller = DocumentController(self)

        # Construction order matters: the ribbon owns the actions the docks
        # reference, and the docks/panels must exist before the tab widget
        # emits its first currentChanged signal.
        self._docks: dict[str, QDockWidget] = {}
        self._build_ribbon()
        self._build_docks()
        self._build_tabs()
        self._build_status_bar()
        self._build_menu()
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._connect_events()
        self._refresh_all_panels()
        self._restore_geometry()

        self.autosave.start()
        if self.settings.data.autosave.crash_recovery and not os.environ.get(
            "PDFSTUDIO_NO_RECOVERY_PROMPT"
        ):
            QTimer.singleShot(400, self._check_recovery)
        log.info("{} {} ready", APP_NAME, __version__)

    # ------------------------------------------------------------------ #
    # Construction
    # ------------------------------------------------------------------ #
    def _build_tabs(self) -> None:
        self.tabs = QTabWidget(self)
        self.tabs.setDocumentMode(True)
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.setElideMode(Qt.TextElideMode.ElideMiddle)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        # currentChanged is connected in __init__ once every panel, the ribbon
        # and the status bar exist, so the first signal cannot fire early.
        self.setCentralWidget(self.tabs)

        self._welcome = QLabel(
            f"<h2 style='font-weight:500'>{APP_NAME}</h2>"
            "<p>Open a PDF with <b>Ctrl+O</b>, or drop files here.</p>",
            self,
        )
        self._welcome.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.tabs.addTab(self._welcome, "Welcome")

    def _build_ribbon(self) -> None:
        self.ribbon = RibbonBar(parent=self)
        self.ribbon.command.connect(self.dispatch)
        self.ribbon.toggled.connect(self._on_ribbon_toggle)
        self.ribbon.zoom_requested.connect(self._on_zoom_requested)
        self.ribbon.page_requested.connect(self._on_page_requested)

        self._ribbon_dock = QDockWidget("Ribbon", self)
        self._ribbon_dock.setObjectName("RibbonDock")
        self._ribbon_dock.setTitleBarWidget(QWidget())
        self._ribbon_dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        self._ribbon_dock.setWidget(self.ribbon)
        self.addDockWidget(Qt.DockWidgetArea.TopDockWidgetArea, self._ribbon_dock)

        self.classic_toolbar = ClassicToolBar(self)
        self.classic_toolbar.setObjectName("ClassicToolBar")
        self.classic_toolbar.command.connect(self.dispatch)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self.classic_toolbar)
        self.set_toolbar_mode(self.settings.data.ui.toolbar_mode)

    def _build_docks(self) -> None:
        self.thumbnail_panel = ThumbnailPanel(self)
        self.bookmark_panel = BookmarkPanel(self)
        self.comments_panel = CommentsPanel(self)
        self.attachments_panel = AttachmentsPanel(self)
        self.layers_panel = LayersPanel(self)
        self.search_panel = SearchPanel(self)
        self.properties_panel = PropertiesPanel(self)

        left = [
            ("thumbnails", "Pages", self.thumbnail_panel),
            ("bookmarks", "Bookmarks", self.bookmark_panel),
            ("comments", "Comments", self.comments_panel),
            ("layers", "Layers", self.layers_panel),
            ("attachments", "Attachments", self.attachments_panel),
        ]
        previous: QDockWidget | None = None
        for key, title, widget in left:
            dock = self._make_dock(key, title, widget, Qt.DockWidgetArea.LeftDockWidgetArea)
            if previous is not None:
                self.tabifyDockWidget(previous, dock)
            previous = dock
        self._docks["thumbnails"].raise_()

        self._make_dock(
            "search", "Search", self.search_panel, Qt.DockWidgetArea.RightDockWidgetArea
        )
        self._make_dock(
            "properties",
            "Properties",
            self.properties_panel,
            Qt.DockWidgetArea.RightDockWidgetArea,
        )
        self._docks["search"].hide()
        self._docks["properties"].hide()
        self.resizeDocks(
            [self._docks["thumbnails"]],
            [self.settings.data.ui.sidebar_width],
            Qt.Orientation.Horizontal,
        )

        # panel signals
        self.thumbnail_panel.page_selected.connect(self._goto_page)
        self.thumbnail_panel.pages_reordered.connect(self.controller.move_pages)
        self.thumbnail_panel.pages_action.connect(self.controller.page_action)
        self.bookmark_panel.navigate.connect(self._goto_point)
        self.bookmark_panel.bookmarks_changed.connect(self.controller.set_bookmarks)
        self.bookmark_panel.generate_requested.connect(lambda: self.dispatch("ai.bookmarks"))
        self.comments_panel.navigate.connect(self._goto_point)
        self.comments_panel.comment_action.connect(self.controller.comment_action)
        self.attachments_panel.attachment_action.connect(self.controller.attachment_action)
        self.layers_panel.layer_toggled.connect(self.controller.toggle_layer)
        self.search_panel.search_requested.connect(self.controller.run_search)
        self.search_panel.hit_selected.connect(self._show_search_hit)
        self.search_panel.highlight_all_requested.connect(
            lambda: self.dispatch("search.highlight_all")
        )
        self.properties_panel.metadata_edit_requested.connect(
            lambda: self.dispatch("document.metadata")
        )

    def _make_dock(
        self, key: str, title: str, widget: QWidget, area: Qt.DockWidgetArea
    ) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"Dock_{key}")
        dock.setWidget(widget)
        dock.setAllowedAreas(
            Qt.DockWidgetArea.LeftDockWidgetArea | Qt.DockWidgetArea.RightDockWidgetArea
        )
        self.addDockWidget(area, dock)
        self._docks[key] = dock
        dock.visibilityChanged.connect(
            lambda visible, k=key: self.ribbon.set_checked(f"panel.{k}", visible)
        )
        return dock

    def _build_status_bar(self) -> None:
        bar = QStatusBar(self)
        self.setStatusBar(bar)

        self._status_label = QLabel("Ready", bar)
        self._page_label = QLabel("—", bar)
        self._zoom_label = QLabel("100%", bar)
        self._size_label = QLabel("", bar)
        self._job_label = QLabel("", bar)
        self._progress = QProgressBar(bar)
        self._progress.setMaximumWidth(170)
        self._progress.setVisible(False)

        self._cancel_button = QToolButton(bar)
        self._cancel_button.setText("✕")
        self._cancel_button.setToolTip("Cancel the running task")
        self._cancel_button.setVisible(False)
        self._cancel_button.clicked.connect(lambda: jobs().cancel_all())

        bar.addWidget(self._status_label, 1)
        bar.addPermanentWidget(self._job_label)
        bar.addPermanentWidget(self._progress)
        bar.addPermanentWidget(self._cancel_button)
        bar.addPermanentWidget(self._size_label)
        bar.addPermanentWidget(self._page_label)
        bar.addPermanentWidget(self._zoom_label)

    def _build_menu(self) -> None:
        """Menu bar mirroring the ribbon (needed on macOS and for screen readers)."""
        menubar = self.menuBar()
        actions = self.ribbon.actions_map()

        def add(menu: QMenu, identifier: str, fallback: str = "") -> None:
            action = actions.get(identifier)
            title = action.text() if action else fallback or identifier
            item = QAction(title, self)
            if action and not action.shortcut().isEmpty():
                item.setShortcut(action.shortcut())
            item.triggered.connect(lambda _c=False, key=identifier: self.dispatch(key))
            menu.addAction(item)

        file_menu = menubar.addMenu("&File")
        for key in ("file.new", "file.open", "file.save", "file.save_as"):
            add(file_menu, key, key.split(".")[-1].replace("_", " ").title())
        file_menu.addSeparator()
        self._recent_menu = file_menu.addMenu("Open &recent")
        self._rebuild_recent_menu()
        file_menu.addSeparator()
        for key in ("convert.from_file", "file.print", "file.close"):
            add(file_menu, key)
        file_menu.addSeparator()
        quit_action = QAction("E&xit", self)
        quit_action.setShortcut(QKeySequence.StandardKey.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        edit_menu = menubar.addMenu("&Edit")
        for key in (
            "edit.undo",
            "edit.redo",
            "edit.copy",
            "edit.paste",
            "edit.find",
            "edit.replace",
        ):
            add(edit_menu, key)
        edit_menu.addSeparator()
        add(edit_menu, "app.preferences", "Preferences…")

        view_menu = menubar.addMenu("&View")
        for key in (
            "view.zoom_in",
            "view.zoom_out",
            "view.fit_page",
            "view.fit_width",
            "view.single",
            "view.continuous",
            "view.facing",
            "view.book",
            "view.presentation",
            "view.fullscreen",
            "view.invert",
        ):
            add(view_menu, key)
        view_menu.addSeparator()
        panels_menu = view_menu.addMenu("Panels")
        for dock in self._docks.values():
            panels_menu.addAction(dock.toggleViewAction())

        for title, keys in (
            (
                "&Pages",
                (
                    "pages.insert",
                    "pages.delete",
                    "pages.rotate_right",
                    "pages.crop",
                    "pages.merge",
                    "pages.split",
                    "pages.nup",
                    "pages.booklet",
                ),
            ),
            (
                "&Comment",
                (
                    "annot.highlight",
                    "annot.note",
                    "annot.ink",
                    "annot.export",
                    "annot.import",
                    "annot.flatten",
                ),
            ),
            (
                "F&orms",
                (
                    "form.text",
                    "form.checkbox",
                    "form.dropdown",
                    "form.export",
                    "form.import",
                    "form.flatten",
                ),
            ),
            (
                "&Protect",
                (
                    "secure.encrypt",
                    "secure.permissions",
                    "secure.sanitize",
                    "secure.apply_redaction",
                    "sign.sign",
                    "sign.validate",
                ),
            ),
            (
                "&Tools",
                (
                    "ocr.run",
                    "ai.summarize",
                    "ai.chat",
                    "compare.documents",
                    "batch.run",
                    "optimize.compress",
                    "plugins.manage",
                    "script.console",
                ),
            ),
        ):
            menu = menubar.addMenu(title)
            for key in keys:
                add(menu, key)

        help_menu = menubar.addMenu("&Help")
        add(help_menu, "help.manual", "User manual")
        add(help_menu, "help.shortcuts", "Keyboard shortcuts")
        add(help_menu, "help.logs", "Open log folder")
        add(help_menu, "help.plugins_folder", "Open plugin folder")
        help_menu.addSeparator()
        add(help_menu, "help.about", f"About {APP_NAME}")

        # global shortcuts not tied to a visible button
        for sequence, command in (
            ("Ctrl+F", "edit.find"),
            ("F5", "view.presentation"),
            ("F11", "view.fullscreen"),
            ("Ctrl+Shift+P", "app.command_palette"),
            ("Ctrl+Tab", "app.next_tab"),
            ("Ctrl+Shift+Tab", "app.prev_tab"),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda key=command: self.dispatch(key))

    def _connect_events(self) -> None:
        """Bridge head-less bus events onto the UI (queued to the GUI thread)."""
        self._unsubscribers = [
            bus().subscribe(Topic.JOB_PROGRESS, self._on_job_progress),
            bus().subscribe(Topic.JOB_FINISHED, self._on_job_finished),
            bus().subscribe(Topic.JOB_FAILED, self._on_job_failed),
            bus().subscribe(Topic.STATUS_MESSAGE, self._on_status_event),
            bus().subscribe(Topic.UNDO_STACK_CHANGED, self._on_undo_changed),
            bus().subscribe(Topic.DOCUMENT_MODIFIED, self._on_document_modified),
        ]

    # ------------------------------------------------------------------ #
    # Tabs and documents
    # ------------------------------------------------------------------ #
    @property
    def current_tab(self) -> DocumentTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, DocumentTab) else None

    @property
    def document(self) -> PdfDocument | None:
        tab = self.current_tab
        return tab.document if tab else None

    @property
    def view(self) -> PageView | None:
        tab = self.current_tab
        return tab.view if tab else None

    def tabs_list(self) -> list[DocumentTab]:
        return [
            self.tabs.widget(i)
            for i in range(self.tabs.count())
            if isinstance(self.tabs.widget(i), DocumentTab)
        ]

    def add_document(self, document: PdfDocument, *, activate: bool = True) -> DocumentTab:
        """Open ``document`` in a new tab and wire it up."""
        tab = DocumentTab(document, self)
        tab.view.page_changed.connect(self._on_page_changed)
        tab.view.zoom_changed.connect(self._on_zoom_changed)
        tab.view.selection_changed.connect(self._on_selection_changed)
        tab.view.annotation_requested.connect(self.controller.create_annotation)
        tab.view.text_edit_requested.connect(self.controller.edit_text_block)
        tab.view.context_menu_requested.connect(self._show_canvas_menu)
        tab.view.object_moved.connect(self.controller.move_object)
        tab.view.object_selected.connect(self._on_object_selected)
        tab.view.object_delete_requested.connect(self.controller.delete_object)
        tab.view.object_clipboard_requested.connect(self._on_object_clipboard)
        tab.view.command_requested.connect(self.dispatch)
        tab.view.status_message.connect(self.show_status)

        if self.tabs.widget(0) is self._welcome:
            self.tabs.removeTab(0)
        index = self.tabs.addTab(tab, tab.title)
        self.tabs.setTabToolTip(index, str(document.path or document.display_name))
        if activate:
            self.tabs.setCurrentIndex(index)
        self.autosave.track(document)
        if document.path:
            self.history.touch_recent(document.path, page_count=document.page_count)
            self._rebuild_recent_menu()
        self._refresh_all_panels()
        return tab

    @Slot(int)
    def close_tab(self, index: int, *, discard: bool = False) -> bool:
        """Close a tab, prompting to save when it has unsaved changes.

        Args:
            discard: Skip the prompt and throw away unsaved changes. Used by
                automated tests and by ``--discard-on-exit``.
        """
        widget = self.tabs.widget(index)
        if not isinstance(widget, DocumentTab):
            return False
        document = widget.document
        if document.is_modified and not (discard or self.discard_on_close):
            answer = QMessageBox.question(
                self,
                "Save changes?",
                f"“{document.display_name}” has unsaved changes.\n\nSave before closing?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Save,
            )
            if answer == QMessageBox.StandardButton.Cancel:
                return False
            if answer == QMessageBox.StandardButton.Save and not self.controller.save():
                return False
        if document.path:
            view = widget.view
            self.history.touch_recent(document.path, page=view.current_page, zoom=view.zoom)
        self.autosave.untrack(document)
        self.tabs.removeTab(index)
        widget.close_document()
        widget.deleteLater()
        if self.tabs.count() == 0:
            self.tabs.addTab(self._welcome, "Welcome")
            self._refresh_all_panels()
        return True

    def _on_tab_changed(self, _index: int) -> None:
        self._refresh_all_panels()
        self._update_window_title()
        tab = self.current_tab
        if tab is not None:
            self._on_page_changed(tab.view.current_page)
            self._on_zoom_changed(tab.view.zoom)

    def _refresh_all_panels(self) -> None:
        tab = self.current_tab
        document = tab.document if tab else None
        renderer = tab.renderer if tab else None
        self.thumbnail_panel.set_document(document, renderer)
        self.bookmark_panel.set_document(document)
        self.comments_panel.set_document(document)
        self.attachments_panel.set_document(document)
        self.layers_panel.set_document(document)
        self.properties_panel.set_document(document)
        has_document = document is not None
        for key in (
            "file.save",
            "file.save_as",
            "file.print",
            "file.close",
            "pages.insert",
            "pages.delete",
            "ocr.run",
            "ai.summarize",
            "secure.encrypt",
            "optimize.compress",
        ):
            self.ribbon.set_enabled(key, has_document)

    def refresh_after_edit(self, page: int | None = None) -> None:
        """Called by the controller after any document mutation."""
        tab = self.current_tab
        if tab is None:
            return
        tab.view.refresh(page)
        if page is None:
            self.thumbnail_panel.rebuild()
        else:
            self.thumbnail_panel.refresh_page(page)
        self.comments_panel.reload()
        self.bookmark_panel.reload()
        self.layers_panel.reload()
        self.properties_panel.reload(tab.view.current_page)
        self.tabs.setTabText(self.tabs.currentIndex(), tab.title)
        self._update_window_title()

    # ------------------------------------------------------------------ #
    # Command dispatch
    # ------------------------------------------------------------------ #
    @Slot(str)
    def dispatch(self, command: str) -> None:
        """Route a command identifier to the controller (or a UI-local action)."""
        log.debug("Command: {}", command)
        if command in TOOL_MAP:
            view = self.view
            if view is not None:
                tool = TOOL_MAP[command]
                view.set_tool(tool)
                self.sync_tool_buttons(tool)
                self.show_status(TOOL_HINTS.get(tool, f"{_tool_name(tool)} tool"), 8000)
            return
        local = {
            "app.next_tab": lambda: self.tabs.setCurrentIndex(
                (self.tabs.currentIndex() + 1) % max(1, self.tabs.count())
            ),
            "app.prev_tab": lambda: self.tabs.setCurrentIndex(
                (self.tabs.currentIndex() - 1) % max(1, self.tabs.count())
            ),
            "edit.find": self._focus_search,
            "view.toolbar_mode": lambda: self.set_toolbar_mode(
                "classic" if self.settings.data.ui.toolbar_mode == "ribbon" else "ribbon"
            ),
            "view.fullscreen": self._toggle_fullscreen,
            "help.logs": lambda: self._open_folder(
                self.settings.data
                and __import__("pdfstudio.core.paths", fromlist=["app_paths"]).app_paths().logs
            ),
            "help.plugins_folder": lambda: self._open_folder(
                __import__("pdfstudio.core.paths", fromlist=["app_paths"]).app_paths().plugins
            ),
        }
        if command in local:
            local[command]()
            return
        if self.plugins is not None and command in self.plugins.commands():
            try:
                self.plugins.execute(command)
            except PdfStudioError as exc:
                self.show_error(exc)
            return
        try:
            self.controller.execute(command)
        except PdfStudioError as exc:
            self.show_error(exc)
        except Exception as exc:
            log.opt(exception=exc).error("Command {} failed", command)
            self.show_error(PdfStudioError("Something went wrong.", detail=str(exc)))

    def _on_ribbon_toggle(self, identifier: str, state: bool) -> None:
        if identifier.startswith("panel."):
            key = identifier.split(".", 1)[1]
            dock = self._docks.get(key)
            if dock is not None:
                dock.setVisible(state)
            return
        if identifier == "view.invert" and self.view:
            self.view.set_invert(state)
        elif identifier == "view.annotations" and self.view:
            self.view.set_show_annotations(state)
        elif identifier == "a11y.high_contrast":
            self.themes.apply("high-contrast" if state else "dark", QApplication.instance())
        elif (
            identifier.startswith("view.")
            and identifier[5:] in ("single", "continuous", "facing", "book")
            and state
            and self.view
        ):
            self.view.set_layout(PageLayout(identifier[5:]))

    # ------------------------------------------------------------------ #
    # View reactions
    # ------------------------------------------------------------------ #
    def _on_page_changed(self, page: int) -> None:
        document = self.document
        total = document.page_count if document else 0
        self._page_label.setText(f"Page {page + 1} / {total}" if total else "—")
        self.ribbon.set_page_display(page, total)
        self.thumbnail_panel.set_current_page(page)
        self.properties_panel.reload(page)

    def _on_zoom_changed(self, zoom: float) -> None:
        self._zoom_label.setText(f"{zoom * 100:.0f}%")
        self.ribbon.set_zoom_display(zoom)

    def sync_tool_buttons(self, tool: object) -> None:
        """Check every ribbon button that maps to ``tool``.

        More than one entry can arm the same tool ("Move object" appears on
        both Home and Edit); all of them must light up, or the ribbon shows
        the wrong tool as active.
        """
        for identifier, mapped in TOOL_MAP.items():
            self.ribbon.set_checked(identifier, mapped is tool)

    def _select_for_moving(self, target: object) -> None:
        """Arm the Move tool with ``target`` already selected."""
        from pdfstudio.ui.widgets.page_view import Tool

        view = self.view
        if view is None:
            return
        view.set_tool(Tool.MOVE_OBJECT)
        self.sync_tool_buttons(Tool.MOVE_OBJECT)
        view.set_selected_object(target)

    def _on_object_selected(self, target: object) -> None:
        """Report the selected object in the status bar."""
        if target is None:
            self.show_status(self.idle_hint(), 0)
            return
        self.show_status(
            f"Selected {target.describe()} — drag, arrow keys to nudge, "
            "Ctrl+C/Ctrl+V to copy, Ctrl+D to duplicate, Shift+Delete to remove"
        )

    def _on_object_clipboard(self, action: str, target: object) -> None:
        """Route a clipboard shortcut from the canvas to the controller."""
        match action:
            case "copy":
                self.controller.copy_object(target)
            case "cut":
                self.controller.cut_object(target)
            case "paste":
                self.controller.paste_object()
            case "duplicate":
                self.controller.duplicate_object(target)

    def _on_selection_changed(self, text: str) -> None:
        if text:
            self.show_status(f"{len(text)} characters selected")

    def _on_zoom_requested(self, zoom: float) -> None:
        if self.view:
            self.view.set_zoom(zoom)

    def _on_page_requested(self, page: int) -> None:
        self._goto_page(page)

    def _goto_page(self, page: int) -> None:
        if self.view:
            self.view.go_to_page(page)

    def _goto_point(self, page: int, point: Point | None) -> None:
        if not self.view:
            return
        if point is None:
            self.view.go_to_page(page)
        else:
            self.view.go_to_point(page, point)

    def _show_search_hit(self, index: int) -> None:
        if self.view:
            self.view.show_hit(index)

    def _focus_search(self) -> None:
        dock = self._docks["search"]
        dock.show()
        dock.raise_()
        self.search_panel.focus_input()

    def _toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def _show_canvas_menu(self, position: QPoint, page: int, point: Point | None) -> None:
        menu = QMenu(self)
        view = self.view
        if view is None:
            return
        if view.selected_text:
            menu.addAction("Copy", view.copy_selection)
            menu.addAction("Highlight", lambda: self.dispatch("annot.highlight"))
            menu.addAction(
                "Search selection", lambda: self._search_selection(view.selected_text)
            )
            menu.addSeparator()
        menu.addAction("Add sticky note", lambda: self.dispatch("annot.note"))
        if point is not None:
            # Edit straight away at the point that was right-clicked, rather
            # than only arming the tool and making the user click again.
            menu.addAction(
                "Edit this line",
                lambda: self.controller.edit_text_block(page, None, point, False),
            )
            menu.addAction(
                "Edit this paragraph",
                lambda: self.controller.edit_text_block(page, None, point, True),
            )

            # Word-style paragraph tools, acting on the text just clicked.
            view.set_last_text_point(page, point)
            para = menu.addMenu("Paragraph")
            para.addAction("Move line up\tAlt+Up", lambda: self.dispatch("text.move_line_up"))
            para.addAction(
                "Move line down\tAlt+Down", lambda: self.dispatch("text.move_line_down")
            )
            para.addSeparator()
            para.addAction("Duplicate line", lambda: self.dispatch("text.duplicate_line"))
            para.addAction("Delete line", lambda: self.dispatch("text.delete_line"))
            para.addSeparator()
            lists = para.addMenu("Lists")
            lists.addAction("Bulleted", lambda: self.dispatch("text.bullets"))
            lists.addAction("Numbered", lambda: self.dispatch("text.numbering"))
            lists.addAction("Remove list", lambda: self.dispatch("text.list_none"))
            case = para.addMenu("Change case")
            for label, command in (
                ("UPPERCASE", "text.case_upper"),
                ("lowercase", "text.case_lower"),
                ("Title Case", "text.case_title"),
                ("Sentence case", "text.case_sentence"),
            ):
                case.addAction(label, lambda c=command: self.dispatch(c))
            spacing = para.addMenu("Line spacing")
            for label, command in (
                ("Single", "text.spacing_single"),
                ("1.5 lines", "text.spacing_15"),
                ("Double", "text.spacing_double"),
            ):
                spacing.addAction(label, lambda c=command: self.dispatch(c))
            para.addSeparator()
            para.addAction("Copy formatting", lambda: self.dispatch("text.copy_format"))
            if self.controller.has_format_clip:
                para.addAction("Apply formatting", lambda: self.dispatch("text.paste_format"))
        menu.addAction("Edit text tool", lambda: self.dispatch("tool.edit_text"))
        menu.addSeparator()

        # Move whatever was right-clicked, without hunting for the tool first.
        if point is not None and self.document is not None:
            from pdfstudio.pdfengine.objects import ObjectService

            target = ObjectService(self.document).object_at(page, point)
            if target is not None:
                # Copy/duplicate sit at the top level, not buried in a
                # submenu: they are the actions people reach for most.
                menu.addAction(
                    f"Copy {target.kind.value}",
                    lambda t=target: self.controller.copy_object(t),
                )
                menu.addAction(
                    f"Cut {target.kind.value}",
                    lambda t=target: self.controller.cut_object(t),
                )
                menu.addAction(
                    f"Duplicate {target.kind.value}",
                    lambda t=target: self.controller.duplicate_object(t),
                )

                move_menu = menu.addMenu(f"Move {target.kind.value}")
                move_menu.addAction(
                    "Select for moving",
                    lambda t=target: self._select_for_moving(t),
                )
                move_menu.addSeparator()
                for label, dx, dy in (
                    ("Nudge left", -2.0, 0.0),
                    ("Nudge right", 2.0, 0.0),
                    ("Nudge up", 0.0, -2.0),
                    ("Nudge down", 0.0, 2.0),
                ):
                    move_menu.addAction(
                        label,
                        lambda t=target, x=dx, y=dy: self.controller.move_object(t, x, y),
                    )

                align_menu = menu.addMenu("Align")
                for label, edge in (
                    ("Left margin", "left"),
                    ("Horizontal centre", "center"),
                    ("Right margin", "right"),
                    ("Top margin", "top"),
                    ("Vertical middle", "middle"),
                    ("Bottom margin", "bottom"),
                ):
                    align_menu.addAction(
                        label,
                        lambda t=target, e=edge: (
                            self._select_for_moving(t),
                            self.controller.align_selected_object(e),
                        ),
                    )
                menu.addAction(
                    f"Delete {target.kind.value}",
                    lambda t=target: self.controller.delete_object(t),
                )
            if self.controller.has_object_clip:
                # Paste lands where the user right-clicked, which is far more
                # predictable than a fixed offset from the original.
                menu.addAction(
                    "Paste object here",
                    lambda p=page, pt=point: self.controller.paste_object(at=pt, page=p),
                )
            menu.addSeparator()
        menu.addAction(
            "Rotate page right", lambda: self.controller.page_action("rotate-right", [page])
        )
        menu.addAction("Delete page", lambda: self.controller.page_action("delete", [page]))
        menu.addAction("Extract page…", lambda: self.controller.page_action("extract", [page]))
        menu.addSeparator()
        menu.addAction("Fit page", lambda: view.set_zoom_mode(ZoomMode.FIT_PAGE))
        menu.addAction("Fit width", lambda: view.set_zoom_mode(ZoomMode.FIT_WIDTH))
        menu.addSeparator()
        menu.addAction("Document properties…", lambda: self.dispatch("document.properties"))
        menu.exec(position)

    def _search_selection(self, text: str) -> None:
        self.search_panel.input.setText(text[:120])
        self._focus_search()
        self.controller.run_search(self.search_panel.build_query())

    # ------------------------------------------------------------------ #
    # HostApplication protocol (used by plugins)
    # ------------------------------------------------------------------ #
    def active_document(self) -> PdfDocument | None:
        """Document the user is working on (``pdfstudio.plugins.api.HostApplication``)."""
        return self.document

    def open_document(self, path: str | Path) -> PdfDocument:
        """Open ``path`` in a new tab and return the document."""
        tab = self.controller.open_path(path)
        if tab is None:
            raise PdfStudioError(f"Could not open {path}")
        return tab.document

    def notify(self, message: str, *, level: str = "info") -> None:
        """Show a message in the status bar (or an error box for failures)."""
        if level == "error":
            self.show_error(PdfStudioError(message))
        else:
            self.show_status(message)

    def ask(self, question: str, options: list[str]) -> str | None:
        """Ask the user to choose between ``options``."""
        from PySide6.QtWidgets import QInputDialog

        choice, ok = QInputDialog.getItem(self, APP_NAME, question, options, 0, False)
        return choice if ok else None

    # ------------------------------------------------------------------ #
    # Status & events
    # ------------------------------------------------------------------ #
    def show_status(self, message: str, timeout: int = 4000) -> None:
        self._status_label.setText(message)
        if timeout:
            QTimer.singleShot(
                timeout,
                lambda: (
                    self._status_label.setText(self.idle_hint())
                    if self._status_label.text() == message
                    else None
                ),
            )

    def idle_hint(self) -> str:
        """What the status bar says when nothing else is happening.

        Falling back to the active tool's hint rather than a bare "Ready"
        keeps the controls on screen: the reminder that arrow keys nudge and
        Ctrl+D duplicates is exactly what a user needs while they are using
        the tool, not for four seconds after arming it.
        """
        view = self.view
        if view is None:
            return "Open a document to begin  ·  Ctrl+O"
        hint = TOOL_HINTS.get(view.tool)
        return hint or f"{_tool_name(view.tool)} tool"

    # ------------------------------------------------------------------ #
    # Modal dialogs
    # ------------------------------------------------------------------ #
    # Every modal must go through these helpers. A raw ``QMessageBox.exec()``
    # blocks forever when there is nobody to click it, which makes the command
    # unreachable from tests, the CLI and headless automation — and hides any
    # bug behind it. Routing them here means ``PDFSTUDIO_NO_DIALOGS`` reliably
    # turns the whole surface non-blocking.
    def inform(self, title: str, message: str) -> None:
        """Show an informational dialog, or a status message when suppressed."""
        if self.suppress_dialogs:
            self.show_status(f"{title}: {message}".replace("\n", " ")[:300])
            return
        QMessageBox.information(self, title, message)

    def warn(self, title: str, message: str) -> None:
        """Show a warning dialog, or a status message when suppressed."""
        if self.suppress_dialogs:
            self.show_status(f"{title}: {message}".replace("\n", " ")[:300])
            return
        QMessageBox.warning(self, title, message)

    def confirm(self, title: str, message: str, *, default_yes: bool = False) -> bool:
        """Ask a yes/no question. Suppressed runs take ``default_yes``.

        The default is deliberately *no* for anything destructive, so an
        automated run can never silently delete pages or discard changes.
        """
        if self.suppress_dialogs:
            self.show_status(f"{title}: {message}".replace("\n", " ")[:300])
            return default_yes
        return QMessageBox.question(self, title, message) == QMessageBox.StandardButton.Yes

    def show_error(self, error: BaseException) -> None:
        """Report an error to the user (and always to the log)."""
        title = getattr(error, "title", "Error")
        message = getattr(error, "message", str(error))
        detail = getattr(error, "detail", "")
        log.warning("UI error: {} — {}", title, message)
        if self.suppress_dialogs:
            # Automated runs must never block on a modal window.
            self.show_status(f"{title}: {message}")
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(message)
        if detail:
            box.setInformativeText(detail)
        box.exec()

    def _on_job_progress(self, event: Event) -> None:
        progress = event.get("progress")
        if progress is None:
            return
        # Bus events arrive on worker threads; marshal to the GUI thread.
        self.controller.call_on_gui_thread(lambda: self._update_progress(progress))

    def _update_progress(self, progress: Any) -> None:
        self._progress.setVisible(True)
        self._cancel_button.setVisible(True)
        if progress.total:
            self._progress.setRange(0, 100)
            self._progress.setValue(progress.percent)
        else:
            self._progress.setRange(0, 0)
        self._job_label.setText(progress.name)
        if progress.message:
            self._status_label.setText(progress.message)

    def _on_job_finished(self, event: Event) -> None:
        self.controller.call_on_gui_thread(self._clear_progress)

    def _on_job_failed(self, event: Event) -> None:
        error = event.get("error")
        self.controller.call_on_gui_thread(self._clear_progress)
        if isinstance(error, PdfStudioError):
            self.controller.call_on_gui_thread(lambda: self.show_error(error))

    def _clear_progress(self) -> None:
        if not jobs().active_jobs():
            self._progress.setVisible(False)
            self._cancel_button.setVisible(False)
            self._job_label.setText("")

    def _on_status_event(self, event: Event) -> None:
        message = event.get("message", "")
        if message:
            timeout = int(event.get("timeout", 4000))
            self.controller.call_on_gui_thread(lambda: self.show_status(message, timeout))

    def _on_undo_changed(self, event: Event) -> None:
        def apply() -> None:
            self.ribbon.set_enabled("edit.undo", bool(event.get("can_undo")))
            self.ribbon.set_enabled("edit.redo", bool(event.get("can_redo")))
            self._update_window_title()

        self.controller.call_on_gui_thread(apply)

    def _on_document_modified(self, _event: Event) -> None:
        self.controller.call_on_gui_thread(self._update_tab_titles)

    def _update_tab_titles(self) -> None:
        for index in range(self.tabs.count()):
            widget = self.tabs.widget(index)
            if isinstance(widget, DocumentTab):
                self.tabs.setTabText(index, widget.title)
        self._update_window_title()

    def _update_window_title(self) -> None:
        document = self.document
        if document is None:
            self.setWindowTitle(APP_NAME)
            self._size_label.setText("")
            return
        marker = "• " if document.is_modified else ""
        self.setWindowTitle(f"{marker}{document.display_name} — {APP_NAME}")
        size = document.file_size
        self._size_label.setText(_human(size) if size else "unsaved")

    # ------------------------------------------------------------------ #
    # Recent files, recovery, settings
    # ------------------------------------------------------------------ #
    def _rebuild_recent_menu(self) -> None:
        if not hasattr(self, "_recent_menu"):
            return
        self._recent_menu.clear()
        entries = self.history.recent(limit=15)
        if not entries:
            action = QAction("No recent files", self)
            action.setEnabled(False)
            self._recent_menu.addAction(action)
            return
        for entry in entries:
            action = QAction(f"{entry.name}  —  {entry.path.parent}", self)
            action.triggered.connect(
                lambda _c=False, p=entry.path: self.controller.open_path(p)
            )
            self._recent_menu.addAction(action)
        self._recent_menu.addSeparator()
        clear = QAction("Clear list", self)
        clear.triggered.connect(
            lambda: (self.history.clear_recent(), self._rebuild_recent_menu())
        )
        self._recent_menu.addAction(clear)

    def _check_recovery(self) -> None:
        """Offer to restore documents left behind by a crash."""
        pending = RecoveryManager().pending()
        if not pending:
            return
        names = "\n".join(f"• {r.display_name}" for r in pending[:8])
        answer = QMessageBox.question(
            self,
            "Recover unsaved work?",
            f"{APP_NAME} did not shut down cleanly.\n\n"
            f"The following documents can be recovered:\n\n{names}\n\nRestore them now?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        manager = RecoveryManager()
        if answer == QMessageBox.StandardButton.Yes:
            for record in pending:
                try:
                    self.add_document(manager.restore(record))
                except Exception as exc:
                    log.error("Recovery failed for {}: {}", record.display_name, exc)
            self.show_status(f"Recovered {len(pending)} document(s)")
        else:
            manager.discard_all()

    def set_toolbar_mode(self, mode: str) -> None:
        """Switch between the ribbon and the classic toolbar."""
        ribbon = mode == "ribbon"
        self._ribbon_dock.setVisible(ribbon)
        self.classic_toolbar.setVisible(not ribbon)
        self.settings.set("ui.toolbar_mode", mode)
        self.ribbon.set_checked("view.toolbar_mode", not ribbon)

    def apply_theme(self, identifier: str) -> None:
        self.themes.apply(identifier, QApplication.instance())
        self.settings.set("ui.theme", identifier)

    # ------------------------------------------------------------------ #
    # Window lifecycle
    # ------------------------------------------------------------------ #
    def _restore_geometry(self) -> None:
        """Restore the window's size and dock layout — but never its mode.

        ``saveGeometry()`` encodes the full-screen and maximised flags, so a
        session that ended in full screen (F11) or presentation mode (F5) came
        back full screen on *every* subsequent launch, with the chrome hidden
        and no obvious way out. Size and position are worth remembering;
        an immersive mode a user entered once is not.
        """
        if not self.settings.data.ui.remember_window_geometry:
            return
        store = QSettings("PDFStudio", "MainWindow")
        geometry = store.value("geometry")
        state = store.value("state")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        if isinstance(state, QByteArray):
            self.restoreState(state)
        self._leave_immersive_modes()

    def _leave_immersive_modes(self) -> None:
        """Drop full-screen/presentation flags inherited from a saved state."""
        state = self.windowState()
        immersive = Qt.WindowState.WindowFullScreen
        if state & immersive:
            # Keep "maximised" — that is a normal, reversible window mode with
            # visible chrome — but always clear full screen.
            self.setWindowState(state & ~immersive)
        view = self.view
        if view is not None and view.is_presentation:
            view.set_presentation_mode(False)

        # Presentation mode hides the ribbon and every dock, and saveState()
        # records that. Clearing the window flag alone would still leave a
        # chromeless window, so the toolbar is put back explicitly.
        if getattr(self, "_ribbon_dock", None) is not None and self._ribbon_dock.isHidden():
            self.set_toolbar_mode(self.settings.data.ui.toolbar_mode)

    def _save_geometry(self) -> None:
        store = QSettings("PDFStudio", "MainWindow")
        # Save the *normal* geometry: capturing it while full screen records a
        # screen-sized window, so leaving full screen later would restore to
        # something that still covers the whole display.
        if self.isFullScreen():
            self.setWindowState(self.windowState() & ~Qt.WindowState.WindowFullScreen)
        store.setValue("geometry", self.saveGeometry())
        store.setValue("state", self.saveState())

    def save_session(self) -> None:
        """Persist open tabs so they can be restored next launch."""
        tabs = []
        for tab in self.tabs_list():
            if tab.document.path:
                tabs.append(
                    {
                        "path": str(tab.document.path),
                        "page": tab.view.current_page,
                        "zoom": tab.view.zoom,
                    }
                )
        self.history.save_session("last", {"tabs": tabs, "active": self.tabs.currentIndex()})

    def restore_session(self) -> int:
        """Reopen the documents from the previous session."""
        session = self.history.load_session("last")
        if not session:
            return 0
        opened = 0
        for entry in session.get("tabs", []):
            path = Path(entry.get("path", ""))
            if not path.exists():
                continue
            try:
                tab = self.controller.open_path(path, activate=False)
                if tab is not None:
                    tab.view.set_zoom(float(entry.get("zoom", 1.0)))
                    tab.view.go_to_page(int(entry.get("page", 0)), animate=False)
                    opened += 1
            except PdfStudioError as exc:
                log.warning("Could not restore {}: {}", path, exc)
        if opened:
            index = min(int(session.get("active", 0)), self.tabs.count() - 1)
            self.tabs.setCurrentIndex(max(0, index))
            self.show_status(f"Restored {opened} document(s) from the last session")
        return opened

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        self._closing = True
        for index in reversed(range(self.tabs.count())):
            if isinstance(self.tabs.widget(index), DocumentTab) and not self.close_tab(index):
                event.ignore()
                self._closing = False
                return
        self.save_session()
        self._save_geometry()
        self.autosave.stop()
        if self.plugins is not None:
            self.plugins.shutdown()
        jobs().shutdown()
        self.history.close()
        log.info("{} closed", APP_NAME)
        event.accept()

    # -- drag & drop ------------------------------------------------------ #
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:  # noqa: N802
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:  # noqa: N802
        paths = [
            Path(url.toLocalFile()) for url in event.mimeData().urls() if url.isLocalFile()
        ]
        for path in paths:
            self.controller.open_path(path)
        event.acceptProposedAction()

    @staticmethod
    def _open_folder(path: Path) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"
