"""Edit → Toggle Line Comment, Lines (Move Up / Down, Sort) and Change Case."""

from __future__ import annotations

from conftest import open_editor, write_files
from qtpy.QtCore import Qt
from qtpy.QtGui import QTextCursor
from qtpy.QtTest import QTest

from vibefpmgui import line_edits as le


# ------------------------------------------------------------ the text
def test_comment_free_form_fortran():
    lines = ["  if (x > 0) then", "", "    x = 1", "  end if"]
    commented = le.toggle_comment(lines, "!")
    assert commented == ["  ! if (x > 0) then", "", "  !   x = 1", "  ! end if"]  # (at the least indent)
    assert le.toggle_comment(commented, "!") == lines  # (all commented: back)
    some = ["  ! a note", "  x = 1"]
    assert le.toggle_comment(some, "!") == ["  ! ! a note", "  ! x = 1"]  # (not all: all commented)
    assert le.toggle_comment(["!no space"], "!") == ["no space"]


def test_a_directive_is_code_not_a_comment():
    lines = ["!$omp parallel do", "do i = 1, n"]
    assert le.toggle_comment(lines, "!") == ["! !$omp parallel do", "! do i = 1, n"]
    assert le.toggle_comment(["!$omp end parallel do"], "!") == ["! !$omp end parallel do"]


def test_comment_fixed_form_in_the_first_column():
    lines = ["      X = 1 +", "     &    2", "   10 CONTINUE"]
    commented = le.toggle_comment(lines, "!", fixed=True)
    assert commented == ["!      X = 1 +", "!     &    2", "!   10 CONTINUE"]  # (column 6 untouched)
    assert le.toggle_comment(commented, "!", fixed=True) == lines


def test_other_files_comments():
    assert le.comment_style("fpm.toml") == ("#", False) and le.comment_style("a.c") == ("//", False)
    assert le.comment_style("x.f90") == ("!", False) and le.comment_style("x.F") == ("!", True)
    assert le.comment_style("Makefile") == ("#", False) and le.comment_style("README.md") == ("", False)
    assert le.toggle_comment(['name = "x"'], "#") == ['# name = "x"']
    assert le.toggle_comment(["a"], "") == ["a"]  # (no line comment: nothing)


def test_sort_and_case():
    assert le.sort_lines(["use b", "use A", "use c"]) == ["use A", "use b", "use c"]  # (ignoring case)
    assert le.sort_lines(["b", "a", "C"], descending=True) == ["C", "b", "a"]
    assert le.change_case("Real :: X_Val", le.UPPER) == "REAL :: X_VAL"
    assert le.change_case("REAL :: X", le.LOWER) == "real :: x"
    assert le.change_case("hello wORLD_two", le.TITLE) == "Hello World_Two"
    assert le.change_case("straße", le.UPPER) == "STRASSE"


# ------------------------------------------------------------ in the editor
TEXT = "program p\n  x = 1\n  y = 2\n  z = 3\nend program p\n"


def select_lines(editor, first: int, last: int):
    code = editor.editor
    doc = code.document()
    cursor = QTextCursor(doc.findBlockByNumber(first))
    end = doc.findBlockByNumber(last)
    cursor.setPosition(end.position() + end.length() - 1, QTextCursor.KeepAnchor)
    code.setTextCursor(cursor)


