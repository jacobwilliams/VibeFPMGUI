"""*View → Theme*: the colors of the whole window (the editor's syntax colors
are *View → Syntax Colors*; with those on Automatic, a theme picks the
matching Pygments style).

*System*, *Light* and *Dark* keep the platform's own look (on macOS: its
buttons, tabs and dialogs) and only choose light or dark, through Qt's color
scheme. The colorful themes need a style that draws with the palette's
colors (macOS's doesn't), so they use Qt's Fusion style with a palette of
their own; the color scheme is still set to light or dark to match, so the
window's title bar and native dialogs agree.

*View → Window Style* chooses the style itself instead (any of Qt's on this
platform: Fusion everywhere, macOS on a Mac, windows11 / windowsvista on
Windows…). Fusion draws the panels' dividers with a grip of dots, macOS's a
plain line; only Fusion (and the plain Windows style) show a colorful
theme's colors throughout.

Either style is wrapped in `StillStyle`, which turns off the moving of
panels: Qt's dock layout could otherwise keep "animating" forever on macOS.
"""

from typing import NamedTuple

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import QApplication, QProxyStyle, QStyle, QStyleFactory


class Theme(NamedTuple):
    title: str
    dark: bool
    window: str = ""        # "" for the platform's own (System, Light, Dark)
    base: str = ""          # text fields, lists, the editor's surroundings
    text: str = ""
    button: str = ""
    highlight: str = ""     # selection and accents
    highlighted_text: str = ""
    link: str = ""
    syntax: str = ""        # the Pygments style for Syntax Colors → Automatic ("": light/dark default)

    @property
    def native(self) -> bool:
        """The platform's look (only light or dark chosen), not our palette."""
        return not self.window


SYSTEM = "system"
THEMES: dict[str, Theme] = {
    SYSTEM: Theme("System (follow light/dark mode)", False),
    "light": Theme("Light", False),
    "dark": Theme("Dark", True),
    # Dark, colorful
    "nord": Theme("Nord", True, "#2e3440", "#272c36", "#d8dee9", "#3b4252", "#88c0d0", "#2e3440", "#81a1c1",
                  "nord"),
    "dracula": Theme("Dracula", True, "#282a36", "#21222c", "#f8f8f2", "#44475a", "#bd93f9", "#282a36",
                     "#8be9fd", "dracula"),
    "solarized-dark": Theme("Solarized Dark", True, "#002b36", "#073642", "#93a1a1", "#0a4352", "#268bd2",
                            "#fdf6e3", "#2aa198", "solarized-dark"),
    "gruvbox-dark": Theme("Gruvbox Dark", True, "#282828", "#1d2021", "#ebdbb2", "#3c3836", "#d79921", "#282828",
                          "#83a598", "gruvbox-dark"),
    "one-dark": Theme("One Dark", True, "#282c34", "#21252b", "#abb2bf", "#323842", "#61afef", "#282c34",
                      "#56b6c2", "one-dark"),
    # Light, colorful
    "solarized-light": Theme("Solarized Light", False, "#eee8d5", "#fdf6e3", "#586e75", "#e4ddc8", "#268bd2",
                             "#fdf6e3", "#2aa198", "solarized-light"),
    "gruvbox-light": Theme("Gruvbox Light", False, "#ebdbb2", "#fbf1c7", "#3c3836", "#d5c4a1", "#b57614",
                           "#fbf1c7", "#076678", "gruvbox-light"),
    "ocean": Theme("Ocean", False, "#e3eef9", "#f7fbff", "#1b2a3a", "#d0e2f4", "#2f7fd8", "#ffffff", "#1f6fbf",
                   "friendly"),
    "mint": Theme("Mint", False, "#e6f4ea", "#f8fffa", "#1f3326", "#d2eadb", "#2f9e5c", "#ffffff", "#1d7a47",
                  "tango"),
}

class StillStyle(QProxyStyle):
    """A style (the platform's, or Fusion) whose widgets don't animate their
    geometry. Even with the main window's AnimatedDocks off, Qt moves panels
    with a zero-length animation whenever the style has an animation
    duration; on macOS each one's end could re-lay out the panels' tab bars,
    which started another, forever (a frozen window at 100% CPU). With no
    duration, Qt moves them in one go."""

    def styleHint(self, hint, option=None, widget=None, data=None):
        if hint == QStyle.SH_Widget_Animation_Duration:
            return 0
        return super().styleHint(hint, option, widget, data)


