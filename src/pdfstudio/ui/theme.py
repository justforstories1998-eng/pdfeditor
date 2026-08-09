"""Theme engine: dark, light, high-contrast and user-defined themes.

A theme is a flat palette of named colours plus a handful of metrics.  The
engine renders it into a Qt style sheet and a ``QPalette`` so both custom
widgets and stock Qt widgets follow the theme, then hot-swaps it at runtime
without restarting.

Custom themes are plain JSON files in the user theme directory, so users can
share them.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont, QPalette

from pdfstudio.core.events import Topic, bus
from pdfstudio.core.logging_setup import get_logger
from pdfstudio.core.paths import app_paths, resources_dir

log = get_logger("theme")


@dataclass(slots=True)
class Palette:
    """Named colours used throughout the UI."""

    # surfaces
    window: str = "#17181c"
    surface: str = "#1e2024"
    surface_alt: str = "#25272d"
    elevated: str = "#2b2e35"
    canvas: str = "#0f1013"
    page: str = "#ffffff"
    # text
    text: str = "#eceef2"
    text_muted: str = "#9ba2ae"
    text_disabled: str = "#5c626c"
    text_inverse: str = "#16181b"
    # accents
    accent: str = "#3d7eff"
    accent_hover: str = "#5a92ff"
    accent_pressed: str = "#2f6ae0"
    accent_text: str = "#ffffff"
    # feedback
    success: str = "#2fbf71"
    warning: str = "#e8a33d"
    danger: str = "#e5484d"
    info: str = "#4aa3df"
    # chrome
    border: str = "#31343b"
    border_strong: str = "#3f434c"
    shadow: str = "#00000080"
    hover: str = "#ffffff12"
    selection: str = "#3d7eff55"
    highlight: str = "#ffe066"
    scrollbar: str = "#454a54"
    # tools
    toolbar: str = "#1e2024"
    ribbon: str = "#1e2024"
    sidebar: str = "#1a1c20"
    statusbar: str = "#1a1c20"
    #: The selected tab. Matches ``surface`` so the tab merges into the pane
    #: below it, which is what makes it read as "in front".
    tab_active: str = "#2b2e35"
    #: Unselected tabs. Must never equal ``tab_strip``, or they vanish.
    tab_inactive: str = "#212429"
    #: The rail the tabs sit on, behind and below them.
    tab_strip: str = "#141619"


@dataclass(slots=True)
class Metrics:
    """Sizing and spacing tokens."""

    radius: int = 8
    radius_small: int = 5
    padding: int = 7
    spacing: int = 6
    toolbar_height: int = 40
    ribbon_height: int = 96
    icon_size: int = 20
    font_size: int = 10
    border_width: int = 1


@dataclass(slots=True)
class Theme:
    """A complete, named theme."""

    name: str = "Dark"
    identifier: str = "dark"
    dark: bool = True
    palette: Palette = field(default_factory=Palette)
    metrics: Metrics = field(default_factory=Metrics)
    font_family: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Theme:
        palette = Palette(
            **{
                k: v
                for k, v in (data.get("palette") or {}).items()
                if k in {f.name for f in fields(Palette)}
            }
        )
        metrics = Metrics(
            **{
                k: v
                for k, v in (data.get("metrics") or {}).items()
                if k in {f.name for f in fields(Metrics)}
            }
        )
        return cls(
            name=data.get("name", "Custom"),
            identifier=data.get("identifier", "custom"),
            dark=bool(data.get("dark", True)),
            palette=palette,
            metrics=metrics,
            font_family=data.get("font_family", ""),
        )

    def save(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), "utf-8")
        return path


def dark_theme() -> Theme:
    return Theme()


def light_theme() -> Theme:
    return Theme(
        name="Light",
        identifier="light",
        dark=False,
        palette=Palette(
            window="#f1f3f6",
            surface="#ffffff",
            surface_alt="#e9ecf1",
            elevated="#ffffff",
            canvas="#d5d9e0",
            page="#ffffff",
            text="#1b1d21",
            text_muted="#5c6169",
            text_disabled="#a3a8b0",
            text_inverse="#ffffff",
            accent="#2563eb",
            accent_hover="#3b82f6",
            accent_pressed="#1d4ed8",
            accent_text="#ffffff",
            success="#15803d",
            warning="#b45309",
            danger="#dc2626",
            info="#0369a1",
            border="#dde1e7",
            border_strong="#c2c8d1",
            shadow="#00000022",
            hover="#00000010",
            selection="#2563eb33",
            highlight="#ffe066",
            scrollbar="#b9bec6",
            toolbar="#ffffff",
            ribbon="#ffffff",
            sidebar="#f5f7fa",
            statusbar="#f5f7fa",
            tab_active="#ffffff",
            tab_inactive="#dfe4ec",
            tab_strip="#eef1f6",
        ),
    )


def high_contrast_theme() -> Theme:
    """WCAG AAA-oriented theme for low-vision users."""
    return Theme(
        name="High contrast",
        identifier="high-contrast",
        dark=True,
        palette=Palette(
            window="#000000",
            surface="#000000",
            surface_alt="#0d0d0d",
            elevated="#141414",
            canvas="#000000",
            page="#ffffff",
            text="#ffffff",
            text_muted="#e0e0e0",
            text_disabled="#8a8a8a",
            text_inverse="#000000",
            accent="#ffd400",
            accent_hover="#ffe34d",
            accent_pressed="#e6bf00",
            accent_text="#000000",
            success="#00e676",
            warning="#ffd400",
            danger="#ff5252",
            info="#40c4ff",
            border="#ffffff",
            border_strong="#ffffff",
            shadow="#00000000",
            hover="#ffffff33",
            selection="#ffd40066",
            highlight="#ffd400",
            scrollbar="#ffffff",
            toolbar="#000000",
            ribbon="#000000",
            sidebar="#000000",
            statusbar="#000000",
            # Pure black inactive tabs were indistinguishable from the black
            # surface behind them; a lifted grey keeps the strip legible while
            # staying within the high-contrast palette.
            tab_active="#1a1a1a",
            tab_inactive="#2e2e2e",
            tab_strip="#000000",
        ),
        metrics=Metrics(border_width=2, radius=2, radius_small=2, font_size=11),
    )


def sepia_theme() -> Theme:
    """Warm, low-blue-light theme for long reading sessions."""
    return Theme(
        name="Sepia",
        identifier="sepia",
        dark=False,
        palette=Palette(
            window="#efe6d5",
            surface="#f7f0e2",
            surface_alt="#e8dcc6",
            elevated="#fdf8ee",
            canvas="#ded2ba",
            page="#fbf3e3",
            text="#3b3228",
            # #6f6353 gave only 4.32:1 on the tab colour, under the 4.5:1
            # minimum for body text.
            text_muted="#635742",
            text_disabled="#a89a85",
            text_inverse="#fbf3e3",
            accent="#a2662f",
            accent_hover="#bd7c40",
            accent_pressed="#83521f",
            accent_text="#fff8ec",
            border="#cbbb9e",
            border_strong="#b3a084",
            hover="#00000012",
            selection="#a2662f33",
            toolbar="#f7f0e2",
            ribbon="#f1e8d8",
            sidebar="#ece1cd",
            statusbar="#f7f0e2",
            tab_active="#fdf8ee",
            tab_inactive="#dccdaf",
            tab_strip="#eee4d3",
        ),
    )


BUILTIN_THEMES: dict[str, Theme] = {
    t.identifier: t for t in (dark_theme(), light_theme(), high_contrast_theme(), sepia_theme())
}


class ThemeManager(QObject):
    """Loads, applies and hot-swaps themes."""

    theme_changed = Signal(object)  # Theme

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._themes: dict[str, Theme] = dict(BUILTIN_THEMES)
        self._current: Theme = BUILTIN_THEMES["dark"]
        self.load_user_themes()

    # -- catalogue -------------------------------------------------------------- #
    def load_user_themes(self) -> int:
        """Load JSON themes from the user theme directory and bundled resources."""
        count = 0
        for directory in (resources_dir() / "themes", app_paths().ensure().themes):
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.json")):
                try:
                    theme = Theme.from_dict(json.loads(path.read_text("utf-8")))
                    self._themes[theme.identifier] = theme
                    count += 1
                except (OSError, ValueError) as exc:
                    log.warning("Ignoring bad theme {}: {}", path.name, exc)
        return count

    def themes(self) -> list[Theme]:
        return sorted(self._themes.values(), key=lambda t: (not t.dark, t.name))

    def get(self, identifier: str) -> Theme:
        return self._themes.get(identifier, self._current)

    def add(self, theme: Theme, *, persist: bool = True) -> None:
        self._themes[theme.identifier] = theme
        if persist:
            theme.save(app_paths().ensure().themes / f"{theme.identifier}.json")

    @property
    def current(self) -> Theme:
        return self._current

    # -- application ------------------------------------------------------------- #
    def apply(self, identifier: str | Theme, app: Any | None = None) -> Theme:
        """Apply a theme to the running application."""
        theme = identifier if isinstance(identifier, Theme) else self.get(identifier)
        self._current = theme
        if app is None:
            from PySide6.QtWidgets import QApplication

            app = QApplication.instance()
        if app is not None:
            app.setPalette(build_qpalette(theme))
            app.setStyleSheet(build_stylesheet(theme))
            if theme.font_family:
                font = QFont(theme.font_family, theme.metrics.font_size)
                app.setFont(font)
        self.theme_changed.emit(theme)
        bus().publish(
            Topic.THEME_CHANGED,
            {"identifier": theme.identifier, "dark": theme.dark},
            source="theme",
        )
        log.info("Applied theme {}", theme.name)
        return theme

    def toggle_dark_light(self, app: Any | None = None) -> Theme:
        return self.apply("light" if self._current.dark else "dark", app)


def build_qpalette(theme: Theme) -> QPalette:
    """Map the theme onto Qt's own palette so stock widgets match."""
    p = theme.palette
    palette = QPalette()
    role = QPalette.ColorRole
    group = QPalette.ColorGroup

    palette.setColor(role.Window, QColor(p.window))
    palette.setColor(role.WindowText, QColor(p.text))
    palette.setColor(role.Base, QColor(p.surface))
    palette.setColor(role.AlternateBase, QColor(p.surface_alt))
    palette.setColor(role.Text, QColor(p.text))
    palette.setColor(role.PlaceholderText, QColor(p.text_muted))
    palette.setColor(role.Button, QColor(p.surface))
    palette.setColor(role.ButtonText, QColor(p.text))
    palette.setColor(role.BrightText, QColor(p.danger))
    palette.setColor(role.Highlight, QColor(p.accent))
    palette.setColor(role.HighlightedText, QColor(p.accent_text))
    palette.setColor(role.Link, QColor(p.accent))
    palette.setColor(role.LinkVisited, QColor(p.accent_pressed))
    palette.setColor(role.ToolTipBase, QColor(p.elevated))
    palette.setColor(role.ToolTipText, QColor(p.text))
    palette.setColor(role.Mid, QColor(p.border))
    palette.setColor(role.Shadow, QColor(p.shadow))

    for disabled_role in (role.WindowText, role.Text, role.ButtonText):
        palette.setColor(group.Disabled, disabled_role, QColor(p.text_disabled))
    return palette


