"""Edit menu commands on lines and selections: Toggle Line Comment, Move
Line Up / Down, Sort Lines, Change Case.

The text work is in plain functions (no Qt: `toggle_comment`, `sort_lines`,
`change_case`); `LineEditsMixin` (a part of `vibefpmgui.code_editor.CodeEditor`)
applies them to the document, each as one step to undo.
"""

from __future__ import annotations

import re
from pathlib import Path

from qtpy.QtGui import QTextCursor

from .complexity import FIXED_FORM, FORTRAN

# A file's line comment, by its suffix or name ("": none, e.g. Markdown's).
HASH_COMMENTS = {".toml", ".py", ".sh", ".bash", ".zsh", ".yaml", ".yml", ".cmake", ".mk", ".cfg", ".ini",
                 ".r", ".pl", ".rb", ".nml"}
SLASH_COMMENTS = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".js", ".ts", ".java", ".cs", ".go", ".rs",
                  ".swift", ".kt", ".cu"}
HASH_NAMES = {"makefile", "gnumakefile", "cmakelists.txt", "dockerfile", ".gitignore"}


def comment_style(path: Path | str) -> tuple[str, bool]:
    """(the line comment for this file, whether it's fixed-form Fortran):
    "!" for Fortran, "#" for TOML, Python, shell…, "//" for C and the like,
    "" for a file without a line comment."""
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in FORTRAN or suffix in (".inc", ".fi", ".fypp"):
        return "!", suffix in FIXED_FORM
    if suffix in HASH_COMMENTS or path.name.lower() in HASH_NAMES:
        return "#", False
    if suffix in SLASH_COMMENTS:
        return "//", False
    return "", False


def _commented(line: str, prefix: str, fixed: bool) -> bool:
    if fixed:
        return line.startswith(prefix)
    rest = line.lstrip()
    # (A directive, as "!$omp parallel", is code, not a comment.)
    return rest.startswith(prefix) and not (prefix == "!" and rest.startswith("!$"))


def toggle_comment(lines: list[str], prefix: str, fixed: bool = False) -> list[str]:
    """The lines commented out, or, if every one with code already is, back
    as they were. A comment goes at the lines' least indentation ("! x = 1"),
    or, in fixed form, in the first column ("!" there is a comment; at the
    sixth it would make the line a continuation). Blank lines stay blank."""
    code = [line for line in lines if line.strip()]
    if not prefix or not code:
        return list(lines)
    if all(_commented(line, prefix, fixed) for line in code):
        out = []
        for line in lines:
            if not line.strip() or not _commented(line, prefix, fixed):
                out.append(line)
            elif fixed:
                out.append(line[len(prefix):])
            else:
                start = len(line) - len(line.lstrip())
                rest = line[start + len(prefix):]
                out.append(line[:start] + (rest.removeprefix(" ")))
        return out
    if fixed:
        return [prefix + line if line.strip() else line for line in lines]
    indent = min(len(line) - len(line.lstrip()) for line in code)
    return [line[:indent] + prefix + " " + line[indent:] if line.strip() else line for line in lines]


def sort_lines(lines: list[str], descending: bool = False) -> list[str]:
    """The lines in order, ignoring case (and, where only case differs, as given)."""
    return sorted(lines, key=lambda line: (line.casefold(), line), reverse=descending)


UPPER, LOWER, TITLE = "upper", "lower", "title"


def change_case(text: str, case: str) -> str:
    """UPPER CASE, lower case, or Title Case (each word's first letter, the rest lower)."""
    if case == UPPER:
        return text.upper()
    if case == LOWER:
        return text.lower()
    return re.sub(r"[^\W_]+", lambda m: m.group()[:1].upper() + m.group()[1:].lower(), text)


def _qt_length(text: str) -> int:
    """Its length in the document's positions (UTF-16: a character beyond U+FFFF counts twice)."""
    return len(text) if text.isascii() else len(text.encode("utf-16-le")) // 2


