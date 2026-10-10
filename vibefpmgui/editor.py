"""A small built-in code editor: line numbers, syntax highlighting (Pygments,
chosen by file extension), find/replace, go to line, and compiler warning /
error markers in the gutter.

:class:`EditorTab` wraps a :class:`CodeEditor` with a find bar and a status
line, and takes care of loading and saving the file (keeping its encoding and
line endings) and of noticing when it changes on disk.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import QFileSystemWatcher, QRect, QSize, Qt, QTimer, Signal
from qtpy.QtGui import (
    QColor, QFont, QFontDatabase, QPainter, QPalette, QTextCharFormat,
    QTextCursor, QTextDocument, QTextFormat,
)
from qtpy.QtWidgets import (
    QApplication, QCheckBox, QFileDialog, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPlainTextEdit, QTextEdit, QToolButton, QToolTip, QVBoxLayout, QWidget,
)

from .highlighter import DARK_STYLE, LIGHT_STYLE, PygmentsHighlighter, is_dark, lexer_for_path

INDENT = "    "
MAX_FIND_HIGHLIGHTS = 2000
MARKER_COLORS = {"error": "#e03131", "fatal error": "#e03131", "warning": "#f08c00",
                 "remark": "#1c7ed6"}


class LineNumberArea(QWidget):
    """The gutter: line numbers plus warning/error markers."""

    def __init__(self, editor: "CodeEditor"):
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
            notes = self.editor.markers.get(line)
            if notes:
                QToolTip.showText(event.globalPos(), "\n".join(m for _, m in notes), self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)

    def mousePressEvent(self, event):
        # Clicking a number selects that line.
        line = self.editor.line_at_y(event.pos().y())
        if line:
            self.editor.go_to_line(line, select=True)


class CodeEditor(QPlainTextEdit):
    """Plain-text editor with a line-number gutter, current-line highlight,
    auto-indent and soft tabs."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.markers: dict[int, list[tuple[str, str]]] = {}  # line -> [(severity, message)]
        self.find_selections: list[QTextEdit.ExtraSelection] = []
        self.gutter = LineNumberArea(self)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(self._update_extra_selections)
        self._update_gutter_width()
        self._update_extra_selections()

    # ------------------------------------------------------------- gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return 22 + self.fontMetrics().horizontalAdvance("9") * digits

    def _update_gutter_width(self, *_):
        self.setViewportMargins(self.gutter_width(), 0, 0, 0)

    def _update_gutter(self, rect, dy):
        if dy:
            self.gutter.scroll(0, dy)
        else:
            self.gutter.update(0, rect.y(), self.gutter.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self._update_gutter_width()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))

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
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                line = number + 1
                font = QFont(self.font())
                font.setBold(number == current)
                painter.setFont(font)
                painter.setPen(text if number == current else muted)
                painter.drawText(0, top, width - 8, height, Qt.AlignRight | Qt.AlignVCenter, str(line))
                notes = self.markers.get(line)
                if notes:
                    worst = "error" if any(s in ("error", "fatal error") for s, _ in notes) else notes[0][0]
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(MARKER_COLORS.get(worst, "#868e96")))
                    d = max(6, height // 2)
                    painter.drawEllipse(4, top + (height - d) // 2, d, d)
            block = block.next()
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())
            number += 1
        painter.end()

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
        self._update_extra_selections()
        self.gutter.update()

    # -------------------------------------------------------- highlights
    def _update_extra_selections(self):
        """Current line, lines with errors/warnings, and find matches."""
        selections = []
        for line, notes in self.markers.items():
            block = self.document().findBlockByNumber(line - 1)
            if not block.isValid():
                continue
            worst = "error" if any(s in ("error", "fatal error") for s, _ in notes) else notes[0][0]
            sel = QTextEdit.ExtraSelection()
            color = QColor(MARKER_COLORS.get(worst, "#868e96"))
            color.setAlpha(34)
            sel.format.setBackground(color)
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            selections.append(sel)
        if not self.isReadOnly():
            sel = QTextEdit.ExtraSelection()
            color = QColor(self.palette().color(QPalette.Highlight))
            color.setAlpha(28)
            sel.format.setBackground(color)
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = self.textCursor()
            sel.cursor.clearSelection()
            selections.append(sel)
        self.setExtraSelections(selections + self.find_selections)

    def set_find_selections(self, selections):
        self.find_selections = selections
        self._update_extra_selections()

    # ------------------------------------------------------------ editing
    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Tab and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            cursor = self.textCursor()
            if cursor.hasSelection() and " " in cursor.selectedText():
                self._indent_selection(+1)
            else:
                cursor.insertText(INDENT)
            return
        if key == Qt.Key_Backtab:
            self._indent_selection(-1)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            # Keep the current line's indentation.
            line = self.textCursor().block().text()
            indent = line[:len(line) - len(line.lstrip())]
            super().keyPressEvent(event)
            self.textCursor().insertText(indent)
            return
        if key == Qt.Key_Backspace and not self.textCursor().hasSelection():
            # Backspace in leading spaces removes a whole indent level.
            cursor = self.textCursor()
            before = cursor.block().text()[:cursor.positionInBlock()]
            if before and not before.strip() and len(before) % len(INDENT) == 0:
                for _ in INDENT:
                    cursor.deletePreviousChar()
                return
        super().keyPressEvent(event)

    def _indent_selection(self, direction: int):
        cursor = self.textCursor()
        start = self.document().findBlock(cursor.selectionStart())
        end = self.document().findBlock(cursor.selectionEnd())
        cursor.beginEditBlock()
        block = start
        while block.isValid():
            c = QTextCursor(block)
            if direction > 0:
                c.insertText(INDENT)
            else:
                text = block.text()
                remove = min(len(INDENT), len(text) - len(text.lstrip(" ")))
                for _ in range(remove):
                    c.deleteChar()
            if block == end:
                break
            block = block.next()
        cursor.endEditBlock()

    def go_to_line(self, line: int, select: bool = False):
        block = self.document().findBlockByNumber(max(0, line - 1))
        if not block.isValid():
            block = self.document().lastBlock()
        cursor = QTextCursor(block)
        if select:
            cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()
        self.setFocus()


class FindBar(QWidget):
    """Find (and optionally replace) bar shown below the editor."""

    def __init__(self, editor: CodeEditor):
        super().__init__()
        self.editor = editor
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find")
        self.find_edit.setClearButtonEnabled(True)
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace with")
        self.case_check = QCheckBox("Match case")
        self.word_check = QCheckBox("Whole words")
        self.count_label = QLabel()

        def button(text, tip, slot):
            b = QToolButton()
            b.setText(text)
            b.setToolTip(tip)
            b.setAutoRaise(True)
            b.clicked.connect(slot)
            return b

        self.prev_button = button("‹", "Previous match (Shift+Enter)", self.find_previous)
        self.next_button = button("›", "Next match (Enter)", self.find_next)
        self.replace_button = button("Replace", "Replace this match", self.replace_one)
        self.replace_all_button = button("All", "Replace every match", self.replace_all)
        close = button("✕", "Close (Esc)", self.close_bar)

        top = QHBoxLayout()
        top.setContentsMargins(6, 4, 6, 0)
        top.addWidget(self.find_edit, 1)
        top.addWidget(self.count_label)
        top.addWidget(self.prev_button)
        top.addWidget(self.next_button)
        top.addWidget(self.case_check)
        top.addWidget(self.word_check)
        top.addWidget(close)
        self.replace_row = QWidget()
        bottom = QHBoxLayout(self.replace_row)
        bottom.setContentsMargins(6, 0, 6, 0)
        bottom.addWidget(self.replace_edit, 1)
        bottom.addWidget(self.replace_button)
        bottom.addWidget(self.replace_all_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 4)
        layout.setSpacing(4)
        layout.addLayout(top)
        layout.addWidget(self.replace_row)

        self.find_edit.textChanged.connect(self._update_highlights)
        self.find_edit.returnPressed.connect(self.find_next)
        self.replace_edit.returnPressed.connect(self.replace_one)
        self.case_check.toggled.connect(self._update_highlights)
        self.word_check.toggled.connect(self._update_highlights)
        self.editor.textChanged.connect(self._update_highlights_if_visible)
        self.hide()

    def open_bar(self, replace: bool = False):
        selected = self.editor.textCursor().selectedText()
        if selected and " " not in selected:
            self.find_edit.setText(selected)
        self.replace_row.setVisible(replace and not self.editor.isReadOnly())
        self.show()
        self.find_edit.setFocus()
        self.find_edit.selectAll()
        self._update_highlights()

    def close_bar(self):
        self.hide()
        self.editor.set_find_selections([])
        self.editor.setFocus()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close_bar()
            return
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ShiftModifier:
            self.find_previous()
            return
        super().keyPressEvent(event)

    def _flags(self, backward: bool = False):
        flags = QTextDocument.FindFlag(0)
        if self.case_check.isChecked():
            flags |= QTextDocument.FindCaseSensitively
        if self.word_check.isChecked():
            flags |= QTextDocument.FindWholeWords
        if backward:
            flags |= QTextDocument.FindBackward
        return flags

    def find_next(self):
        self._find(backward=False)

    def find_previous(self):
        self._find(backward=True)

    def _find(self, backward: bool):
        text = self.find_edit.text()
        if not text:
            return False
        if not self.editor.find(text, self._flags(backward)):
            # Wrap around.
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.End if backward else QTextCursor.Start)
            self.editor.setTextCursor(cursor)
            if not self.editor.find(text, self._flags(backward)):
                return False
        self._update_count()
        return True

    def replace_one(self):
        cursor = self.editor.textCursor()
        text = self.find_edit.text()
        if cursor.hasSelection() and self._matches(cursor.selectedText(), text):
            cursor.insertText(self.replace_edit.text())
        self.find_next()

    def replace_all(self):
        text = self.find_edit.text()
        if not text:
            return
        doc = self.editor.document()
        cursor = QTextCursor(doc)
        cursor.beginEditBlock()
        count = 0
        found = doc.find(text, 0, self._flags())
        while not found.isNull():
            found.insertText(self.replace_edit.text())
            count += 1
            found = doc.find(text, found.position(), self._flags())
        cursor.endEditBlock()
        self.count_label.setText(f"{count} replaced")

    def _matches(self, selected: str, text: str) -> bool:
        return selected == text if self.case_check.isChecked() else selected.lower() == text.lower()

    def _all_matches(self) -> list:
        text = self.find_edit.text()
        if not text:
            return []
        doc = self.editor.document()
        out = []
        found = doc.find(text, 0, self._flags())
        while not found.isNull() and len(out) < MAX_FIND_HIGHLIGHTS:
            out.append(found)
            found = doc.find(text, found.selectionEnd(), self._flags())
        return out

    def _update_highlights_if_visible(self):
        if self.isVisible():
            self._update_highlights()

    def _update_highlights(self):
        matches = self._all_matches()
        fmt = QTextCharFormat()
        color = QColor("#fcc419")
        color.setAlpha(110)
        fmt.setBackground(color)
        selections = []
        for cursor in matches:
            sel = QTextEdit.ExtraSelection()
            sel.format = fmt
            sel.cursor = cursor
            selections.append(sel)
        self.editor.set_find_selections(selections)
        self._matches_cache = matches
        self._update_count()

    def _update_count(self):
        matches = getattr(self, "_matches_cache", [])
        if not self.find_edit.text():
            self.count_label.setText("")
            return
        if not matches:
            self.count_label.setText("no matches")
            return
        pos = self.editor.textCursor().selectionStart()
        index = next((i for i, c in enumerate(matches) if c.selectionStart() == pos), None)
        more = "+" if len(matches) >= MAX_FIND_HIGHLIGHTS else ""
        self.count_label.setText(f"{index + 1} of {len(matches)}{more}" if index is not None
                                 else f"{len(matches)}{more} matches")


