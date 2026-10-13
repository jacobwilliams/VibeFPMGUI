"""*View → Theme*: the colors of the whole window (the editor's syntax colors
are *View → Syntax Colors*; with those on Automatic, a theme picks the
matching Pygments style).

*System*, *Light* and *Dark* keep the platform's own look (on macOS: its
buttons, tabs and dialogs) and only choose light or dark, through Qt's color
scheme. The colorful themes need a style that draws with the palette's
colors (macOS's doesn't), so they use Qt's Fusion style with a palette of
their own; the color scheme is still set to light or dark to match, so the
window's title bar and native dialogs agree.
"""

from typing import NamedTuple

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import QApplication


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

current = SYSTEM   # the theme in use (see apply)
_native_style = ""  # the platform's style ("macOS", "Windows", …), to go back to from Fusion


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
        _native_style = app.style().name()
    hints = app.styleHints()
    if hasattr(hints, "setColorScheme"):  # (Qt 6.8+)
        scheme = Qt.ColorScheme.Unknown if current == SYSTEM else \
            Qt.ColorScheme.Dark if theme.dark else Qt.ColorScheme.Light
        hints.setColorScheme(scheme)
    if theme.native:
        if app.style().name().lower() != _native_style.lower():
            app.setStyle(_native_style)
        app.setPalette(QPalette())  # (the platform's own, for the color scheme)
    else:
        if app.style().name().lower() != "fusion":
            app.setStyle("Fusion")
        app.setPalette(palette(theme))


def syntax_style() -> str:
    """The Pygments style the current theme goes with ("" for none)."""
    return THEMES.get(current, THEMES[SYSTEM]).syntax
