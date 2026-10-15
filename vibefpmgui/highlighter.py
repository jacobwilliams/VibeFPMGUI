"""Syntax highlighting for QPlainTextEdit/QTextEdit using a Pygments lexer.

The whole document is lexed at once (so multi-line strings and the like are
handled correctly) and each text block then just applies the token formats
that overlap it. After an edit (once typing pauses) the document is lexed
again, on a thread for a big one, and only the lines whose colors changed
are recolored. Meanwhile the line being typed on is lexed on its own, at
once, so what's typed is colored straight away (the whole-document lexing
then corrects anything that depends on other lines).
"""

from __future__ import annotations

import sys
import threading
from bisect import bisect_right
from contextlib import contextmanager
from pathlib import Path
from typing import ClassVar

from pygments import lex
from pygments.lexers import (
    FortranFixedLexer,
    FortranLexer,
    JsonLexer,
    TextLexer,
    TOMLLexer,
    get_lexer_for_filename,
)
from pygments.styles import get_all_styles, get_style_by_name
from pygments.token import Comment, Generic, Keyword, Name, String, Text, Token
from pygments.util import ClassNotFound
from qtpy.QtCore import QTimer, Signal
from qtpy.QtGui import QColor, QFont, QPalette, QSyntaxHighlighter, QTextCharFormat
from qtpy.QtWidgets import QApplication

LIGHT_STYLE = "default"
DARK_STYLE = "monokai"
AUTO = "auto"  # LIGHT_STYLE or DARK_STYLE, following the system's light/dark mode
chosen_style = AUTO  # View → Syntax Colors (set by the main window)


def document_text(document) -> str:
    """A QTextDocument's text exactly as it is ("\\n" between lines). Qt's
    toPlainText() turns non-breaking spaces into spaces (and U+2028 into a
    line break): saving that would change the file."""
    return document.toRawText().replace("\u2029", "\n")


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
    """The style to use: the chosen one, or automatic: the one that goes with
    the app's theme (View → Theme), else by light/dark mode."""
    from . import themes

    for name in ((chosen_style,) if chosen_style != AUTO else ()) + (themes.syntax_style(),):
        if name:
            try:
                get_style_by_name(name)
                return name
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
# "JSON with Comments" (as VS Code calls it): workspace files, .jsonc, and the
# .json files in a .vscode folder (settings, launch, tasks…).
_JSONC = {".code-workspace", ".jsonc"}


class JsonWithCommentsLexer(JsonLexer):
    """Pygments' JSON lexer already accepts // and /* */ comments (and
    trailing commas); this just gives it VS Code's name for the format."""

    name = "JSON with Comments"
    aliases: ClassVar[list[str]] = ["jsonc"]
    filenames: ClassVar[list[str]] = ["*.jsonc", "*.code-workspace"]


def lexer_for_path(path) -> object:
    """A Pygments lexer chosen from the file name (plain text if unknown),
    which keeps the text's positions (see keep_positions)."""
    name = str(path)
    suffix = ("." + name.rsplit(".", 1)[-1].lower()) if "." in name else ""
    if suffix in _FIXED_FORM:
        return keep_positions(FortranFixedLexer())
    if suffix in _FREE_FORM:
        return keep_positions(FortranLexer())
    if suffix in _JSONC or (suffix == ".json" and Path(name).parent.name == ".vscode"):
        return keep_positions(JsonWithCommentsLexer())
    try:
        return keep_positions(get_lexer_for_filename(name))
    except ClassNotFound:
        return keep_positions(TextLexer())


def keep_positions(lexer):
    """A lexer that doesn't drop the text's leading and trailing line breaks
    (Pygments does by default): the tokens' positions are then the text's.
    Otherwise a file starting with blank lines is colored a character off
    for each of them."""
    lexer.stripnl = False
    lexer.stripall = False
    return lexer


# Lexers for files that are all prose (their text is spell checked, not only
# comments and strings).
PROSE_LEXERS = {"Text only", "Markdown", "reStructuredText"}
NOT_PROSE = (Comment.Preproc, Comment.PreprocFile, String.Escape, String.Interpol, String.Regex,
             String.Symbol, String.Affix, String.Backtick)  # (Markdown's `code`)


