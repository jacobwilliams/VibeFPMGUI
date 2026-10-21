"""The text editor widget (`CodeEditor`): keys, mouse, selections, indentation, highlights.

Its gutter, issue squiggles and multiple cursors are mixins:
`vibefpmgui.editor_gutter`, `vibefpmgui.editor_issues`, `vibefpmgui.multi_cursor`.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections.abc import Callable

from qtpy.QtCore import QPoint, QRect, Qt, QTimer, Signal
from qtpy.QtGui import (
    QAction,
    QColor,
    QCursor,
    QKeySequence,
    QPainter,
    QPalette,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
    QTextOption,
)
from qtpy.QtWidgets import QMenu, QPlainTextEdit, QTextEdit

from . import gremlins as gremlins_mod
from . import lint
from .completion import CompletionPopup, SignaturePopup
from .editor_gutter import EditorGutterMixin, LineNumberArea
from .editor_issues import EditorIssuesMixin
from .editor_shared import (
    COVERAGE_COLORS,
    EXECUTION_COLOR,
    MARKER_COLORS,
    MODIFIER_KEYS,
    backspace_length,
    occurrence_color,
    outdent_length,
    tab_text,
    worst_severity,
)
from .highlighter import document_text
from .hover_popup import HoverPopup
from .line_edits import LineEditsMixin
from .multi_cursor import MultiCursorMixin

FIND_COLOR = QColor(252, 196, 25, 110)  # the find bar's matches

class CodeEditor(EditorGutterMixin, EditorIssuesMixin, MultiCursorMixin, LineEditsMixin, QPlainTextEdit):
    """Plain-text editor with a line-number gutter, current-line highlight,
    auto-indent and soft tabs."""

    definition_requested = Signal()  # ⌘/Ctrl-click on a name (or the right-click menu)
    implementation_requested = Signal()  # right-click menu → Go to Implementation
    references_requested = Signal()  # right-click menu → Find References
    rename_requested = Signal()      # right-click menu → Rename Symbol…
    lint_fix_requested = Signal(object)  # a lint.LintIssue whose fix to apply (hover link or menu)
    word_added = Signal(str)  # "Add to Dictionary" (hover link or menu)
    explain_requested = Signal(int)  # right-click → Explain Error with AI, on this line (1-based)
    edit_selection_requested = Signal()  # right-click → Edit Selection with AI
    typed = Signal(str)                # a key that changed the text (after it did; "\b" = Backspace)
    completion_chosen = Signal(object)  # Enter/Tab in the completion list
    cursors_changed = Signal()          # extra cursors added or removed (multiple cursors)
    breakpoints_changed = Signal()      # a breakpoint added or removed (a click in the gutter, F9)

    indent_width = 4   # View → Indentation (set by the window)
    use_tabs = False
    highlight_matches = True  # View → Highlight Other Occurrences of the Selection
    show_coverage = True      # View → Show Coverage
    MAX_MATCHES = 2000        # (highlighted at most)

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
        self.change_marks: dict[int, str] = {}                  # line -> "unsaved" / "saved"
        self.deletions: dict[int, tuple[str, int]] = {}          # line -> (kind, lines deleted above it)
        # Breakpoints (see editor_gutter): cursors at their lines' starts, so they move with edits.
        self.debuggable = False  # a source file the debugger can stop in: a breakpoint column
        self._breakpoints: list[tuple[QTextCursor, bool]] = []  # (at its line, enabled)
        self.breakpoint_problems: dict[int, str] = {}  # line -> why the debugger couldn't place it
        self.execution_line: int | None = None  # where the program is stopped (an arrow, the line tinted)
        self.commit_lines: tuple[set[int], QColor] | None = None  # a git blame card hovered: its lines, its color
        self.minimap = None  # the vibefpmgui.minimap.Minimap beside it, if any
        self.find_selections: list[tuple[int, int]] = []  # the find bar's matches: (start, end) positions
        self.coverage: dict[int, int] = {}  # line -> times the tests ran it (see set_coverage)
        self.coverage_branches: dict[int, tuple[int, int]] = {}  # line -> (branches taken, branches)
        # The selection's other occurrences, highlighted (see update_occurrences):
        # Match case / Whole words come from the tab's find bar.
        self.occurrence_selections: list[tuple[int, int]] = []  # (start, end) positions
        self._occurrence_color = QColor()
        self.match_options: Callable[[], tuple[bool, bool]] = lambda: (False, False)
        self._occurrence_timer = QTimer(self)
        self._occurrence_timer.setSingleShot(True)
        self._occurrence_timer.setInterval(120)  # (once the selection rests: not on every step of a drag)
        self._occurrence_timer.timeout.connect(self.update_occurrences)
        self.selectionChanged.connect(self._occurrence_timer.start)
        self.lint_issues: list[lint.LintIssue] = []  # underlined, with a tooltip
        self._spelling: list = []        # the misspelled words among them, in line order (see set_lint)
        self._spelling_lines: list[int] = []
        # The highlights that only change when the markers, issues or gremlins
        # do, for the lines around the view (see update_extra_selections).
        self._fixed_selections: list[QTextEdit.ExtraSelection] = []
        self._fixed_key: tuple | None = None   # (what they were made for: the data's version, the lines)
        self._highlights_version = 0
        self._issue_lines: dict[int, list] = {}  # line -> its underlined issues (not spelling)
        self.gremlins: dict[int, list[gremlins_mod.Gremlin]] = {}  # line -> its gremlins (View → Show Gremlins)
        # Multiple cursors: ⌥/Alt-click adds one, ⌘⇧/Ctrl+Shift-click a column
        # of them. Typing, deleting and moving happen at all of them; these
        # QTextCursors stay in place as the text changes.
        self.extra_cursors: list[QTextCursor] = []
        # With several cursors they're all drawn here (Qt's own is hidden), so
        # they look alike and blink together, at the system's rate.
        self._caret_width = self.cursorWidth()  # Qt's (restored with one cursor)
        self._caret_on = True
        self._caret_timer = QTimer(self)
        self._caret_timer.timeout.connect(self._blink)
        self.cursorPositionChanged.connect(self._restart_blink)
        self.textChanged.connect(self._restart_blink)
        self.gutter = LineNumberArea(self)
        self.setLineWrapMode(QPlainTextEdit.NoWrap)
        self.blockCountChanged.connect(self.update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(lambda: self.update_extra_selections(rebuild=False))
        self.verticalScrollBar().valueChanged.connect(self._scrolled)
        self.update_gutter_width()
        self.update_extra_selections()

    def toPlainText(self) -> str:
        """The text exactly as it is (see highlighter.document_text): what's
        saved, linted, searched and sent to the language server."""
        return document_text(self.document())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cr = self.contentsRect()
        self.gutter.setGeometry(QRect(cr.left(), cr.top(), self.gutter_width(), cr.height()))
        if self.lineWrapMode() != QPlainTextEdit.NoWrap:  # (lines re-wrap: highlight their new rows)
            QTimer.singleShot(0, self, self.update_extra_selections)
        else:
            self._scrolled()  # (more or fewer lines on view)

    def set_word_wrap(self, on: bool):
        """Wrap long lines at the window's edge (at a space if there is
        one), or scroll sideways."""
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)
        QTimer.singleShot(0, self, self.update_extra_selections)
        self.gutter.update()
        self._update_minimap()

    def _update_minimap(self):
        if self.minimap is not None:
            self.minimap.update()

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
        if self.extra_cursors:
            self._caret_timer.stop()
            self.viewport().update()  # (no cursors without the focus, like Qt's)

    def hideEvent(self, event):
        self.hide_popups()
        super().hideEvent(event)

    def mousePressEvent(self, event):
        mods = event.modifiers()
        if event.button() == Qt.LeftButton:
            point = event.position().toPoint()
            if mods & Qt.AltModifier and not mods & (Qt.ControlModifier | Qt.ShiftModifier):
                self.toggle_cursor_at(self.cursorForPosition(point).position())  # ⌥/Alt-click
                return
            if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:  # ⌘⇧/Ctrl+Shift-click
                clicked = self.cursorForPosition(point)
                self.add_column_cursors(clicked.blockNumber(), clicked.positionInBlock())
                return
            self.clear_extra_cursors()  # (any other click: back to one cursor)
        if (event.button() == Qt.LeftButton and event.modifiers() & Qt.ControlModifier
                and self.hover_provider is not None):
            # ⌘-click (Ctrl-click on Linux/Windows): go to the definition.
            self.setTextCursor(self.cursorForPosition(event.position().toPoint()))
            self.definition_requested.emit()
            return
        super().mousePressEvent(event)

    def contextMenuEvent(self, event):
        menu = self.context_menu(event.pos(), from_mouse=event.reason() == event.Reason.Mouse)
        menu.exec(event.globalPos())
        menu.deleteLater()

    def context_menu(self, pos: QPoint, from_mouse: bool = True) -> QMenu:
        """The usual Cut/Copy/Paste menu, plus fortitude's fixes for the
        issues at that spot and the language server's commands for the name
        there (when one is attached). ``pos``: where the menu was asked for
        (viewport coordinates); ``from_mouse`` False: from the keyboard, for
        the text cursor's spot."""
        menu = self.createStandardContextMenu(pos)
        # fortitude's fixes for the issues at that spot.
        spot = self.cursorForPosition(pos).position() if from_mouse else self.textCursor().position()
        fixes = [i for i in self.lint_issues if i.fixable
                 and (span := self._lint_span(i)) and span[0] <= spot <= span[1]]
        misspelled = next((i for i in self.lint_issues if i.severity == "spelling"
                           and (span := self._lint_span(i)) and span[0] <= spot <= span[1]), None)
        if misspelled is not None:
            first = menu.actions()[0] if menu.actions() else None
            words = misspelled.suggestions()
            for word in words:
                action = QAction(word, menu)
                font = action.font()
                font.setBold(True)
                action.setFont(font)
                action.triggered.connect(lambda _=False, i=misspelled, w=word: self.replace_word(i, w))
                menu.insertAction(first, action)
            if not words:
                none = QAction("(No Spelling Suggestions)", menu)
                none.setEnabled(False)
                menu.insertAction(first, none)
            add = QAction(f"Add “{misspelled.word}” to Dictionary", menu)
            add.triggered.connect(lambda _=False, w=misspelled.word: self.word_added.emit(w))
            menu.insertAction(first, add)
            menu.insertSeparator(first)
        if self.textCursor().hasSelection():
            first = menu.actions()[0] if menu.actions() else None
            edit = QAction("Edit Selection with AI…", menu)
            edit.setToolTip("Ask the local AI model to change the selected lines; you accept or reject each edit")
            edit.triggered.connect(self.edit_selection_requested.emit)
            menu.insertAction(first, edit)
            menu.insertSeparator(first)
        line = self.document().findBlock(spot).blockNumber() + 1
        if any(severity not in ("review", "remark") for severity, _ in self.markers.get(line, [])):
            first = menu.actions()[0] if menu.actions() else None
            explain = QAction("Explain Error with AI", menu)
            explain.setToolTip("Ask the local AI model what this line's problem means, and how to fix it")
            explain.triggered.connect(lambda _=False, n=line: self.explain_requested.emit(n))
            menu.insertAction(first, explain)
            menu.insertSeparator(first)
        if fixes:
            first = menu.actions()[0] if menu.actions() else None
            for issue in fixes:
                label = f"Fix: {issue.fix or issue.message}" + ("" if issue.fix_safe else "  (unsafe — check it)")
                action = QAction(label, menu)
                action.triggered.connect(lambda _=False, i=issue: self.lint_fix_requested.emit(i))
                menu.insertAction(first, action)
            menu.insertSeparator(first)
        if self.hover_provider is not None:
            on_name = False
            if from_mouse:
                point = pos  # (already in viewport coordinates)
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
                                       ("Go to Implementation", "Ctrl+F12", self.implementation_requested),
                                       ("Find References", "Shift+F12", self.references_requested),
                                       ("Rename Symbol…", "F2", self.rename_requested)):
                action = QAction(text, menu)
                action.setShortcut(QKeySequence(keys))  # shown in the menu; the Edit menu's work
                action.setShortcutContext(Qt.WidgetShortcut)
                action.setEnabled(on_name)
                action.triggered.connect(signal.emit)
                menu.insertAction(first, action)
            menu.insertSeparator(first)
        return menu

    def mouseMoveEvent(self, event):
        super().mouseMoveEvent(event)
        if self.hover_popup.isVisible():
            point = event.position().toPoint()
            anchor = self.hover_popup.anchor
            on_line = (isinstance(anchor, tuple) and anchor[:1] == ("line",)
                       and self.cursorForPosition(point).blockNumber() + 1 == anchor[1])
            if on_line or self.hover_key(point) == anchor:
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
    def update_extra_selections(self, rebuild: bool = True):
        """Current line, lines with errors/warnings, underlined issues,
        gremlins, the extra cursors' selections, the selection's other
        occurrences and find matches.

        Only the lines around the view get highlights (see `_line_window`):
        each is a QTextCursor that Qt moves along at every edit and goes
        through at every cursor move, and a big file's thousands of issues
        made typing, and replacing its text, take seconds. Scrolling makes
        those of the lines coming into view. ``rebuild``: the markers,
        issues or gremlins changed (or the colors, or the line wrapping).

        Not while the text is being replaced (``setPlainText``, as a file
        changed on disk is reloaded): emptying the document can narrow the
        gutter and so scroll, calling this with no valid line anywhere, and a
        cursor made from one crashed Qt. It's called again once the text is in."""
        if not self.textCursor().block().isValid():
            return
        if rebuild:
            self._highlights_version += 1
            self._issue_lines = {}
            for issue in self.lint_issues:
                if issue.severity != "spelling":  # (drawn by paintEvent: see _paint_squiggles)
                    self._issue_lines.setdefault(issue.line, []).append(issue)
        window = self._line_window()
        key = (self._highlights_version, window)
        if key != self._fixed_key:
            self._fixed_selections = self._marker_and_issue_selections(*window)
            self._fixed_key = key
        selections = list(self._fixed_selections)
        if not self.isReadOnly():
            color = QColor(self.palette().color(QPalette.Highlight))
            color.setAlpha(28)
            selections += self._line_highlight(self.textCursor().block(), color)
        if self.execution_line:  # (where the debugger stopped)
            block = self.document().findBlockByNumber(self.execution_line - 1)
            if block.isValid():
                tint = QColor(EXECUTION_COLOR)
                tint.setAlpha(70)
                selections += self._line_highlight(block, tint)
        if self.commit_lines is not None:  # (a git blame card under the mouse: its commit's lines)
            lines, tint = self.commit_lines
            first, last = window
            block = self.document().findBlockByNumber(first)
            while block.isValid() and block.blockNumber() <= last:
                if block.blockNumber() in lines:
                    selections += self._line_highlight(block, tint)
                block = block.next()
        selected = QColor(self.palette().color(QPalette.Highlight))
        for c in self.extra_cursors:
            if c.hasSelection():
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(selected)
                sel.format.setForeground(self.palette().color(QPalette.HighlightedText))
                sel.cursor = QTextCursor(c)
                selections.append(sel)
        selections += self._span_selections(self.occurrence_selections, self._occurrence_color, window)
        selections += self._span_selections(self.find_selections, FIND_COLOR, window)
        self.setExtraSelections(selections)

    def show_commit_lines(self, lines: set[int] | None, color: QColor | None = None):
        """Tint these lines (0-based; None: none) in ``color``: a git blame
        card's commit, while the mouse is on its card."""
        self.commit_lines = (lines, color) if lines else None
        self.update_extra_selections(rebuild=False)

    def _line_window(self) -> tuple[int, int]:
        """The lines (0-based, first and last) to highlight: those on view, and
        a screenful either side, in steps of a screenful (scrolling a line
        doesn't make them all again)."""
        rows = max(8, self.viewport().height() // max(1, self.fontMetrics().lineSpacing()))
        first = self.firstVisibleBlock().blockNumber() // rows * rows
        return max(0, first - rows), first + 3 * rows

    def _scrolled(self, *_):
        if self._fixed_key is not None and self._fixed_key[1] != self._line_window():
            self.update_extra_selections(rebuild=False)

    def _span_selections(self, spans: list[tuple[int, int]], color: QColor, window: tuple[int, int]) -> list:
        """Highlights for the (start, end) positions, in order, that are in the window's lines."""
        if not spans:
            return []
        doc = self.document()
        low = doc.findBlockByNumber(window[0]).position()
        last = doc.findBlockByNumber(window[1])
        high = last.position() + last.length() if last.isValid() else doc.characterCount()
        out = []
        for start, end in spans[bisect_left(spans, (low, 0)):]:
            if start > high:
                break
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(color)
            sel.cursor = QTextCursor(doc)
            sel.cursor.setPosition(start)
            sel.cursor.setPosition(end, QTextCursor.KeepAnchor)
            out.append(sel)
        return out

    def _marker_and_issue_selections(self, first: int, last: int) -> list:
        """The highlights of lines ``first`` to ``last`` (0-based): the tests'
        missed lines, then the markers' tints, the issues' squiggles and the
        gremlins' tints (each drawn over the one before)."""
        missed = QColor(COVERAGE_COLORS["missed"])
        missed.setAlpha(26)
        tint = QColor(gremlins_mod.COLOR)
        tint.setAlpha(70)
        coverage, markers, issues, gremlins = [], [], [], []
        block = self.document().findBlockByNumber(first)
        while block.isValid() and block.blockNumber() <= last:
            line = block.blockNumber() + 1
            if self.show_coverage and self.coverage.get(line) == 0:
                coverage += self._line_highlight(block, missed)
            notes = self.markers.get(line)
            if notes:
                color = QColor(MARKER_COLORS.get(worst_severity(notes), "#868e96"))
                color.setAlpha(34)
                markers += self._line_highlight(block, color)
            for issue in self._issue_lines.get(line, ()):
                span = self._lint_span(issue)
                if span is None:
                    continue
                sel = QTextEdit.ExtraSelection()
                sel.format.setUnderlineStyle(QTextCharFormat.WaveUnderline)
                sel.format.setUnderlineColor(QColor(MARKER_COLORS[issue.severity]))
                sel.cursor = QTextCursor(self.document())
                sel.cursor.setPosition(span[0])
                sel.cursor.setPosition(span[1], QTextCursor.KeepAnchor)
                issues.append(sel)
            for g in self.gremlins.get(line, ()):  # (a tint: an invisible one would show nothing else)
                if g.column >= block.length() - 1:
                    continue
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(tint)
                sel.cursor = QTextCursor(block)
                sel.cursor.setPosition(block.position() + g.column)
                sel.cursor.setPosition(block.position() + g.column + 1, QTextCursor.KeepAnchor)
                gremlins.append(sel)
            block = block.next()
        return coverage + markers + issues + gremlins

    @staticmethod
    def _line_highlight(block, color: QColor) -> list:
        """A full-width background for a line: one per row it is shown in
        (with View → Word Wrap a long line takes several, and Qt fills only
        the row the selection's cursor is in). Nothing for a block not in a
        document: a cursor made from it crashes Qt."""
        if not block.isValid():
            return []
        layout = block.layout()
        starts = [layout.lineAt(i).textStart() for i in range(layout.lineCount())] if layout else []
        out = []
        for start in starts or [0]:
            sel = QTextEdit.ExtraSelection()
            sel.format.setBackground(color)
            sel.format.setProperty(QTextFormat.FullWidthSelection, True)
            sel.cursor = QTextCursor(block)
            sel.cursor.setPosition(block.position() + start)
            out.append(sel)
        return out

    def update_occurrences(self):
        """Highlight the other occurrences of the selected text in the file
        (single-line selections of 2 to 200 characters; Match case and Whole
        words as set in the find bar)."""
        found: list[tuple[int, int]] = []
        cursor = self.textCursor()
        selected = cursor.selectedText()
        if (self.highlight_matches and len(self.extra_cursors) == 0 and 2 <= len(selected) <= 200
                and selected.strip() and "\u2029" not in selected):
            case, word = self.match_options()
            pattern = re.escape(selected)
            if word:  # (as the find bar: "_" is part of a word, so count doesn't match in count_all)
                pattern = rf"(?<!\w){pattern}(?!\w)"
            text = self.toPlainText()
            simple = text.isascii()  # (else Qt counts characters beyond U+FFFF twice)
            self._occurrence_color = occurrence_color(self.palette().color(QPalette.Base))
            mine = cursor.selectionStart()
            for match in re.finditer(pattern, text, 0 if case else re.IGNORECASE):
                start, end = match.span()
                if not simple:
                    start, end = self._qt_position(text, start), self._qt_position(text, end)
                if start == mine:
                    continue  # (the selection itself)
                found.append((start, end))
                if len(found) >= self.MAX_MATCHES:
                    break
        if found or self.occurrence_selections:
            self.occurrence_selections = found
            self.update_extra_selections(rebuild=False)

    def set_find_selections(self, spans: list[tuple[int, int]]):
        """The find bar's matches: (start, end) positions, in order."""
        self.find_selections = spans
        self.update_extra_selections(rebuild=False)  # (kept apart: see update_extra_selections)
        self._update_minimap()

    def paintEvent(self, event):
        super().paintEvent(event)
        self._paint_squiggles(event.rect())
        if self.extra_cursors and self._caret_on and self.hasFocus():  # (Qt's own is hidden: see _caret_width)
            painter = QPainter(self.viewport())
            color = self.palette().color(QPalette.Text)
            for c in (self.textCursor(), *self.extra_cursors):
                rect = self.cursorRect(c)
                painter.fillRect(QRect(rect.left(), rect.top(), self._caret_width, rect.height()), color)
            painter.end()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self._restart_blink()

    def keyPressEvent(self, event):
        key = event.key()
        if key in MODIFIER_KEYS:
            # ⌘/⇧/⌥/⌃ on their own (e.g. to ⌘-click, or for a screenshot):
            # keep the hover popup open.
            super().keyPressEvent(event)
            return
        self.hover_popup.hide()
        if self.extra_cursors and self._multi_cursor_key(event):
            return
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
        if key in (Qt.Key_Up, Qt.Key_Down) and event.modifiers() == Qt.AltModifier:
            # ⌥↑ / ⌥↓ (Alt+↑ / ↓): Move Line Up / Down. (Here too, not only as the
            # menu's shortcut: on macOS the text field takes these keys for itself.)
            self.move_lines(down=key == Qt.Key_Down)
            return
        before = self.document().revision()
        self._edit_key(event)
        if self.document().revision() != before:
            self.typed.emit("\b" if key == Qt.Key_Backspace else event.text())

    def _edit_key(self, event):
        key = event.key()
        if key == Qt.Key_Tab and not event.modifiers() & (Qt.ControlModifier | Qt.AltModifier):
            cursor = self.textCursor()
            if cursor.hasSelection() and "\u2029" in cursor.selectedText():  # (several lines: indent them)
                self._indent_selection(+1)
            else:
                cursor.beginEditBlock()
                cursor.removeSelectedText()
                cursor.insertText(self.tab_text(cursor))
                cursor.endEditBlock()
                self.setTextCursor(cursor)
            return
        if key == Qt.Key_Backtab:
            self._indent_selection(-1)
            return
        if key in (Qt.Key_Return, Qt.Key_Enter) and not event.modifiers() & Qt.ShiftModifier:
            # Keep the current line's indentation.
            line = self.textCursor().block().text()
            indent = line[:len(line) - len(line.lstrip())]
            cursor = self.textCursor()
            cursor.beginEditBlock()  # (one Undo step, the line break and its indentation)
            super().keyPressEvent(event)
            self.textCursor().insertText(indent)
            cursor.endEditBlock()
            return
        if key == Qt.Key_Backspace and not self.textCursor().hasSelection() and not self.use_tabs:
            # Backspace in leading spaces goes back to the previous tab stop.
            cursor = self.textCursor()
            count = backspace_length(cursor.block().text()[:cursor.positionInBlock()], self.indent_width)
            if count:
                cursor.movePosition(QTextCursor.Left, QTextCursor.KeepAnchor, count)
                cursor.removeSelectedText()  # (one Undo step)
                self.setTextCursor(cursor)
                return
        super().keyPressEvent(event)

    def tab_text(self, cursor: QTextCursor) -> str:
        """What Tab types at a cursor (see `tab_text`)."""
        return tab_text(cursor.block().text()[:cursor.positionInBlock()], self.indent_width, self.use_tabs)

    def indent_unit(self) -> str:
        """One indent level: a tab, or ``indent_width`` spaces."""
        return "\t" if self.use_tabs else " " * self.indent_width

    def set_indentation(self, width: int, use_tabs: bool):
        """View → Indentation (tabs are shown ``width`` columns wide too)."""
        self.indent_width, self.use_tabs = width, use_tabs
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * width)

    def _indent_selection(self, direction: int):
        """Indent (Tab) or outdent (⇧Tab) the selected lines, or the cursor's;
        a selection ending at the start of a line leaves that line alone."""
        cursor = self.textCursor()
        start = self.document().findBlock(cursor.selectionStart())
        end = self.document().findBlock(cursor.selectionEnd())
        if end != start and cursor.selectionEnd() == end.position():
            end = end.previous()
        cursor.beginEditBlock()
        block = start
        while block.isValid():
            c = QTextCursor(block)
            if direction > 0:
                if block.text().strip() or block == start == end:  # (blank lines stay blank)
                    c.insertText(self.indent_unit())
            else:
                c.movePosition(QTextCursor.Right, QTextCursor.KeepAnchor,
                               outdent_length(block.text(), self.indent_width))
                c.removeSelectedText()
            if block == end:
                break
            block = block.next()
        cursor.endEditBlock()

    def _qt_position(self, text: str, offset: int) -> int:
        """A position in the document for an offset in toPlainText() (Qt
        counts characters beyond U+FFFF twice)."""
        return len(text[:offset].encode("utf-16-le")) // 2

    def replace_ranges(self, ranges: list[tuple[int, int, str]]):
        """Replace (start, end, new text) ranges, given as offsets in
        toPlainText(), as one step to undo."""
        text = self.toPlainText()
        cursor = QTextCursor(self.document())
        cursor.beginEditBlock()
        for start, end, new in sorted(ranges, key=lambda r: r[0], reverse=True):  # (from the end: offsets stay right)
            cursor.setPosition(self._qt_position(text, start))
            cursor.setPosition(self._qt_position(text, end), QTextCursor.KeepAnchor)
            cursor.insertText(new)
        cursor.endEditBlock()

    def select_range(self, start: int, end: int):
        """Select (and show) a range given as offsets in toPlainText()."""
        text = self.toPlainText()
        cursor = QTextCursor(self.document())
        cursor.setPosition(self._qt_position(text, end))
        cursor.setPosition(self._qt_position(text, start), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)
        self.centerCursor()

    def selected_lines(self) -> tuple[int, int] | None:
        """The first and last lines of the selection (1-based), if any; a
        selection ending at the start of a line doesn't include that line."""
        cursor = self.textCursor()
        if not cursor.hasSelection():
            return None
        doc = self.document()
        first = doc.findBlock(cursor.selectionStart()).blockNumber() + 1
        end_block = doc.findBlock(cursor.selectionEnd())
        last = end_block.blockNumber() + (0 if cursor.selectionEnd() == end_block.position() else 1)
        return first, max(first, last)

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
