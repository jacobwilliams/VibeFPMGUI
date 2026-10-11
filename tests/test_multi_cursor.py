"""Multiple cursors in the editor: ⌥/Alt-click, ⌘⇧/Ctrl+Shift-click, and
typing, deleting, moving, copying and pasting at all of them."""

from __future__ import annotations

import pytest
from conftest import open_editor, write_files
from qtpy.QtCore import Qt
from qtpy.QtGui import QTextCursor
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication

TEXT = "alpha = 1\nbeta = 2\ngo\ndelta = 4\n"


@pytest.fixture
def editor(window, project):
    write_files(project, {"src/vars.f90": TEXT})
    return open_editor(window, project / "src/vars.f90")


def point(editor, line: int, column: int):
    """Where (line, column) is on screen, in the viewport (1-based line)."""
    code = editor.editor
    cursor = QTextCursor(code.document().findBlockByNumber(line - 1))
    cursor.setPosition(cursor.block().position() + column)
    return code.cursorRect(cursor).center()


def click(editor, line, column, modifiers=Qt.NoModifier):
    QTest.mouseClick(editor.editor.viewport(), Qt.LeftButton, modifiers, point(editor, line, column))


def place(editor, *spots):
    """The main cursor at the first (line, column), ⌥-click the others."""
    click(editor, *spots[0])
    for line, column in spots[1:]:
        click(editor, line, column, Qt.AltModifier)


def text(editor) -> str:
    return editor.editor.toPlainText()


COMMAND_SHIFT = Qt.ControlModifier | Qt.ShiftModifier  # (⌘⇧ on macOS: Qt calls ⌘ Control)


def test_alt_click_adds_and_removes_cursors(editor):
    place(editor, (1, 0), (2, 0), (4, 0))
    assert len(editor.editor.extra_cursors) == 2
    click(editor, 2, 0, Qt.AltModifier)  # (again: removes it)
    assert len(editor.editor.extra_cursors) == 1
    click(editor, 3, 1)  # (a plain click: one cursor)
    assert editor.editor.extra_cursors == []


def test_typing_and_deleting_at_every_cursor(editor):
    place(editor, (1, 0), (2, 0), (4, 0))
    QTest.keyClicks(editor.editor, "x_")
    assert text(editor) == "x_alpha = 1\nx_beta = 2\ngo\nx_delta = 4\n"
    QTest.keyClick(editor.editor, Qt.Key_Backspace)
    assert text(editor) == "xalpha = 1\nxbeta = 2\ngo\nxdelta = 4\n"
    QTest.keyClick(editor.editor, Qt.Key_Delete)
    assert text(editor) == "xlpha = 1\nxeta = 2\ngo\nxelta = 4\n"
    editor.editor.undo()  # (the Delete, at all three)
    assert text(editor) == "xalpha = 1\nxbeta = 2\ngo\nxdelta = 4\n"
    assert "3 cursors" in editor.status.text()


def test_column_of_cursors(editor):
    click(editor, 1, 5)
    click(editor, 4, 0, COMMAND_SHIFT)
    assert len(editor.editor.extra_cursors) == 3
    QTest.keyClicks(editor.editor, "|")
    # ("go" is shorter: its cursor is at the end of the line)
    assert text(editor) == "alpha| = 1\nbeta |= 2\ngo|\ndelta| = 4\n"


def test_column_upwards_and_moving(editor):
    click(editor, 4, 0)
    click(editor, 2, 0, COMMAND_SHIFT)
    assert len(editor.editor.extra_cursors) == 2
    QTest.keyClick(editor.editor, Qt.Key_End)
    QTest.keyClicks(editor.editor, ";")
    assert text(editor) == "alpha = 1\nbeta = 2;\ngo;\ndelta = 4;\n"
    QTest.keyClick(editor.editor, Qt.Key_Home)
    QTest.keyClick(editor.editor, Qt.Key_Right)
    QTest.keyClicks(editor.editor, "+")
    assert text(editor).splitlines()[1:] == ["b+eta = 2;", "g+o;", "d+elta = 4;"]


def test_shift_selects_and_typing_replaces(editor):
    place(editor, (1, 0), (2, 0))
    for _ in range(4):
        QTest.keyClick(editor.editor, Qt.Key_Right, Qt.ShiftModifier)
    assert editor.editor.textCursor().selectedText() == "alph"
    assert editor.editor.extra_cursors[0].selectedText() == "beta"
    QTest.keyClicks(editor.editor, "v")
    assert text(editor).splitlines()[:2] == ["va = 1", "v = 2"]


def test_enter_and_tab(editor):
    write = editor.editor
    write.setPlainText("  a\n  b\n")
    place(editor, (1, 3), (2, 3))
    QTest.keyClick(write, Qt.Key_Return)
    QTest.keyClicks(write, "x")
    assert write.toPlainText() == "  a\n  x\n  b\n  x\n"  # (each new line keeps its indentation)
    QTest.keyClick(write, Qt.Key_Tab)
    assert write.toPlainText() == "  a\n  x    \n  b\n  x    \n"  # (an indent at each cursor)


def test_copy_and_paste_one_line_per_cursor(editor):
    place(editor, (1, 0), (2, 0), (4, 0))
    for _ in range(4):
        QTest.keyClick(editor.editor, Qt.Key_Right, Qt.ShiftModifier)
    editor.editor.copy()
    assert QApplication.clipboard().text() == "alph\nbeta\ndelt"
    QTest.keyClick(editor.editor, Qt.Key_End)
    QTest.keyClicks(editor.editor, " ! ")
    editor.editor.paste()  # (three lines, three cursors: one each)
    assert text(editor) == "alpha = 1 ! alph\nbeta = 2 ! beta\ngo\ndelta = 4 ! delt\n"
    QApplication.clipboard().setText("same")
    editor.editor.paste()
    assert text(editor).splitlines()[0].endswith("alphsame")


def test_cut(editor):
    place(editor, (1, 0), (2, 0))
    for _ in range(5):
        QTest.keyClick(editor.editor, Qt.Key_Right, Qt.ShiftModifier)
    editor.editor.cut()
    assert QApplication.clipboard().text() == "alpha\nbeta "
    assert text(editor).splitlines()[:2] == [" = 1", "= 2"]


def test_escape_and_merging(editor):
    place(editor, (1, 1), (1, 2))
    QTest.keyClick(editor.editor, Qt.Key_Left)
    QTest.keyClick(editor.editor, Qt.Key_Left)  # (both at the start of the line: one cursor)
    assert editor.editor.extra_cursors == []
    place(editor, (1, 0), (2, 0))
    QTest.keyClick(editor.editor, Qt.Key_Escape)
    assert editor.editor.extra_cursors == [] and "cursors" not in editor.status.text()


def test_extra_cursors_are_drawn(editor):
    place(editor, (1, 0), (3, 1))
    assert not editor.editor.viewport().grab().isNull()
