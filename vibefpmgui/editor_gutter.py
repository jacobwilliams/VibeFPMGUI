"""The editor's gutter: breakpoints, line numbers, change bars, coverage, gremlins and markers.

A part of `vibefpmgui.code_editor.CodeEditor`."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

from qtpy.QtCore import QPointF, QRect, QRectF, QSize, Qt
from qtpy.QtGui import QColor, QFont, QPainter, QPalette, QPen, QPolygonF, QTextCursor
from qtpy.QtWidgets import QToolTip, QWidget

from . import gremlins as gremlins_mod
from .editor_shared import (
    BREAKPOINT_COLOR,
    BREAKPOINT_UNPLACED,
    BREAKPOINT_WIDTH,
    CHANGE_COLORS,
    COVERAGE_COLORS,
    COVERAGE_WIDTH,
    EXECUTION_COLOR,
    GREMLIN_WIDTH,
    MARKER_COLORS,
    worst_severity,
)

if TYPE_CHECKING:
    from .code_editor import CodeEditor
from . import hover_card


class LineNumberArea(QWidget):
    """The gutter: line numbers plus warning/error markers."""

    def __init__(self, editor: CodeEditor):
        super().__init__(editor)
        self.editor = editor
        self.setMouseTracking(True)

    def sizeHint(self):
        return QSize(self.editor.gutter_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_gutter(event)

    def event(self, event):
        if event.type() == event.Type.ToolTip:
            line = self.editor.line_at_y(event.pos().y())
            if event.pos().x() < self.editor._breakpoint_column():
                tip = self.editor.breakpoint_tip(line)
                if tip:
                    QToolTip.showText(event.globalPos(), tip, self)
                else:
                    QToolTip.hideText()
                return True
            origin = self.editor._breakpoint_column()
            if origin <= event.pos().x() < origin + self.editor._coverage_column() and self.editor.coverage_note(line):
                QToolTip.showText(event.globalPos(), self.editor.coverage_note(line), self)
                return True
            change_tip = self._change_tip(event.pos(), line)
            if change_tip:  # over a change bar or a deletion marker
                QToolTip.showText(event.globalPos(), change_tip, self)
                return True
            notes = self.editor.markers.get(line)
            # (Spelling has no gutter marks: its notes are on the words only.)
            issues = [i for i in self.editor.lint_issues if i.line == line and i.severity != "spelling"]
            popup = self.editor.hover_popup
            if issues:  # the same card as hovering the text (with its fix links)
                if not (popup.isVisible() and popup.anchor == ("gutter", line)):
                    # (fortitude's and fortls's notes are the card's issues already)
                    others = [m for severity, m in notes or [] if severity not in hover_card.LINT_COLORS]
                    popup.show_at(event.globalPos(), self.editor.lint_card_html(issues, others), ("gutter", line))
                QToolTip.hideText()
            elif notes or line in self.editor.gremlins:
                lines = [m for _, m in notes or []] + self.editor.gremlin_notes(line)
                QToolTip.showText(event.globalPos(), "\n".join(lines), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def _change_tip(self, pos, line: int) -> str:
        """Tooltip for the change bars at the gutter's right edge."""
        if pos.x() < self.width() - 9:
            return ""
        editor = self.editor
        block = editor.document().findBlockByNumber(line - 1)
        if block.isValid():
            top = editor.blockBoundingGeometry(block).translated(editor.contentOffset()).top()
            bottom = top + editor.blockBoundingRect(block).height()
            for deleted_at, y in ((line, top), (line + 1, bottom)):
                if deleted_at in editor.deletions and abs(pos.y() - y) <= 5:
                    kind, count = editor.deletions[deleted_at]
                    what = f"{count} line{'s' * (count != 1)} deleted here"
                    return what + (" — not saved yet" if kind == "unsaved" else " (saved)")
        change = editor.change_marks.get(line)
        if change == "unsaved":
            return "Changed — not saved yet"
        if change == "saved":
            return "Changed since the file was opened (saved)"
        return ""

    def mouseMoveEvent(self, event):
        popup = self.editor.hover_popup
        if popup.isVisible() and popup.anchor and popup.anchor[0] == "gutter":
            if popup.anchor == ("gutter", self.editor.line_at_y(event.position().toPoint().y())):
                popup.keep()
            else:
                popup.schedule_hide()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event):
        self.editor.hover_popup.schedule_hide()  # (unless the mouse went into it)
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        # Clicking the breakpoint column sets or clears one; a number selects that line.
        line = self.editor.line_at_y(event.pos().y())
        if line and event.pos().x() < self.editor._breakpoint_column():
            if event.modifiers() & Qt.ControlModifier:  # (⌘ on macOS: disable or enable it)
                self.editor.toggle_breakpoint_enabled(line)
            else:
                self.editor.toggle_breakpoint(line)
        elif line:
            self.editor.go_to_line(line, select=True)


