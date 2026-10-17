"""The editor's find and replace bar."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from qtpy.QtCore import QRegularExpression, Qt
from qtpy.QtGui import QTextCursor, QTextDocument
from qtpy.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .editor_shared import MAX_FIND_HIGHLIGHTS

BEYOND_BMP_RE = re.compile("[\U00010000-\U0010FFFF]")  # (characters Qt counts as two)

if TYPE_CHECKING:
    from .code_editor import CodeEditor


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
        if backward:
            flags |= QTextDocument.FindBackward
        return flags

    def _needle(self):
        """What to look for: the text, or with *Whole words* a regular
        expression (Qt's own whole-word search counts "_" as a break, so
        "count" would match in count_all)."""
        text = self.find_edit.text()
        if not self.word_check.isChecked():
            return text
        options = QRegularExpression.NoPatternOption
        if not self.case_check.isChecked():
            options |= QRegularExpression.CaseInsensitiveOption
        return QRegularExpression(rf"(?<!\w){QRegularExpression.escape(text)}(?!\w)", options)

    def find_next(self):
        self._find(backward=False)

    def find_previous(self):
        self._find(backward=True)

    def _find(self, backward: bool):
        text = self.find_edit.text()
        if not text:
            return False
        if not self.editor.find(self._needle(), self._flags(backward)):
            # Wrap around.
            cursor = self.editor.textCursor()
            cursor.movePosition(QTextCursor.End if backward else QTextCursor.Start)
            self.editor.setTextCursor(cursor)
            if not self.editor.find(self._needle(), self._flags(backward)):
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
        found = doc.find(self._needle(), 0, self._flags())
        while not found.isNull():
            found.insertText(self.replace_edit.text())
            count += 1
            found = doc.find(self._needle(), found.position(), self._flags())
        cursor.endEditBlock()
        self.count_label.setText(f"{count} replaced")

    def _matches(self, selected: str, text: str) -> bool:
        return selected == text if self.case_check.isChecked() else selected.lower() == text.lower()

    def _all_matches(self) -> list[tuple[int, int]]:
        """The matches' (start, end) positions (not cursors: a cursor kept is
        one more that Qt moves along at every edit), found in the text at
        once (Qt's find, match by match, took a fifth of a second for a big
        file's 2000)."""
        needle = self.find_edit.text()
        if not needle:
            return []
        text = self.editor.toPlainText()
        if BEYOND_BMP_RE.search(text):  # (Qt counts those characters twice: its own find, then)
            return self._qt_matches()
        pattern = re.escape(needle)
        if self.word_check.isChecked():
            pattern = rf"(?<!\w){pattern}(?!\w)"
        flags = 0 if self.case_check.isChecked() else re.IGNORECASE
        out = []
        for match in re.finditer(pattern, text, flags):
            out.append(match.span())
            if len(out) >= MAX_FIND_HIGHLIGHTS:
                break
        return out

    def _qt_matches(self) -> list[tuple[int, int]]:
        doc = self.editor.document()
        out = []
        found = doc.find(self._needle(), 0, self._flags())
        while not found.isNull() and len(out) < MAX_FIND_HIGHLIGHTS:
            out.append((found.selectionStart(), found.selectionEnd()))
            found = doc.find(self._needle(), found.selectionEnd(), self._flags())
        return out

    def _update_highlights_if_visible(self):
        if self.isVisible():
            self._update_highlights()

    def _update_highlights(self):
        matches = self._all_matches()
        self.editor.set_find_selections(matches)
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
        index = next((i for i, (start, _end) in enumerate(matches) if start == pos), None)
        more = "+" if len(matches) >= MAX_FIND_HIGHLIGHTS else ""
        self.count_label.setText(f"{index + 1} of {len(matches)}{more}" if index is not None
                                 else f"{len(matches)}{more} matches")
