"""Showing two versions of a file: side by side, or as one unified list.

`align` lines the versions up row by row (difflib), and `with_context`
keeps a few unchanged lines around each change, folding the rest into a
"⋯ N" row. `DiffView` draws the rows: each side in a syntax-colored pane
with its own line numbers, removed lines tinted red and added ones green,
and the two sides scrolling together.
"""

from __future__ import annotations

import difflib
from typing import NamedTuple

from qtpy.QtCore import QRect, QSize, Qt, Signal
from qtpy.QtGui import QColor, QFont, QPainter, QPalette, QTextCharFormat, QTextFormat
from qtpy.QtWidgets import (
    QHBoxLayout,
    QPlainTextEdit,
    QStackedWidget,
    QTextEdit,
    QWidget,
)

from .highlighter import PygmentsHighlighter, apply_style, is_dark, lexer_for_path

SAME, CHANGED, REMOVED, ADDED, GAP = "same", "changed", "removed", "added", "gap"
FILLER = "filler"  # (a pane's row with no line: the other side has one)
CONTEXT = 3  # unchanged lines kept around each change
REMOVED_COLOR, ADDED_COLOR = QColor("#e03131"), QColor("#2f9e44")


class Row(NamedTuple):
    kind: str                # SAME, CHANGED (a line edited: old and new), REMOVED, ADDED, GAP
    old_no: int | None       # 1-based line numbers (None: no line on that side)
    old: str | None
    new_no: int | None
    new: str | None
    hidden: int = 0          # GAP: how many unchanged lines it stands for


def lines_of(text: str | None) -> list[str]:
    """A text's lines (none for None or "")."""
    if not text:
        return []
    lines = text.split("\n")
    return lines[:-1] if text.endswith("\n") else lines


def align(old: list[str], new: list[str]) -> list[Row]:
    """Every line of both versions, lined up: unchanged lines side by side,
    edited ones paired (CHANGED), the rest REMOVED or ADDED. The lines the
    two begin and end with alike are matched as they are; only those
    between (usually a small part of a file) are compared line by line,
    which takes time growing with their number squared (a 12,000-line file
    whole: a third of a second)."""
    shorter = min(len(old), len(new))
    start = 0
    while start < shorter and old[start] == new[start]:
        start += 1
    end = 0  # (lines alike at the end, after those at the start)
    while end < shorter - start and old[-1 - end] == new[-1 - end]:
        end += 1
    rows: list[Row] = [Row(SAME, i + 1, old[i], i + 1, new[i]) for i in range(start)]
    matcher = difflib.SequenceMatcher(None, old[start:len(old) - end], new[start:len(new) - end], autojunk=False)
    for op, a0, a1, b0, b1 in matcher.get_opcodes():
        a0, a1, b0, b1 = a0 + start, a1 + start, b0 + start, b1 + start
        if op == "equal":
            rows += [Row(SAME, a0 + i + 1, old[a0 + i], b0 + i + 1, new[b0 + i]) for i in range(a1 - a0)]
            continue
        paired = min(a1 - a0, b1 - b0) if op == "replace" else 0
        rows += [Row(CHANGED, a0 + i + 1, old[a0 + i], b0 + i + 1, new[b0 + i]) for i in range(paired)]
        rows += [Row(REMOVED, a, old[a - 1], None, None) for a in range(a0 + paired + 1, a1 + 1)]
        rows += [Row(ADDED, None, None, b, new[b - 1]) for b in range(b0 + paired + 1, b1 + 1)]
    rows += [Row(SAME, len(old) - end + i + 1, old[len(old) - end + i], len(new) - end + i + 1,
                 new[len(new) - end + i]) for i in range(end)]
    return rows


def compare(old: str | None, new: str | None) -> list[Row]:
    """Two versions' rows to show (`align`, folded with `with_context`): no
    Qt, so it can be worked out on a pool thread (see DiffView.show_rows)."""
    return with_context(align(lines_of(old), lines_of(new)))


def with_context(rows: list[Row], context: int = CONTEXT) -> list[Row]:
    """The rows near a change (``context`` unchanged lines around each), the
    other unchanged ones folded into GAP rows."""
    near = [False] * len(rows)
    for i, row in enumerate(rows):
        if row.kind != SAME:
            for j in range(max(0, i - context), min(len(rows), i + context + 1)):
                near[j] = True
    out: list[Row] = []
    i = 0
    while i < len(rows):
        if near[i]:
            out.append(rows[i])
            i += 1
            continue
        j = i
        while j < len(rows) and not near[j]:
            j += 1
        out.append(Row(GAP, None, None, None, None, j - i))
        i = j
    return out