class EditorGutterMixin:
    """The editor's gutter (a part of `vibefpmgui.code_editor.CodeEditor`)."""

    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return (22 + self.fontMetrics().horizontalAdvance("9") * digits + self._coverage_column()
                + self._gremlin_column() + self._breakpoint_column())

    def _breakpoint_column(self) -> int:
        return BREAKPOINT_WIDTH if self.debuggable else 0

    # ------------------------------------------------------------ breakpoints
    def breakpoint_lines(self) -> list[int]:
        """The lines (1-based) with a breakpoint, enabled or not, as they are
        now (they move with edits)."""
        return sorted({cursor.blockNumber() + 1 for cursor, _on in self._breakpoints})

    def disabled_breakpoints(self) -> list[int]:
        """The lines whose breakpoint is disabled (kept, drawn hollow; the debugger doesn't stop there)."""
        enabled = {cursor.blockNumber() + 1 for cursor, on in self._breakpoints if on}
        return sorted({cursor.blockNumber() + 1 for cursor, on in self._breakpoints if not on} - enabled)

    def enabled_breakpoints(self) -> list[int]:
        disabled = set(self.disabled_breakpoints())
        return [n for n in self.breakpoint_lines() if n not in disabled]

    def set_breakpoint_lines(self, lines: list[int], disabled: list[int] | None = None):
        """Breakpoints on these lines (those past the end: dropped); ``disabled``: these of them, disabled."""
        doc = self.document()
        off = set(disabled or ())
        self._breakpoints = []
        for line in sorted(set(lines)):
            block = doc.findBlockByNumber(line - 1)
            if block.isValid():
                self._breakpoints.append((QTextCursor(block), line not in off))
        self.breakpoint_problems = {}
        self.gutter.update()

    def toggle_breakpoint(self, line: int):
        """A breakpoint on ``line`` set, or removed (gutter click, F9)."""
        if not self.debuggable:
            return
        lines, off = self.breakpoint_lines(), self.disabled_breakpoints()
        self.set_breakpoint_lines([n for n in lines if n != line] if line in lines else [*lines, line], off)
        self.breakpoints_changed.emit()

    def toggle_breakpoint_enabled(self, line: int):
        """The breakpoint on ``line`` disabled (kept, hollow), or enabled again
        (⌘/Ctrl-click, Ctrl+F9); none there: one set."""
        if not self.debuggable:
            return
        if line not in self.breakpoint_lines():
            self.toggle_breakpoint(line)
            return
        self.set_breakpoints_enabled([line], line in self.disabled_breakpoints())

    def set_breakpoints_enabled(self, lines: list[int], enabled: bool):
        """These lines' breakpoints enabled, or disabled (Debug → Disable All Breakpoints)."""
        if not self.debuggable:
            return
        wanted = set(lines)
        changed = [(cursor, enabled if cursor.blockNumber() + 1 in wanted else on) for cursor, on in self._breakpoints]
        if changed != self._breakpoints:
            self._breakpoints = changed
            self.gutter.update()
            self.breakpoints_changed.emit()

    def set_breakpoint_problems(self, problems: dict[int, str]):
        """Why the debugger couldn't place some of them (line -> why): drawn grey."""
        if problems != self.breakpoint_problems:
            self.breakpoint_problems = dict(problems)
            self.gutter.update()

    def breakpoint_tip(self, line: int) -> str:
        if line == self.execution_line:
            return "Stopped here (the debugger)"
        modifier = "⌘" if sys.platform == "darwin" else "Ctrl"
        if line in self.disabled_breakpoints():
            return f"Breakpoint, disabled ({modifier}-click to enable it; click to remove it)"
        if line in self.breakpoint_problems:
            return f"Breakpoint not placed: {self.breakpoint_problems[line]}"
        if line in self.breakpoint_lines():
            return f"Breakpoint (click to remove it; {modifier}-click to disable it)"
        return "Click to set a breakpoint (F9)" if line else ""

    def set_execution_line(self, line: int | None):
        """Where the debugger stopped, in this file (None: not here): an arrow
        in the gutter, and the line tinted."""
        if line != self.execution_line:
            self.execution_line = line
            self.gutter.update()
            self.update_extra_selections()

    def _gremlin_column(self) -> int:
        return GREMLIN_WIDTH if self.gremlins else 0

    def _coverage_column(self) -> int:
        return COVERAGE_WIDTH if self.coverage and self.show_coverage else 0

    def set_coverage(self, lines: dict[int, int] | None, branches: dict[int, tuple[int, int]] | None = None):
        """The last test run's coverage of this file: line -> times run (lines
        with code only), shown as green and red bars in the gutter (amber: it
        ran, but not all of its ``branches``: line -> (taken, total))."""
        lines, branches = dict(lines or {}), dict(branches or {}) if lines else {}
        if lines == self.coverage and branches == self.coverage_branches:
            return
        self.coverage, self.coverage_branches = lines, branches
        self.refresh_gutter()

    def coverage_state(self, line: int) -> str | None:
        """ "ran", "partial" (ran; not every branch taken), "missed", or None
        (no code there, or no coverage)."""
        if line not in self.coverage:
            return None
        if not self.coverage[line]:
            return "missed"
        taken, total = self.coverage_branches.get(line, (0, 0))
        return "partial" if taken < total else "ran"

    def missed_starts(self) -> list[int]:
        """The first line of each stretch of lines the tests didn't run
        (lines without code in between don't end a stretch)."""
        starts, previous = [], None
        for line in sorted(self.coverage):
            missed = self.coverage[line] == 0
            if missed and previous is not True:
                starts.append(line)
            previous = missed
        return starts

    def go_to_missed(self, forward: bool = True) -> bool:
        """Edit → Go To → Next / Previous Missed Line: to the next stretch of lines
        the tests didn't run (around to the start at the end); False if
        there's none."""
        starts = self.missed_starts()
        if not starts:
            return False
        here = self.textCursor().blockNumber() + 1
        if forward:
            target = next((n for n in starts if n > here), starts[0])
        else:
            # (From inside a stretch, the one before it: not its own start.)
            current = max((n for n in starts if n <= here), default=None)
            target = next((n for n in reversed(starts) if n < (current if current is not None else here)),
                          starts[-1])
        self.go_to_line(target)
        return True

    def refresh_gutter(self):
        """The gutter again, at its width now (a column shown or hidden), and
        the lines' tints (missed lines)."""
        self.update_gutter_width()
        self.gutter.setGeometry(QRect(self.contentsRect().left(), self.contentsRect().top(),
                                      self.gutter_width(), self.contentsRect().height()))
        self.gutter.update()
        self.update_extra_selections()
        self._update_minimap()

    def coverage_note(self, line: int) -> str:
        """The gutter's tooltip for a coverage bar."""
        if line not in self.coverage:
            return ""
        count = self.coverage[line]
        if not count:
            return "Not run by the tests"
        note = f"Run {count:,} time{'s' * (count != 1)} by the tests"
        taken, total = self.coverage_branches.get(line, (0, 0))
        if total:
            note += f" · {taken} of {total} branch{'es' * (total != 1)} taken"
        return note

    def set_gremlins(self, found: list[gremlins_mod.Gremlin]):
        """The file's gremlins (non-ASCII and invisible characters): a gremlin
        in the gutter on their lines, and the characters tinted."""
        by_line: dict[int, list[gremlins_mod.Gremlin]] = {}
        for g in found:
            by_line.setdefault(g.line, []).append(g)
        if by_line == self.gremlins:
            return
        self.gremlins = by_line
        self.update_gutter_width()
        self.gutter.setGeometry(QRect(self.contentsRect().left(), self.contentsRect().top(),
                                      self.gutter_width(), self.contentsRect().height()))
        self.update_extra_selections()
        self.gutter.update()

    def gremlin_notes(self, line: int) -> list[str]:
        """The gremlins on a line, described (for the gutter's tooltip)."""
        return [f"Gremlin at column {g.column + 1}: {g.message} ({g.code})" for g in self.gremlins.get(line, [])]

    def update_gutter_width(self, *_):
        # (Called at every repaint of the whole view: recoloring a big file
        # repaints it once per line. Worked out again only when the number of
        # digits, the font or the columns may have changed.)
        key = (len(str(self.blockCount())), self.font().key(), self._coverage_column(), self._gremlin_column(),
               self._breakpoint_column())
        if key == getattr(self, "_gutter_key", None):
            return
        self._gutter_key = key
        width = self.gutter_width()
        if width != self.viewportMargins().left():
            self.setViewportMargins(width, 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_gutter_width()

    def paint_gutter(self, event):
        pal = self.palette()
        painter = QPainter(self.gutter)
        base = pal.color(QPalette.Base)
        gutter_bg = base.darker(104) if base.lightness() >= 128 else base.lighter(118)
        painter.fillRect(event.rect(), gutter_bg)
        text = pal.color(QPalette.Text)
        muted = QColor(text)
        muted.setAlpha(110)
        current = self.textCursor().blockNumber()
        block = self.firstVisibleBlock()
        number = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        height = self.fontMetrics().height()
        width = self.gutter.width()
        painter.setRenderHint(QPainter.Antialiasing)
        origin = self._breakpoint_column()  # (the breakpoints, at the left edge: the rest after them)
        breakpoints = set(self.breakpoint_lines()) if origin else set()
        disabled = set(self.disabled_breakpoints()) if origin else set()
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                line = number + 1
                if origin:
                    self._paint_breakpoint(painter, line, line in breakpoints, line in disabled, top, height)
                left = self._coverage_column()
                state = self.coverage_state(line) if left else None
                if state:  # (ran: green; ran, not every branch: amber; didn't: red)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(COVERAGE_COLORS[state]))
                    painter.drawRect(QRectF(origin, top, 3, bottom - top))
                left += origin
                if line in self.gremlins:
                    size = min(GREMLIN_WIDTH - 2, height)
                    gremlins_mod.paint_gremlin(painter, QRectF(1 + left, top + (height - size) / 2, size, size))
                font = QFont(self.font())
                font.setBold(number == current)
                painter.setFont(font)
                painter.setPen(text if number == current else muted)
                painter.drawText(0, top, width - 8, height, Qt.AlignRight | Qt.AlignVCenter, str(line))
                notes = self.markers.get(line)
                if notes:
                    worst = worst_severity(notes)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(MARKER_COLORS.get(worst, "#868e96")))
                    d = max(6, height // 2)
                    painter.drawEllipse(4 + left + self._gremlin_column(), top + (height - d) // 2, d, d)
                # Change bar (full line height) at the edge next to the text.
                change = self.change_marks.get(line)
                if change:
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(CHANGE_COLORS[change]))
                    painter.drawRect(QRectF(width - 4, top, 3, bottom - top))
                # Deleted lines: a small triangle on the boundary above this
                # line (and below the last one, for a deletion at the end).
                for deleted_at, y in ((line, top), (line + 1, bottom)):
                    if deleted_at == line + 1 and block.next().isValid():
                        continue
                    if deleted_at in self.deletions:
                        kind, _count = self.deletions[deleted_at]
                        painter.setPen(Qt.NoPen)
                        painter.setBrush(QColor(CHANGE_COLORS[kind]))
                        painter.drawPolygon(QPolygonF([QPointF(width - 7, y - 4.5), QPointF(width - 1, y),
                                                       QPointF(width - 7, y + 4.5)]))
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

    def _paint_breakpoint(self, painter: QPainter, line: int, on: bool, disabled: bool, top: int, height: int):
        """A line's breakpoint (a red dot; hollow: disabled; grey: the debugger
        couldn't place it) and, where the program is stopped, an arrow over it."""
        d = min(BREAKPOINT_WIDTH - 5, height - 4)
        x, y = (BREAKPOINT_WIDTH - d) / 2, top + (height - d) / 2
        if on:
            color = QColor(BREAKPOINT_COLOR)
            if disabled:
                painter.setPen(QPen(color, 1.5))
                painter.setBrush(Qt.NoBrush)
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(BREAKPOINT_UNPLACED) if line in self.breakpoint_problems else color)
            painter.drawEllipse(QRectF(x, y, d, d))
        if line == self.execution_line:
            painter.setPen(QPen(QColor(EXECUTION_COLOR).darker(140), 1))
            painter.setBrush(QColor(EXECUTION_COLOR))
            mid = top + height / 2
            painter.drawPolygon(QPolygonF([QPointF(1.5, mid - d / 2), QPointF(1.5 + d * 0.55, mid - d / 2),
                                           QPointF(BREAKPOINT_WIDTH - 1.5, mid),
                                           QPointF(1.5 + d * 0.55, mid + d / 2), QPointF(1.5, mid + d / 2)]))

    def set_change_marks(self, marks: dict[int, str], deletions: dict[int, tuple[str, int]]):
        if marks != self.change_marks or deletions != self.deletions:
            self.change_marks, self.deletions = marks, deletions
            self.gutter.update()
            self._update_minimap()

    def line_at_y(self, y: int) -> int:
        block = self.firstVisibleBlock()
        top = self.blockBoundingGeometry(block).translated(self.contentOffset()).top()
        while block.isValid():
            bottom = top + self.blockBoundingRect(block).height()
            if top <= y < bottom:
                return block.blockNumber() + 1
            block = block.next()
            top = bottom
        return 0

    def set_markers(self, markers: dict[int, list[tuple[str, str]]]):
        self.markers = markers
        self.update_extra_selections()
        self.gutter.update()
        self._update_minimap()