def _css_color(value: str) -> str:
    """Convert ``#rrggbbaa`` to ``rgba(r, g, b, a)``.

    Qt style sheets only understand 3- and 6-digit hex; an 8-digit value is
    silently mis-parsed (it renders bright yellow), so translucent tokens must
    be emitted in functional notation.
    """
    text = value.strip()
    if text.startswith("#") and len(text) == 9:
        r, g, b, a = (int(text[i : i + 2], 16) for i in range(1, 9, 2))
        return f"rgba({r}, {g}, {b}, {a / 255:.3f})"
    return text


class _CssPalette:
    """Palette proxy that renders every colour in a style-sheet-safe form."""

    __slots__ = ("_palette",)

    def __init__(self, palette: Palette) -> None:
        self._palette = palette

    def __getattr__(self, name: str) -> str:
        return _css_color(getattr(self._palette, name))


def build_stylesheet(theme: Theme) -> str:
    """Render the theme as a Qt style sheet."""
    p = _CssPalette(theme.palette)
    m = theme.metrics
    return f"""
/* ---------- base ---------- */
QWidget {{
    background-color: {p.window};
    color: {p.text};
    font-size: {m.font_size}pt;
}}
QMainWindow, QDialog {{ background-color: {p.window}; }}
QToolTip {{
    background-color: {p.elevated};
    color: {p.text};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
    padding: 4px 6px;
}}

/* ---------- toolbars & ribbon ---------- */
QToolBar {{
    background-color: {p.toolbar};
    border: none;
    border-bottom: {m.border_width}px solid {p.border};
    spacing: {m.spacing}px;
    padding: 3px 6px;
}}
QToolBar::separator {{
    background: {p.border};
    width: 1px;
    margin: 6px 5px;
}}
QToolButton {{
    background: transparent;
    border: {m.border_width}px solid transparent;
    border-radius: {m.radius_small}px;
    padding: 5px 8px;
    color: {p.text};
}}
QToolButton:hover {{
    background-color: {p.hover};
    border-color: {p.border_strong};
}}
QToolButton:pressed {{ background-color: {p.accent_pressed}; color: {p.accent_text}; }}
QToolButton:checked {{
    background-color: {p.accent};
    color: {p.accent_text};
    border-color: {p.accent_pressed};
    font-weight: 600;
}}
QToolButton:disabled {{ color: {p.text_disabled}; }}
QToolButton::menu-indicator {{ image: none; width: 0; }}

#RibbonBar {{
    background-color: {p.ribbon};
    border-bottom: {m.border_width}px solid {p.border};
}}
#RibbonGroup {{
    background: transparent;
    border-right: {m.border_width}px solid {p.border};
    margin: 2px 0;
}}
#RibbonGroupLabel {{
    color: {p.text_muted};
    font-size: {max(7, m.font_size - 2)}pt;
    letter-spacing: 0.4px;
    padding-top: 2px;
}}

/* ---------- menus ---------- */
QMenuBar {{
    background-color: {p.toolbar};
    border-bottom: {m.border_width}px solid {p.border};
    padding: 2px 4px;
}}
QMenuBar::item {{
    padding: 6px 11px;
    background: transparent;
    border-radius: {m.radius_small}px;
}}
QMenuBar::item:selected {{ background-color: {p.hover}; }}
QMenu {{
    background-color: {p.elevated};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius}px;
    padding: 5px;
}}
QMenu::item {{ padding: 6px 26px 6px 22px; border-radius: {m.radius_small}px; }}
QMenu::item:selected {{ background-color: {p.accent}; color: {p.accent_text}; }}
QMenu::item:disabled {{ color: {p.text_disabled}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: 5px 8px; }}

/* ---------- docks & panels ---------- */
QDockWidget {{
    background-color: {p.sidebar};
    titlebar-close-icon: none;
    titlebar-normal-icon: none;
}}
QDockWidget::title {{
    background-color: {p.sidebar};
    color: {p.text_muted};
    padding: 9px 12px;
    border-bottom: {m.border_width}px solid {p.border};
    font-weight: 600;
    letter-spacing: 0.3px;
}}
#SidePanel {{ background-color: {p.sidebar}; border-right: {m.border_width}px solid {p.border}; }}

/* ---------- tabs ---------- */
/* The tab strip needs to read as a strip of *tabs*, not as a row of loose
   words. Two things are essential and were both missing:
     * inactive tabs must differ from the surface behind them — several
       palettes set ``tab_inactive`` to exactly ``surface``, which made them
       invisible;
     * the selected tab needs an enclosing shape, not only an underline, or
       there is no visual anchor and the whole strip disappears.
   Hence the explicit separator, rounded top corners and accent top edge. */
QTabWidget::pane {{
    border: {m.border_width}px solid {p.border};
    background: {p.surface};
    top: -1px;
}}
QTabBar {{ background: {p.tab_strip}; qproperty-drawBase: 0; }}
QTabBar::tab {{
    background: {p.tab_inactive};
    color: {p.text_muted};
    padding: 8px 18px;
    border: {m.border_width}px solid {p.border};
    border-bottom: none;
    border-top-left-radius: {m.radius}px;
    border-top-right-radius: {m.radius}px;
    margin-right: 2px;
    margin-top: 3px;
}}
QTabBar::tab:selected {{
    background: {p.tab_active};
    color: {p.text};
    border-color: {p.border};
    border-top: 2px solid {p.accent};
    margin-top: 0px;
    padding-bottom: 9px;
    font-weight: 600;
}}
QTabBar::tab:focus {{ outline: none; }}
QTabBar::tab:hover:!selected {{ background: {p.hover}; color: {p.text}; }}
QTabBar::close-button {{
    subcontrol-position: right;
    padding: 2px;
    border-radius: 3px;
}}
QTabBar::close-button:hover {{ background: {p.danger}; }}

/* ---------- inputs ---------- */
QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {p.surface_alt};
    color: {p.text};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
    padding: 6px 9px;
    min-height: 18px;
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
    border-color: {p.border_strong};
}}
QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus,
QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {p.accent};
    background-color: {p.surface};
}}
QComboBox::drop-down {{ border: none; width: 18px; }}
QComboBox QAbstractItemView {{
    background-color: {p.elevated};
    border: {m.border_width}px solid {p.border};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}

/* ---------- buttons ---------- */
QPushButton {{
    background-color: {p.surface_alt};
    color: {p.text};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
    padding: 7px 16px;
    min-height: 18px;
    font-weight: 500;
}}
QPushButton:hover {{ background-color: {p.hover}; border-color: {p.border_strong}; }}
QPushButton:pressed {{ background-color: {p.accent_pressed}; color: {p.accent_text}; }}
QPushButton:disabled {{ color: {p.text_disabled}; border-color: {p.border}; }}
QPushButton[primary="true"] {{
    background-color: {p.accent};
    color: {p.accent_text};
    border-color: {p.accent_pressed};
    font-weight: 600;
    padding: 8px 20px;
}}
QPushButton[primary="true"]:hover {{ background-color: {p.accent_hover}; }}
QPushButton[primary="true"]:pressed {{ background-color: {p.accent_pressed}; }}

/* ---------- lists & trees ---------- */
QListView, QTreeView, QTableView {{
    background-color: {p.surface};
    alternate-background-color: {p.surface_alt};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
    outline: none;
}}
QListView::item, QTreeView::item {{
    padding: 6px 8px;
    border-radius: {m.radius_small}px;
    margin: 1px 2px;
}}
QListView::item:hover, QTreeView::item:hover {{ background-color: {p.hover}; }}
QListView::item:selected, QTreeView::item:selected {{
    background-color: {p.accent};
    color: {p.accent_text};
}}
QHeaderView::section {{
    background-color: {p.surface_alt};
    color: {p.text_muted};
    padding: 5px 7px;
    border: none;
    border-right: {m.border_width}px solid {p.border};
    border-bottom: {m.border_width}px solid {p.border};
}}

/* ---------- scrollbars ---------- */
QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p.scrollbar};
    border-radius: 4px;
    min-height: 32px;
    margin: 2px 3px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.accent}; }}
QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {p.scrollbar};
    border-radius: 4px;
    min-width: 32px;
    margin: 3px 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.accent}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ---------- misc ---------- */
QStatusBar {{
    background-color: {p.statusbar};
    border-top: {m.border_width}px solid {p.border};
    color: {p.text_muted};
    padding: 2px 6px;
}}
QStatusBar QLabel {{ padding: 0 6px; }}
QStatusBar::item {{ border: none; }}
QProgressBar {{
    background-color: {p.surface_alt};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
    text-align: center;
    height: 16px;
}}
QProgressBar::chunk {{ background-color: {p.accent}; border-radius: {m.radius_small - 1}px; }}
QSplitter::handle {{ background-color: {p.border}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QGroupBox {{
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius}px;
    margin-top: 16px;
    padding: 12px 10px 10px 10px;
    font-weight: 600;
    background-color: {p.surface};
}}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 5px; color: {p.text_muted}; }}
QCheckBox::indicator, QRadioButton::indicator {{
    width: 15px; height: 15px;
    border: {m.border_width}px solid {p.border_strong};
    border-radius: 3px;
    background: {p.surface};
}}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{
    background-color: {p.accent};
    border-color: {p.accent_pressed};
    image: none;
}}
QCheckBox::indicator:hover, QRadioButton::indicator:hover {{
    border-color: {p.accent};
}}
QCheckBox {{ spacing: 8px; padding: 2px 0; }}
QSlider::groove:horizontal {{ height: 4px; background: {p.border}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {p.accent};
    width: 14px; height: 14px;
    margin: -6px 0;
    border-radius: 7px;
}}
#PageCanvas {{ background-color: {p.canvas}; }}

/* ---------- editable-text affordance ---------- */
#PageCanvas[editing="true"] {{ background-color: {p.canvas}; }}

/* ---------- inline text editing ---------- */
#InlineFormatBar {{
    background-color: {p.elevated};
    border: {m.border_width}px solid {p.border_strong};
    border-radius: {m.radius}px;
}}
#InlineFormatBar QComboBox {{
    background-color: {p.surface};
    color: {p.text};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
    padding: 2px 6px;
    min-height: 20px;
}}
#InlineFormatBar QComboBox:hover {{ border-color: {p.accent}; }}
#InlineFormatBar QComboBox QAbstractItemView {{
    background-color: {p.elevated};
    color: {p.text};
    selection-background-color: {p.accent};
    selection-color: {p.accent_text};
}}
#InlineFormatBar QToolButton {{
    background-color: {p.surface};
    color: {p.text};
    border: {m.border_width}px solid {p.border};
    border-radius: {m.radius_small}px;
}}
#InlineFormatBar QToolButton:hover {{ background-color: {p.hover}; border-color: {p.accent}; }}
#InlineFormatBar QToolButton:checked {{
    background-color: {p.accent};
    color: {p.accent_text};
    border-color: {p.accent_pressed};
}}
#ThumbnailList {{ background-color: {p.sidebar}; border: none; }}
#SearchBar {{ background-color: {p.surface_alt}; border-bottom: {m.border_width}px solid {p.border}; }}
"""