def test_the_edit_menu_entries(window, project):
    write_files(project, {"src/le.f90": TEXT})
    editor = open_editor(window, project / "src/le.f90")
    code = editor.editor
    window._update_editor_actions()
    assert window.comment_action.isEnabled() and window.lines_menu.isEnabled() and window.case_menu.isEnabled()
    select_lines(editor, 1, 2)
    window.comment_action.trigger()
    assert code.toPlainText() == "program p\n  ! x = 1\n  ! y = 2\n  z = 3\nend program p\n"
    code.undo()  # (one step)
    assert code.toPlainText() == TEXT
    select_lines(editor, 1, 2)
    window.move_down_action.trigger()
    assert code.toPlainText() == "program p\n  z = 3\n  x = 1\n  y = 2\nend program p\n"
    assert code.textCursor().selectedText() == "  x = 1   y = 2"  # (the selection went with them)
    window.move_up_action.trigger()
    assert code.toPlainText() == TEXT
    select_lines(editor, 1, 3)
    window.sort_down_action.trigger()
    assert code.toPlainText() == "program p\n  z = 3\n  y = 2\n  x = 1\nend program p\n"
    code.undo()
    cursor = code.textCursor()
    cursor.setPosition(2)  # (in "program", no selection: the word)
    code.setTextCursor(cursor)
    next(a for a in window.case_menu.actions() if a.text() == "UPPER CASE").trigger()
    assert code.toPlainText().startswith("PROGRAM p\n")


def test_alt_up_and_down_in_the_editor(window, project):
    write_files(project, {"src/le.f90": TEXT})
    editor = open_editor(window, project / "src/le.f90")
    code = editor.editor
    code.setTextCursor(QTextCursor(code.document().findBlockByNumber(3)))  # ("  z = 3")
    QTest.keyClick(code, Qt.Key_Up, Qt.AltModifier)
    QTest.keyClick(code, Qt.Key_Up, Qt.AltModifier)
    assert code.toPlainText() == "program p\n  z = 3\n  x = 1\n  y = 2\nend program p\n"
    assert code.textCursor().blockNumber() == 1  # (the cursor went with it)
    for _ in range(9):  # (at the end: no further)
        QTest.keyClick(code, Qt.Key_Down, Qt.AltModifier)
    assert code.toPlainText().splitlines()[-1] == "  z = 3"


def test_comment_needs_a_file_with_line_comments(window, project):
    write_files(project, {"notes.md": "a\nb\n", "src/fixed.f": "      X = 1\n"})
    open_editor(window, project / "notes.md")
    window._update_editor_actions()
    assert not window.comment_action.isEnabled()
    fixed = open_editor(window, project / "src/fixed.f")
    window._update_editor_actions()
    assert window.comment_action.isEnabled()
    window.comment_action.trigger()
    assert fixed.editor.toPlainText() == "!      X = 1\n"  # (fixed form: the first column)


def test_comment_with_several_cursors(window, project):
    write_files(project, {"src/le.f90": TEXT})
    editor = open_editor(window, project / "src/le.f90")
    code = editor.editor
    code.setTextCursor(QTextCursor(code.document().findBlockByNumber(1)))
    code.toggle_cursor_at(code.document().findBlockByNumber(3).position())
    window.comment_action.trigger()
    assert code.toPlainText() == "program p\n  ! x = 1\n  y = 2\n  ! z = 3\nend program p\n"


def test_undo_keeps_the_cursor_where_it_was(qtbot):
    """(Each edit began with a cursor at the document's start, and Undo put the cursor
    back there: at the top of the file.)"""
    from vibefpmgui.code_editor import CodeEditor

    text = "".join(f"line {i} = {i}\n" for i in range(40))
    commands = {"case": lambda c: c.change_selection_case(le.UPPER),
                "sort": lambda c: c.sort_selected_lines(descending=True),
                "comment": lambda c: c.toggle_comment(),
                "move": lambda c: c.move_lines(down=True)}
    for name, command in commands.items():
        code = CodeEditor()
        qtbot.addWidget(code)
        code.setPlainText(text)
        doc = code.document()
        cursor = QTextCursor(doc.findBlockByNumber(30))
        cursor.setPosition(cursor.position() + 2)
        if name == "sort":
            end = doc.findBlockByNumber(32)
            cursor.setPosition(end.position() + end.length() - 1, QTextCursor.KeepAnchor)
        code.setTextCursor(cursor)
        line = code.textCursor().blockNumber()
        command(code)
        assert code.toPlainText() != text, name
        code.undo()
        assert code.toPlainText() == text and code.textCursor().blockNumber() == line, name
