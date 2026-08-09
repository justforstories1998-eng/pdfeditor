"""Preferences dialog covering every settings section."""

from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from pdfstudio.core.paths import app_paths
from pdfstudio.core.settings import settings
from pdfstudio.ui.theme import ThemeManager


class PreferencesDialog(QDialog):
    """Category list plus a stacked set of settings pages."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Preferences")
        self.resize(760, 580)
        self.settings = settings()

        layout = QVBoxLayout(self)
        body = QHBoxLayout()
        layout.addLayout(body, 1)

        self.categories = QListWidget(self)
        self.categories.setFixedWidth(180)
        self.pages = QStackedWidget(self)
        body.addWidget(self.categories)
        body.addWidget(self.pages, 1)

        for title, builder in (
            ("Appearance", self._appearance_page),
            ("Viewer", self._viewer_page),
            ("Performance", self._performance_page),
            ("OCR", self._ocr_page),
            ("AI", self._ai_page),
            ("Security", self._security_page),
            ("Autosave", self._autosave_page),
            ("Plugins", self._plugins_page),
            ("Storage", self._storage_page),
        ):
            self.categories.addItem(title)
            self.pages.addWidget(builder())
        self.categories.currentRowChanged.connect(self.pages.setCurrentIndex)
        self.categories.setCurrentRow(0)

        box = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
            | QDialogButtonBox.StandardButton.RestoreDefaults
        )
        box.accepted.connect(self._save)
        box.rejected.connect(self.reject)
        box.button(QDialogButtonBox.StandardButton.RestoreDefaults).clicked.connect(
            self._restore_defaults
        )
        layout.addWidget(box)

    # -- pages ------------------------------------------------------------- #
    def _appearance_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        ui = self.settings.data.ui

        self.theme = QComboBox(page)
        for theme in ThemeManager().themes():
            self.theme.addItem(theme.name, theme.identifier)
        index = self.theme.findData(ui.theme)
        self.theme.setCurrentIndex(max(0, index))

        self.toolbar_mode = QComboBox(page)
        self.toolbar_mode.addItems(["ribbon", "classic"])
        self.toolbar_mode.setCurrentText(ui.toolbar_mode)

        self.font_size = QSpinBox(page)
        self.font_size.setRange(7, 18)
        self.font_size.setValue(ui.font_size)

        self.icon_size = QSpinBox(page)
        self.icon_size.setRange(14, 40)
        self.icon_size.setValue(ui.icon_size)

        self.animations = QCheckBox("Enable animations", page)
        self.animations.setChecked(ui.animations)

        self.restore_session = QCheckBox("Reopen the last session at start-up", page)
        self.restore_session.setChecked(ui.restore_session)

        self.units = QComboBox(page)
        self.units.addItems(["mm", "cm", "in", "pt"])
        self.units.setCurrentText(ui.units)

        form.addRow("Theme:", self.theme)
        form.addRow("Toolbar style:", self.toolbar_mode)
        form.addRow("Font size:", self.font_size)
        form.addRow("Icon size:", self.icon_size)
        form.addRow("Measurement units:", self.units)
        form.addRow("", self.animations)
        form.addRow("", self.restore_session)
        return page

    def _viewer_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        viewer = self.settings.data.viewer

        self.layout_mode = QComboBox(page)
        self.layout_mode.addItems(["single", "continuous", "facing", "book"])
        self.layout_mode.setCurrentText(viewer.layout)

        self.zoom_mode = QComboBox(page)
        self.zoom_mode.addItems(["fit-page", "fit-width", "fit-height", "custom"])
        self.zoom_mode.setCurrentText(viewer.zoom_mode)

        self.zoom_step = QDoubleSpinBox(page)
        self.zoom_step.setRange(1.02, 2.0)
        self.zoom_step.setSingleStep(0.05)
        self.zoom_step.setValue(viewer.zoom_step)

        self.page_gap = QSpinBox(page)
        self.page_gap.setRange(0, 80)
        self.page_gap.setValue(viewer.page_gap)

        self.smooth_scroll = QCheckBox("Smooth scrolling", page)
        self.smooth_scroll.setChecked(viewer.smooth_scroll)

        self.show_annotations = QCheckBox("Show comments", page)
        self.show_annotations.setChecked(viewer.show_annotations)

        form.addRow("Default layout:", self.layout_mode)
        form.addRow("Default zoom:", self.zoom_mode)
        form.addRow("Zoom step:", self.zoom_step)
        form.addRow("Gap between pages:", self.page_gap)
        form.addRow("", self.smooth_scroll)
        form.addRow("", self.show_annotations)
        return page

    def _performance_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        perf = self.settings.data.performance

        self.render_threads = QSpinBox(page)
        self.render_threads.setRange(0, 64)
        self.render_threads.setValue(perf.render_threads)
        self.render_threads.setSpecialValueText("Automatic")

        self.render_dpi = QSpinBox(page)
        self.render_dpi.setRange(72, 600)
        self.render_dpi.setValue(perf.render_dpi)

        self.page_cache = QSpinBox(page)
        self.page_cache.setRange(64, 8192)
        self.page_cache.setSingleStep(64)
        self.page_cache.setSuffix(" MB")
        self.page_cache.setValue(perf.page_cache_mb)

        self.prefetch = QSpinBox(page)
        self.prefetch.setRange(0, 20)
        self.prefetch.setValue(perf.prefetch_pages)

        self.disk_cache = QCheckBox("Cache rendered pages on disk", page)
        self.disk_cache.setChecked(perf.disk_cache)

        self.gpu = QCheckBox("Use hardware acceleration when available", page)
        self.gpu.setChecked(perf.gpu_acceleration)

        form.addRow("Worker threads:", self.render_threads)
        form.addRow("Render DPI:", self.render_dpi)
        form.addRow("Page cache:", self.page_cache)
        form.addRow("Pages to prefetch:", self.prefetch)
        form.addRow("", self.disk_cache)
        form.addRow("", self.gpu)
        return page

    def _ocr_page(self) -> QWidget:
        from pdfstudio.ocr.engine import available_engines

        page = QWidget(self)
        form = QFormLayout(page)
        ocr = self.settings.data.ocr

        self.ocr_engine = QComboBox(page)
        installed = [engine.name for engine in available_engines()]
        self.ocr_engine.addItems(["auto", *installed] if installed else ["auto"])
        self.ocr_engine.setCurrentText(ocr.engine)

        self.ocr_languages = QLineEdit("+".join(ocr.languages), page)
        self.ocr_languages.setPlaceholderText("eng+fra+deu")

        self.ocr_dpi = QSpinBox(page)
        self.ocr_dpi.setRange(72, 600)
        self.ocr_dpi.setValue(ocr.dpi)

        self.ocr_deskew = QCheckBox("Correct skew", page)
        self.ocr_deskew.setChecked(ocr.deskew)
        self.ocr_denoise = QCheckBox("Remove noise", page)
        self.ocr_denoise.setChecked(ocr.denoise)
        self.ocr_force = QCheckBox("Re-OCR pages that already have text", page)
        self.ocr_force.setChecked(ocr.force)
        self.ocr_gpu = QCheckBox("Use the GPU (EasyOCR)", page)
        self.ocr_gpu.setChecked(ocr.gpu)

        status = QLabel(
            f"Installed engines: {', '.join(installed) or 'none — install pytesseract'}",
            page,
        )
        status.setStyleSheet("color: palette(mid);")

        form.addRow("Engine:", self.ocr_engine)
        form.addRow("Languages:", self.ocr_languages)
        form.addRow("Resolution:", self.ocr_dpi)
        form.addRow("", self.ocr_deskew)
        form.addRow("", self.ocr_denoise)
        form.addRow("", self.ocr_force)
        form.addRow("", self.ocr_gpu)
        form.addRow("", status)
        return page

    def _ai_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        ai = self.settings.data.ai

        self.ai_enabled = QCheckBox("Enable AI features", page)
        self.ai_enabled.setChecked(ai.enabled)

        self.ai_provider = QComboBox(page)
        self.ai_provider.addItems(["local", "openai", "custom"])
        self.ai_provider.setCurrentText(ai.provider)

        self.ai_model = QLineEdit(ai.model, page)
        self.ai_endpoint = QLineEdit(ai.endpoint, page)
        self.ai_endpoint.setPlaceholderText("https://api.openai.com/v1")
        self.ai_key_env = QLineEdit(ai.api_key_env, page)

        note = QLabel(
            "The local engine works offline with no API key: it provides "
            "extractive summaries, keyword tagging, bookmark generation and "
            "question answering. Remote providers add translation and rewriting.",
            page,
        )
        note.setWordWrap(True)
        note.setStyleSheet("color: palette(mid);")

        form.addRow("", self.ai_enabled)
        form.addRow("Provider:", self.ai_provider)
        form.addRow("Model:", self.ai_model)
        form.addRow("Endpoint:", self.ai_endpoint)
        form.addRow("API key variable:", self.ai_key_env)
        form.addRow("", note)
        return page

    def _security_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        security = self.settings.data.security

        self.default_encryption = QComboBox(page)
        self.default_encryption.addItems(["AES-256", "AES-128", "RC4-128"])
        self.default_encryption.setCurrentText(security.default_encryption)

        self.sanitize_export = QCheckBox("Sanitise documents on export", page)
        self.sanitize_export.setChecked(security.sanitize_on_export)
        self.strip_metadata = QCheckBox("Strip metadata on export", page)
        self.strip_metadata.setChecked(security.strip_metadata_on_export)
        self.warn_js = QCheckBox("Warn when a document contains JavaScript", page)
        self.warn_js.setChecked(security.warn_on_javascript)
        self.allow_remote = QCheckBox("Allow documents to load remote content", page)
        self.allow_remote.setChecked(security.allow_remote_content)

        form.addRow("Default encryption:", self.default_encryption)
        form.addRow("", self.sanitize_export)
        form.addRow("", self.strip_metadata)
        form.addRow("", self.warn_js)
        form.addRow("", self.allow_remote)
        return page

    def _autosave_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        autosave = self.settings.data.autosave

        self.autosave_enabled = QCheckBox("Autosave open documents", page)
        self.autosave_enabled.setChecked(autosave.enabled)

        self.autosave_interval = QSpinBox(page)
        self.autosave_interval.setRange(15, 3600)
        self.autosave_interval.setSuffix(" seconds")
        self.autosave_interval.setValue(autosave.interval_seconds)

        self.autosave_versions = QSpinBox(page)
        self.autosave_versions.setRange(1, 50)
        self.autosave_versions.setValue(autosave.keep_versions)

        self.crash_recovery = QCheckBox("Offer crash recovery at start-up", page)
        self.crash_recovery.setChecked(autosave.crash_recovery)

        form.addRow("", self.autosave_enabled)
        form.addRow("Interval:", self.autosave_interval)
        form.addRow("Versions to keep:", self.autosave_versions)
        form.addRow("", self.crash_recovery)
        return page

    def _plugins_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        plugins = self.settings.data.plugins

        self.plugins_enabled = QCheckBox("Enable plugins", page)
        self.plugins_enabled.setChecked(plugins.enabled)
        self.plugins_sandbox = QCheckBox("Restrict plugin imports (recommended)", page)
        self.plugins_sandbox.setChecked(plugins.sandbox)
        self.plugins_hot_reload = QCheckBox("Reload plugins when files change", page)
        self.plugins_hot_reload.setChecked(plugins.hot_reload)

        open_folder = QPushButton("Open plugin folder", page)
        open_folder.clicked.connect(self._open_plugin_folder)

        form.addRow("", self.plugins_enabled)
        form.addRow("", self.plugins_sandbox)
        form.addRow("", self.plugins_hot_reload)
        form.addRow("", open_folder)
        return page

    def _storage_page(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)
        paths = app_paths()
        for label, value in (
            ("Settings:", paths.config),
            ("Data:", paths.data),
            ("Cache:", paths.cache),
            ("Logs:", paths.logs),
            ("Plugins:", paths.plugins),
            ("Autosave:", paths.autosave),
        ):
            field = QLineEdit(str(value), page)
            field.setReadOnly(True)
            form.addRow(label, field)

        clear_cache = QPushButton("Clear the render cache", page)
        clear_cache.clicked.connect(self._clear_cache)
        form.addRow("", clear_cache)
        return page

    # -- actions ------------------------------------------------------------ #
    def _open_plugin_folder(self) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        QDesktopServices.openUrl(QUrl.fromLocalFile(str(app_paths().plugins)))

    def _clear_cache(self) -> None:
        from pdfstudio.render.cache import DiskCache

        cache = DiskCache()
        size = cache.size_bytes()
        cache.clear()
        QMessageBox.information(self, "Cache cleared", f"Freed {size / (1024 * 1024):.1f} MB.")

    def _restore_defaults(self) -> None:
        if (
            QMessageBox.question(
                self, "Restore defaults", "Reset every preference to its default value?"
            )
            == QMessageBox.StandardButton.Yes
        ):
            self.settings.reset()
            self.accept()

    def _save(self) -> None:
        self.settings.update(
            {
                "ui.theme": self.theme.currentData(),
                "ui.toolbar_mode": self.toolbar_mode.currentText(),
                "ui.font_size": self.font_size.value(),
                "ui.icon_size": self.icon_size.value(),
                "ui.units": self.units.currentText(),
                "ui.animations": self.animations.isChecked(),
                "ui.restore_session": self.restore_session.isChecked(),
                "viewer.layout": self.layout_mode.currentText(),
                "viewer.zoom_mode": self.zoom_mode.currentText(),
                "viewer.zoom_step": self.zoom_step.value(),
                "viewer.page_gap": self.page_gap.value(),
                "viewer.smooth_scroll": self.smooth_scroll.isChecked(),
                "viewer.show_annotations": self.show_annotations.isChecked(),
                "performance.render_threads": self.render_threads.value(),
                "performance.render_dpi": self.render_dpi.value(),
                "performance.page_cache_mb": self.page_cache.value(),
                "performance.prefetch_pages": self.prefetch.value(),
                "performance.disk_cache": self.disk_cache.isChecked(),
                "performance.gpu_acceleration": self.gpu.isChecked(),
                "ocr.engine": self.ocr_engine.currentText(),
                "ocr.languages": [
                    lang.strip()
                    for lang in self.ocr_languages.text().replace(",", "+").split("+")
                    if lang.strip()
                ]
                or ["eng"],
                "ocr.dpi": self.ocr_dpi.value(),
                "ocr.deskew": self.ocr_deskew.isChecked(),
                "ocr.denoise": self.ocr_denoise.isChecked(),
                "ocr.force": self.ocr_force.isChecked(),
                "ocr.gpu": self.ocr_gpu.isChecked(),
                "ai.enabled": self.ai_enabled.isChecked(),
                "ai.provider": self.ai_provider.currentText(),
                "ai.model": self.ai_model.text(),
                "ai.endpoint": self.ai_endpoint.text(),
                "ai.api_key_env": self.ai_key_env.text(),
                "security.default_encryption": self.default_encryption.currentText(),
                "security.sanitize_on_export": self.sanitize_export.isChecked(),
                "security.strip_metadata_on_export": self.strip_metadata.isChecked(),
                "security.warn_on_javascript": self.warn_js.isChecked(),
                "security.allow_remote_content": self.allow_remote.isChecked(),
                "autosave.enabled": self.autosave_enabled.isChecked(),
                "autosave.interval_seconds": self.autosave_interval.value(),
                "autosave.keep_versions": self.autosave_versions.value(),
                "autosave.crash_recovery": self.crash_recovery.isChecked(),
                "plugins.enabled": self.plugins_enabled.isChecked(),
                "plugins.sandbox": self.plugins_sandbox.isChecked(),
                "plugins.hot_reload": self.plugins_hot_reload.isChecked(),
            }
        )
        self.accept()