class PaneRows(NamedTuple):
    """What a pane shows, row by row."""
    text: list[str]
    gutter: list[tuple[str, str]]  # (numbers, mark: "-", "+" or "")
    kinds: list[str]               # SAME, REMOVED, ADDED, FILLER, GAP
    new_lines: list[int | None]    # the newer version's line on each row (None: none there)


def side_by_side(rows: list[Row]) -> tuple[PaneRows, PaneRows]:
    """The older and the newer version's panes: a row each per Row."""
    old, new = PaneRows([], [], [], []), PaneRows([], [], [], [])
    for row in rows:
        for pane, number, line, kind, mark in ((old, row.old_no, row.old, REMOVED, "-"),
                                              (new, row.new_no, row.new, ADDED, "+")):
            pane.new_lines.append(row.new_no)
            if row.kind == GAP:
                pane.text.append("")
                pane.gutter.append((f"⋯ {row.hidden:,}", ""))
                pane.kinds.append(GAP)
                continue
            changed = row.kind in (CHANGED, kind)
            pane.text.append(line if line is not None else "")
            pane.gutter.append((str(number) if number else "", mark if changed else ""))
            pane.kinds.append(kind if changed else FILLER if number is None else SAME)
    return old, new


def unified(rows: list[Row]) -> PaneRows:
    """One pane: unchanged lines once; within each run of changes, the
    removed lines and then the added ones."""
    pane = PaneRows([], [], [], [])
    removed: list[Row] = []
    added: list[Row] = []

    def flush():
        for row in removed:
            pane.text.append(row.old)
            pane.gutter.append((f"{row.old_no:>5} {'':>5}", "-"))
            pane.kinds.append(REMOVED)
            pane.new_lines.append(None)
        for row in added:
            pane.text.append(row.new)
            pane.gutter.append((f"{'':>5} {row.new_no:>5}", "+"))
            pane.kinds.append(ADDED)
            pane.new_lines.append(row.new_no)
        removed.clear()
        added.clear()

    for row in rows:
        if row.kind in (REMOVED, CHANGED):
            removed.append(row)
        if row.kind in (ADDED, CHANGED):
            added.append(row)
        if row.kind in (SAME, GAP):
            flush()
            pane.text.append(row.new if row.kind == SAME else "")
            pane.gutter.append((f"{row.old_no:>5} {row.new_no:>5}", "") if row.kind == SAME
                               else (f"⋯ {row.hidden:,}", ""))
            pane.kinds.append(row.kind)
            pane.new_lines.append(row.new_no)
    flush()
    return pane


# ------------------------------------------------------------------ widgets
class _Gutter(QWidget):
    def __init__(self, pane: DiffPane):
        super().__init__(pane)
        self.pane = pane

    def sizeHint(self):
        return QSize(self.pane.gutter_width(), 0)

    def paintEvent(self, event):
        self.pane.paint_gutter(event)