def token_role(ttype, all_prose: bool) -> str | None:
    """"prose" (a comment or string: spell checked), "name" (a name or
    keyword: not a misspelling elsewhere) or None."""
    if any(ttype in t for t in NOT_PROSE):
        return None
    if ttype in Comment or ttype in String or (all_prose and (ttype in Text or ttype in Generic)):
        return "prose"
    if ttype in Name or ttype in Keyword:
        return "name"
    return None


def token_format(style, ttype) -> QTextCharFormat:
    """How a Pygments style shows a token type."""
    # Some lexers make their own token types (e.g. YAML's
    # Literal.Scalar.Plain) that styles don't list: use the nearest
    # parent type the style knows, as Pygments' own formatters do.
    styled = ttype
    while not style.styles_token(styled) and styled.parent is not None:
        styled = styled.parent
    spec = style.style_for_token(styled)
    if ttype is Name and not spec["color"]:
        # The TOML lexer marks keys as plain Name, which most styles
        # leave uncolored; borrow the attribute-name color instead.
        spec = style.style_for_token(Name.Attribute)
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
    return fmt


def is_dark(palette: QPalette) -> bool:
    return palette.color(QPalette.Base).lightness() < 128


LEX_IN_THREAD = 100_000  # characters: bigger documents are lexed on a thread (typing stays smooth)


def _common_prefix(a: str, b: str) -> int:
    """How many characters two texts start with in common (compared a
    chunk at a time, so big texts are quick)."""
    n, i, step = min(len(a), len(b)), 0, 4096
    while i < n and a[i:i + step] == b[i:i + step]:
        i += step
    i = min(i, n)
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _common_suffix(a: str, b: str, limit: int) -> int:
    """How many characters two texts end with in common (at most ``limit``)."""
    k, step = 0, 4096
    while k < limit and a[len(a) - min(k + step, limit):len(a) - k] == b[len(b) - min(k + step, limit):len(b) - k]:
        k = min(k + step, limit)
    while k < limit and a[len(a) - k - 1] == b[len(b) - k - 1]:
        k += 1
    return k


_switching_lock = threading.Lock()
_switching_users = 0


@contextmanager
def _quick_thread_switching(interval: float = 0.0005):
    """Python switches between threads more often while inside (for every
    lexing thread at once; the normal interval comes back after the last)."""
    global _switching_users
    with _switching_lock:
        if _switching_users == 0:
            _quick_thread_switching.saved = sys.getswitchinterval()
            sys.setswitchinterval(interval)
        _switching_users += 1
    try:
        yield
    finally:
        with _switching_lock:
            _switching_users -= 1
            if _switching_users == 0:
                sys.setswitchinterval(_quick_thread_switching.saved)


QUICK_LINES = 50  # lines colored on their own per edit (more, e.g. the whole text replaced: wait for the lexing)


