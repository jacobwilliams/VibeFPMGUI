"""Syntax highlighting for QPlainTextEdit/QTextEdit using a Pygments lexer.

The whole document is lexed at once (so multi-line strings and the like are
handled correctly) and each text block then just applies the token formats
that overlap it.
"""

from __future__ import annotations

from bisect import bisect_right

from pygments import lex
from pygments.lexers import (
    FortranFixedLexer,
    FortranLexer,
    TextLexer,
    TOMLLexer,
    get_lexer_for_filename,
)
from pygments.styles import get_all_styles, get_style_by_name
from pygments.token import Name, Text, Token
from pygments.util import ClassNotFound
from qtpy.QtCore import QTimer
from qtpy.QtGui import QColor, QFont, QPalette, QSyntaxHighlighter, QTextCharFormat
from qtpy.QtWidgets import QApplication

LIGHT_STYLE = "default"
DARK_STYLE = "monokai"
AUTO = "auto"  # LIGHT_STYLE or DARK_STYLE, following the system's light/dark mode
chosen_style = AUTO  # View → Syntax Colors (set by the main window)


def style_is_dark(name: str) -> bool:
    try:
        return QColor(get_style_by_name(name).background_color).lightness() < 128
    except ClassNotFound:
        return False


def available_styles() -> tuple[list[str], list[str]]:
    """(light styles, dark styles): every Pygments style installed, sorted."""
    light, dark = [], []
    for name in sorted(get_all_styles()):
        (dark if style_is_dark(name) else light).append(name)
    return light, dark


def style_name(palette: QPalette) -> str:
    """The style to use: the chosen one, or automatic by light/dark mode."""
    if chosen_style != AUTO:
        try:
            get_style_by_name(chosen_style)
            return chosen_style
        except ClassNotFound:
            pass  # uninstalled since: fall back to automatic
    return DARK_STYLE if is_dark(palette) else LIGHT_STYLE


def style_text_color(style) -> QColor:
    """The color a style uses for plain text (styles that don't say get
    near-white on a dark background, near-black on a light one)."""
    for token in (Text, Token):
        color = style.style_for_token(token).get("color")
        if color:
            return QColor("#" + color)
    dark = QColor(style.background_color).lightness() < 128
    return QColor("#f8f8f2") if dark else QColor("#1b1b1b")


def apply_style(widget, highlighter):
    """Highlight with the current style and give the text widget its
    background and text colors."""
    highlighter.set_style(style_name(QApplication.palette()))
    palette = widget.palette()
    palette.setColor(QPalette.Base, highlighter.background())
    palette.setColor(QPalette.Text, style_text_color(highlighter.style))
    widget.setPalette(palette)


# Fortran extensions Pygments doesn't know (or maps to another language).
_FIXED_FORM = {".for", ".ftn", ".f77"}
_FREE_FORM = {".f95", ".f03", ".f08", ".fpp", ".inc"}


def lexer_for_path(path) -> object:
    """A Pygments lexer chosen from the file name (plain text if unknown)."""
    name = str(path)
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix in _FIXED_FORM:
        return FortranFixedLexer()
    if suffix in _FREE_FORM:
        return FortranLexer()
    try:
        return get_lexer_for_filename(name)
    except ClassNotFound:
        return TextLexer()


def is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.Base).lightness() < 128


class PygmentsHighlighter(QSyntaxHighlighter):
    def __init__(self, document, lexer=None, style: str | None = None):
        super().__init__(document)
        self.lexer = lexer or TOMLLexer()
        self._spans: list[tuple[int, int, QTextCharFormat]] = []
        self._starts: list[int] = []
        self._formats: dict = {}
        self._busy = False
        # While typing, wait for a short pause before re-lexing the document.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._relex)
        self.set_style(style or LIGHT_STYLE)
        document.contentsChange.connect(self._schedule)

    def _schedule(self, *_):
        if not self._busy:
            self._timer.start()

    def set_lexer(self, lexer):
        self.lexer = lexer
        self._relex()

    def set_style(self, name: str):
        self.style = get_style_by_name(name)
        self._formats.clear()
        self._relex()

    def background(self) -> QColor:
        return QColor(self.style.background_color)

    def _format(self, ttype) -> QTextCharFormat:
        fmt = self._formats.get(ttype)
        if fmt is None:
            # Some lexers make their own token types (e.g. YAML's
            # Literal.Scalar.Plain) that styles don't list: use the nearest
            # parent type the style knows, as Pygments' own formatters do.
            styled = ttype
            while not self.style.styles_token(styled) and styled.parent is not None:
                styled = styled.parent
            spec = self.style.style_for_token(styled)
            if ttype is Name and not spec["color"]:
                # The TOML lexer marks keys as plain Name, which most styles
                # leave uncolored; borrow the attribute-name color instead.
                spec = self.style.style_for_token(Name.Attribute)
            fmt = QTextCharFormat()
            if spec["color"]:
                fmt.setForeground(QColor("#" + spec["color"]))
            if spec["bgcolor"]:
                fmt.setBackground(QColor("#" + spec["bgcolor"]))
            if spec["bold"]:
                fmt.setFontWeight(QFont.Bold)
            if spec["italic"]:
                fmt.setFontItalic(True)
            if spec["underline"]:
                fmt.setFontUnderline(True)
            self._formats[ttype] = fmt
        return fmt

    def _relex(self, *_):
        # rehighlight() itself emits contentsChange; don't recurse.
        if self._busy:
            return
        self._busy = True
        try:
            self._lex_and_highlight()
        except Exception:  # noqa: BLE001 - never let highlighting break the editor
            self._spans, self._starts = [], []  # show it as plain text
            self.rehighlight()
        finally:
            self._busy = False

    def _lex_and_highlight(self):
        text = self.document().toPlainText()
        spans, pos = [], 0
        for ttype, value in lex(text, self.lexer):
            # Pygments may append a trailing newline that isn't in the document.
            length = min(len(value), len(text) - pos)
            if length > 0:
                spans.append((pos, length, self._format(ttype)))
            pos += len(value)
        self._spans = spans
        self._starts = [s[0] for s in spans]
        self.rehighlight()

    def highlightBlock(self, text: str):
        start = self.currentBlock().position()
        end = start + len(text)
        first = max(bisect_right(self._starts, start) - 1, 0)
        for pos, length, fmt in self._spans[first:]:
            if pos >= end:
                break
            if pos + length <= start:
                continue
            a, b = max(pos, start), min(pos + length, end)
            self.setFormat(a - start, b - a, fmt)
