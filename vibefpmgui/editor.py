"""A small built-in code editor: line numbers, syntax highlighting (Pygments,
chosen by file extension), find/replace, go to line, and compiler warning /
error markers in the gutter, fortitude lint findings (see :mod:`.lint`), and
for Fortran files the language server's hover, go to definition and outline
(see :mod:`.lsp`).

:class:`EditorTab` wraps a :class:`CodeEditor` with a find bar and a status
line, and takes care of loading and saving the file (keeping its encoding and
line endings) and of noticing when it changes on disk.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from pathlib import Path

from qtpy.QtCore import (
    QFileSystemWatcher,
    QPoint,
    QProcess,
    QRect,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from qtpy.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QFontDatabase,
    QKeySequence,
    QPainter,
    QPalette,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
)
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import lint
from . import lsp as lsp_mod
from .completion import CompletionPopup, SignaturePopup, global_rect
from .highlighter import (
    DARK_STYLE,
    LIGHT_STYLE,
    PygmentsHighlighter,
    is_dark,
    lexer_for_path,
)

INDENT = "    "
MAX_FIND_HIGHLIGHTS = 2000
MARKER_COLORS = {"error": "#e03131", "fatal error": "#e03131", "warning": "#f08c00",
                 "review": "#7048e8", "remark": "#1c7ed6",
                 "lint error": "#e03131", "lint": "#0ca678"}
# Which marker colour wins when a line has several kinds of note.
MARKER_PRIORITY = ["fatal error", "error", "lint error", "warning", "review", "lint", "remark"]
LINT_DELAY_MS = 600  # lint once typing pauses this long
LSP_SYNC_MS = 300    # send edits to the language server once typing pauses this long
COMPLETE_MS = 120    # ask for completions once typing pauses this long
AUTO_COMPLETE_MIN = 2  # characters of a name typed before completions appear by themselves
WORD_RE = re.compile(r"[A-Za-z0-9_]")


def worst_severity(notes: list[tuple[str, str]]) -> str:
    return min((s for s, _ in notes),
               key=lambda s: MARKER_PRIORITY.index(s) if s in MARKER_PRIORITY else len(MARKER_PRIORITY))


class HoverPopup(QFrame):
    """A tooltip-like popup for hover information that can scroll: fortls's
    answer for a long declaration or doc comment can be taller than the
    screen, and QToolTip can't scroll. It stays open while the mouse is on
    the name or inside the popup (so it can be scrolled), and closes when
    the mouse leaves both, on a key press, or when the editor scrolls."""

    MAX_WIDTH, MAX_HEIGHT = 720, 420  # also at most ~60% / 45% of the screen

    def __init__(self, editor: CodeEditor):
        super().__init__(editor, Qt.ToolTip | Qt.FramelessWindowHint)
        self.editor = editor
        self.anchor = None  # what the popup is about (see CodeEditor.hover_key)
        self.setFrameShape(QFrame.Box)
        self.browser = QTextBrowser()
        self.browser.setFrameShape(QFrame.NoFrame)
        self.browser.setOpenLinks(False)
        self.browser.setFont(QToolTip.font())
        self.browser.document().setDocumentMargin(6)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.addWidget(self.browser)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(300)  # time to move the mouse into the popup
        self._hide_timer.timeout.connect(self._hide_unless_hovered)

    def show_at(self, global_pos: QPoint, text_html: str, anchor):
        self.anchor = anchor
        self._hide_timer.stop()
        # The editor's colours (Qt's tooltip colours don't follow dark mode on macOS).
        pal = self.editor.palette()
        base, text = pal.color(QPalette.Base), pal.color(QPalette.Text)
        bg = base.lighter(115) if base.lightness() < 128 else base.darker(103)
        border = QColor(text.red(), text.green(), text.blue(), 90).name(QColor.HexArgb)
        self.setStyleSheet(f"HoverPopup {{ background: {bg.name()}; border: 1px solid {border}; }}"
                           f"QTextBrowser {{ background: {bg.name()}; color: {text.name()}; }}")
        self.browser.setHtml(text_html)
        screen = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        max_w = min(self.MAX_WIDTH, int(screen.width() * 0.6))
        max_h = min(self.MAX_HEIGHT, int(screen.height() * 0.45))
        doc = self.browser.document()
        doc.setTextWidth(-1)
        bar = self.browser.verticalScrollBar().sizeHint().width()
        width = min(int(doc.idealWidth()) + 4, max_w)
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 4
        if height > max_h:  # it will scroll: leave room for the scroll bar
            width = min(width + bar, max_w)
            height = max_h
        self.resize(width + 2, height + 2)
        # Below the mouse; above it if there's no room; always on screen.
        x = min(max(global_pos.x(), screen.left()), screen.right() - self.width())
        y = global_pos.y() + 18
        if y + self.height() > screen.bottom():
            y = max(screen.top(), global_pos.y() - 8 - self.height())
        self.move(x, y)
        self.browser.verticalScrollBar().setValue(0)
        self.show()

    def schedule_hide(self):
        if self.isVisible() and not self._hide_timer.isActive():
            self._hide_timer.start()

    def keep(self):
        self._hide_timer.stop()

    def _hide_unless_hovered(self):
        if not self.geometry().contains(QCursor.pos()):
            self.hide()

    def leaveEvent(self, event):
        self.schedule_hide()
        super().leaveEvent(event)

    def enterEvent(self, event):
        self.keep()
        super().enterEvent(event)


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

    definition_requested = Signal()  # ⌘/Ctrl-click on a name (or the right-click menu)
    references_requested = Signal()  # right-click menu → Find References
    rename_requested = Signal()      # right-click menu → Rename Symbol…
    typed = Signal(str)                # a key that changed the text (after it did; "\b" = Backspace)
    completion_chosen = Signal(object)  # Enter/Tab in the completion list

    def __init__(self, parent=None):
        super().__init__(parent)
        # Set while a language server is attached: called with (line, column,
        # global position, lint tooltip HTML) when the mouse rests on a name.
        self.hover_provider: Callable | None = None
        self._hover_word: tuple[int, int] | None = None
        self.hover_popup = HoverPopup(self)
        self.completion_popup = CompletionPopup(self)
        self.signature_popup = SignaturePopup(self)
        self.viewport().setMouseTracking(True)
        for bar in (self.verticalScrollBar(), self.horizontalScrollBar()):
            bar.valueChanged.connect(self.hide_popups)
        self.markers: dict[int, list[tuple[str, str]]] = {}  # line -> [(severity, message)]
        self.find_selections: list[QTextEdit.ExtraSelection] = []
        self.lint_issues: list[lint.LintIssue] = []  # underlined, with a tooltip
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
                    worst = worst_severity(notes)
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

    def set_lint(self, issues: list[lint.LintIssue]):
        self.lint_issues = issues
        self._update_extra_selections()

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
        i = cursor.positionInBlock()
        if not (i < len(text) and WORD_RE.match(text[i])):
            if i > 0 and WORD_RE.match(text[i - 1]):
                i -= 1
            else:
                return None
        start, end = i, i
        while start > 0 and WORD_RE.match(text[start - 1]):
            start -= 1
        while end < len(text) and WORD_RE.match(text[end]):
            end += 1
        word = text[start:end]
        if word[0].isdigit():
            return None  # a number
        return cursor.blockNumber() + 1, start, word

    def hover_key(self, point: QPoint):
        """What the mouse is on, for deciding when the hover popup is stale:
        the name (line, start column), or else the character position."""
        word = self.word_at(point)
        return word[:2] if word else ("at", self.cursorForPosition(point).position())

    def viewportEvent(self, event):
        if event.type() == event.Type.ToolTip:
            if self.hover_popup.isVisible() and self.hover_popup.anchor == self.hover_key(event.pos()):
                return True  # already showing (and maybe scrolled): leave it
            pos = self.cursorForPosition(event.pos()).position()
            notes = [i.describe() for i in self.lint_issues
                     if (span := self._lint_span(i)) and span[0] <= pos <= span[1]]
            lint_html = "".join(f"<p style='margin:0'>{html.escape(n)}</p>" for n in notes)
            word = self.word_at(event.pos()) if self.hover_provider is not None else None
            self._hover_word = word[:2] if word else None
            key = self.hover_key(event.pos())
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

    def wheelEvent(self, event):
        self.hide_popups()
        super().wheelEvent(event)

    def focusOutEvent(self, event):
        if not self.hover_popup.geometry().contains(QCursor.pos()):
            self.hover_popup.hide()
        if not self.completion_popup.geometry().contains(QCursor.pos()):
            self.completion_popup.hide()  # (unless it's being double-clicked)
        self.signature_popup.hide()
        super().focusOutEvent(event)

    def hideEvent(self, event):
        self.hide_popups()
        super().hideEvent(event)

    def mousePressEvent(self, event):
        if (event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier
                and self.hover_provider is not None):
            # ⌘-click (Ctrl-click on Linux/Windows): go to the definition.
            self.setTextCursor(self.cursorForPosition(event.position().toPoint()))
            self.definition_requested.emit()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        """The usual Cut/Copy/Paste menu, plus the language server's commands
        for the name that was right-clicked (when one is attached)."""
        menu = self.createStandardContextMenu(event.pos())
        if self.hover_provider is not None:
            on_name = False
            if event.reason() == event.Reason.Mouse:
                point = event.pos()  # (already in viewport coordinates)
                on_name = self.word_at(point) is not None
                if on_name:  # the commands act on the name at the cursor
                    cursor = self.cursorForPosition(point)
                    if not (self.textCursor().selectionStart() <= cursor.position()
                            <= self.textCursor().selectionEnd() and self.textCursor().hasSelection()):
                        self.setTextCursor(cursor)
            else:  # opened from the keyboard: the name at the cursor
                rect = self.cursorRect()
                on_name = self.word_at(rect.center()) is not None
            first = menu.actions()[0] if menu.actions() else None
            for text, keys, signal in (("Go to Definition", "F12", self.definition_requested),
                                       ("Find References", "Shift+F12", self.references_requested),
                                       ("Rename Symbol…", "F2", self.rename_requested)):
                action = QAction(text, menu)
                action.setShortcut(QKeySequence(keys))  # shown in the menu; the Edit menu's work
                action.setShortcutContext(Qt.WidgetShortcut)
                action.setEnabled(on_name)
                action.triggered.connect(signal.emit)
                menu.insertAction(first, action)
            menu.insertSeparator(first)
        menu.exec(event.globalPos())
        menu.deleteLater()

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self.hover_popup.isVisible():
            if self.hover_key(event.position().toPoint()) == self.hover_popup.anchor:
                self.hover_popup.keep()
            else:
                self.hover_popup.schedule_hide()
        if self.hover_provider is not None and event.modifiers() & Qt.ControlModifier \
                and self.word_at(event.position().toPoint()):
            self.viewport().setCursor(Qt.PointingHandCursor)
        else:
            self.viewport().setCursor(Qt.IBeamCursor)

    def go_to_position(self, line: int, column: int = 0):
        """Put the cursor at a line (1-based) and column (0-based)."""
        self.go_to_line(line)
        cursor = self.textCursor()
        cursor.setPosition(cursor.block().position() + min(column, max(0, cursor.block().length() - 1)))
        self.setTextCursor(cursor)
        self.centerCursor()

    # -------------------------------------------------------- highlights
    def _update_extra_selections(self):
        """Current line, lines with errors/warnings, and find matches."""
        selections = []
        for line, notes in self.markers.items():
            block = self.document().findBlockByNumber(line - 1)
            if not block.isValid():
                continue
            worst = worst_severity(notes)
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
        for issue in self.lint_issues:
            span = self._lint_span(issue)
            if span is None:
                continue
            sel = QTextEdit.ExtraSelection()
            sel.format.setUnderlineStyle(QTextCharFormat.WaveUnderline)
            sel.format.setUnderlineColor(QColor(MARKER_COLORS[issue.severity]))
            sel.cursor = QTextCursor(self.document())
            sel.cursor.setPosition(span[0])
            sel.cursor.setPosition(span[1], QTextCursor.KeepAnchor)
            selections.append(sel)
        self.setExtraSelections(selections + self.find_selections)

    def set_find_selections(self, selections):
        self.find_selections = selections
        self._update_extra_selections()

    # ------------------------------------------------------------ editing
    def keyPressEvent(self, event):
        self.hover_popup.hide()
        key = event.key()
        popup = self.completion_popup
        plain = not event.modifiers() & (Qt.ShiftModifier | Qt.ControlModifier | Qt.AltModifier | Qt.MetaModifier)
        if popup.isVisible():
            steps = {Qt.Key_Up: -1, Qt.Key_Down: 1,
                     Qt.Key_PageUp: -popup.page_rows(), Qt.Key_PageDown: popup.page_rows()}
            if key in steps and plain:
                popup.move_selection(steps[key])
                return
            if key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab) and plain:
                self.completion_chosen.emit(popup.current())
                return
            if key == Qt.Key_Escape:
                popup.hide()
                return
        if key == Qt.Key_Escape and self.signature_popup.isVisible():
            self.signature_popup.hide()
            return
        before = self.document().revision()
        self._edit_key(event)
        if self.document().revision() != before:
            self.typed.emit("\b" if key == Qt.Key_Backspace else event.text())

    def _edit_key(self, event):
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

    lint_enabled = True  # lint Fortran files with fortitude (View menu)

    dirty_changed = Signal(bool)
    saved = Signal(str)
    path_changed = Signal(str)
    navigate_requested = Signal(str, int, int)  # go to file, line (1-based), column (0-based)
    references_found = Signal(str, object)  # name, [(file, line, start column, end column)]
    rename_ready = Signal(str, str, object)  # old name, new name, {file: [lsp.TextEdit]}

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path).resolve()
        self.encoding = "utf-8"
        self.newline = "\n"
        self._disk_text = ""
        self._compiler_markers: dict[int, list[tuple[str, str]]] = {}
        self._review_markers: dict[int, list[tuple[str, str]]] = {}
        self._lint_markers: dict[int, list[tuple[str, str]]] = {}
        self._lint_process: QProcess | None = None
        self._lint_again = False  # the text changed while fortitude was running
        self.lsp: lsp_mod.LspClient | None = None  # the language server, for Fortran files
        self._outline: list[tuple[int, str, str, int, int]] = []
        self.editor = CodeEditor()
        self.highlighter = PygmentsHighlighter(self.editor.document(), lexer_for_path(self.path))
        self.find_bar = FindBar(self.editor)
        self.status = QLabel()
        self.status.setContentsMargins(8, 2, 8, 2)

        # Outline of the file (modules, types, procedures), from the language server.
        self.outline_box = QComboBox()
        self.outline_box.setMinimumContentsLength(30)
        self.outline_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.outline_box.setToolTip("Outline: jump to a module, type or procedure in this file")
        self.outline_box.activated.connect(self._outline_chosen)
        self.outline_bar = QWidget()
        bar = QHBoxLayout(self.outline_bar)
        bar.setContentsMargins(6, 3, 6, 3)
        bar.addWidget(self.outline_box)
        bar.addStretch(1)
        self.outline_bar.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.outline_bar)
        layout.addWidget(self.editor, 1)
        layout.addWidget(self.find_bar)
        layout.addWidget(self.status)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self._file_changed)
        self._check_timer = QTimer(self)  # coalesce bursts of change events
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(200)
        self._check_timer.timeout.connect(self._check_disk)

        self._lint_timer = QTimer(self)
        self._lint_timer.setSingleShot(True)
        self._lint_timer.setInterval(LINT_DELAY_MS)
        self._lint_timer.timeout.connect(self.lint)
        self._lsp_timer = QTimer(self)
        self._lsp_timer.setSingleShot(True)
        self._lsp_timer.setInterval(LSP_SYNC_MS)
        self._lsp_timer.timeout.connect(self._lsp_sync)
        self.editor.definition_requested.connect(self.go_to_definition)
        self.editor.references_requested.connect(self.find_references)
        self.editor.rename_requested.connect(self.rename_symbol)
        self._complete_timer = QTimer(self)
        self._complete_timer.setSingleShot(True)
        self._complete_timer.setInterval(COMPLETE_MS)
        self._complete_timer.timeout.connect(lambda: self.request_completion(manual=False))
        self._signature_timer = QTimer(self)
        self._signature_timer.setSingleShot(True)
        self._signature_timer.setInterval(COMPLETE_MS)
        self._signature_timer.timeout.connect(self.request_signature)
        self.editor.typed.connect(self._typed)
        self.editor.completion_chosen.connect(self.accept_completion)
        self.editor.completion_popup.accepted.connect(self.accept_completion)
        self.editor.cursorPositionChanged.connect(self._cursor_moved_popups)

        self.editor.document().modificationChanged.connect(self.dirty_changed)
        self.editor.cursorPositionChanged.connect(self._update_status)
        self.load()
        self.refresh_style()
        self.editor.textChanged.connect(self._lint_timer.start)
        self.editor.textChanged.connect(lambda: self.lsp is not None and self._lsp_timer.start())
        self.editor.cursorPositionChanged.connect(self._outline_follow_cursor)
        self.lint()

    # ------------------------------------------------------ language server
    def attach_lsp(self, client: lsp_mod.LspClient | None):
        """Use a language server for this tab (Fortran files only; None detaches)."""
        if client is self.lsp:
            return
        self.detach_lsp()
        if client is None or not client.is_running() or not lsp_mod.is_fortran(self.path):
            return
        self.lsp = client
        client.open_document(self.path, self.editor.toPlainText())
        self.editor.hover_provider = self._request_hover
        self.outline_bar.show()
        self.refresh_outline()

    def detach_lsp(self):
        self._lsp_timer.stop()
        self._complete_timer.stop()
        self._signature_timer.stop()
        self.editor.hide_popups()
        if self.lsp is not None:
            self.lsp.close_document(self.path)
        self.lsp = None
        self.editor.hover_provider = None
        self._outline = []
        self.outline_box.clear()
        self.outline_bar.hide()

    def _lsp_sync(self):
        """Send the current text (once typing pauses), then refresh the outline."""
        self._lsp_timer.stop()
        if self.lsp is not None:
            self.lsp.change_document(self.path, self.editor.toPlainText())
            self.refresh_outline()

    def _flush_lsp(self):
        """Before a question: make sure the server has the latest text."""
        if self._lsp_timer.isActive():
            self._lsp_sync()

    def _request_hover(self, line: int, column: int, global_pos, lint_html: str):
        if self.lsp is None:
            return
        self._flush_lsp()

        def answered(result):
            text = lsp_mod.hover_html(result.get("contents")) if isinstance(result, dict) else ""
            if text:
                sep = "<hr>" if lint_html else ""
                self.editor.show_hover(line, column, global_pos, lint_html + sep + text)

        self.lsp.hover(self.path, line, column, answered)

    def go_to_definition(self):
        """Ask where the name at the cursor is defined, and go there."""
        if self.lsp is None:
            return
        self._flush_lsp()
        word = self._name_at_cursor()
        if word is not None:
            line, column = word[0], word[1]
        else:
            cursor = self.editor.textCursor()
            line, column = cursor.blockNumber() + 1, cursor.positionInBlock()

        def answered(result):
            found = lsp_mod.first_location(result)
            if found is None:
                name = f"'{word[2]}'" if word else "the name at the cursor"
                self._update_status(f"no definition found for {name}")
                return
            path, to_line, to_column = found
            self.navigate_requested.emit(str(path), to_line, to_column)

        self.lsp.definition(self.path, line, column, answered)

    def refresh_outline(self):
        if self.lsp is None:
            return
        self.lsp.document_symbols(self.path, self._show_outline)

    def _show_outline(self, result):
        if self.lsp is None:
            return
        document = self.editor.document()
        self._outline = lsp_mod.outline(result, lambda n: document.findBlockByNumber(n - 1).text())
        self.outline_box.blockSignals(True)
        self.outline_box.clear()
        for depth, kind, name, _first, _last in self._outline:
            self.outline_box.addItem("    " * depth + f"{name}  ({kind})")
        self.outline_box.setPlaceholderText(f"Outline ({len(self._outline)})" if self._outline
                                            else "Outline (no procedures)")
        self.outline_box.blockSignals(False)
        self._outline_follow_cursor()

    def _outline_chosen(self, index: int):
        if 0 <= index < len(self._outline):
            self.editor.go_to_line(self._outline[index][3])

    def _outline_follow_cursor(self):
        """Show the innermost module/procedure the cursor is in."""
        if not self._outline:
            return
        line = self.editor.textCursor().blockNumber() + 1
        best = -1
        for i, (_depth, _kind, _name, first, last) in enumerate(self._outline):
            if first <= line <= last:
                best = i  # rows are in file order, so later = deeper
        if best != self.outline_box.currentIndex():
            self.outline_box.blockSignals(True)
            self.outline_box.setCurrentIndex(best)
            self.outline_box.blockSignals(False)

    # ------------------------------------------------- completion / hints
    def _typed(self, text: str):
        """After a key changed the text: offer completions or argument hints."""
        if self.lsp is None or not text:
            return
        ch = text[-1]
        if WORD_RE.match(ch):
            if not self.editor.completion_popup.isVisible():
                self._complete_timer.start()  # request_completion checks the length
        elif ch == "%":  # a type's components
            self._complete_timer.start()
        elif ch in "(," or ch == ")" and self.editor.signature_popup.isVisible():
            self._signature_timer.start()

    def _name_start(self) -> tuple[int, int, str, str]:
        """(block number, column where the name before the cursor starts,
        that partial name, the line's text) for the cursor's position."""
        cursor = self.editor.textCursor()
        text = cursor.block().text()
        column = cursor.positionInBlock()
        start = column
        while start > 0 and WORD_RE.match(text[start - 1]):
            start -= 1
        return cursor.blockNumber(), start, text[start:column], text

    def request_completion(self, manual: bool = True):
        """Ask for completions of the name being typed (Show Completions
        when ``manual``, or by itself once a few letters are typed)."""
        if self.lsp is None:
            return
        block, start, prefix, text = self._name_start()
        column = self.editor.textCursor().positionInBlock()
        after_percent = start > 0 and text[start - 1] == "%"
        if not manual:
            if lsp_mod.in_comment_or_string(text, column) or prefix[:1].isdigit():
                return
            if len(prefix) < AUTO_COMPLETE_MIN and not after_percent:
                return
        self._flush_lsp()

        def answered(result):
            cursor = self.editor.textCursor()
            if cursor.blockNumber() != block or cursor.positionInBlock() < start:
                return  # the cursor has moved on
            now = cursor.block().text()[start:cursor.positionInBlock()]
            if not all(WORD_RE.match(c) for c in now):
                return
            anchor_cursor = QTextCursor(cursor.block())
            anchor_cursor.setPosition(cursor.block().position() + start)
            items = lsp_mod.completions(result)
            shown = self.editor.completion_popup.open(items, (block, start), now,
                                                      global_rect(self.editor, anchor_cursor))
            if manual and not shown:
                self._update_status("no completions here")

        self.lsp.completion(self.path, block + 1, column, answered)

    def accept_completion(self, item):
        popup = self.editor.completion_popup
        start = popup.start
        popup.hide()
        if item is None or start is None:
            return
        cursor = self.editor.textCursor()
        if cursor.blockNumber() != start[0]:
            return
        block = cursor.block()
        cursor.setPosition(block.position() + start[1])
        cursor.setPosition(self.editor.textCursor().position(), QTextCursor.KeepAnchor)
        cursor.insertText(item.insert)
        self.editor.setTextCursor(cursor)

    def request_signature(self):
        """Show (or update, or hide) the argument hints for the call the
        cursor is in."""
        popup = self.editor.signature_popup
        if self.lsp is None:
            return
        cursor = self.editor.textCursor()
        block, column = cursor.blockNumber(), cursor.positionInBlock()
        before = cursor.block().text()[:column]
        index = lsp_mod.argument_index(before)
        if index is None or lsp_mod.in_comment_or_string(cursor.block().text(), column):
            popup.hide()
            return
        self._flush_lsp()

        def answered(result):
            now = self.editor.textCursor()
            if now.blockNumber() != block:
                return
            text = lsp_mod.signature_html(result, index)
            if not text:
                popup.hide()
                return
            line_start = QTextCursor(now.block())
            rect = global_rect(self.editor, now)
            rect.moveLeft(global_rect(self.editor, line_start).left() + self.editor.fontMetrics()
                          .horizontalAdvance(" ") * (len(before) - len(before.lstrip())))
            popup.open(text, block, rect)

        self.lsp.signature_help(self.path, block + 1, column, answered)

    def _cursor_moved_popups(self):
        """Keep the completion list filtered to what's typed (or close it),
        and the argument hints up to date."""
        cursor = self.editor.textCursor()
        block, column = cursor.blockNumber(), cursor.positionInBlock()
        popup = self.editor.completion_popup
        if popup.isVisible():
            start = popup.start
            typed = cursor.block().text()[start[1]:column] if start and start[0] == block else None
            if typed is None or column < start[1] or not all(WORD_RE.match(c) for c in typed):
                popup.hide()
            else:
                popup.filter(typed)
        hint = self.editor.signature_popup
        if hint.isVisible():
            if hint.line != block:
                hint.hide()
            else:
                self._signature_timer.start()

    # ------------------------------------------------- references / rename
    def _name_at_cursor(self) -> tuple[int, int, str] | None:
        """(1-based line, start column, name) of the name at the cursor, or
        of the selected text if that is a single name."""
        cursor = self.editor.textCursor()
        if cursor.hasSelection():
            selected = cursor.selectedText()
            if re.fullmatch(r"[A-Za-z_]\w*", selected):
                start = QTextCursor(self.editor.document())
                start.setPosition(cursor.selectionStart())
                return start.blockNumber() + 1, start.positionInBlock(), selected
        text = cursor.block().text()
        i = cursor.positionInBlock()
        if not (i < len(text) and WORD_RE.match(text[i])):
            if i > 0 and WORD_RE.match(text[i - 1]):
                i -= 1
            else:
                return None
        start, end = i, i
        while start > 0 and WORD_RE.match(text[start - 1]):
            start -= 1
        while end < len(text) and WORD_RE.match(text[end]):
            end += 1
        word = text[start:end]
        return None if word[0].isdigit() else (cursor.blockNumber() + 1, start, word)

    def find_references(self):
        if self.lsp is None:
            return
        found = self._name_at_cursor()
        if found is None:
            self._update_status("put the cursor on a name to find its references")
            return
        line, column, name = found
        self._flush_lsp()
        self.lsp.references(self.path, line, column,
                            lambda result: self.references_found.emit(name, lsp_mod.locations(result)))

    def rename_symbol(self):
        if self.lsp is None:
            return
        found = self._name_at_cursor()
        if found is None:
            self._update_status("put the cursor on a name to rename it")
            return
        line, column, name = found
        new, ok = QInputDialog.getText(self, "Rename Symbol", f"Rename '{name}' everywhere it is used, to:",
                                       QLineEdit.Normal, name)
        new = new.strip()
        if not ok or not new or new == name:
            return
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", new):
            QMessageBox.warning(self, "Rename Symbol", f"'{new}' isn't a valid Fortran name.")
            return
        self._flush_lsp()

        def answered(result):
            edits = lsp_mod.workspace_edits(result)
            if not edits:
                self._update_status(f"'{name}' can't be renamed (fortls found nothing to change)")
                return
            self.rename_ready.emit(name, new, edits)

        self.lsp.rename(self.path, line, column, new, answered)

    def apply_text_edits(self, edits):
        """Apply language-server edits to the text as one Undo step."""
        document = self.editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        for e in sorted(edits, key=lambda e: (e.line, e.column), reverse=True):
            start, end = document.findBlockByNumber(e.line), document.findBlockByNumber(e.end_line)
            if not start.isValid() or not end.isValid():
                continue
            cursor.setPosition(start.position() + min(e.column, start.length() - 1))
            cursor.setPosition(end.position() + min(e.end_column, end.length() - 1), QTextCursor.KeepAnchor)
            cursor.insertText(e.new_text)
        cursor.endEditBlock()

    def close_down(self):
        """Before the tab is closed: stop background work."""
        self.stop_lint()
        self.detach_lsp()

    # -------------------------------------------------------------- lint
    def can_lint(self) -> bool:
        return self.lint_enabled and lint.can_lint(self.path) and lint.executable() is not None

    def set_lint_enabled(self, on: bool):
        if on:
            self.lint()
        else:
            self._lint_timer.stop()
            self._show_lint([])

    def lint(self):
        """Run fortitude on the current text (in the background)."""
        if not self.can_lint():
            self._show_lint([])
            return
        if self._lint_process is not None:
            self._lint_again = True
            return
        self._lint_again = False
        process = QProcess(self)
        process.setWorkingDirectory(str(self.path.parent))
        process.finished.connect(lambda *_: self._lint_finished(process))
        process.errorOccurred.connect(lambda err: err == QProcess.FailedToStart and
                                      self._lint_finished(process))
        self._lint_process = process
        process.start(lint.executable(), lint.command(self.path))
        process.write(self.editor.toPlainText().encode("utf-8"))
        process.closeWriteChannel()

    def _lint_finished(self, process: QProcess):
        if process is not self._lint_process:
            return
        self._lint_process = None
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        process.deleteLater()
        if self.can_lint():
            self._show_lint(lint.parse(output))
        if self._lint_again:
            self._lint_timer.start()

    def stop_lint(self):
        """Before the tab is closed: don't leave fortitude running."""
        self._lint_timer.stop()
        if self._lint_process is not None:
            process, self._lint_process = self._lint_process, None
            process.kill()
            process.waitForFinished(1000)

    def _show_lint(self, issues: list[lint.LintIssue]):
        markers: dict[int, list[tuple[str, str]]] = {}
        for issue in issues:
            markers.setdefault(issue.line, []).append((issue.severity, issue.describe()))
        self._lint_markers = markers
        self.editor.set_lint(issues)
        self._apply_markers()

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
        # Line numbers in the compiler/review notes may no longer be right.
        self._compiler_markers, self._review_markers = {}, {}
        self.editor.set_markers({})
        self._watch()
        if self.lsp is not None:
            self._flush_lsp()
            self.lsp.save_document(self.path)
        self.saved.emit(str(self.path))
        return True

    def save_as(self) -> bool:
        filename, _ = QFileDialog.getSaveFileName(self, "Save As", str(self.path))
        if not filename:
            return False
        client = self.lsp
        self.detach_lsp()  # (under the old name)
        self.path = Path(filename).resolve()
        self.highlighter.set_lexer(lexer_for_path(self.path))
        self.path_changed.emit(str(self.path))
        saved = self.save()
        self.lint()  # it may (no longer) be a Fortran file
        self.attach_lsp(client)
        return saved

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
        if self._compiler_markers:
            notes = [s for ns in self._compiler_markers.values() for s, _ in ns]
            errors = sum(1 for s in notes if s in ("error", "fatal error"))
            warnings = sum(1 for s in notes if s == "warning")
            parts.append(f"{errors} errors, {warnings} warnings from the last build")
        if self._review_markers:
            count = sum(len(ns) for ns in self._review_markers.values())
            parts.append(f"{count} AI review note{'s' * (count != 1)}")
        if self._lint_markers:
            count = sum(len(ns) for ns in self._lint_markers.values())
            parts.append(f"{count} fortitude issue{'s' * (count != 1)}")
        if note:
            parts.append(note)
        self.status.setText("<span style='color:gray'>" + " · ".join(parts) + "</span>")

    def show_diagnostics(self, diagnostics):
        """Mark compiler messages (objects with .line, .severity, .message)."""
        markers: dict[int, list[tuple[str, str]]] = {}
        for d in diagnostics:
            markers.setdefault(d.line, []).append((d.severity, d.message))
        self._compiler_markers = markers
        self._apply_markers()

    def show_review(self, findings):
        """Mark AI review findings (objects with .line, .category, .comment)."""
        markers: dict[int, list[tuple[str, str]]] = {}
        for f in findings:
            markers.setdefault(f.line, []).append(("review", f"AI ({f.category}): {f.comment}"))
        self._review_markers = markers
        self._apply_markers()

    def clear_review(self):
        self._review_markers = {}
        self._apply_markers()

    def _apply_markers(self):
        merged: dict[int, list[tuple[str, str]]] = {}
        for source in (self._compiler_markers, self._review_markers, self._lint_markers):
            for line, notes in source.items():
                merged.setdefault(line, []).extend(notes)
        self.editor.set_markers(merged)
        self._update_status()

    def replace_text(self, text: str):
        """Replace the whole text as one undoable edit, keeping the cursor
        on the same line and column and the view where it was."""
        cursor = self.editor.textCursor()
        line, column = cursor.blockNumber(), cursor.positionInBlock()
        scroll = self.editor.verticalScrollBar().value()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.Document)
        cursor.insertText(text)
        cursor.endEditBlock()
        block = self.editor.document().findBlockByNumber(min(line, self.editor.blockCount() - 1))
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + min(column, max(0, block.length() - 1)))
        self.editor.setTextCursor(cursor)
        self.editor.verticalScrollBar().setValue(scroll)

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

