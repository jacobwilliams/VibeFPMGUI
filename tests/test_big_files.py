"""Big files: incremental recoloring, lexing on a thread, change bars."""

from __future__ import annotations

import random

import pytest
from pygments.lexers import FortranLexer
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import QPlainTextEdit

from vibefpmgui import highlighter as hl
from vibefpmgui.editor import line_changes

SOURCE = "".join(
    f"! routine {i}\nsubroutine s{i}(x)\n  real :: x  ! 'not a string'\n  x = x + {i}.0\n"
    f"  print *, \"value {i}\"\nend subroutine s{i}\n" for i in range(60))


def formats(edit: QPlainTextEdit) -> list:
    """Every line's colors: (start, length, color) per format range."""
    out, block = [], edit.document().firstBlock()
    while block.isValid():
        out.append([(r.start, r.length, r.format.foreground().color().name(), r.format.fontItalic())
                    for r in block.layout().formats()])
        block = block.next()
    return out


@pytest.fixture
def edit(qtbot):
    widget = QPlainTextEdit()
    qtbot.addWidget(widget)
    widget.setPlainText(SOURCE)
    widget.highlighter = hl.PygmentsHighlighter(widget.document(), FortranLexer(), "default")
    qtbot.waitUntil(lambda: not widget.highlighter._full, timeout=5_000)
    return widget


def recolor_everything(qtbot, edit, style="default") -> list:
    """The colors a fresh highlighter gives the same text."""
    fresh = QPlainTextEdit()
    qtbot.addWidget(fresh)
    fresh.setPlainText(edit.toPlainText())
    fresh.highlighter = hl.PygmentsHighlighter(fresh.document(), FortranLexer(), style)
    qtbot.waitUntil(lambda: not fresh.highlighter._full, timeout=5_000)
    return formats(fresh)


def settle(qtbot, edit):
    edit.highlighter._relex()  # (instead of waiting for the typing pause)
    qtbot.waitUntil(lambda: not edit.highlighter._lexing and edit.highlighter._text == edit.toPlainText(),
                    timeout=10_000)


@pytest.mark.parametrize("change", [
    ("insert", 200, "x"), ("insert", 210, "'"), ("insert", 300, "! a comment\n"),
    ("delete", 150, 40), ("insert", 0, "\"open string\n"), ("insert", len(SOURCE), "\nend\n"),
])
def test_recoloring_only_the_changes_gives_the_same_colors(qtbot, edit, change):
    cursor = QTextCursor(edit.document())
    if change[0] == "insert":
        cursor.setPosition(change[1])
        cursor.insertText(change[2])
    else:
        cursor.setPosition(change[1])
        cursor.setPosition(change[1] + change[2], QTextCursor.KeepAnchor)
        cursor.removeSelectedText()
    settle(qtbot, edit)
    assert formats(edit) == recolor_everything(qtbot, edit)


def test_random_edits_stay_right(qtbot, edit):
    rng = random.Random(7)
    for _ in range(15):
        cursor = QTextCursor(edit.document())
        cursor.setPosition(rng.randrange(len(edit.toPlainText())))
        cursor.insertText(rng.choice(["a", "'", "\"", "!", "\n", "end ", "x = 1\n"]))
        settle(qtbot, edit)
    assert formats(edit) == recolor_everything(qtbot, edit)


def test_a_small_edit_recolors_few_lines(qtbot, edit, monkeypatch):
    recolored = []
    real = edit.highlighter.rehighlightBlock
    monkeypatch.setattr(edit.highlighter, "rehighlightBlock", lambda b: recolored.append(b.blockNumber()) or real(b))
    cursor = QTextCursor(edit.document().findBlockByNumber(100))
    cursor.insertText("y")
    settle(qtbot, edit)
    assert 0 < len(recolored) <= 3