class DiffPane(QPlainTextEdit):
    """One side of a diff (or the unified list): read-only text, a gutter of
    line numbers (and -/+ marks), and a tint behind each changed line."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.rows = PaneRows([], [], [], [])
        self.highlighter: PygmentsHighlighter | None = None
        self.gutter = _Gutter(self)
        self.updateRequest.connect(self._scroll_gutter)

    def gutter_width(self) -> int:
        widest = max((len(numbers) for numbers, _mark in self.rows.gutter), default=1)
        return self.fontMetrics().horizontalAdvance("9" * widest + " + ") + 8

    def _place_gutter(self):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)
        r = self.contentsRect()
        self.gutter.setGeometry(QRect(r.left(), r.top(), self.gutter_width(), r.height()))

    def _scroll_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._place_gutter()

    def show_rows(self, rows: PaneRows, path: str):
        """``path`` picks the syntax colors."""
        self.rows = rows
        lexer = lexer_for_path(path)
        if self.highlighter is None:
            self.highlighter = PygmentsHighlighter(self.document(), lexer)
        with self.highlighter.replacing_text(lexer):  # (colored at once: not typed)
            self.setPlainText("\n".join(rows.text))
        apply_style(self, self.highlighter)
        self._place_gutter()
        self._tint()

    def refresh_style(self):
        if self.highlighter is not None:
            apply_style(self, self.highlighter)
        self._tint()

    def set_font(self, font: QFont):
        self.setFont(font)
        self._place_gutter()

    def line_at(self, row: int) -> int | None:
        """The newer version's line at a row, or the nearest one before it (a
        removed line has none of its own)."""
        for line in reversed(self.rows.new_lines[:row + 1]):
            if line is not None:
                return line
        return None

    def _tint(self):
        """A tint behind removed and added lines; rows with no line, and
        folded unchanged ones, a little darker than the background."""
        dark = is_dark(self.palette())
        base = self.palette().color(QPalette.Base)
        colors = {REMOVED: QColor(REMOVED_COLOR), ADDED: QColor(ADDED_COLOR)}
        for color in colors.values():
            color.setAlpha(70 if dark else 45)
        colors[FILLER] = colors[GAP] = base.lighter(125) if dark else base.darker(106)
        selections = []
        block = self.document().firstBlock()
        kinds = self.rows.kinds
        while block.isValid():
            n = block.blockNumber()
            color = colors.get(kinds[n]) if n < len(kinds) else None
            if color is not None:
                selection = QTextEdit.ExtraSelection()
                selection.format = QTextCharFormat()
                selection.format.setBackground(color)
                selection.format.setProperty(QTextFormat.FullWidthSelection, True)
                selection.cursor = self.textCursor()
                selection.cursor.setPosition(block.position())
                selections.append(selection)
            block = block.next()
        self.setExtraSelections(selections)

    def paint_gutter(self, event):
        painter = QPainter(self.gutter)
        dark = is_dark(self.palette())
        base = self.palette().color(QPalette.Base)
        painter.fillRect(event.rect(), base.lighter(112) if dark else base.darker(104))
        muted = QColor(self.palette().color(QPalette.Text))
        muted.setAlpha(130)
        block = self.firstVisibleBlock()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        height = self.fontMetrics().height()
        while block.isValid() and top <= event.rect().bottom():
            n = block.blockNumber()
            if n < len(self.rows.gutter):
                numbers, mark = self.rows.gutter[n]
                if self.rows.kinds[n] == GAP:
                    painter.setPen(muted)
                    painter.drawText(QRect(0, top, self.gutter.width(), height), Qt.AlignCenter, numbers)
                else:
                    painter.setPen(REMOVED_COLOR if mark == "-" else ADDED_COLOR if mark == "+" else muted)
                    painter.drawText(QRect(0, top, self.gutter.width() - 6, height),
                                     Qt.AlignRight | Qt.AlignVCenter, f"{numbers} {mark or ' '}")
            top += round(self.blockBoundingRect(block).height())
            block = block.next()


class DiffView(QWidget):
    """Two versions of a file, side by side or as one unified list.
    Double-clicking a line asks to open the file there."""

    open_requested = Signal(int)  # a line of the newer version (1-based)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.unified = False
        self.rows: list[Row] = []
        self.path = ""
        self.old_pane, self.new_pane, self.unified_pane = DiffPane(), DiffPane(), DiffPane()
        side = QWidget()
        side_layout = QHBoxLayout(side)
        side_layout.setContentsMargins(0, 0, 0, 0)
        side_layout.setSpacing(2)
        side_layout.addWidget(self.old_pane)
        side_layout.addWidget(self.new_pane)
        self.stack = QStackedWidget()
        self.stack.addWidget(side)
        self.stack.addWidget(self.unified_pane)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.stack)
        # The two sides scroll together (they have the same rows).
        for a, b in ((self.old_pane, self.new_pane), (self.new_pane, self.old_pane)):
            a.verticalScrollBar().valueChanged.connect(b.verticalScrollBar().setValue)
            a.horizontalScrollBar().valueChanged.connect(b.horizontalScrollBar().setValue)
        for pane in self.panes():
            pane.viewport().installEventFilter(self)

    def panes(self) -> tuple[DiffPane, DiffPane, DiffPane]:
        return self.old_pane, self.new_pane, self.unified_pane

    def set_unified(self, on: bool):
        self.unified = on
        self.stack.setCurrentIndex(1 if on else 0)
        self._show()

    def show_diff(self, old: str | None, new: str | None, path: str):
        """Compare two versions (None: there's no such version, so every line
        of the other is added or removed)."""
        self.show_rows(compare(old, new), path)

    def show_rows(self, rows: list[Row], path: str):
        """Rows already compared (`compare`, on a pool thread for big files)."""
        self.path = path
        self.rows = rows
        self._show()

    def clear(self):
        self.rows, self.path = [], ""
        for pane in self.panes():
            pane.show_rows(PaneRows([], [], [], []), "")

    def _show(self):
        if self.unified:
            self.unified_pane.show_rows(unified(self.rows), self.path)
        else:
            old, new = side_by_side(self.rows)
            self.old_pane.show_rows(old, self.path)
            self.new_pane.show_rows(new, self.path)

    def refresh_style(self):
        for pane in self.panes():
            pane.refresh_style()

    def set_font(self, font: QFont):
        for pane in self.panes():
            pane.set_font(font)

    def eventFilter(self, obj, event):
        if event.type() == event.Type.MouseButtonDblClick:
            pane = next((p for p in self.panes() if p.viewport() is obj), None)
            if pane is not None:
                line = pane.line_at(pane.cursorForPosition(event.position().toPoint()).blockNumber())
                if line is not None:
                    self.open_requested.emit(line)
                    return True
        return False
