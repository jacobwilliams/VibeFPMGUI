"""*Gremlins*: characters in a file that aren't plain printable ASCII.

Non-ASCII letters and symbols (``é``, ``°``, smart quotes), invisible ones
(non-breaking and zero-width spaces, a byte-order mark) and control
characters. Compilers are often fussy about them (gfortran rejects most
outside comments and strings), and the invisible ones can't be seen in the
editor. Tabs and line ends are not gremlins.

The editor draws a gremlin in the gutter of each line that has one
(`paint_gremlin`), tints the characters, and lists them in the *Problems*
tab (View → Show Gremlins turns it off).
"""

from __future__ import annotations

import re
import unicodedata
from typing import NamedTuple

from qtpy.QtCore import QPointF, QRectF, Qt
from qtpy.QtGui import QColor, QPainter, QPainterPath, QPen

MAX_GREMLINS = 2000  # per file (a file in another language would have one per accented letter)
GREMLIN_RE = re.compile(r"[^\t\n\x20-\x7e]")
COLOR = "#e8590c"  # the tint, badge and Problems color

# Names for the ones that have none, or a long one.
NAMES = {"\u00a0": "non-breaking space", "\u200b": "zero-width space", "\u200c": "zero-width non-joiner",
         "\u200d": "zero-width joiner", "\u2060": "word joiner", "\ufeff": "byte-order mark (zero-width)",
         "\u00ad": "soft hyphen", "\r": "carriage return", "\x0c": "form feed", "\x00": "null",
         "\x1b": "escape", "\x7f": "delete"}


class Gremlin(NamedTuple):
    line: int        # 1-based
    column: int      # 0-based
    char: str

    @property
    def code(self) -> str:
        return f"U+{ord(self.char):04X}"

    @property
    def message(self) -> str:
        return describe(self.char)


def describe(char: str) -> str:
    """What a gremlin is, e.g. "non-breaking space, not ASCII" or "latin
    small letter e with acute (é), not ASCII"."""
    name = NAMES.get(char)
    if name is None:
        if unicodedata.category(char) in ("Cc", "Cf", "Zs", "Zl", "Zp"):  # (can't be seen: just the name)
            name = unicodedata.name(char, "control character").lower()
        else:
            return f"{unicodedata.name(char, 'unnamed character').lower()} ({char}), not ASCII"
    return f"{name}, not ASCII" if ord(char) > 0x7F else f"{name} (a control character)"


def find(text: str, limit: int = MAX_GREMLINS) -> list[Gremlin]:
    """Every gremlin in a text ("\\n" line ends), in order."""
    out: list[Gremlin] = []
    line, line_start, last = 1, 0, 0
    for match in GREMLIN_RE.finditer(text):
        start = match.start()
        newlines = text.count("\n", last, start)
        if newlines:
            line += newlines
            line_start = text.rfind("\n", 0, start) + 1
        last = start
        out.append(Gremlin(line, start - line_start, match.group()))
        if len(out) >= limit:
            break
    return out


def paint_gremlin(painter: QPainter, rect: QRectF):
    """A little gremlin (big ears, round eyes, a grin) filling ``rect``."""
    painter.save()
    painter.setRenderHint(QPainter.Antialiasing)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 24, rect.height() / 24)  # (drawn on a 24 × 24 grid)
    green, dark = QColor("#5fb04a"), QColor("#2b5a22")
    painter.setPen(QPen(dark, 1.2))
    painter.setBrush(green)
    for side in (-1, 1):  # ears: long and pointed, out to the sides
        ear = QPainterPath(QPointF(12 + side * 4, 9))
        ear.lineTo(QPointF(12 + side * 11.5, 4))
        ear.lineTo(QPointF(12 + side * 6, 14))
        ear.closeSubpath()
        painter.drawPath(ear)
    painter.drawEllipse(QRectF(5, 5.5, 14, 15))  # the head
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor("#fff7d6"))
    for x in (9, 15):
        painter.drawEllipse(QPointF(x, 11.5), 2.6, 2.9)  # eyes
    painter.setBrush(QColor("#c92a2a"))
    for x in (9.5, 14.5):
        painter.drawEllipse(QPointF(x, 12), 1.2, 1.4)  # (red pupils)
    painter.setPen(QPen(dark, 1.1))
    painter.setBrush(Qt.NoBrush)
    grin = QPainterPath(QPointF(8.5, 16))
    grin.quadTo(QPointF(12, 19), QPointF(15.5, 16))
    painter.drawPath(grin)
    painter.restore()
