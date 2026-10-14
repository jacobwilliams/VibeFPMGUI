"""A small built-in code editor: line numbers, syntax highlighting (Pygments,
chosen by file extension), find/replace, go to line, and compiler warning /
error markers in the gutter, fortitude lint findings (see `vibefpmgui.lint`), and
for Fortran files the language server's hover, go to definition and outline
(see `vibefpmgui.lsp`).

`EditorTab` wraps a `CodeEditor` with a find bar and a status
line, and takes care of loading and saving the file (keeping its encoding and
line endings) and of noticing when it changes on disk.
"""

from __future__ import annotations

import difflib
import html
import re
import sys
from bisect import bisect_left
from collections.abc import Callable
from pathlib import Path

from qtpy.QtCore import (
    QFileSystemWatcher,
    QPoint,
    QPointF,
    QProcess,
    QRect,
    QRectF,
    QRegularExpression,
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
    QPainterPath,
    QPalette,
    QPen,
    QPolygonF,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextFormat,
    QTextOption,
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
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTextBrowser,
    QTextEdit,
    QToolButton,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from . import gremlins as gremlins_mod
from . import hover_card, lint, spelling
from . import lsp as lsp_mod
from .completion import CompletionPopup, SignaturePopup, global_rect
from .highlighter import (
    PygmentsHighlighter,
    apply_style,
    document_text,
    lexer_for_path,
)
from .icons import SYMBOL_INDENT, symbol_icon
from .minimap import Minimap
from .result_tree import Problem
from .widgets import StatusLabel

INDENT_WIDTHS = (2, 3, 4, 8)  # View → Indentation


def visual_column(text: str, width: int) -> int:
    """How far across a line's ``text`` reaches, with tabs to the next
    multiple of ``width``."""
    column = 0
    for ch in text:
        column = (column // width + 1) * width if ch == "\t" else column + 1
    return column


def tab_text(before: str, width: int, use_tabs: bool) -> str:
    """What Tab types after ``before`` (the line up to the cursor): a tab,
    or spaces to the next tab stop."""
    if use_tabs:
        return "\t"
    return " " * (width - visual_column(before, width) % width)


def outdent_length(line: str, width: int) -> int:
    """How many characters at the start of a line ⇧Tab removes: one tab, or
    spaces up to one indent level (or both, up to that width)."""
    spaces = min(len(line) - len(line.lstrip(" ")), width)
    if spaces < width and line[spaces:spaces + 1] == "\t":
        return spaces + 1  # (a tab, after any spaces short of a whole level)
    return spaces


def backspace_length(before: str, width: int) -> int:
    """In leading spaces, Backspace removes back to the previous tab stop:
    how many spaces (0: an ordinary Backspace)."""
    if not before or before.strip(" "):
        return 0  # (not just spaces before the cursor)
    return (len(before) - 1) % width + 1
MAX_FIND_HIGHLIGHTS = 2000
MARKER_COLORS = {"error": "#e03131", "fatal error": "#e03131", "warning": "#f08c00",
                 "review": "#7048e8", "remark": "#1c7ed6",
                 "lint error": "#e03131", "lint": "#0ca678",
                 "fortls error": "#e03131", "fortls": "#1098ad", "spelling": "#ae3ec9",
                 "gremlin": gremlins_mod.COLOR}
# Underline colors on a dark background, where these would be too dim to see.
DARK_UNDERLINE_COLORS = {"spelling": "#e599f7"}
# Which marker color wins when a line has several kinds of note.
MARKER_PRIORITY = ["fatal error", "error", "lint error", "fortls error", "warning", "review", "lint",
                   "fortls", "remark"]
# Which tool an underlined issue is from (by its severity), for the Problems tab.
PROBLEM_SOURCES = {"lint": "fortitude", "lint error": "fortitude", "fortls": "fortls",
                   "fortls error": "fortls", "spelling": "spelling"}
LINT_DELAY_MS = 600  # lint once typing pauses this long
GREMLIN_WIDTH = 16  # the gutter's gremlin column (only while the file has gremlins)
COVERAGE_WIDTH = 5  # the gutter's coverage bars, at its left edge (only while the file has coverage)
COVERAGE_COLORS = {True: "#2f9e44", False: "#e03131"}  # (a line the tests ran, one they didn't)
SPELLING_DELAY_MS = 250  # spell check this long after the colors are updated
LSP_SYNC_MS = 300    # send edits to the language server once typing pauses this long
COMPLETE_MS = 120    # ask for completions once typing pauses this long
AUTO_COMPLETE_MIN = 2  # characters of a name typed before completions appear by themselves
WORD_RE = re.compile(r"[A-Za-z0-9_]")
MODIFIER_KEYS = {Qt.Key_Shift, Qt.Key_Control, Qt.Key_Meta, Qt.Key_Alt, Qt.Key_AltGr,
                 Qt.Key_CapsLock, Qt.Key_Super_L, Qt.Key_Super_R}


def worst_severity(notes: list[tuple[str, str]]) -> str:
    return min((s for s, _ in notes),
               key=lambda s: MARKER_PRIORITY.index(s) if s in MARKER_PRIORITY else len(MARKER_PRIORITY))


# Change bars in the gutter: lines changed and not saved yet, and lines
# changed since the file was opened but saved.
CHANGE_COLORS = {"unsaved": "#f59f00", "saved": "#40c057"}
CHANGE_DIFF_MAX_LINES = 30_000  # (bigger files: no change bars, the diff would be slow)


def line_changes(opened: str, saved: str, current: str) -> tuple[dict[int, str], dict[int, tuple[str, int]]]:
    """Which lines of ``current`` differ from the saved text ("unsaved") or,
    if not, from the text as opened ("saved"); and where lines were deleted:
    {line: (kind, count)}, meaning removed just above that line (line count
    + 1: at the end). Lines are 1-based."""
    lines = current.split("\n")
    if len(lines) > CHANGE_DIFF_MAX_LINES:
        return {}, {}

    def diff(base: str):
        changed: set[int] = set()
        deleted: dict[int, int] = {}
        old = base.split("\n")
        # Only the middle differs (edits are usually in one place): skip the
        # lines that are the same at the start and the end before diffing.
        top, limit = 0, min(len(old), len(lines))
        while top < limit and old[top] == lines[top]:
            top += 1
        bottom = 0
        while bottom < limit - top and old[-1 - bottom] == lines[-1 - bottom]:
            bottom += 1
        matcher = difflib.SequenceMatcher(None, old[top:len(old) - bottom], lines[top:len(lines) - bottom],
                                          autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag in ("replace", "insert"):
                changed.update(range(top + j1 + 1, top + j2 + 1))
            elif tag == "delete":
                deleted[top + j1 + 1] = i2 - i1
        return changed, deleted

    unsaved, unsaved_deleted = diff(saved)
    since_open, open_deleted = (unsaved, unsaved_deleted) if opened == saved else diff(opened)
    marks = {line: "saved" for line in since_open - unsaved}
    marks.update({line: "unsaved" for line in unsaved})
    deletions = {line: ("saved", n) for line, n in open_deleted.items() if line not in unsaved_deleted}
    deletions.update({line: ("unsaved", n) for line, n in unsaved_deleted.items()})
    return marks, deletions


def line_endings(text: str) -> tuple[str, bool]:
    """(the line end most lines of a file's text have: "\\n" or "\\r\\n",
    whether it has more than one kind). Old Mac line ends (a lone "\\r") count
    as another kind: Qt reads them as line breaks, saved as the common one."""
    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    lf = text.count("\n") - crlf
    kinds = sum(1 for n in (crlf, lone_cr, lf) if n)
    return ("\r\n" if crlf > lf else "\n"), kinds > 1


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
        # A rounded card: the window is transparent, the card inside is drawn.
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.card = QFrame()
        self.card.setObjectName("HoverCard")
        self.browser = QTextBrowser()
        self.browser.setFrameShape(QFrame.NoFrame)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._link_clicked)
        self.browser.setFont(QToolTip.font())
        self.browser.document().setDocumentMargin(9)
        self.browser.viewport().setAutoFillBackground(False)
        inner = QVBoxLayout(self.card)
        inner.setContentsMargins(1, 3, 1, 3)
        inner.addWidget(self.browser)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.card)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(300)  # time to move the mouse into the popup
        self._hide_timer.timeout.connect(self._hide_unless_hovered)

    def show_at(self, global_pos: QPoint, text_html: str, anchor):
        self.anchor = anchor
        self._hide_timer.stop()
        # The editor's colors (Qt's tooltip colors don't follow dark mode on macOS).
        pal = self.editor.palette()
        base, text = pal.color(QPalette.Base), pal.color(QPalette.Text)
        bg = base.lighter(115) if base.lightness() < 128 else base.darker(103)
        border = QColor(text.red(), text.green(), text.blue(), 70).name(QColor.HexArgb)
        self.setStyleSheet(f"#HoverCard {{ background: {bg.name()}; border: 1px solid {border}; "
                           f"border-radius: 9px; }} QTextBrowser {{ background: transparent; "
                           f"color: {text.name()}; }}")
        self.browser.setHtml(text_html)
        screen = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        max_w = min(self.MAX_WIDTH, int(screen.width() * 0.6))
        max_h = min(self.MAX_HEIGHT, int(screen.height() * 0.45))
        doc = self.browser.document()
        doc.setTextWidth(-1)
        bar = self.browser.verticalScrollBar().sizeHint().width()
        width = min(int(doc.idealWidth()) + 4, max_w)
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 10
        if height > max_h:  # it will scroll: leave room for the scroll bar
            width = min(width + bar, max_w)
            height = max_h
        self.resize(width + 4, height + 8)  # (the card's border and margins)
        # Below the mouse; above it if there's no room; always on screen.
        x = min(max(global_pos.x(), screen.left()), screen.right() - self.width())
        y = global_pos.y() + 18
        if y + self.height() > screen.bottom():
            y = max(screen.top(), global_pos.y() - 8 - self.height())
        self.move(x, y)
        self.browser.verticalScrollBar().setValue(0)
        self.show()

    def _link_clicked(self, url):
        """The card's links: "Apply fix" (fix:N = the editor's Nth lint
        issue), a spelling suggestion (spell:N:word) or "Add to Dictionary"
        (addword:N)."""
        if url.scheme() in ("spell", "addword"):
            number, _, word = url.path().partition(":")
            try:
                issue = self.editor.lint_issues[int(number)]
            except (ValueError, IndexError):
                return
            self.hide()
            if url.scheme() == "spell":
                self.editor.replace_word(issue, word)
            else:
                self.editor.word_added.emit(issue.word)
            return
        if url.scheme() == "fix":
            try:
                issue = self.editor.lint_issues[int(url.path())]
            except (ValueError, IndexError):
                return
            self.hide()
            self.editor.lint_fix_requested.emit(issue)

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
            if event.pos().x() < self.editor._coverage_column() and self.editor.coverage_note(line):
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
        # Clicking a number selects that line.
        line = self.editor.line_at_y(event.pos().y())
        if line:
            self.editor.go_to_line(line, select=True)


class CodeEditor(QPlainTextEdit):
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
        self.minimap = None  # the vibefpmgui.minimap.Minimap beside it, if any
        self.find_selections: list[QTextEdit.ExtraSelection] = []
        self.coverage: dict[int, int] = {}  # line -> times the tests ran it (see set_coverage)
        # The selection's other occurrences, highlighted (see _update_occurrences):
        # Match case / Whole words come from the tab's find bar.
        self.occurrence_selections: list[QTextEdit.ExtraSelection] = []
        self.match_options: Callable[[], tuple[bool, bool]] = lambda: (False, False)
        self._occurrence_timer = QTimer(self)
        self._occurrence_timer.setSingleShot(True)
        self._occurrence_timer.setInterval(120)  # (once the selection rests: not on every step of a drag)
        self._occurrence_timer.timeout.connect(self._update_occurrences)
        self.selectionChanged.connect(self._occurrence_timer.start)
        self.lint_issues: list[lint.LintIssue] = []  # underlined, with a tooltip
        self._spelling: list = []        # the misspelled words among them, in line order (see set_lint)
        self._spelling_lines: list[int] = []
        # The highlights that only change when the markers, issues or gremlins
        # do (rebuilt then, not on every cursor move: see _update_extra_selections).
        self._fixed_selections: list[QTextEdit.ExtraSelection] = []
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
        self.blockCountChanged.connect(self._update_gutter_width)
        self.updateRequest.connect(self._update_gutter)
        self.cursorPositionChanged.connect(lambda: self._update_extra_selections(rebuild=False))
        self._update_gutter_width()
        self._update_extra_selections()

    def toPlainText(self) -> str:
        """The text exactly as it is (see highlighter.document_text): what's
        saved, linted, searched and sent to the language server."""
        return document_text(self.document())

    # ------------------------------------------------------------- gutter
    def gutter_width(self) -> int:
        digits = max(3, len(str(max(1, self.blockCount()))))
        return (22 + self.fontMetrics().horizontalAdvance("9") * digits + self._coverage_column()
                + self._gremlin_column())

    def _gremlin_column(self) -> int:
        return GREMLIN_WIDTH if self.gremlins else 0

    def _coverage_column(self) -> int:
        return COVERAGE_WIDTH if self.coverage and self.show_coverage else 0

    def set_coverage(self, lines: dict[int, int] | None):
        """The last test run's coverage of this file: line -> times run (lines
        with code only), shown as green and red bars in the gutter."""
        lines = dict(lines or {})
        if lines == self.coverage:
            return
        self.coverage = lines
        self.refresh_gutter()

    def refresh_gutter(self):
        """The gutter again, at its width now (a column shown or hidden)."""
        self._update_gutter_width()
        self.gutter.setGeometry(QRect(self.contentsRect().left(), self.contentsRect().top(),
                                      self.gutter_width(), self.contentsRect().height()))
        self.gutter.update()

    def coverage_note(self, line: int) -> str:
        """The gutter's tooltip for a coverage bar."""
        if line not in self.coverage:
            return ""
        count = self.coverage[line]
        return f"Run {count:,} time{'s' * (count != 1)} by the tests" if count else "Not run by the tests"

    def set_gremlins(self, found: list[gremlins_mod.Gremlin]):
        """The file's gremlins (non-ASCII and invisible characters): a gremlin
        in the gutter on their lines, and the characters tinted."""
        by_line: dict[int, list[gremlins_mod.Gremlin]] = {}
        for g in found:
            by_line.setdefault(g.line, []).append(g)
        if by_line == self.gremlins:
            return
        self.gremlins = by_line
        self._update_gutter_width()
        self.gutter.setGeometry(QRect(self.contentsRect().left(), self.contentsRect().top(),
                                      self.gutter_width(), self.contentsRect().height()))
        self._update_extra_selections()
        self.gutter.update()

    def gremlin_notes(self, line: int) -> list[str]:
        """The gremlins on a line, described (for the gutter's tooltip)."""
        return [f"Gremlin at column {g.column + 1}: {g.message} ({g.code})" for g in self.gremlins.get(line, [])]

    def _update_gutter_width(self, *_):
        width = self.gutter_width()
        if width != self.viewportMargins().left():  # (called on every repaint of the whole view)
            self.setViewportMargins(width, 0, 0, 0)

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
        if self.lineWrapMode() != QPlainTextEdit.NoWrap:  # (lines re-wrap: highlight their new rows)
            QTimer.singleShot(0, self._update_extra_selections)

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
                left = self._coverage_column()
                if left and line in self.coverage:  # (ran: green; didn't: red)
                    painter.setPen(Qt.NoPen)
                    painter.setBrush(QColor(COVERAGE_COLORS[self.coverage[line] > 0]))
                    painter.drawRect(QRectF(0, top, 3, bottom - top))
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

    def set_word_wrap(self, on: bool):
        """Wrap long lines at the window's edge (at a space if there is
        one), or scroll sideways."""
        self.setWordWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        self.setLineWrapMode(QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap)
        QTimer.singleShot(0, self._update_extra_selections)
        self.gutter.update()
        self._update_minimap()

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
        self._update_extra_selections()
        self.gutter.update()
        self._update_minimap()

    def _update_minimap(self):
        if self.minimap is not None:
            self.minimap.update()

    def set_lint(self, issues: list[lint.LintIssue]):
        self.lint_issues = issues
        # The misspelled words in line order, for the squiggles (see squiggle_spans).
        self._spelling = sorted((i for i in issues if i.severity == "spelling"), key=lambda i: (i.line, i.column))
        self._spelling_lines = [i.line for i in self._spelling]
        self._update_extra_selections()
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
    def _update_extra_selections(self, rebuild: bool = True):
        """Current line, lines with errors/warnings, underlined issues,
        gremlins, the extra cursors' selections and find matches. The
        markers', issues' and gremlins' are kept between calls and only built
        again with ``rebuild`` (they changed, or the colors or line wrapping
        did): moving the cursor only moves the current line's."""
        if rebuild:
            self._fixed_selections = self._marker_and_issue_selections()
        selections = list(self._fixed_selections)
        if not self.isReadOnly():
            color = QColor(self.palette().color(QPalette.Highlight))
            color.setAlpha(28)
            selections += self._line_highlight(self.textCursor().block(), color)
        selected = QColor(self.palette().color(QPalette.Highlight))
        for c in self.extra_cursors:
            if c.hasSelection():
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(selected)
                sel.format.setForeground(self.palette().color(QPalette.HighlightedText))
                sel.cursor = QTextCursor(c)
                selections.append(sel)
        self.setExtraSelections(selections + self.occurrence_selections + self.find_selections)

    def _marker_and_issue_selections(self) -> list:
        selections = []
        for line, notes in self.markers.items():
            block = self.document().findBlockByNumber(line - 1)
            if not block.isValid():
                continue
            worst = worst_severity(notes)
            color = QColor(MARKER_COLORS.get(worst, "#868e96"))
            color.setAlpha(34)
            selections += self._line_highlight(block, color)
        for issue in self.lint_issues:
            if issue.severity == "spelling":
                continue  # (drawn by paintEvent: see _paint_squiggles)
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
        tint = QColor(gremlins_mod.COLOR)
        tint.setAlpha(70)
        for found in self.gremlins.values():  # (a tint: an invisible one would show nothing else)
            for g in found:
                block = self.document().findBlockByNumber(g.line - 1)
                if not block.isValid() or g.column >= block.length() - 1:
                    continue
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(tint)
                sel.cursor = QTextCursor(block)
                sel.cursor.setPosition(block.position() + g.column)
                sel.cursor.setPosition(block.position() + g.column + 1, QTextCursor.KeepAnchor)
                selections.append(sel)
        return selections

    @staticmethod
    def _line_highlight(block, color: QColor) -> list:
        """A full-width background for a line: one per row it is shown in
        (with View → Word Wrap a long line takes several, and Qt fills only
        the row the selection's cursor is in)."""
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

    def _update_occurrences(self):
        """Highlight the other occurrences of the selected text in the file
        (single-line selections of 2 to 200 characters; Match case and Whole
        words as set in the find bar)."""
        found: list[QTextEdit.ExtraSelection] = []
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
            color = occurrence_color(self.palette().color(QPalette.Base))
            mine = cursor.selectionStart()
            for match in re.finditer(pattern, text, 0 if case else re.IGNORECASE):
                start, end = match.span()
                if not simple:
                    start, end = self._qt_position(text, start), self._qt_position(text, end)
                if start == mine:
                    continue  # (the selection itself)
                sel = QTextEdit.ExtraSelection()
                sel.format.setBackground(color)
                sel.cursor = QTextCursor(self.document())
                sel.cursor.setPosition(start)
                sel.cursor.setPosition(end, QTextCursor.KeepAnchor)
                found.append(sel)
                if len(found) >= self.MAX_MATCHES:
                    break
        if found or self.occurrence_selections:
            self.occurrence_selections = found
            self._update_extra_selections(rebuild=False)

    def set_find_selections(self, selections):
        self.find_selections = selections
        self._update_extra_selections(rebuild=False)  # (kept apart: see _update_extra_selections)
        self._update_minimap()

    # ------------------------------------------------------------ editing
    # ------------------------------------------------- multiple cursors
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
        self._update_extra_selections(rebuild=False)  # (the cursors' selections are always made anew)
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


def occurrence_color(background: QColor) -> QColor:
    """The highlight of the selection's other occurrences on a syntax
    style's background: a blue mixed into it until it's plainly lighter (on
    a dark background) or darker (on a light one). The app's selection color
    was all but invisible on some styles (github-dark's near-black)."""
    tint = QColor(90, 150, 255)
    dark = background.lightness() < 128
    wanted = OCCURRENCE_CONTRAST[dark]
    color = QColor(background)
    for amount in (0.25, 0.35, 0.45, 0.55, 0.65, 0.75):
        color = QColor(round(background.red() + (tint.red() - background.red()) * amount),
                       round(background.green() + (tint.green() - background.green()) * amount),
                       round(background.blue() + (tint.blue() - background.blue()) * amount))
        if abs(color.lightness() - background.lightness()) >= wanted:
            break
    return color


# How far the highlight is from the background (lightness, 0–255): a dark
# background needs more to show (dark colors look closer than they measure).
OCCURRENCE_CONTRAST = {False: 34, True: 60}


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

    def _all_matches(self) -> list:
        text = self.find_edit.text()
        if not text:
            return []
        doc = self.editor.document()
        out = []
        found = doc.find(self._needle(), 0, self._flags())
        while not found.isNull() and len(out) < MAX_FIND_HIGHLIGHTS:
            out.append(found)
            found = doc.find(self._needle(), found.selectionEnd(), self._flags())
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


DEPTH_ROLE = Qt.UserRole + 7  # outline entries: how deeply nested


class _OutlineDelegate(QStyledItemDelegate):
    """Draws the outline's entries with the badge and name together,
    indented one step per nesting level (row background across the full
    width)."""

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        style = opt.widget.style() if opt.widget else QApplication.style()
        selected = bool(opt.state & QStyle.State_Selected) or bool(opt.state & QStyle.State_MouseOver)
        painter.save()
        if selected:
            painter.fillRect(opt.rect, opt.palette.color(QPalette.Active, QPalette.Highlight))
        else:
            style.drawPrimitive(QStyle.PE_PanelItemViewItem, opt, painter, opt.widget)
        x = opt.rect.left() + 8 + SYMBOL_INDENT * int(index.data(DEPTH_ROLE) or 0)
        size = opt.decorationSize
        top = opt.rect.top() + (opt.rect.height() - size.height()) // 2
        opt.icon.paint(painter, QRect(x, top, size.width(), size.height()))
        painter.setPen(opt.palette.color(QPalette.Active, QPalette.HighlightedText) if selected
                       else opt.palette.color(QPalette.Text))
        painter.setFont(opt.font)
        text_rect = QRect(x + size.width() + 6, opt.rect.top(), opt.rect.right() - x - size.width() - 6,
                          opt.rect.height())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft, opt.text)
        painter.restore()

    def sizeHint(self, option, index):
        hint = super().sizeHint(option, index)
        return QSize(hint.width() + 16 + SYMBOL_INDENT * int(index.data(DEPTH_ROLE) or 0),
                     max(hint.height(), 22))


