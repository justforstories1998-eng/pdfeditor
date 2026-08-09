"""Application bootstrap: ``python -m pdfstudio`` and the ``pdfstudio`` script.

Handles HiDPI setup, single-instance behaviour, theme application, plugin
loading, session restore and the crash handler, then hands control to Qt.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path

from pdfstudio import APP_ID, APP_NAME, ORGANISATION, __version__


def _configure_qt_environment() -> None:
    """Set Qt environment variables *before* QApplication is created."""
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    # Wayland/X11 fallback keeps Linux users out of trouble on mixed sessions.
    headless = not os.environ.get("WAYLAND_DISPLAY") and not os.environ.get("DISPLAY")
    linux_without_display = (
        sys.platform.startswith("linux") and not os.environ.get("QT_QPA_PLATFORM") and headless
    )
    if linux_without_display:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pdfstudio",
        description=f"{APP_NAME} — a professional PDF editor.",
    )
    parser.add_argument("files", nargs="*", type=Path, help="documents to open")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--log-level", default=None, help="DEBUG, INFO, WARNING, ERROR")
    parser.add_argument("--theme", default=None, help="theme identifier to start with")
    parser.add_argument("--page", type=int, default=None, help="open at this page (1-based)")
    parser.add_argument("--no-plugins", action="store_true", help="start without plugins")
    parser.add_argument(
        "--no-restore", action="store_true", help="do not restore the last session"
    )
    parser.add_argument(
        "--safe-mode", action="store_true", help="defaults, no plugins, no restore"
    )
    parser.add_argument(
        "--reset-settings", action="store_true", help="restore default settings"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Start the graphical application. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)

    _configure_qt_environment()

    from pdfstudio.core.logging_setup import get_logger, setup_logging

    setup_logging(level=args.log_level)
    log = get_logger("app")

    from pdfstudio.core.settings import settings

    config = settings()
    if args.reset_settings:
        config.reset()
        log.warning("Settings restored to defaults")

    from PySide6.QtCore import Qt
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_UseHighDpiPixmaps, True)
    app = QApplication(list(sys.argv[:1]))
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setOrganizationName(ORGANISATION)
    app.setDesktopFileName(APP_ID)
    app.setStyle("Fusion")

    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    icon_path = base / "pdfstudio" / "resources" / "icons" / "pdfstudio.png"
    if not icon_path.exists():
        icon_path = Path(__file__).parent / "resources" / "icons" / "pdfstudio.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    from pdfstudio.ui.theme import ThemeManager

    themes = ThemeManager()
    themes.apply(args.theme or config.data.ui.theme, app)

    plugins = None
    if not args.no_plugins and not args.safe_mode and config.data.plugins.enabled:
        from pdfstudio.plugins.manager import PluginManager

        plugins = PluginManager(settings=config)

    from pdfstudio.ui.main_window import MainWindow

    window = MainWindow(theme_manager=themes, plugin_manager=plugins)

    if plugins is not None:
        plugins.host = window  # type: ignore[assignment]
        loaded = plugins.load_all()
        window.ribbon.add_plugin_commands(plugins.commands())
        log.info("Loaded {} plugin(s)", len(loaded))

    window.show()

    for path in args.files:
        tab = window.controller.open_path(path)
        if tab is not None and args.page:
            tab.view.go_to_page(args.page - 1, animate=False)

    if (
        not args.files
        and not args.no_restore
        and not args.safe_mode
        and config.data.ui.restore_session
    ):
        window.restore_session()

    signal.signal(signal.SIGINT, signal.SIG_DFL)  # Ctrl+C in a terminal
    log.info("{} {} started", APP_NAME, __version__)
    return app.exec()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
