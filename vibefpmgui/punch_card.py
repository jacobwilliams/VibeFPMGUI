"""A hidden command (type "punch card" in the Command Palette): the line at
the cursor as an IBM punched card, as an 029 keypunch would have punched it.

Fixed-form Fortran's columns come from these cards: 1–5 the statement
number, 6 a continuation mark, 7–72 the statement, 73–80 left for a
sequence number (so a dropped deck could be sorted again). Each column
holds one character, as holes in its rows (12, 11, 0–9, top to bottom):
the Hollerith code of the IBM 029 (1964). The 029 had capital letters
only, and no [ ] { } ~ ^ \\ ` — those columns are left blank.
"""

from __future__ import annotations

from qtpy.QtCore import QPointF, QRectF, Qt
from qtpy.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from qtpy.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

COLUMNS = 80
ROWS = (12, 11, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9)  # top to bottom
TAB = 8  # (a tab: spaces to the next multiple of 8, as a keypunch operator would have typed)

# Fortran's fields on the card: (first column, last column, name, short name for a narrow card).
FIELDS = ((1, 5, "STATEMENT NUMBER", "STMT NO."), (6, 6, "CONT", "C"), (7, 72, "FORTRAN STATEMENT", "FORTRAN"),
          (73, 80, "IDENTIFICATION", "IDENT."))

_SPECIAL = {
    " ": (), "&": (12,), "-": (11,), "/": (0, 1),
    "¢": (12, 2, 8), ".": (12, 3, 8), "<": (12, 4, 8), "(": (12, 5, 8), "+": (12, 6, 8), "|": (12, 7, 8),
    "!": (11, 2, 8), "$": (11, 3, 8), "*": (11, 4, 8), ")": (11, 5, 8), ";": (11, 6, 8), "¬": (11, 7, 8),
    ",": (0, 3, 8), "%": (0, 4, 8), "_": (0, 5, 8), ">": (0, 6, 8), "?": (0, 7, 8),
    ":": (2, 8), "#": (3, 8), "@": (4, 8), "'": (5, 8), "=": (6, 8), '"': (7, 8),
}


def holes(ch: str) -> tuple[int, ...] | None:
    """The rows an IBM 029 punches for a character (upper case: it had no
    lower case); None: it can't punch it."""
    ch = ch.upper()
    if ch.isdigit() and ch.isascii():
        return (int(ch),)
    if "A" <= ch <= "I":
        return 12, ord(ch) - ord("A") + 1
    if "J" <= ch <= "R":
        return 11, ord(ch) - ord("J") + 1
    if "S" <= ch <= "Z":
        return 0, ord(ch) - ord("S") + 2
    return _SPECIAL.get(ch)


def card_text(line: str) -> tuple[str, list[str], str]:
    """A line as punched: its first 80 columns (tabs as spaces, upper case),
    the characters an 029 couldn't punch (left blank), and what didn't fit."""
    expanded = ""
    for ch in line:
        expanded += " " * (TAB - len(expanded) % TAB) if ch == "\t" else ch
    punched, left_off = expanded[:COLUMNS], expanded[COLUMNS:]
    unpunchable = sorted({ch for ch in punched if holes(ch) is None})
    return punched.upper(), unpunchable, left_off