class EditorTab(QWidget):
    """One open file: editor + find bar + status line."""

    lint_enabled = True  # lint Fortran files with fortitude (View menu)
    lsp_diagnostics_enabled = True  # show fortls's own checks (View menu)
    spelling_enabled = True  # spell check comments and strings (View menu)
    gremlins_enabled = True  # View → Show Gremlins (non-ASCII and invisible characters)
    minimap_enabled = True  # View → Show Minimap
    word_wrap = False       # View → Word Wrap

    dirty_changed = Signal(bool)
    saved = Signal(str)
    path_changed = Signal(str)
    navigate_requested = Signal(str, int, int)  # go to file, line (1-based), column (0-based)
    references_found = Signal(str, object)  # name, [(file, line, start column, end column)]
    rename_ready = Signal(str, str, object)  # old name, new name, {file: [lsp.TextEdit]}
    problems_changed = Signal()  # (see problems)
    explain_requested = Signal(object)  # a Problem to Explain with AI (from the editor's menu)

    def __init__(self, path: Path, parent=None):
        super().__init__(parent)
        self.path = Path(path).resolve()
        self.encoding = "utf-8"
        self.newline = "\n"
        self._disk_newline = "\n"  # the line end of the file as saved (see is_dirty)
        self.mixed_newlines = False  # the file has both LF and CRLF (or old Mac CR) line ends (see line_endings)
        self._disk_mixed = False
        self._disk_text = ""
        self._compiler_markers: dict[int, list[tuple[str, str]]] = {}
        self._review_markers: dict[int, list[tuple[str, str]]] = {}
        self._lint_markers: dict[int, list[tuple[str, str]]] = {}
        self._lsp_markers: dict[int, list[tuple[str, str]]] = {}
        self._fortitude_issues: list = []  # (the editor underlines these and fortls's together)
        self._lsp_issues: list = []
        self._spelling_issues: list = []
        self._lint_process: QProcess | None = None
        self._lint_again = False  # the text changed while fortitude was running
        self.rewriting = False  # Format or Fix All is running on this text (see MainWindow._rewrite_text)
        self.lsp: lsp_mod.LspClient | None = None  # the language server, for Fortran files
        self._outline: list[tuple[int, str, str, int, int]] = []
        self.editor = CodeEditor()
        self.highlighter = PygmentsHighlighter(self.editor.document(), lexer_for_path(self.path))
        self.find_bar = FindBar(self.editor)
        # A change to the text: the last test run's coverage no longer fits its lines.
        self.editor.document().contentsChange.connect(
            lambda _pos, removed, added: (removed or added) and self.editor.coverage and self.editor.set_coverage(None))
        # The selection's other occurrences: matched as the find bar is set.
        self.editor.match_options = lambda: (self.find_bar.case_check.isChecked(),
                                             self.find_bar.word_check.isChecked())
        self.find_bar.case_check.toggled.connect(self.editor._update_occurrences)
        self.find_bar.word_check.toggled.connect(self.editor._update_occurrences)
        self.status = StatusLabel()
        self.status.setContentsMargins(8, 2, 8, 2)

        # Outline of the file (modules, types, procedures), from the language server.
        self.outline_box = QComboBox()
        self.outline_box.setMinimumContentsLength(30)
        self.outline_box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.outline_box.setToolTip("Outline: jump to a module, type or procedure in this file")
        self.outline_box.setIconSize(QSize(16, 16))
        self.outline_box.setItemDelegate(_OutlineDelegate(self.outline_box))
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
        # Shown when the file changes on disk while it has unsaved edits: the
        # choice waits here, in the tab (a dialog from a timer could open on
        # top of another one, e.g. Save As).
        self._disk_change: tuple[str, str, str] | None = None  # (text, encoding, line end) on disk, waiting
        self.disk_bar = QFrame()
        self.disk_bar.setObjectName("DiskBar")
        self.disk_bar.setStyleSheet("#DiskBar { background: rgba(240, 140, 0, 60); border-bottom: 1px solid "
                                    "rgba(240, 140, 0, 160); }")
        self.disk_label = QLabel()
        self.disk_label.setWordWrap(True)
        reload_button = QPushButton("Reload")
        reload_button.setToolTip("Show the file as it is on disk (your unsaved edits are lost; Undo brings them back)")
        reload_button.clicked.connect(self.reload_from_disk)
        keep_button = QPushButton("Keep Mine")
        keep_button.setToolTip("Keep your edits; saving them replaces the file on disk")
        keep_button.clicked.connect(self.keep_my_edits)
        for button in (reload_button, keep_button):
            button.setAttribute(Qt.WA_MacSmallSize)
        disk_row = QHBoxLayout(self.disk_bar)
        disk_row.setContentsMargins(8, 4, 8, 4)
        disk_row.addWidget(self.disk_label, 1)
        disk_row.addWidget(reload_button)
        disk_row.addWidget(keep_button)
        self.disk_bar.hide()
        layout.addWidget(self.disk_bar)
        # The text, with the minimap at its right.
        self.minimap = Minimap(self.editor)
        self.editor.minimap = self.minimap
        self.minimap.setVisible(self.minimap_enabled)
        self.editor.set_word_wrap(self.word_wrap)
        self.highlighter.relexed.connect(self.minimap.invalidate)
        self._spelling_timer = QTimer(self)  # (once the colors, so the comments and strings, are updated)
        self._spelling_timer.setSingleShot(True)
        self._spelling_timer.setInterval(SPELLING_DELAY_MS)
        self._spelling_timer.timeout.connect(self.check_spelling)
        self.highlighter.relexed.connect(self._spelling_timer.start)
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.editor, 1)
        body.addWidget(self.minimap)
        layout.addLayout(body, 1)
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
        self._changes_timer = QTimer(self)  # the gutter's change bars, once typing pauses
        self._changes_timer.setSingleShot(True)
        self._changes_timer.setInterval(300)
        self._changes_timer.timeout.connect(self.update_change_marks)
        self._lsp_timer = QTimer(self)
        self._lsp_timer.setSingleShot(True)
        self._lsp_timer.setInterval(LSP_SYNC_MS)
        self._lsp_timer.timeout.connect(self._lsp_sync)
        self.editor.definition_requested.connect(self.go_to_definition)
        self.editor.implementation_requested.connect(self.go_to_implementation)
        self.editor.references_requested.connect(self.find_references)
        self.editor.rename_requested.connect(self.rename_symbol)
        self.editor.lint_fix_requested.connect(self.apply_lint_fix)
        self.editor.explain_requested.connect(self._explain_line)
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
        self.editor.cursors_changed.connect(self._update_status)
        self.load()
        self.refresh_style()
        self.editor.textChanged.connect(self._lint_timer.start)
        self.editor.textChanged.connect(self._changes_timer.start)
        self._changes_timer.timeout.connect(self.check_gremlins)  # (the same typing pause)
        self.editor.textChanged.connect(lambda: self.lsp is not None and self._lsp_timer.start())
        self.editor.cursorPositionChanged.connect(self._outline_follow_cursor)
        self.check_gremlins()
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

        text = self.editor.document().findBlockByNumber(line - 1).text()
        name = re.match(r"\w*", text[column:]).group(0)

        def answered(result):
            hover_card.set_text_color(self.editor.palette().color(QPalette.Text))
            info = lsp_mod.hover_info(result.get("contents")) if isinstance(result, dict) else None
            if info is not None:
                self.editor.show_hover(line, column, global_pos, hover_card.card_html(info, name, lint_html))

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

    def go_to_implementation(self):
        """For a type-bound procedure: the procedure it binds (Go to
        Definition stops at the binding); for a generic interface: its first
        module procedure."""
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
                self._update_status(f"no implementation found for {name} (only type-bound procedures "
                                    "and generic interfaces have one)")
                return
            path, to_line, to_column = found
            self.navigate_requested.emit(str(path), to_line, to_column)

        self.lsp.implementation(self.path, line, column, answered)

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
        # Each entry: a badge for its kind, then its name; the list indents
        # nested entries (see _OutlineDelegate).
        for depth, kind, name, first, _last in self._outline:
            self.outline_box.addItem(symbol_icon(kind), name)
            row = self.outline_box.count() - 1
            self.outline_box.setItemData(row, f"{kind} {name} (line {first})", Qt.ToolTipRole)
            self.outline_box.setItemData(row, depth, DEPTH_ROLE)
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
        self.highlighter.close()  # (a big file may be being lexed on a thread)

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
        # (Methods, not lambdas holding the process: see LspClient.start.)
        process.finished.connect(self._lint_process_finished)
        process.errorOccurred.connect(self._lint_process_error)
        self._lint_process = process
        process.start(lint.executable(), lint.command(self.path))
        # (The text itself, not the document's revision: recoloring the
        # syntax also counts as a revision.)
        self._lint_sent_text = self.editor.toPlainText()
        process.write(self._lint_sent_text.encode("utf-8"))
        process.closeWriteChannel()

    def _lint_process_finished(self, *_):
        if isinstance(self.sender(), QProcess):
            self._lint_finished(self.sender())

    def _lint_process_error(self, error):
        if error == QProcess.FailedToStart and isinstance(self.sender(), QProcess):
            self._lint_finished(self.sender())

    def _lint_finished(self, process: QProcess):
        if process is not self._lint_process:
            return
        self._lint_process = None
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        process.deleteLater()
        if self.can_lint():
            self._show_lint(lint.parse(output))
            self._lint_text = self._lint_sent_text  # the text the issues are about
        if self._lint_again:
            self._lint_timer.start()

    def apply_lint_fix(self, issue):
        """Apply fortitude's fix for one issue (one Undo step). Only if the
        text is still the one fortitude checked: otherwise the positions
        may be off, so check again first."""
        if not issue.fixable:
            return
        if getattr(self, "_lint_text", None) != self.editor.toPlainText():
            self._update_status("the file changed since fortitude checked it — try again in a moment")
            self._lint_timer.start()
            return
        self.apply_text_edits(issue.edits)
        self._update_status(f"applied fix: {issue.fix or issue.code}")

    def update_change_marks(self):
        """The gutter's change bars: lines changed and not saved yet, and
        lines changed since the file was opened (and saved)."""
        self._changes_timer.stop()
        marks, deletions = line_changes(getattr(self, "_opened_text", self._disk_text), self._disk_text,
                                        self.editor.toPlainText())
        self.editor.set_change_marks(marks, deletions)

    def stop_lint(self):
        """Before the tab is closed: don't leave fortitude running."""
        self._lint_timer.stop()
        if self._lint_process is not None:
            process, self._lint_process = self._lint_process, None
            process.kill()
            process.waitForFinished(1000)

    def _show_lint(self, issues: list[lint.LintIssue]):
        self._fortitude_issues = list(issues)
        self._lint_markers = self._markers_for(issues)
        self._show_issues()

    # ---------------------------------------------------------- gremlins
    def set_gremlins_enabled(self, on: bool):
        self.check_gremlins()

    def check_gremlins(self):
        """Look for gremlins (non-ASCII and invisible characters) again."""
        found = gremlins_mod.find(self.editor.toPlainText()) if self.gremlins_enabled else []
        before = self.editor.gremlins
        self.editor.set_gremlins(found)
        if self.editor.gremlins != before:
            self._update_status()
            self.problems_changed.emit()

    # ---------------------------------------------------------- spelling
    def set_spelling_enabled(self, on: bool):
        self.check_spelling() if on else self._show_spelling([])

    def check_spelling(self):
        """Spell check the comments and strings (as the highlighter last
        found them: it lexes again after each edit, then this runs)."""
        self._spelling_timer.stop()
        if not (self.spelling_enabled and spelling.available()):
            self._show_spelling([])
            return
        hl = self.highlighter
        if not hl.is_current():
            return  # (the highlighter is behind: it calls again when it's caught up)
        self._show_spelling(spelling.check(hl.text, hl.prose, hl.names))

    def _show_spelling(self, issues: list):
        if issues == self._spelling_issues:
            return  # (the same words: nothing to redraw, or to tell the Problems tab)
        self._spelling_issues = issues
        self._show_issues()

    def show_lsp_diagnostics(self, issues: list):
        """fortls's check of this file (it sends one when the file is opened
        or saved), shown like fortitude's issues (View menu: on or off)."""
        issues = list(issues) if self.lsp_diagnostics_enabled else []
        self._lsp_issues = issues
        self._lsp_markers = self._markers_for(issues)
        self._show_issues()

    @staticmethod
    def _markers_for(issues) -> dict[int, list[tuple[str, str]]]:
        markers: dict[int, list[tuple[str, str]]] = {}
        for issue in issues:
            markers.setdefault(issue.line, []).append((issue.severity, issue.describe()))
        return markers

    def _show_issues(self):
        self.editor.set_lint(self._fortitude_issues + self._lsp_issues + self._spelling_issues)
        self._apply_markers()

    # ------------------------------------------------------------- files
    @staticmethod
    def is_probably_text(path: Path) -> bool:
        try:
            with open(path, "rb") as f:
                return b"\0" not in f.read(8192)
        except OSError:
            return False

    def _read(self) -> tuple[str, str, str, bool]:
        """(text with "\\n" line ends, encoding, line end, whether the line
        ends are mixed) of the file on disk (see line_endings)."""
        raw = self.path.read_bytes()
        try:
            text, encoding = raw.decode("utf-8"), "utf-8"
        except UnicodeDecodeError:
            text, encoding = raw.decode("latin-1"), "latin-1"
        newline, mixed = line_endings(text)
        return text.replace("\r\n", "\n"), encoding, newline, mixed

    def load(self):
        text, self.encoding, self.newline, self.mixed_newlines = self._read()
        self._disk_newline = self.newline  # (a converted file is unsaved: see is_dirty)
        self._disk_mixed = self.mixed_newlines
        self._disk_text = text
        self._opened_text = text  # (the change bars compare with this and the saved text)
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
        """Unsaved changes: to the text, or to its line endings (Convert Line Endings)."""
        return (self.editor.document().isModified() or self.newline != self._disk_newline
                or self._disk_mixed != self.mixed_newlines)

    def convert_line_endings(self, newline: str) -> bool:
        """Edit → Convert Line Endings: the file is saved with ``newline``
        ("\\n" Unix, "\\r\\n" Windows) at every line end (a file with mixed
        ones, or old Mac ones, which Qt reads as line breaks, gets the same
        everywhere). False if it already has them."""
        if newline == self.newline and not self.mixed_newlines:
            return False
        self.newline = newline
        self.mixed_newlines = False  # (all the same from the next save)
        self.dirty_changed.emit(self.is_dirty())
        self._update_status()
        return True

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
        self._disk_newline = self.newline
        self.mixed_newlines = self._disk_mixed = False  # (saved: every line ends the same way now)
        self._disk_change = None  # (the save replaced any other version on disk)
        self.disk_bar.hide()
        self.editor.document().setModified(False)
        self.dirty_changed.emit(False)  # (also when only the line endings had changed)
        # Line numbers in the compiler/review notes may no longer be right.
        self._compiler_markers, self._review_markers = {}, {}
        self.editor.set_markers({})
        self._watch()
        if self.lsp is not None:
            self._flush_lsp()
            self.lsp.save_document(self.path)
        self.update_change_marks()  # unsaved bars become saved ones
        self.saved.emit(str(self.path))
        return True

    def save_as(self) -> bool:
        filename, _ = QFileDialog.getSaveFileName(self, "Save As", str(self.path))
        if not filename:
            return False
        client = self.lsp
        self.detach_lsp()  # (under the old name)
        self._set_path(filename)
        saved = self.save()
        self.lint()  # it may (no longer) be a Fortran file
        self.attach_lsp(client)
        return saved

    def renamed(self, path: str | Path):
        """The file was renamed or moved (on disk): the tab follows it,
        keeping any unsaved changes."""
        client = self.lsp
        self.detach_lsp()  # (under the old name)
        self._set_path(path)
        self._watch()
        self.lint()  # it may (no longer) be a Fortran file
        self.attach_lsp(client)
        self._update_status()

    def _set_path(self, path: str | Path):
        self.path = Path(path).resolve()
        self.highlighter.set_lexer(lexer_for_path(self.path))
        self.path_changed.emit(str(self.path))

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
            text, encoding, newline, mixed = self._read()
        except OSError:
            return
        if text == self._disk_text:
            return  # e.g. our own save
        if self.is_dirty():
            # Unsaved edits: ask in the tab (see disk_bar), not with a dialog.
            self._disk_change = (text, encoding, newline, mixed)
            self.disk_label.setText(f"<b>{html.escape(self.path.name)}</b> was changed by another program, "
                                    "and you have unsaved edits.")
            self.disk_bar.show()
            return
        self._load_from_disk(text, encoding, newline, mixed)

    def reload_from_disk(self):
        """The disk bar's Reload: the file as it is on disk, in place of the
        unsaved edits (one Undo step brings them back)."""
        change, self._disk_change = self._disk_change, None
        self.disk_bar.hide()
        if change is not None:
            self._load_from_disk(*change, undoable=True)

    def keep_my_edits(self):
        """The disk bar's Keep Mine: the edits stay (saving replaces the file);
        this version on disk isn't asked about again."""
        change, self._disk_change = self._disk_change, None
        self.disk_bar.hide()
        if change is not None:
            self._disk_text, _encoding, self._disk_newline, self._disk_mixed = change

    def _load_from_disk(self, text: str, encoding: str, newline: str, mixed: bool, undoable: bool = False):
        cursor = self.editor.textCursor().position()
        scroll = self.editor.verticalScrollBar().value()
        self._disk_text = text
        self.encoding, self.newline, self._disk_newline = encoding, newline, newline
        self.mixed_newlines = self._disk_mixed = mixed
        if undoable:  # (replacing the edits: one Undo step, so they can come back)
            whole = QTextCursor(self.editor.document())
            whole.select(QTextCursor.Document)
            whole.insertText(text)
        else:
            self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        self.dirty_changed.emit(False)
        c = self.editor.textCursor()
        c.setPosition(min(cursor, len(text)))
        self.editor.setTextCursor(c)
        self.editor.verticalScrollBar().setValue(scroll)

    # ----------------------------------------------------------- display
    def set_font(self, font: QFont):
        self.editor.setFont(font)
        self.editor.set_indentation(self.editor.indent_width, self.editor.use_tabs)  # (tab width in the new font)
        self.editor._update_gutter_width()

    def refresh_style(self):
        """Apply the syntax colors (View → Syntax Colors, or automatic)."""
        apply_style(self.editor, self.highlighter)
        self.editor._update_extra_selections()
        self.editor.gutter.update()
        if getattr(self, "minimap", None) is not None:
            self.minimap.invalidate()

    def _update_status(self, note: str = ""):
        cursor = self.editor.textCursor()
        indent = f"Tabs: {self.editor.indent_width}" if self.editor.use_tabs else f"Spaces: {self.editor.indent_width}"
        parts = [f"Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}",
                 indent, self.highlighter.lexer.name, self.encoding.upper(),
                 ("CRLF" if self.newline == "\r\n" else "LF") + (" (mixed)" if self.mixed_newlines else "")]
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
        if self._lsp_markers:
            count = sum(len(ns) for ns in self._lsp_markers.values())
            parts.append(f"{count} fortls issue{'s' * (count != 1)}")
        if self._spelling_issues:
            count = len(self._spelling_issues)
            parts.append(f"{count} misspelled word{'s' * (count != 1)}")
        if self.editor.gremlins:
            count = sum(len(g) for g in self.editor.gremlins.values())
            parts.append(f"{count} gremlin{'s' * (count != 1)}")
        if self.editor.extra_cursors:
            parts.insert(1, f"{len(self.editor.extra_cursors) + 1} cursors (Esc: back to one)")
        if note:
            parts.append(note)
        self.status.setText("<span style='color:gray'>" + " · ".join(parts) + "</span>")
        kind = "CRLF" if self.newline == "\r\n" else "LF"
        self.status.setToolTip(
            f"This file has more than one kind of line ending: saving makes them all {kind}, the most "
            "common one (Edit → Convert Line Endings to choose)" if self.mixed_newlines else "")

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
        for source in (self._compiler_markers, self._review_markers, self._lint_markers, self._lsp_markers):
            for line, notes in source.items():
                merged.setdefault(line, []).extend(notes)
        self.editor.set_markers(merged)
        self._update_status()
        self.problems_changed.emit()

    def problems(self) -> list[Problem]:
        """Everything flagged in the file, in line order: the last build's
        compiler messages, the AI review's findings, and the underlined
        issues (fortitude, fortls, spelling)."""
        found = []
        for markers, source in ((self._compiler_markers, "build"), (self._review_markers, "review")):
            for line, notes in markers.items():
                for severity, message in notes:
                    found.append(Problem(line, 0, line, 0, severity, severity, message,
                                         MARKER_COLORS.get(severity, "#868e96"), source))
        for line_gremlins in self.editor.gremlins.values():
            for g in line_gremlins:
                found.append(Problem(g.line, g.column, g.line, g.column + 1, "gremlin", g.code, g.message,
                                     gremlins_mod.COLOR, "gremlins"))
        for issue in self.editor.lint_issues:
            found.append(Problem(issue.line, max(0, issue.column - 1), issue.end_line, max(0, issue.end_column - 1),
                                 issue.severity, issue.code, issue.message, MARKER_COLORS[issue.severity],
                                 PROBLEM_SOURCES.get(issue.severity, "fortitude")))
        found.sort(key=lambda p: (p.line, p.column))
        return found

    def _explain_line(self, line: int):
        """Explain Error with AI on a line: its most serious compiler, fortitude
        or fortls problem."""
        rank = {"fatal error": 0, "error": 1, "lint error": 2, "fortls error": 3, "warning": 4}
        found = [p for p in self.problems() if p.line == line and p.source in ("build", "fortitude", "fortls")]
        if found:
            self.explain_requested.emit(min(found, key=lambda p: rank.get(p.severity, 9)))

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


# View → Editor Font (set by the main window): a family and a size in points
# at 100% text size; empty / 0 use the system's fixed-width font.
editor_font_family = ""
editor_font_size = 0.0


def monospace_font(size_factor: float = 1.0, editor: bool = False) -> QFont:
    """The fixed-width font, scaled by ``size_factor`` (View → text size);
    with ``editor``, the one chosen in View → Editor Font."""
    font = QFontDatabase.systemFont(QFontDatabase.FixedFont)
    if editor and editor_font_family:
        font.setFamily(editor_font_family)
        font.setStyleHint(QFont.Monospace)
    if editor and editor_font_size > 0:
        font.setPointSizeF(editor_font_size)
    font.setPointSizeF(font.pointSizeF() * size_factor)
    return font

