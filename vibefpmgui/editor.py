"""A small built-in code editor: line numbers, syntax highlighting (Pygments,
chosen by file extension), find/replace, go to line, and compiler warning /
error markers in the gutter, fortitude lint findings (see `vibefpmgui.lint`), and
for Fortran files the language server's hover, go to definition and outline
(see `vibefpmgui.lsp`).

`EditorTab` wraps a `CodeEditor` (`vibefpmgui.code_editor`) with a find bar
(`vibefpmgui.find_bar`), the outline and a status line, and takes care of
loading and saving the file (keeping its encoding and line endings) and of
noticing when it changes on disk. Its language-server features are in
`vibefpmgui.editor_language`, its checks (fortitude, spelling, gremlins, the
compiler's and the AI's markers) in `vibefpmgui.editor_checks`.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from qtpy.QtCore import QFileSystemWatcher, QProcess, QRect, QSize, Qt, QTimer, Signal
from qtpy.QtGui import QFont, QFontDatabase, QPalette, QTextCursor
from qtpy.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from . import lsp as lsp_mod
from .code_editor import CodeEditor
from .editor_checks import EditorChecksMixin
from .editor_language import EditorLanguageMixin
from .editor_shared import (
    COMPLETE_MS,
    DEPTH_ROLE,
    LINT_DELAY_MS,
    LSP_SYNC_MS,
    SPELLING_DELAY_MS,
    line_changes,
)
from .find_bar import FindBar
from .highlighter import PygmentsHighlighter, apply_style, lexer_for_path
from .icons import SYMBOL_INDENT
from .minimap import Minimap
from .source_text import read_source
from .widgets import StatusLabel

# Files whose trailing whitespace means something (Markdown: a line break;
# a patch: a context line's), not trimmed on save (Edit → Trim Trailing
# Whitespace still trims them).
TRIM_KEEPS = {".md", ".markdown", ".diff", ".patch"}
def _qt_length(text: str) -> int:
    """``text``'s length as Qt counts it (UTF-16)."""
    return len(text) if text.isascii() else len(text.encode("utf-16-le")) // 2


TRAILING_SPACE_RE = re.compile(r"[^\S\n]+$", re.MULTILINE)  # (whitespace at a line's end, as str.rstrip strips)


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


class EditorTab(EditorLanguageMixin, EditorChecksMixin, QWidget):
    """One open file: editor + find bar + status line."""

    lint_enabled = True  # lint Fortran files with fortitude (View menu)
    lsp_diagnostics_enabled = True  # show fortls's own checks (View menu)
    spelling_enabled = True  # spell check comments and strings (View menu)
    gremlins_enabled = True  # View → Show Gremlins (non-ASCII and invisible characters)
    minimap_enabled = True  # View → Show Minimap
    word_wrap = False       # View → Word Wrap
    trim_on_save = True     # Edit → Trim Trailing Whitespace on Save

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
        self.highlighter.line_on_view = lambda: self.editor.firstVisibleBlock().blockNumber()  # (colored first)
        self.find_bar = FindBar(self.editor)
        # A change to the text: the last test run's coverage no longer fits its lines.
        self.editor.document().contentsChange.connect(
            lambda _pos, removed, added: (removed or added) and self.editor.coverage and self.editor.set_coverage(None))
        # The selection's other occurrences: matched as the find bar is set.
        self.editor.match_options = lambda: (self.find_bar.case_check.isChecked(),
                                             self.find_bar.word_check.isChecked())
        self.find_bar.case_check.toggled.connect(self.editor.update_occurrences)
        self.find_bar.word_check.toggled.connect(self.editor.update_occurrences)
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
        # (A child of the tab from the start: shown without a parent, even
        # until the layout below takes it, it was a window of its own for a
        # moment, which Windows showed as a window flashing up.)
        self.minimap = Minimap(self.editor, self)
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
        self.editor.cursorPositionChanged.connect(self.update_status)
        self.editor.cursors_changed.connect(self.update_status)
        self.load()
        self.refresh_style()
        self.editor.textChanged.connect(self._lint_timer.start)
        self.editor.textChanged.connect(self._changes_timer.start)
        self._changes_timer.timeout.connect(self.check_gremlins)  # (the same typing pause)
        self.editor.textChanged.connect(lambda: self.lsp is not None and self._lsp_timer.start())
        self.editor.cursorPositionChanged.connect(self._outline_follow_cursor)
        self.check_gremlins()
        self.lint()

    def close_down(self):
        """Before the tab is closed: stop background work."""
        self.stop_lint()
        self.detach_lsp()
        self.highlighter.close()  # (a big file may be being lexed on a thread)

    def update_change_marks(self):
        """The gutter's change bars: lines changed and not saved yet, and
        lines changed since the file was opened (and saved)."""
        self._changes_timer.stop()
        marks, deletions = line_changes(getattr(self, "_opened_text", self._disk_text), self._disk_text,
                                        self.editor.toPlainText())
        self.editor.set_change_marks(marks, deletions)

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
        return read_source(self.path)

    def load(self):
        text, self.encoding, self.newline, self.mixed_newlines = self._read()
        self._disk_newline = self.newline  # (a converted file is unsaved: see is_dirty)
        self._disk_mixed = self.mixed_newlines
        self._disk_text = text
        self._opened_text = text  # (the change bars compare with this and the saved text)
        self.editor.setPlainText(text)
        self.editor.document().setModified(False)
        self._watch()
        self.update_status()

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
        self.update_status()
        return True

    def trim_trailing_whitespace(self) -> int:
        """Edit → Trim Trailing Whitespace: the spaces and tabs at the ends of
        the lines removed (one step to undo); how many lines had them. The
        lines are found in the text at once (going through a big file's
        every line took a tenth of a second at every save)."""
        text = self.editor.toPlainText()
        lines, line, at = [], 0, 0
        for match in TRAILING_SPACE_RE.finditer(text):
            line += text.count("\n", at, match.start())
            at = match.start()
            lines.append(line)
        if not lines:
            return 0
        document = self.editor.document()
        cursor = QTextCursor(document)
        cursor.beginEditBlock()
        for number in lines:
            block = document.findBlockByNumber(number)
            text = block.text()
            # (Qt's positions count a character beyond U+FFFF as two.)
            cursor.setPosition(block.position() + _qt_length(text.rstrip()))
            cursor.setPosition(block.position() + _qt_length(text), QTextCursor.KeepAnchor)
            cursor.removeSelectedText()
        cursor.endEditBlock()
        return len(lines)

    def save(self) -> bool:
        if self.trim_on_save and self.path.suffix.lower() not in TRIM_KEEPS:
            self.trim_trailing_whitespace()
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
        self.update_status()

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
            self.update_status("deleted on disk")
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
        self.editor.update_gutter_width()

    def refresh_style(self):
        """Apply the syntax colors (View → Syntax Colors, or automatic)."""
        apply_style(self.editor, self.highlighter)
        self.editor.update_extra_selections()
        self.editor.gutter.update()
        if getattr(self, "minimap", None) is not None:
            self.minimap.invalidate()

    def update_status(self, note: str = ""):
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

