"""The editor's minimap: a zoomed-out view of the whole file at its right.

Each line is drawn as small blocks in its syntax colors (one pixel per
character, a few pixels per line), with the gutter's indicators on top:
change bars and deleted-line marks at the left edge, compiler / lint / AI
review markers at the right edge, the current line and find matches. A
translucent box shows the part of the file in view; drag it to scroll, or
click anywhere to jump there. When the file is taller than the minimap, the
minimap scrolls along with the editor (as in VS Code).

Only the visible part is drawn, from per-line block lists cached until the
text or its colors change, so big files stay quick.
"""

from __future__ import annotations

import re

from qtpy.QtCore import QPoint, QPointF, QRectF, QSize, Qt, QTimer
from qtpy.QtGui import QColor, QPainter, QPalette, QPolygonF
from qtpy.QtWidgets import QPlainTextEdit, QWidget

from .editor_shared import CHANGE_COLORS, COVERAGE_COLORS, MARKER_COLORS, worst_severity

LINE_HEIGHT = 2.0  # logical pixels per line
CHAR_WIDTH = 1.0   # logical pixels per character
WIDTH = 104        # the minimap's width
LEFT = 6           # text starts after the change bars
TAB = 4            # columns a tab counts as
WORD_RE = re.compile(r"\S+")
ERROR_SEVERITIES = {"error", "fatal error", "lint error"}  # drawn as a band across the minimap
CODE_OPACITY = 0.45  # the code's blocks; markers, bars and bands are drawn at full strength