class PunchCard(QWidget):
    """The card: cream card stock, the cut corner, each row's digits printed
    faintly, the holes, the characters printed along the top edge (as the
    029 did), Fortran's fields ruled and named, and the column numbers."""

    STOCK = QColor("#efe4c2")
    INK = QColor("#2f2a22")
    PRINT = QColor(160, 120, 70, 150)  # (the card's own printing: faint)
    RULE = QColor(170, 60, 50, 160)    # (Fortran's fields)

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self.text = text
        self.setMinimumSize(760, 340)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_text(self, text: str):
        self.text = text
        self.update()

    def hole_rects(self) -> list[tuple[int, int, QRectF]]:
        """Each hole: (column (1-based), row, where it's drawn)."""
        card, column_width, row_height, top = self._layout()
        out = []
        for i, ch in enumerate(self.text[:COLUMNS]):
            for row in holes(ch) or ():
                x = card.left() + 0.035 * card.width() + i * column_width
                y = top + ROWS.index(row) * row_height
                out.append((i + 1, row, QRectF(x + column_width * 0.22, y + row_height * 0.18,
                                               column_width * 0.56, row_height * 0.64)))
        return out

    def _layout(self) -> tuple[QRectF, float, float, float]:
        """The card (7⅜ × 3¼ inches), a column's width, a row's height, row 12's top."""
        ratio = 7.375 / 3.25
        width = min(self.width() - 8.0, (self.height() - 8.0) * ratio)
        card = QRectF((self.width() - width) / 2, (self.height() - width / ratio) / 2, width, width / ratio)
        column_width = card.width() * 0.93 / COLUMNS
        top = card.top() + card.height() * 0.13  # (room above row 12 for the printed characters)
        row_height = card.height() * 0.78 / len(ROWS)  # (and below row 9 for the fields' names)
        return card, column_width, row_height, top

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        card, column_width, row_height, top = self._layout()
        left = card.left() + 0.035 * card.width()
        # The card, its top-left corner cut (to see at once that a card in the deck is the wrong way round).
        cut = card.height() * 0.12
        outline = QPolygonF([QPointF(card.left() + cut, card.top()), card.topRight(), card.bottomRight(),
                             card.bottomLeft(), QPointF(card.left(), card.top() + cut)])
        painter.setPen(QPen(QColor(0, 0, 0, 60), 1))
        painter.setBrush(self.STOCK)
        painter.drawPolygon(outline)
        # Fortran's fields: ruled, and named under row 9 (the short name where the long one doesn't fit).
        small = QFont("Courier New")
        small.setStyleHint(QFont.TypeWriter)
        small.setPixelSize(max(5, int(column_width * 0.62)))
        painter.setFont(small)
        bottom = top + len(ROWS) * row_height
        for first, last, name, short in FIELDS:
            x0, x1 = left + (first - 1) * column_width, left + last * column_width
            painter.setPen(QPen(self.RULE, 1))
            if first > 1:
                painter.drawLine(QPointF(x0, top - row_height * 0.2), QPointF(x0, bottom + row_height * 0.55))
            label = QRectF(x0, bottom + row_height * 0.05, x1 - x0, row_height * 0.5)
            fits = [n for n in (name, short) if painter.fontMetrics().horizontalAdvance(n) <= label.width() + 2]
            if fits:
                painter.drawText(label, Qt.AlignCenter, fits[0])
        # Each row's digit printed in every column (rows 0–9), and the column numbers under row 0.
        painter.setPen(self.PRINT)
        for row in range(10):
            y = top + ROWS.index(row) * row_height
            for column in range(COLUMNS):
                painter.drawText(QRectF(left + column * column_width, y, column_width, row_height),
                                 Qt.AlignCenter, str(row))
        tiny = QFont(small)
        tiny.setPixelSize(max(4, int(column_width * 0.42)))
        painter.setFont(tiny)
        for row in (0,):  # (under row 0: under row 9, the fields' names)
            y = top + (ROWS.index(row) + 0.72) * row_height
            for column in range(COLUMNS):
                painter.drawText(QRectF(left + column * column_width, y, column_width, row_height * 0.4),
                                 Qt.AlignCenter, str(column + 1))
        # The holes: the dark behind the card, through it.
        painter.setPen(Qt.NoPen)
        painter.setBrush(self.INK)
        for _column, _row, rect in self.hole_rects():
            painter.drawRect(rect)
        # The characters, printed along the top edge as the 029 printed them.
        typed = QFont(small)
        typed.setPixelSize(max(6, int(column_width * 0.95)))
        typed.setBold(True)
        painter.setFont(typed)
        painter.setPen(self.INK)
        for i, ch in enumerate(self.text[:COLUMNS]):
            if holes(ch):
                painter.drawText(QRectF(left + i * column_width, card.top() + card.height() * 0.02, column_width,
                                        card.height() * 0.09), Qt.AlignCenter, ch)
        painter.end()


class PunchCardDialog(QDialog):
    """The card for a line of a file; ↑ and ↓ go through the file's lines."""

    def __init__(self, lines: list[str], index: int, name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Punch Card")
        self.lines, self.index, self.name = lines, index, name
        self.card = PunchCard()
        self.caption = QLabel()
        self.caption.setWordWrap(True)
        self.caption.setTextFormat(Qt.RichText)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(self.card, 1)
        layout.addWidget(self.caption)
        layout.addWidget(buttons)
        self.resize(1000, 520)
        self.show_line(index)

    def show_line(self, index: int):
        self.index = max(0, min(index, len(self.lines) - 1)) if self.lines else 0
        line = self.lines[self.index] if self.lines else ""
        punched, unpunchable, left_off = card_text(line)
        self.card.set_text(punched)
        notes = [(f"Line {self.index + 1} of {self.name}, as an IBM 029 keypunch would have punched it "
                  "(1964: capital letters only; ↑ ↓ for the other lines).")]
        if unpunchable:
            notes.append(f"Not on an 029's keyboard, left blank: {' '.join(unpunchable)}")
        if left_off:
            notes.append(f"Past column 80, on the next card: {len(left_off)} more characters")
        self.caption.setText("<br>".join(f"<span style='color:gray'>{_escape(n)}</span>" for n in notes))

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key_Up, Qt.Key_Down):
            self.show_line(self.index + (1 if event.key() == Qt.Key_Down else -1))
            return
        super().keyPressEvent(event)


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