class EditorTab(QWidget):
    """One open file: editor + find bar + status line."""

    dirty_changed = Signal(bool)
    saved = Signal(str)
    path_changed = Signal(str)

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path).resolve()
        self.encoding = "utf-8"
        self.newline = "\n"
        self._disk_text = ""
        self.editor = CodeEditor()
        self.highlighter = PygmentsHighlighter(self.editor.document(), lexer_for_path(self.path))
        self.find_bar = FindBar(self.editor)
        self.status = QLabel()
        self.status.setContentsMargins(8, 2, 8, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.find_bar)
        layout.addWidget(self.status)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._file_changed)
        self._check_timer = QTimer(self)  # coalesce bursts of change events
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(200)
        self._check_timer.timeout.connect(self._check_disk)

        self.editor.document().modificationChanged.connect(self.dirty_changed)
        self.editor.cursorPositionChanged.connect(self._update_status)
        self.load()
        self.refresh_style()

    # ------------------------------------------------------------- files
    @staticmethod
    def is_probably_text(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                return b"\0" not in f.read(8192)
        except OSError:
            return False

    def _read(self) -> str:
        raw = self.path.read_bytes()
        try:
            text = raw.decode("utf-8")
            self.encoding = "utf-8"
        except UnicodeDecodeError:
            text = raw.decode("latin-1")
            self.encoding = "latin-1"
        self.newline = "\r\n" if "\r\n" in text else "\n"
        return text.replace("\r\n", "\n")

    def load(self):
        text = self._read()
        self._disk_text = text
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        self._watch()
        self._update_status()

    def _watch(self):
        if self.watcher.files():
            self.watcher.removePaths(self.watcher.files())
        if self.path.exists():
            self.watcher.addPath(str(self.path))

    def is_dirty(self) -> bool:
        return self.editor.document().isModified()

    def save(self) -> bool:
        text = self.editor.toPlainText()
        try:
            data = text.replace("\n", self.newline).encode(self.encoding)
        except UnicodeEncodeError:
            data = text.replace("\n", self.newline).encode("utf-8")
            self.encoding = "utf-8"
        try:
            self.path.write_bytes(data)
        except OSError as exc:
            QMessageBox.warning(self, "Save failed", f"Could not save {self.path}:\n{exc}")
            return False
        self._disk_text = text
        self.editor.document().setModified(False)
        self.editor.set_markers({})  # the compiler messages no longer apply
        self._watch()
        self.saved.emit(str(self.path))
        return True

    def save_as(self) -> bool:
        filename, _ = QFileDialog.getSaveFileName(self, "Save As", str(self.path))
        if not filename:
            return False
        self.path = Path(filename).resolve()
        self.highlighter.set_lexer(lexer_for_path(self.path))
        self.path_changed.emit(str(self.path))
        return self.save()

    def maybe_save(self) -> bool:
        """Ask about unsaved changes; True if it's fine to close."""
        if not self.is_dirty():
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", f"Save changes to {self.path.name}?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Save:
            return self.save()
        return answer == QMessageBox.Discard

    def _file_changed(self, _path: str):
        self._check_timer.start()

    def _check_disk(self):
        """The file changed on disk (another editor, git, ...)."""
        self._watch()  # some tools replace the file, which drops the watch
        if not self.path.exists():
            self._update_status("deleted on disk")
            return
        try:
            text = self._read()
        except OSError:
            return
        if text == self._disk_text:
            return  # e.g. our own save
        if self.is_dirty():
            answer = QMessageBox.question(
                self, "File changed on disk",
                f"{self.path.name} was changed by another program. Reload it and lose your edits?")
            if answer != QMessageBox.Yes:
                self._disk_text = text  # don't ask again for this version
                return
        cursor = self.editor.textCursor().position()
        scroll = self.editor.verticalScrollBar().value()
        self._disk_text = text
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        c = self.editor.textCursor()
        c.setPosition(min(cursor, len(text)))
        self.editor.setTextCursor(c)
        self.editor.verticalScrollBar().setValue(scroll)

    # ----------------------------------------------------------- display
    def set_font(self, font: QFont):
        self.editor.setFont(font)
        self.editor.setTabStopDistance(self.editor.fontMetrics().horizontalAdvance(" ") * len(INDENT))
        self.editor._update_gutter_width()

    def refresh_style(self):
        dark = is_dark(QApplication.palette())
        self.highlighter.set_style(DARK_STYLE if dark else LIGHT_STYLE)
        palette = self.editor.palette()
        palette.setColor(QPalette.Base, self.highlighter.background())
        palette.setColor(QPalette.Text, QColor("#f8f8f2") if dark else QColor("#1b1b1b"))
        self.editor.setPalette(palette)
        self.editor._update_extra_selections()
        self.editor.gutter.update()

    def _update_status(self, note: str = ""):
        cursor = self.editor.textCursor()
        parts = [f"Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}",
                 self.highlighter.lexer.name, self.encoding.upper(),
                 "CRLF" if self.newline == "\r\n" else "LF"]
        if self.editor.markers:
            errors = sum(1 for notes in self.editor.markers.values()
                         for s, _ in notes if s in ("error", "fatal error"))
            warnings = sum(1 for notes in self.editor.markers.values() for s, _ in notes if s == "warning")
            parts.append(f"{errors} errors, {warnings} warnings from the last build")
        if note:
            parts.append(note)
        self.status.setText("<span style='color:gray'>" + " · ".join(parts) + "</span>")

    def show_diagnostics(self, diagnostics):
        """Mark compiler messages (objects with .line, .severity, .message)."""
        markers: dict[int, list[tuple[str, str]]] = {}
        for d in diagnostics:
            markers.setdefault(d.line, []).append((d.severity, d.message))
        self.editor.set_markers(markers)
        self._update_status()

    def go_to_line_dialog(self):
        line, ok = QInputDialog.getInt(
            self, "Go to Line", f"Line (1–{self.editor.blockCount()}):",
            self.editor.textCursor().blockNumber() + 1, 1, self.editor.blockCount())
        if ok:
            self.editor.go_to_line(line)


def monospace_font(size_factor: float = 1.0) -> QFont:
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    font.setPointSizeF(font.pointSizeF() * size_factor)
    return font

