"""Several cursors at once in the editor (⌥-click, column selection).

A part of `vibefpmgui.code_editor.CodeEditor`."""

from __future__ import annotations

import sys

from qtpy.QtCore import Qt
from qtpy.QtGui import QKeySequence, QTextCursor
from qtpy.QtWidgets import QApplication


class MultiCursorMixin:
    """Several cursors at once (a part of `vibefpmgui.code_editor.CodeEditor`)."""

    def all_cursors(self) -> list[QTextCursor]:
        """Every cursor, the main one included, in the order they're in the text."""
        return sorted([self.textCursor(), *self.extra_cursors], key=lambda c: c.position())

    def toggle_cursor_at(self, position: int):
        """⌥/Alt-click: add a cursor there, or remove the one that's there."""
        for c in self.extra_cursors:
            if c.position() == position:
                self.extra_cursors.remove(c)
                self._cursors_changed()
                return
        if position == self.textCursor().position() and not self.textCursor().hasSelection():
            return  # (the main cursor is already there)
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        self.extra_cursors.append(cursor)
        self._cursors_changed()

    def add_column_cursors(self, to_line: int, to_column: int | None = None):
        """⌘⇧/Ctrl+Shift-click: a cursor on each line from the main cursor's
        line to ``to_line`` (0-based). Clicked in the main cursor's column:
        cursors in that column; in another (``to_column``): a box selection,
        each line selected from the one column to the other, its cursor at
        the clicked end (both clipped to a shorter line's end)."""
        main = self.textCursor()
        first, column = main.blockNumber(), main.positionInBlock()
        end_column = column if to_column is None else to_column

        def on_line(block) -> QTextCursor:
            last = block.length() - 1  # (the line's end, before the line break)
            cursor = QTextCursor(block)
            cursor.setPosition(block.position() + min(column, last))
            cursor.setPosition(block.position() + min(end_column, last), QTextCursor.KeepAnchor)
            return cursor

        self.setTextCursor(on_line(main.block()))
        step = 1 if to_line >= first else -1
        for number in range(first + step, to_line + step, step):
            block = self.document().findBlockByNumber(number)
            if not block.isValid():
                break
            self.extra_cursors.append(on_line(block))
        self._cursors_changed()

    def clear_extra_cursors(self):
        if self.extra_cursors:
            self.extra_cursors = []
            self._cursors_changed()

    def _cursors_changed(self):
        # Two cursors on the same spot are one (e.g. after deleting between them).
        seen = {(self.textCursor().position(), self.textCursor().anchor())}
        kept = []
        for c in self.extra_cursors:
            key = (c.position(), c.anchor())
            if key not in seen:
                seen.add(key)
                kept.append(c)
        self.extra_cursors = kept
        if kept and self.cursorWidth():  # several cursors: draw them all ourselves
            self._caret_width = self.cursorWidth()
            self.setCursorWidth(0)
        elif not kept and not self.cursorWidth():  # one again: Qt's own
            self.setCursorWidth(self._caret_width)
        self._restart_blink()
        self.update_extra_selections(rebuild=False)  # (the cursors' selections are always made anew)
        self.viewport().update()
        self.cursors_changed.emit()

    def _restart_blink(self):
        """Typing or moving shows the cursors and starts the blink again (as
        Qt does with its own)."""
        if not self.extra_cursors:
            self._caret_timer.stop()
            return
        self._caret_on = True
        flash = QApplication.cursorFlashTime()  # (a whole on-off cycle; 0: no blinking)
        if flash > 0:
            self._caret_timer.start(flash // 2)
        else:
            self._caret_timer.stop()
        self.viewport().update()

    def _blink(self):
        self._caret_on = not self._caret_on
        self.viewport().update()

    def _at_each_cursor(self, edit):
        """``edit(cursor)`` at every cursor, as one Undo step."""
        main = self.textCursor()
        cursors = [main, *self.extra_cursors]
        main.beginEditBlock()  # (the document's: every cursor's edits are in it)
        for c in sorted(cursors, key=lambda c: c.position(), reverse=True):
            edit(c)
        main.endEditBlock()
        self.setTextCursor(main)
        self._cursors_changed()

    def visual_column(self, cursor: QTextCursor) -> int:
        """The column a position shows at (a tab: to the next tab stop)."""
        tab = max(1, round(self.tabStopDistance() / max(1, self.fontMetrics().horizontalAdvance(" "))))
        column = 0
        for ch in cursor.block().text()[:cursor.positionInBlock()]:
            column = (column // tab + 1) * tab if ch == "\t" else column + 1
        return column

    def align_cursors(self) -> bool:
        """Edit → Lines → Align Cursors: spaces before each cursor left of the
        right-most one, so they're all in its column (one Undo step). Several
        cursors on a line: the first on each line aligned, then the second, and
        so on. A cursor with a selection: by its left edge (the selection moves
        along). False: fewer than two cursors (nothing to align)."""
        cursors = self.all_cursors()
        if len(cursors) < 2:
            return False
        lines: dict[int, list[QTextCursor]] = {}
        for c in cursors:
            edge = QTextCursor(c)  # (where the spaces go: a selection's left edge)
            edge.setPosition(c.selectionStart())
            lines.setdefault(edge.blockNumber(), []).append(edge)
        for row in lines.values():
            row.sort(key=lambda c: c.position())
        main = self.textCursor()
        main.beginEditBlock()
        for k in range(max(len(row) for row in lines.values())):
            column_k = [row[k] for row in lines.values() if len(row) > k]
            target = max(self.visual_column(c) for c in column_k)
            for c in column_k:
                gap = target - self.visual_column(c)
                if gap > 0:
                    c.insertText(" " * gap)  # (the cursors after it on the line move along)
        main.endEditBlock()
        self.setTextCursor(main)
        self._cursors_changed()
        return True

    def _move_each_cursor(self, operation, select: bool):
        mode = QTextCursor.KeepAnchor if select else QTextCursor.MoveAnchor
        main = self.textCursor()
        for c in (main, *self.extra_cursors):
            c.movePosition(operation, mode)
        self.setTextCursor(main)
        self._cursors_changed()

    def _multi_cursor_key(self, event) -> bool:
        """A key with several cursors: True if it was handled here."""
        self.completion_popup.hide()
        self.signature_popup.hide()
        key, mods = event.key(), event.modifiers()
        shift = bool(mods & Qt.ShiftModifier)
        word = bool(mods & (Qt.AltModifier if sys.platform == "darwin" else Qt.ControlModifier))
        if key == Qt.Key_Escape:
            self.clear_extra_cursors()
            return True
        if event.matches(QKeySequence.Copy):
            self.copy()
            return True
        if event.matches(QKeySequence.Cut):
            self.cut()
            return True
        if event.matches(QKeySequence.Paste):
            self.paste()
            return True
        moves = {Qt.Key_Left: QTextCursor.WordLeft if word else QTextCursor.Left,
                 Qt.Key_Right: QTextCursor.WordRight if word else QTextCursor.Right,
                 Qt.Key_Up: QTextCursor.Up, Qt.Key_Down: QTextCursor.Down,
                 Qt.Key_Home: QTextCursor.StartOfBlock, Qt.Key_End: QTextCursor.EndOfBlock}
        command = sys.platform == "darwin" and mods & Qt.ControlModifier
        if command and key in (Qt.Key_Left, Qt.Key_Right):  # ⌘← / ⌘→: the start / end of the line
            self._move_each_cursor(QTextCursor.StartOfBlock if key == Qt.Key_Left else QTextCursor.EndOfBlock,
                                   shift)
            return True
        if key in moves and not command:
            self._move_each_cursor(moves[key], shift)
            return True
        if key == Qt.Key_Backspace:
            self._at_each_cursor(lambda c: c.removeSelectedText() if c.hasSelection() else c.deletePreviousChar())
            return True
        if key == Qt.Key_Delete:
            self._at_each_cursor(lambda c: c.removeSelectedText() if c.hasSelection() else c.deleteChar())
            return True
        if key in (Qt.Key_Return, Qt.Key_Enter):
            def newline(c):
                line = c.block().text()
                c.insertText("\n" + line[:len(line) - len(line.lstrip())])  # (keeping the indentation)
            self._at_each_cursor(newline)
            return True
        if key == Qt.Key_Tab:
            self._at_each_cursor(lambda c: c.insertText(self.tab_text(c)))
            return True
        text = event.text()
        if text and text.isprintable() and not mods & (Qt.ControlModifier | Qt.MetaModifier):
            self._at_each_cursor(lambda c: c.insertText(text))
            return True
        return False  # (other keys: shortcuts and the like, for the main cursor)

    def copy(self):
        if not self.extra_cursors:
            super().copy()
            return
        texts = [c.selectedText().replace("\u2029", "\n") for c in self.all_cursors() if c.hasSelection()]
        if texts:
            QApplication.clipboard().setText("\n".join(texts))

    def cut(self):
        if not self.extra_cursors:
            super().cut()
            return
        self.copy()
        self._at_each_cursor(lambda c: c.removeSelectedText())

    def paste(self):
        if not self.extra_cursors:
            super().paste()
            return
        text = QApplication.clipboard().text().replace("\r\n", "\n")
        lines = text.split("\n")
        cursors = self.all_cursors()
        # As many lines as cursors: one each (e.g. copied with these cursors).
        pieces = dict(zip((c.position() for c in cursors), lines)) if len(lines) == len(cursors) else None

        def insert(c):
            c.insertText(pieces[c.position()] if pieces is not None and c.position() in pieces else text)

        self._at_each_cursor(insert)
