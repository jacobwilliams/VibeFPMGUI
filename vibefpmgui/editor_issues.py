"""Issues in the editor's text: their squiggles, and the hover cards over them.

A part of `vibefpmgui.code_editor.CodeEditor`."""

from __future__ import annotations

import html
from bisect import bisect_left

from qtpy.QtCore import QPoint, QPointF, QRect, Qt
from qtpy.QtGui import (
    QColor,
    QCursor,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QTextCursor,
)

from . import hover_card, lint
from .editor_shared import (
    DARK_UNDERLINE_COLORS,
    MARKER_COLORS,
    word_bounds,
)


class EditorIssuesMixin:
    """The issues' squiggles and hover cards (a part of `vibefpmgui.code_editor.CodeEditor`)."""

    def set_lint(self, issues: list[lint.LintIssue]):
        self.lint_issues = issues
        # The misspelled words in line order, for the squiggles (see squiggle_spans).
        self._spelling = sorted((i for i in issues if i.severity == "spelling"), key=lambda i: (i.line, i.column))
        self._spelling_lines = [i.line for i in self._spelling]
        self.update_extra_selections()
        self.viewport().update()  # (the spelling squiggles: see _paint_squiggles)

    def replace_word(self, issue, word: str):
        """A spelling suggestion: the misspelled word becomes ``word`` (if
        it's still there)."""
        span = self._lint_span(issue)
        if span is None:
            return
        cursor = QTextCursor(self.document())
        cursor.setPosition(span[0])
        cursor.setPosition(span[1], QTextCursor.KeepAnchor)
        if cursor.selectedText() != issue.word:
            return  # (the text changed since)
        cursor.insertText(word)
        self.setTextCursor(cursor)

    def _lint_span(self, issue: lint.LintIssue) -> tuple[int, int] | None:
        """Document positions to underline: the issue's text, only on its
        first line (a syntax error can span the rest of the file); at least
        one character so it can be seen and hovered."""
        block = self.document().findBlockByNumber(issue.line - 1)
        if not block.isValid():
            return None
        length = max(0, block.length() - 1)
        start = min(max(issue.column - 1, 0), length)
        end = min(issue.end_column - 1, length) if issue.end_line == issue.line else length
        if end <= start:
            start, end = (start, start + 1) if start < length else (max(0, length - 1), length)
        if end <= start:
            return None  # an empty line
        return block.position() + start, block.position() + end

    def word_at(self, point: QPoint) -> tuple[int, int, str] | None:
        """(1-based line, 0-based start column, word) of the name under a
        viewport point, or None (whitespace, past the end of the line…)."""
        cursor = self.cursorForPosition(point)
        if abs(self.cursorRect(cursor).center().x() - point.x()) > self.fontMetrics().horizontalAdvance("M"):
            return None  # the nearest text is too far away
        text = cursor.block().text()
        bounds = word_bounds(text, cursor.positionInBlock())
        if bounds is None:
            return None
        start, end = bounds
        return cursor.blockNumber() + 1, start, text[start:end]

    def lint_card_html(self, issues, other_notes=()) -> str:
        """fortitude's messages for ``issues`` (with fix links), plus any
        other notes for the line (compiler, AI review) as plain paragraphs."""
        hover_card.set_text_color(self.palette().color(QPalette.Text))
        text = hover_card.lint_notes_html(issues, [self.lint_issues.index(i) for i in issues])
        for note in other_notes:
            text += f"<p style='margin:4px 0 0 0'>{html.escape(note)}</p>"
        return text

    def _lint_visible(self, issue) -> bool:
        """Is the start of an issue's underline within the visible text area
        (not scrolled off to the side, as a long line's S001 usually is)?"""
        span = self._lint_span(issue)
        if span is None:
            return False
        cursor = QTextCursor(self.document())
        cursor.setPosition(span[0])
        x = self.cursorRect(cursor).left()
        return 0 <= x < self.viewport().width()

    def hover_key(self, point: QPoint):
        """What the mouse is on, for deciding when the hover popup is stale:
        the name (line, start column), or else the character position."""
        word = self.word_at(point)
        return word[:2] if word else ("at", self.cursorForPosition(point).position())

    def viewportEvent(self, event):
        if event.type() == event.Type.ToolTip:
            if self.hover_popup.isVisible() and self.hover_popup.anchor == self.hover_key(event.pos()):
                return True  # already showing (and maybe scrolled): leave it
            cursor = self.cursorForPosition(event.pos())
            pos = cursor.position()
            notes = [i for i in self.lint_issues
                     if (span := self._lint_span(i)) and span[0] <= pos <= span[1]]
            key = self.hover_key(event.pos())
            if not notes:
                # An issue whose underline is scrolled out of view (a long
                # line's S001 starts at column 101) shows anywhere on its line.
                line = cursor.blockNumber() + 1
                notes = [i for i in self.lint_issues if i.line == line and not self._lint_visible(i)]
                if notes:
                    key = ("line", line)
                    if self.hover_popup.isVisible() and self.hover_popup.anchor == key:
                        return True
            lint_html = self.lint_card_html(notes) if notes else ""
            word = self.word_at(event.pos()) if self.hover_provider is not None else None
            self._hover_word = word[:2] if word else None
            if notes:
                self.hover_popup.show_at(event.globalPos(), lint_html, key)
            elif self.hover_popup.anchor != key:
                self.hover_popup.hide()
            if word:  # the language server's answer is added when it arrives
                self.hover_provider(word[0], word[1], event.globalPos(), lint_html)
            return True
        if event.type() == event.Type.Leave:
            self.hover_popup.schedule_hide()
        return super().viewportEvent(event)

    def show_hover(self, line: int, column: int, global_pos: QPoint, text_html: str):
        """Show a hover answer, unless the mouse has moved off that name."""
        if self._hover_word != (line, column):
            return
        word = self.word_at(self.viewport().mapFromGlobal(QCursor.pos()))
        if word is None or word[:2] != (line, column):
            return
        self.hover_popup.show_at(global_pos, text_html, (line, column))

    def hide_popups(self):
        self.hover_popup.hide()
        self.completion_popup.hide()
        self.signature_popup.hide()

    def squiggle_spans(self, area: QRect) -> list[tuple[float, float, float, str]]:
        """Where the misspelled words' squiggles go in ``area`` (viewport
        coordinates): (left, right, y, color) per row a word is on."""
        issues = self._spelling
        if not issues:
            return []
        dark = self.palette().color(QPalette.Base).lightness() < 128
        color = (DARK_UNDERLINE_COLORS if dark else MARKER_COLORS)["spelling"]
        first = self.firstVisibleBlock()
        top_line = first.blockNumber() + 1
        offset = self.contentOffset()
        out = []
        # (Only the words in view: from the first visible line, until one is below the area.)
        for issue in issues[bisect_left(self._spelling_lines, top_line):]:
            block = self.document().findBlockByNumber(issue.line - 1)
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.top() > area.bottom():
                break
            if geometry.bottom() < area.top() or not block.isVisible():
                continue
            span = self._lint_span(issue)
            layout = block.layout()
            if span is None or layout is None:
                continue
            start, end = span[0] - block.position(), span[1] - block.position()
            for n in range(layout.lineCount()):
                row = layout.lineAt(n)
                a, b = max(start, row.textStart()), min(end, row.textStart() + row.textLength())
                if a >= b:
                    continue
                x1, x2 = row.cursorToX(a), row.cursorToX(b)
                x1 = x1[0] if isinstance(x1, tuple) else x1  # (PySide: (x, position))
                x2 = x2[0] if isinstance(x2, tuple) else x2
                y = geometry.top() + row.y() + row.ascent() + max(1.5, row.descent() * 0.45)
                out.append((geometry.left() + x1, geometry.left() + x2, y, color))
        return out

    def _paint_squiggles(self, area: QRect):
        """Misspelled words' wavy underline, drawn here (thicker than the one
        Qt draws for the other issues, which is easy to miss under a
        comment's thin text, especially on a dark background)."""
        spans = self.squiggle_spans(area)
        if not spans:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        size = self.font().pointSizeF() if self.font().pointSizeF() > 0 else 12.0
        width = max(1.4, size / 8)    # pen width
        height = max(1.6, size / 7)   # half the wave's height
        step = max(2.5, size / 4.5)   # half a wave's length
        for left, right, y, color in spans:
            path = QPainterPath(QPointF(left, y))
            x, up = left, True
            while x < right:
                nxt = min(x + step, right)
                path.quadTo(QPointF((x + nxt) / 2, y - height if up else y + height), QPointF(nxt, y))
                x, up = nxt, not up
            pen = QPen(QColor(color), width)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawPath(path)
        painter.end()