class LineEditsMixin:
    """Toggle Line Comment, Move Line Up / Down, Sort Lines and Change Case
    (a part of `vibefpmgui.code_editor.CodeEditor`)."""

    comment_prefix = "!"   # this file's line comment ("" for none: see comment_style)
    fixed_form = False     # fixed-form Fortran (its comments go in the first column)

    def _edit_cursor(self) -> QTextCursor:
        """A cursor for an edit, where the user's is: an Undo puts the cursor
        back where its edit began (one at the document's start sent it to
        the top of the file)."""
        return QTextCursor(self.textCursor())

    def _line_span(self, cursor: QTextCursor) -> tuple[int, int]:
        """The lines (0-based, first and last) a cursor's selection is on: not
        the last if the selection only reaches its start."""
        doc = self.document()
        start, end = sorted((cursor.anchor(), cursor.position()))
        first = doc.findBlock(start).blockNumber()
        last_block = doc.findBlock(end)
        last = last_block.blockNumber()
        if last > first and end == last_block.position():
            last -= 1
        return first, last

    def _lines(self, first: int, last: int) -> list[str]:
        doc = self.document()
        return [doc.findBlockByNumber(n).text() for n in range(first, last + 1)]

    def _replace_lines(self, cursor: QTextCursor, first: int, last: int, lines: list[str]):
        """Lines ``first`` to ``last`` become ``lines`` (as many), with ``cursor``."""
        doc = self.document()
        end = doc.findBlockByNumber(last)
        cursor.setPosition(doc.findBlockByNumber(first).position())
        cursor.setPosition(end.position() + end.length() - 1, QTextCursor.KeepAnchor)
        cursor.insertText("\n".join(lines))

    def toggle_comment(self):
        """Edit → Toggle Line Comment: the lines of the selection (and of the
        other cursors'), commented out or back."""
        if not self.comment_prefix:
            return
        spans = sorted({self._line_span(c) for c in [self.textCursor(), *self.extra_cursors]})
        lines = sorted({n for first, last in spans for n in range(first, last + 1)})
        if not lines:
            return
        all_lines = self._lines(lines[0], lines[-1])
        chosen = [all_lines[n - lines[0]] for n in lines]
        changed = toggle_comment(chosen, self.comment_prefix, self.fixed_form)
        if changed == chosen:
            return
        new = list(all_lines)
        for n, line in zip(lines, changed, strict=True):
            new[n - lines[0]] = line
        main = self.textCursor()
        line, column = main.blockNumber(), main.positionInBlock()
        anchor_block = self.document().findBlock(main.anchor())
        anchor_line, anchor_column = anchor_block.blockNumber(), main.anchor() - anchor_block.position()
        cursor = self._edit_cursor()
        cursor.beginEditBlock()
        self._replace_lines(cursor, lines[0], lines[-1], new)
        cursor.endEditBlock()
        # The selection stays on the same lines (its columns moved by what was put in or taken out).
        self._select_columns(anchor_line, anchor_column, line, column, all_lines, new, lines[0])

    def _select_columns(self, anchor_line, anchor_column, line, column, old, new, first):
        def moved(n: int, col: int) -> int:
            if not first <= n < first + len(old):
                return col
            before, after = old[n - first], new[n - first]
            shift = _qt_length(after) - _qt_length(before)
            indent = _qt_length(before) - _qt_length(before.lstrip())
            return max(0, col + shift) if col >= indent or self.fixed_form else col

        doc = self.document()
        cursor = QTextCursor(doc)
        block = doc.findBlockByNumber(anchor_line)
        cursor.setPosition(block.position() + min(moved(anchor_line, anchor_column), block.length() - 1))
        block = doc.findBlockByNumber(line)
        cursor.setPosition(block.position() + min(moved(line, column), block.length() - 1), QTextCursor.KeepAnchor)
        self.setTextCursor(cursor)

    def move_lines(self, down: bool):
        """Edit → Lines → Move Line Up / Down: the selection's lines swap
        places with the line above or below, the selection going with them."""
        doc = self.document()
        main = self.textCursor()
        first, last = self._line_span(main)
        if (not down and first == 0) or (down and last >= doc.blockCount() - 1):
            return
        lines = self._lines(first, last)
        if down:
            neighbour = doc.findBlockByNumber(last + 1)
            new, span, shift = [neighbour.text(), *lines], (first, last + 1), neighbour.length()
        else:
            neighbour = doc.findBlockByNumber(first - 1)
            new, span, shift = [*lines, neighbour.text()], (first - 1, last), -neighbour.length()
        anchor, position = main.anchor(), main.position()
        cursor = self._edit_cursor()
        cursor.beginEditBlock()
        self._replace_lines(cursor, *span, new)
        cursor.endEditBlock()
        moved = QTextCursor(doc)
        moved.setPosition(anchor + shift)
        moved.setPosition(position + shift, QTextCursor.KeepAnchor)
        self.setTextCursor(moved)

    def sort_selected_lines(self, descending: bool = False):
        """Edit → Lines → Sort Lines: the selection's lines (two or more) in order."""
        first, last = self._line_span(self.textCursor())
        if last <= first:
            return
        lines = self._lines(first, last)
        ordered = sort_lines(lines, descending)
        if ordered == lines:
            return
        cursor = self._edit_cursor()
        cursor.beginEditBlock()
        self._replace_lines(cursor, first, last, ordered)
        cursor.endEditBlock()
        doc = self.document()
        end = doc.findBlockByNumber(last)
        selection = QTextCursor(doc)
        selection.setPosition(doc.findBlockByNumber(first).position())
        selection.setPosition(end.position() + end.length() - 1, QTextCursor.KeepAnchor)
        self.setTextCursor(selection)

    def change_selection_case(self, case: str):
        """Edit → Change Case: each selection (the main one and the other
        cursors'), or the word at a cursor without one."""
        cursors = [self.textCursor(), *self.extra_cursors]
        edit = self._edit_cursor()
        edit.beginEditBlock()
        for c in cursors:
            target = QTextCursor(c)
            if not target.hasSelection():
                target.select(QTextCursor.WordUnderCursor)
            text = target.selectedText()
            new = change_case(text, case)
            if text and new != text:
                selected = c.hasSelection()
                start = target.selectionStart()
                target.insertText(new)
                if selected:  # (still selected; its length may change, as "ß" → "SS")
                    c.setPosition(start)
                    c.setPosition(start + _qt_length(new), QTextCursor.KeepAnchor)
        edit.endEditBlock()
        self.setTextCursor(cursors[0])