class Minimap(QWidget):
    def __init__(self, editor, parent=None):
        """``editor``: the `vibefpmgui.code_editor.CodeEditor` to show."""
        super().__init__(parent)
        self.editor = editor
        self.setFixedWidth(WIDTH)
        self.setMouseTracking(True)
        self.setCursor(Qt.ArrowCursor)
        self.setToolTip("Minimap: click to jump there, drag the box to scroll")
        self._rows: dict[int, list[tuple[float, float, QColor]]] = {}  # line -> blocks
        self._drag: float | None = None  # where in the view box the drag started
        self._rebuild_timer = QTimer(self)  # (coalesce bursts of edits)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(150)
        self._rebuild_timer.timeout.connect(self.invalidate)
        editor.document().contentsChange.connect(lambda *_: self._rebuild_timer.start())
        for bar in (editor.verticalScrollBar(),):
            bar.valueChanged.connect(self.update)
            bar.rangeChanged.connect(self.update)
        editor.cursorPositionChanged.connect(self.update)

    def sizeHint(self):
        return QSize(WIDTH, 200)

    def invalidate(self):
        """The text or its colors changed: draw the lines again."""
        self._rows.clear()
        self.update()

    # ---------------------------------------------------------- geometry
    def _lines(self) -> int:
        return max(1, self.editor.document().blockCount())

    def _wrapping(self) -> bool:
        return self.editor.lineWrapMode() != QPlainTextEdit.NoWrap

    def _first_line(self) -> int:
        """The first line in view. (Without word wrap, the scroll bar counts
        lines; with it, it counts screen rows, so ask for the block.)"""
        if self._wrapping():
            return self.editor.firstVisibleBlock().blockNumber()
        return self.editor.verticalScrollBar().value()

    def _visible_lines(self) -> float:
        if self._wrapping():
            last = self.editor.cursorForPosition(QPoint(0, self.editor.viewport().height() - 1))
            return max(1.0, last.blockNumber() - self._first_line() + 1.0)
        return max(1.0, self.editor.viewport().height() / max(1, self.editor.fontMetrics().height()))

    def _offset(self) -> float:
        """How far the minimap's own content is scrolled (when the file is
        taller than the minimap): in step with the editor's scroll bar."""
        total = self._lines() * LINE_HEIGHT
        spare = total - self.height()
        if spare <= 0:
            return 0.0
        bar = self.editor.verticalScrollBar()
        fraction = bar.value() / bar.maximum() if bar.maximum() > 0 else 0.0
        return spare * fraction

    def _view_box(self) -> QRectF:
        top = self._first_line() * LINE_HEIGHT - self._offset()
        return QRectF(0, top, self.width(), max(self._visible_lines() * LINE_HEIGHT, 6))

    # ----------------------------------------------------------- drawing
    def _row(self, block, default: QColor) -> list[tuple[float, float, QColor]]:
        """The blocks for one line: (x, width, color) per word, colored like
        the syntax highlighting."""
        text = block.text().expandtabs(TAB)
        colors: list[QColor | None] = [None] * len(text)
        for fr in block.layout().formats():
            brush = fr.format.foreground()
            if brush.style() != Qt.NoBrush:
                for i in range(fr.start, min(fr.start + fr.length, len(colors))):
                    colors[i] = brush.color()
        out = []
        limit = int((WIDTH - LEFT - 4) / CHAR_WIDTH)
        for match in WORD_RE.finditer(text[:limit]):
            start = match.start()
            run_start, run_color = start, colors[start] or default
            for i in range(start + 1, match.end() + 1):
                color = (colors[i] or default) if i < match.end() else None
                if color is None or color != run_color:
                    out.append((LEFT + run_start * CHAR_WIDTH, (i - run_start) * CHAR_WIDTH, run_color))
                    run_start, run_color = i, color
        return out

    def paintEvent(self, event):
        editor = self.editor
        pal = editor.palette()
        base, text_color = pal.color(QPalette.Base), pal.color(QPalette.Text)
        dark = base.lightness() < 128
        painter = QPainter(self)
        painter.fillRect(self.rect(), base.lighter(108) if dark else base.darker(102))
        painter.setPen(Qt.NoPen)

        offset = self._offset()
        doc = editor.document()
        first = max(0, int(offset / LINE_HEIGHT))
        last = min(self._lines() - 1, int((offset + self.height()) / LINE_HEIGHT) + 1)
        default = QColor(text_color)

        # The part of the file in view, behind the text.
        box = self._view_box()
        shade = QColor(text_color)
        shade.setAlpha(28 if dark else 22)
        painter.fillRect(box, shade)

        # Current line.
        current = editor.textCursor().blockNumber()
        if first <= current <= last:
            cur = QColor(pal.color(QPalette.Highlight))
            cur.setAlpha(90)
            painter.fillRect(QRectF(0, current * LINE_HEIGHT - offset, self.width(), LINE_HEIGHT), cur)

        # The text.
        block = doc.findBlockByNumber(first)
        number = first
        painter.setOpacity(CODE_OPACITY)  # (soft, so the markers on top stand out)
        while block.isValid() and number <= last:
            rows = self._rows.get(number)
            if rows is None:
                rows = self._rows[number] = self._row(block, default)
            y = number * LINE_HEIGHT - offset
            for x, w, color in rows:
                painter.fillRect(QRectF(x, y, w, LINE_HEIGHT - 0.5), color)
            block = block.next()
            number += 1
        painter.setOpacity(1.0)

        # Find matches: a soft yellow band on their lines.
        if editor.find_selections:
            found = QColor("#f2c94c")
            found.setAlpha(150)
            for sel in editor.find_selections[:2000]:
                line = sel.cursor.blockNumber()
                if first <= line <= last:
                    painter.fillRect(QRectF(LEFT, line * LINE_HEIGHT - offset, self.width() - LEFT - 4,
                                            LINE_HEIGHT), found)

        # Change bars (left edge) and deleted-line marks.
        if editor.show_coverage and editor.coverage:  # (lines the tests didn't run: under the change bars)
            missed = QColor(COVERAGE_COLORS["missed"])
            for line, count in editor.coverage.items():
                if not count and first <= line - 1 <= last:
                    painter.fillRect(QRectF(0, (line - 1) * LINE_HEIGHT - offset, 3, LINE_HEIGHT), missed)
        for line, kind in editor.change_marks.items():
            if first <= line - 1 <= last:
                painter.fillRect(QRectF(0, (line - 1) * LINE_HEIGHT - offset, 3, LINE_HEIGHT),
                                 QColor(CHANGE_COLORS[kind]))
        for line, (kind, _count) in editor.deletions.items():
            y = (line - 1) * LINE_HEIGHT - offset
            if -4 <= y <= self.height() + 4:
                painter.setBrush(QColor(CHANGE_COLORS[kind]))
                painter.drawPolygon(QPolygonF([QPointF(0, y - 2.5), QPointF(4, y), QPointF(0, y + 2.5)]))

        # Compiler / lint / AI markers (right edge), at least 3 px tall to be seen.
        # Errors (compiler errors, fortitude syntax errors) get a red band
        # across the whole width, so they stand out.
        for line, notes in editor.markers.items():
            y = (line - 1) * LINE_HEIGHT - offset
            if -3 <= y <= self.height():
                worst = worst_severity(notes)
                color = QColor(MARKER_COLORS.get(worst, "#868e96"))
                if worst in ERROR_SEVERITIES:
                    band = QColor(color)
                    band.setAlpha(200)
                    painter.fillRect(QRectF(0, y - 0.5, self.width(), max(LINE_HEIGHT, 3)), band)
                painter.fillRect(QRectF(self.width() - 4, y - 0.5, 4, max(LINE_HEIGHT, 3)), color)

        # The view box's outline, on top.
        outline = QColor(text_color)
        outline.setAlpha(70)
        painter.setPen(outline)
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(box.adjusted(0.5, 0.5, -0.5, -0.5))
        painter.end()

    # ------------------------------------------------------------- mouse
    def _scroll_to_line(self, line: float):
        """Scroll so that ``line`` (0-based) is the first in view."""
        bar = self.editor.verticalScrollBar()
        if self._wrapping():  # (the scroll bar counts screen rows)
            block = self.editor.document().findBlockByNumber(max(0, min(self._lines() - 1, int(line))))
            line = block.firstLineNumber()
        bar.setValue(round(max(0, min(bar.maximum(), line))))

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        y = event.position().y()
        box = self._view_box()
        if box.top() <= y <= box.bottom():
            self._drag = y - box.top()  # drag the box from where it was grabbed
        else:
            # Jump: center the view on the clicked line, then drag from its middle.
            line = (y + self._offset()) / LINE_HEIGHT
            self._scroll_to_line(line - self._visible_lines() / 2)
            self._drag = self._view_box().height() / 2

    def mouseMoveEvent(self, event):
        if self._drag is None or not event.buttons() & Qt.LeftButton:
            return
        y = event.position().y() - self._drag  # where the box's top should be
        total = self._lines() * LINE_HEIGHT
        box_h = self._view_box().height()
        if total <= self.height():
            self._scroll_to_line(y / LINE_HEIGHT)
        else:
            # The minimap scrolls too: move in proportion over its height.
            bar = self.editor.verticalScrollBar()
            room = max(1.0, self.height() - box_h)
            bar.setValue(round(max(0.0, min(1.0, y / room)) * bar.maximum()))

    def mouseReleaseEvent(self, event):
        self._drag = None

    def wheelEvent(self, event):
        # Scroll the editor, as if the wheel were over the text.
        self.editor.wheelEvent(event)