def test_big_documents_are_lexed_on_a_thread(qtbot, edit, monkeypatch):
    monkeypatch.setattr(hl, "LEX_IN_THREAD", 100)  # (this one counts as big)
    threads = []
    import threading
    real_start = threading.Thread.start
    monkeypatch.setattr(threading.Thread, "start", lambda self: threads.append(self.name) or real_start(self))
    cursor = QTextCursor(edit.document())
    cursor.insertText("'")
    settle(qtbot, edit)
    assert "Syntax highlighting" in threads
    assert formats(edit) == recolor_everything(qtbot, edit)


def test_a_style_change_while_lexing_is_not_lost(qtbot, edit, monkeypatch):
    monkeypatch.setattr(hl, "LEX_IN_THREAD", 100)
    cursor = QTextCursor(edit.document())
    cursor.insertText("x")
    edit.highlighter._relex()  # (lexing on the thread…)
    edit.highlighter.set_style("monokai")  # (…when the style changes)
    qtbot.waitUntil(lambda: not edit.highlighter._lexing and not edit.highlighter._full, timeout=10_000)
    assert formats(edit) == recolor_everything(qtbot, edit, "monokai")  # (the new style's colors)


def test_common_prefix_and_suffix():
    a = "x" * 10_000 + "abc" + "y" * 9_000
    b = "x" * 10_000 + "aXc" + "y" * 9_000
    p = hl._common_prefix(a, b)
    assert p == 10_001
    assert hl._common_suffix(a, b, min(len(a), len(b)) - p) == 9_001
    assert hl._common_prefix("abc", "abc") == 3 and hl._common_prefix("", "a") == 0
    assert hl._common_suffix("abc", "zbc", 3) == 2


def test_change_bars_on_a_big_file():
    lines = [f"line {i}" for i in range(30_000)]
    opened = "\n".join(lines)
    edited = list(lines)
    edited[15_000] = "changed"
    del edited[20_000:20_002]
    edited.insert(25_000, "added")
    marks, deletions = line_changes(opened, opened, "\n".join(edited))
    assert marks == {15_001: "unsaved", 25_001: "unsaved"}
    assert deletions == {20_001: ("unsaved", 2)}
    # The same as diffing everything (without skipping the equal start and end).
    import difflib
    whole = difflib.SequenceMatcher(None, lines, edited, autojunk=False).get_opcodes()
    changed = {j + 1 for tag, _i1, _i2, j1, j2 in whole if tag in ("replace", "insert") for j in range(j1, j2)}
    assert set(marks) == changed


def line_colors(edit, line: int) -> set[str]:
    block = edit.document().findBlockByNumber(line)
    return {r.format.foreground().color().name() for r in block.layout().formats()}


def test_typing_is_colored_at_once(qtbot, edit):
    comment = line_colors(edit, 0)  # ("! routine 0": a comment)
    code_line = 2  # ("  real :: x  ! ...")
    assert line_colors(edit, code_line) != comment
    cursor = QTextCursor(edit.document().findBlockByNumber(code_line))
    cursor.insertText("!")  # (the whole line is a comment now)
    assert edit.highlighter._text != edit.toPlainText()  # (not lexed again yet…)
    assert line_colors(edit, code_line) == comment       # (…but colored already)
    settle(qtbot, edit)
    assert formats(edit) == recolor_everything(qtbot, edit)  # (and right once it is)


def test_replacing_everything_colors_few_lines_on_their_own(qtbot, edit, monkeypatch):
    quick = []
    real = edit.highlighter._quick_line
    monkeypatch.setattr(edit.highlighter, "_quick_line", lambda text: quick.append(text) or real(text))
    cursor = QTextCursor(edit.document())
    cursor.select(QTextCursor.Document)
    cursor.insertText(SOURCE.upper())
    assert sum(1 for _ in quick) >= hl.QUICK_LINES  # (asked for every line…)
    assert edit.highlighter._quick_budget == 0       # (…but only QUICK_LINES were lexed on their own)
    settle(qtbot, edit)
    assert formats(edit) == recolor_everything(qtbot, edit)
