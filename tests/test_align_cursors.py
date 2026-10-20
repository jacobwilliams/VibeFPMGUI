"""Edit → Lines → Align Cursors: spaces before each cursor left of the
right-most one, so all the cursors are in its column."""

from __future__ import annotations

import pytest
from conftest import open_editor, write_files
from qtpy.QtGui import QTextCursor

TEXT = "x = 1\nlonger_name = 2\nn = 3 ! three\ni = 4\n"


@pytest.fixture
def editor(window, project):
    write_files(project, {"src/vars.f90": TEXT})
    return open_editor(window, project / "src/vars.f90")


def place(editor, *spots, select: int = 0):
    """Cursors at these (line, column)s (0-based; the first: the main one), each
    with ``select`` characters selected after it."""
    code = editor.editor

    def at(line, column):
        cursor = QTextCursor(code.document().findBlockByNumber(line))
        cursor.setPosition(cursor.block().position() + column)
        if select:
            cursor.setPosition(cursor.position() + select, QTextCursor.KeepAnchor)
        return cursor

    code.setTextCursor(at(*spots[0]))
    code.extra_cursors = [at(*spot) for spot in spots[1:]]
    code._cursors_changed()


def lines(editor) -> list[str]:
    return editor.editor.toPlainText().split("\n")


def columns(editor) -> list[tuple[int, int]]:
    return [(c.blockNumber(), c.positionInBlock()) for c in editor.editor.all_cursors()]


def test_the_cursors_line_up_with_the_right_most(editor):
    place(editor, (0, 2), (1, 12), (2, 2))  # (before each "=")
    assert editor.editor.align_cursors()
    assert lines(editor)[:3] == ["x           = 1", "longer_name = 2", "n           = 3 ! three"]
    assert columns(editor) == [(0, 12), (1, 12), (2, 12)]  # (the cursors: after the spaces, aligned)


def test_one_undo_step(editor):
    place(editor, (0, 2), (1, 12), (3, 2))
    editor.editor.align_cursors()
    editor.editor.undo()
    assert editor.editor.toPlainText() == TEXT


def test_several_on_a_line_align_in_turn(editor):
    """The first on each line together, then the second (an "=" and a "!" comment)."""
    place(editor, (0, 2), (0, 5), (2, 2), (2, 6))  # ("x = 1|" with "n = 3 |! three")
    editor.editor.align_cursors()
    assert lines(editor)[0] == "x = 1 " and lines(editor)[2] == "n = 3 ! three"
    assert [c for _line, c in columns(editor)] == [2, 6, 2, 6]


def test_a_selection_moves_along_by_its_left_edge(editor):
    place(editor, (0, 2), (1, 12), select=1)  # (each "=" selected)
    editor.editor.align_cursors()
    assert lines(editor)[:2] == ["x           = 1", "longer_name = 2"]
    assert [c.selectedText() for c in editor.editor.all_cursors()] == ["=", "="]  # (still selected, not replaced)


def test_tabs_count_as_their_width(editor):
    code = editor.editor
    code.setPlainText("\tx = 1\nabcdefghij = 2\n")
    tab = round(code.tabStopDistance() / code.fontMetrics().horizontalAdvance(" "))
    place(editor, (0, 3), (1, 11))  # (after the tab and "x ", and after "abcdefghij ")
    code.align_cursors()
    first, second = (code.visual_column(c) for c in code.all_cursors())
    assert first == second == max(tab + 2, 11)


def test_from_the_menu_and_with_one_cursor(window, editor):
    place(editor, (0, 2), (1, 12))
    window.align_cursors_action.trigger()
    assert lines(editor)[0] == "x           = 1"
    place(editor, (3, 2))  # (one cursor: nothing to align)
    window.align_cursors_action.trigger()
    assert lines(editor)[3] == "i = 4" and "needs two or more cursors" in window.statusBar().currentMessage()