def style_name(style: QStyle) -> str:
    """The name of a style, or of the one a `StillStyle` wraps."""
    return (style.baseStyle() if isinstance(style, QProxyStyle) else style).name()


current = SYSTEM   # the theme in use (see apply)
_native_style = ""  # the platform's style ("macOS", "Windows", …), to go back to from Fusion
AUTOMATIC = ""     # View → Window Style: the theme's (the platform's, or Fusion for a colorful theme)
chosen_style = AUTOMATIC  # View → Window Style (see window_style)
PALETTE_STYLES = {"fusion", "windows"}  # (styles that draw with a colorful theme's palette)


def styles() -> list[str]:
    """Qt's styles on this platform (always Fusion)."""
    return list(QStyleFactory.keys())


def native_style() -> str:
    """The platform's own style (its name as Qt has it)."""
    return _native_style or style_name(QApplication.instance().style())


def window_style(theme: Theme) -> str:
    """The style to use with a theme: the one chosen in View → Window Style
    if it's here (a setting from another platform may not be), else the
    platform's for System / Light / Dark and Fusion for a colorful theme."""
    available = {name.lower(): name for name in styles()}
    if chosen_style and chosen_style.lower() in available:
        return available[chosen_style.lower()]
    return native_style() if theme.native else "Fusion"


def set_window_style(name: str):
    """View → Window Style (AUTOMATIC: the theme's), applied now."""
    global chosen_style
    chosen_style = name
    apply(current)


def palette(theme: Theme) -> QPalette:
    """A full palette from a theme's few colors."""
    window, base, text = QColor(theme.window), QColor(theme.base), QColor(theme.text)
    button, highlight = QColor(theme.button), QColor(theme.highlight)
    p = QPalette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        p.setColor(group, QPalette.Window, window)
        p.setColor(group, QPalette.Base, base)
        p.setColor(group, QPalette.AlternateBase, base.lighter(112) if theme.dark else base.darker(104))
        p.setColor(group, QPalette.Button, button)
        p.setColor(group, QPalette.ToolTipBase, button)
        p.setColor(group, QPalette.Highlight, highlight)
        p.setColor(group, QPalette.HighlightedText, QColor(theme.highlighted_text))
        p.setColor(group, QPalette.Link, QColor(theme.link))
        p.setColor(group, QPalette.LinkVisited, QColor(theme.link).darker(120))
        p.setColor(group, QPalette.BrightText, QColor("#ff6b6b"))
        # Shades for frames and bevels, around the button color.
        p.setColor(group, QPalette.Light, button.lighter(130))
        p.setColor(group, QPalette.Midlight, button.lighter(115))
        p.setColor(group, QPalette.Mid, button.darker(130))
        p.setColor(group, QPalette.Dark, button.darker(160))
        p.setColor(group, QPalette.Shadow, QColor(0, 0, 0, 160))
        muted = QColor(text)
        muted.setAlpha(140)
        p.setColor(group, QPalette.PlaceholderText, muted)
        faint = text if group != QPalette.Disabled else muted
        for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText, QPalette.ToolTipText):
            p.setColor(group, role, faint)
    p.setColor(QPalette.Inactive, QPalette.Highlight, highlight.darker(115) if theme.dark else highlight.lighter(115))
    return p


def apply(key: str):
    """Use a theme for the whole app (an unknown key: System). Qt then sends
    every window a palette change, which the app's panels follow."""
    global current, _native_style
    app = QApplication.instance()
    theme = THEMES.get(key) or THEMES[SYSTEM]
    current = key if key in THEMES else SYSTEM
    if not _native_style:
        _native_style = style_name(app.style())
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):  # (Qt 6.8+)
        scheme = Qt.ColorScheme.Unknown if current == SYSTEM else \
            Qt.ColorScheme.Dark if theme.dark else Qt.ColorScheme.Light
        hints.setColorScheme(scheme)
    wanted = window_style(theme)
    if not isinstance(app.style(), StillStyle) or style_name(app.style()).lower() != wanted.lower():
        app.setStyle(StillStyle(wanted))
    app.setPalette(QPalette() if theme.native else palette(theme))  # (native: the platform's, for the scheme)


def syntax_style() -> str:
    """The Pygments style the current theme goes with ("" for none)."""
    return THEMES.get(current, THEMES[SYSTEM]).syntax
