"""Terminal color codes (ANSI escape sequences) in program output.

Some compilers color their messages even when writing to a file or pipe
(lfortran always does, e.g. ``\\x1b[0;31;1msemantic error\\x1b[0;0m``).
:class:`AnsiWriter` shows those colors in a text widget; :func:`strip`
removes them for parsing.
"""

from __future__ import annotations

import re

from qtpy.QtGui import QColor, QFont, QPalette, QTextCharFormat, QTextCursor
from qtpy.QtWidgets import QApplication

# Every escape sequence: CSI ("\x1b[...X") and OSC ("\x1b]...\x07").
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# Color/style (SGR) sequences, the only ones that are shown.
SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")
# The start of a sequence cut off at the end of a chunk of output.
PARTIAL_RE = re.compile(r"\x1b(?:\[[0-9;?]*)?$")

# The 16 standard colors (black, red, green, yellow, blue, magenta, cyan,
# white, then the bright versions), chosen to read well on each background.
LIGHT = ["#000000", "#c92a2a", "#2b8a3e", "#a86b00", "#1c5fd4", "#a61e9e", "#0b7285", "#868e96",
         "#495057", "#e03131", "#37b24d", "#c78a00", "#3b7ddd", "#be4bdb", "#1098ad", "#adb5bd"]
DARK = ["#5c5f66", "#ff6b6b", "#69db7c", "#ffd43b", "#74a8ff", "#e599f7", "#66d9e8", "#ced4da",
        "#868e96", "#ff8787", "#8ce99a", "#ffe066", "#a5c8ff", "#eebefa", "#99e9f2", "#ffffff"]


def strip(text: str) -> str:
    """``text`` without any escape sequences."""
    return ANSI_RE.sub("", text)


def split_partial(text: str) -> tuple[str, str]:
    """(complete text, unfinished escape sequence at its end) — the rest of
    the sequence arrives with the next chunk of output."""
    match = PARTIAL_RE.search(text)
    return (text[:match.start()], text[match.start():]) if match else (text, "")


def _xterm_256(n: int, colors: list[str]) -> QColor:
    if n < 16:
        return QColor(colors[n])
    if n < 232:  # 6x6x6 color cube
        n -= 16
        steps = [0, 95, 135, 175, 215, 255]
        return QColor(steps[n // 36], steps[n // 6 % 6], steps[n % 6])
    grey = 8 + (n - 232) * 10
    return QColor(grey, grey, grey)


class AnsiWriter:
    """Appends text to a QPlainTextEdit, turning color codes into formatting.
    The current color carries over between calls, like in a terminal."""

    def __init__(self, edit):
        self.edit = edit
        self.reset()

    def reset(self):
        self.fg: QColor | None = None
        self.bg: QColor | None = None
        self.bold = self.italic = self.underline = False

    def _colors(self) -> list[str]:
        base = QApplication.palette().color(QPalette.Base)
        return DARK if base.lightness() < 128 else LIGHT

    def _apply(self, params: str):
        codes = [int(c) if c else 0 for c in params.split(";")] if params else [0]
        colors = self._colors()
        i = 0
        while i < len(codes):
            c = codes[i]
            if c == 0:
                self.reset()
            elif c == 1:
                self.bold = True
            elif c == 3:
                self.italic = True
            elif c == 4:
                self.underline = True
            elif c == 22:
                self.bold = False
            elif c == 23:
                self.italic = False
            elif c == 24:
                self.underline = False
            elif 30 <= c <= 37 or 90 <= c <= 97:
                self.fg = QColor(colors[c - 30 if c < 90 else c - 90 + 8])
            elif 40 <= c <= 47 or 100 <= c <= 107:
                self.bg = QColor(colors[c - 40 if c < 100 else c - 100 + 8])
            elif c == 39:
                self.fg = None
            elif c == 49:
                self.bg = None
            elif c in (38, 48) and i + 1 < len(codes):
                color = None
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    color, i = _xterm_256(codes[i + 2], colors), i + 2
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    color, i = QColor(*(max(0, min(255, v)) for v in codes[i + 2:i + 5])), i + 4
                if c == 38:
                    self.fg = color
                else:
                    self.bg = color
            i += 1

    def _format(self, bold: bool) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setFontWeight(QFont.Bold if bold or self.bold else QFont.Normal)
        fmt.setFontItalic(self.italic)
        fmt.setFontUnderline(self.underline)
        if self.fg is not None:
            fmt.setForeground(self.fg)
        if self.bg is not None:
            fmt.setBackground(self.bg)
        return fmt

    def write(self, text: str, bold: bool = False, ansi: bool = True):
        """Append ``text``; with ``ansi`` False it is shown as is, in the
        default color (for the app's own messages)."""
        cursor = self.edit.textCursor()
        cursor.movePosition(QTextCursor.End)
        if not ansi:
            saved = (self.fg, self.bg, self.bold, self.italic, self.underline)
            self.reset()
            cursor.insertText(text, self._format(bold))
            self.fg, self.bg, self.bold, self.italic, self.underline = saved
        else:
            pos = 0
            for match in SGR_RE.finditer(text):
                if match.start() > pos:
                    cursor.insertText(strip(text[pos:match.start()]), self._format(bold))
                self._apply(match.group(1))
                pos = match.end()
            if pos < len(text):
                cursor.insertText(strip(text[pos:]), self._format(bold))
        self.edit.setTextCursor(cursor)
        self.edit.ensureCursorVisible()