class PygmentsHighlighter(QSyntaxHighlighter):
    relexed = Signal()  # the whole document has new colors (e.g. for the minimap)
    _lexed = Signal(object)  # (text, tokens) from the lexing thread

    def __init__(self, document, lexer=None, style: str | None = None):
        super().__init__(document)
        self.lexer = keep_positions(lexer or TOMLLexer())
        self._spans: list[tuple[int, int, QTextCharFormat]] = []
        self._starts: list[int] = []
        self._formats: dict = {}
        self._text = ""          # the text the spans are for
        self.prose: list[tuple[int, int]] = []  # (position, length) of each comment and string in it
        self.names: set[str] = set()  # the names and keywords in it (lowercase)
        self._full = True        # recolor every line next time (the style or lexer changed)
        self._lexing = False     # a thread is lexing
        self._generation = 0     # changes with the style or lexer (a thread's answer from before is stale)
        self._busy = False
        self._closed = False      # its tab is closed (see close): a lexing thread then drops its answer
        self._quick_budget = QUICK_LINES  # (see _quick_line)
        self.style = None
        self._style_name = None
        # While typing, wait for a short pause before lexing again.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._relex)
        self._lexed.connect(self._apply)
        self.set_style(style or LIGHT_STYLE)
        document.contentsChange.connect(self._schedule)

    def _schedule(self, *_):
        if not self._busy:
            self._timer.start()

    def close(self):
        """Its tab is closing: no more lexing, and a thread still lexing
        drops its answer instead of sending it to a highlighter that's gone."""
        self._closed = True
        self._timer.stop()
        self._generation += 1

    def set_lexer(self, lexer):
        self.lexer = keep_positions(lexer)
        self._full = True
        self._generation += 1
        self._relex()

    def set_style(self, name: str):
        if name == self._style_name:
            return
        self._style_name = name
        self.style = get_style_by_name(name)
        self._formats.clear()
        self._full = True
        self._generation += 1
        QTimer.singleShot(0, self, self._relex)  # (once, even when the text is set just after; not if deleted)

    def background(self) -> QColor:
        return QColor(self.style.background_color)

    @property
    def text(self) -> str:
        """The text the colors (and `prose`, `names`) are for."""
        return self._text

    def is_current(self) -> bool:
        """Whether the colors are for the document as it is now: no lexing
        waiting or running (and once that's done, `relexed` is emitted)."""
        return not (self._timer.isActive() or self._lexing or self._full)

    def _format(self, ttype) -> QTextCharFormat:
        fmt = self._formats.get(ttype)
        if fmt is None:
            fmt = self._formats[ttype] = token_format(self.style, ttype)
        return fmt

    @staticmethod
    def _work(text: str, lexer, style, formats: dict, old_text: str, old_spans: list, full: bool):
        """Lex ``text`` and work out what to recolor (on a thread for a big
        document: no Qt objects but formats, which are plain values).
        Returns (text, spans, starts, new formats, (start, end) or None for
        everything, prose, names); spans [] if lexing failed (plain text).
        prose and names: for spell checking (see `token_role`)."""
        formats = dict(formats)  # (the window's own copy is not touched here)
        added = {}
        spans, pos = [], 0
        prose: list[tuple[int, int]] = []
        names: set[str] = set()
        all_prose = getattr(lexer, "name", "") in PROSE_LEXERS
        roles: dict = {}
        try:
            for ttype, value in lex(text, lexer):
                # Pygments may append a trailing newline that isn't in the document.
                length = min(len(value), len(text) - pos)
                if length > 0:
                    fmt = formats.get(ttype)
                    if fmt is None:
                        fmt = formats[ttype] = added[ttype] = token_format(style, ttype)
                    spans.append((pos, length, fmt))
                    role = roles.get(ttype, 0)
                    if role == 0:
                        role = roles[ttype] = token_role(ttype, all_prose)
                    if role == "prose":
                        if prose and prose[-1][0] + prose[-1][1] == pos:  # (one comment: several tokens)
                            prose[-1] = (prose[-1][0], prose[-1][1] + length)
                        else:
                            prose.append((pos, length))
                    elif role == "name":
                        names.add(value.strip().lower())
                pos += len(value)
        except Exception:  # noqa: BLE001 - never let highlighting break the editor
            spans, full, prose, names = [], True, [], set()
        starts = [s[0] for s in spans]
        region = None if full else PygmentsHighlighter._changed(old_text, old_spans, text, spans)
        return text, spans, starts, added, region, prose, names

    @staticmethod
    def _changed(old_text: str, old: list, text: str, new: list) -> tuple[int, int]:
        """The part of the new text whose colors may differ: the edited text,
        and the tokens from the first difference to the last (e.g. an opened
        string changes the rest of the file)."""
        prefix = _common_prefix(old_text, text)
        suffix = _common_suffix(old_text, text, min(len(old_text), len(text)) - prefix)
        start, end = prefix, len(text) - suffix
        shift = len(text) - len(old_text)
        n = min(len(old), len(new))
        i = 0
        while i < n and old[i][0] == new[i][0] and old[i][1] == new[i][1] and old[i][2] is new[i][2]:
            i += 1
        j = 0
        while j < n - i:
            a, b = old[-1 - j], new[-1 - j]
            if a[0] + shift != b[0] or a[1] != b[1] or a[2] is not b[2]:
                break
            j += 1
        if i < len(new):
            start = min(start, new[i][0])
        last = len(new) - 1 - j
        if last >= i:
            end = max(end, new[last][0] + new[last][1])
        return start, end

    def _relex(self, *_):
        """Lex the text again (on a thread for a big document) and recolor
        what changed."""
        if self._busy or self._closed:
            return
        self._timer.stop()
        text = document_text(self.document())
        if text == self._text and not self._full:
            return
        if self._lexing:  # (the thread's answer is checked against the text then)
            return
        args = (text, self.lexer, self.style, self._formats, self._text, self._spans, self._full)
        generation = self._generation
        if len(text) > LEX_IN_THREAD:
            self._lexing = True
            threading.Thread(target=self._lex_in_thread, args=(generation, args),
                             daemon=True, name="Syntax highlighting").start()
        else:
            self._apply((generation, self._work(*args)))

    def _lex_in_thread(self, generation, args):
        # The lexer is Python too: while it runs, the window's code waits its
        # turn each time it runs (Python's GIL), up to the switch interval
        # each time. A shorter interval while lexing keeps the window responsive.
        with _quick_thread_switching():
            result = self._work(*args)
        if self._closed:
            return  # (its tab was closed meanwhile: nobody wants the colors)
        try:
            self._lexed.emit((generation, result))
        except RuntimeError:
            pass  # (deleted meanwhile without close(): the same)

    def _apply(self, result):
        generation, (text, spans, starts, added, region, prose, names) = result
        self._lexing = False
        if text != document_text(self.document()) or generation != self._generation:
            self._relex()  # (the text, style or lexer changed while lexing: again)
            return
        self._formats.update(added)
        self._spans, self._starts, self._text = spans, starts, text
        self.prose, self.names = prose, names
        self._busy = True  # (recoloring changes the document's formats: not an edit)
        try:
            if self._full or region is None:
                self.rehighlight()
            else:
                document = self.document()
                block = document.findBlock(region[0])
                while block.isValid() and block.position() <= region[1]:
                    self.rehighlightBlock(block)
                    block = block.next()
        finally:
            self._busy = False
            self._full = False
        self.relexed.emit()

    def highlightBlock(self, text: str):
        start = self.currentBlock().position()
        end = start + len(text)
        lexed = self._text
        if not self._busy and lexed and (lexed[start:end] != text or (end < len(lexed) and lexed[end] != "\n")):
            if self._quick_line(text):
                return  # (an edited line, colored on its own until the next lexing)
            # Past the lines colored on their own (the whole text replaced, e.g.
            # by Fix All): the last lexing's colors are for other characters
            # here. Plain until the next lexing, which then recolors every line
            # (lines that only moved aren't in the part it would recolor).
            self._full = True
            return
        spans, count = self._spans, len(self._spans)
        i = max(bisect_right(self._starts, start) - 1, 0)
        while i < count:  # (by index: slicing the list would copy the rest of it for every line)
            pos, length, fmt = spans[i]
            i += 1
            if pos >= end:
                break
            if pos + length <= start:
                continue
            a, b = max(pos, start), min(pos + length, end)
            self.setFormat(a - start, b - a, fmt)

    def _quick_line(self, text: str) -> bool:
        """Color a line that changed since the last lexing by lexing just it,
        so typing is colored at once (False: over QUICK_LINES this edit)."""
        if self._quick_budget <= 0:
            return False
        if self._quick_budget == QUICK_LINES:
            QTimer.singleShot(0, self, self._reset_quick_budget)  # (the budget is per edit)
        self._quick_budget -= 1
        pos = 0
        try:
            for ttype, value in lex(text, self.lexer):
                length = min(len(value), len(text) - pos)
                if length > 0:
                    self.setFormat(pos, length, self._format(ttype))
                pos += len(value)
        except Exception:  # noqa: BLE001 - never let highlighting break the editor
            return False
        return True

    def _reset_quick_budget(self):
        self._quick_budget = QUICK_LINES
