"""What the editor's modules share: colors, delays, severities, and text helpers
(no widgets, so any module can import it: the minimap, the panels…)."""

from __future__ import annotations

import difflib
import re

from qtpy.QtCore import Qt
from qtpy.QtGui import QColor

from . import gremlins as gremlins_mod

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

COVERAGE_COLORS = {"ran": "#2f9e44", "partial": "#f08c00", "missed": "#e03131"}  # (gutter bars, minimap)

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


DEPTH_ROLE = Qt.UserRole + 7  # outline entries: how deeply nested
