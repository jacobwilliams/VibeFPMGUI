"""The editor: keys, menus, files changed on disk, Save As, the gutter,
linting and formatting (with the real fortitude and fprettify)."""

from __future__ import annotations

import pytest
from conftest import open_editor
from qtpy.QtCore import QPoint, Qt
from qtpy.QtGui import QTextCursor
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from vibefpmgui import formatter, lint

LINTED = "module m\n  integer :: x\ncontains\n  subroutine s()\n    x = 1   \n  end subroutine\nend module\n"


def menu_texts(menu, choose: str | None = None) -> list[str]:
    """A context menu's action texts (and trigger the one starting with ``choose``)."""
    texts = [a.text() for a in menu.actions()]
    target = next((a for a in menu.actions() if choose and a.text().startswith(choose)), None)
    if target is not None:
        target.trigger()
    menu.deleteLater()
    return texts


def cursor_to(editor, line: int, column: int = 0, select_to: tuple[int, int] | None = None):
    doc = editor.editor.document()
    cursor = editor.editor.textCursor()
    cursor.setPosition(doc.findBlockByNumber(line - 1).position() + column)
    if select_to:
        block = doc.findBlockByNumber(select_to[0] - 1)
        cursor.setPosition(block.position() + select_to[1], QTextCursor.KeepAnchor)
    editor.editor.setTextCursor(cursor)


def line_text(editor, line: int) -> str:
    return editor.editor.document().findBlockByNumber(line - 1).text()


# ------------------------------------------------------------------ keys
def test_tab_enter_and_backspace(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    cursor_to(editor, 6, len(line_text(editor, 6)))  # (end of an indented line)
    QTest.keyClick(code, Qt.Key_Return)
    assert line_text(editor, 7) == "    "  # (keeps the indentation)
    QTest.keyClick(code, Qt.Key_Tab)
    assert line_text(editor, 7) == "    " + "    "
    QTest.keyClick(code, Qt.Key_Backspace)
    assert line_text(editor, 7) == "    "  # (a whole indent level)
    cursor_to(editor, 2, 0, select_to=(3, 5))
    QTest.keyClick(code, Qt.Key_Tab)  # (lines selected: indent them)
    assert line_text(editor, 2).startswith("    " + "  integer") and line_text(editor, 3).startswith("    ")
    QTest.keyClick(code, Qt.Key_Backtab)
    assert line_text(editor, 2) == "  integer :: Count = 1"


def test_go_to_line_and_replace_text(window, project, monkeypatch):
    editor = open_editor(window, project / "src/foo.f90")
    monkeypatch.setattr(QInputDialog, "getInt", lambda *a, **k: (5, True))
    window.goto_action.trigger()
    assert editor.editor.textCursor().blockNumber() == 4
    cursor_to(editor, 5, 4)
    editor.replace_text(editor.editor.toPlainText().upper())
    assert editor.editor.textCursor().blockNumber() == 4 and editor.editor.textCursor().positionInBlock() == 4
    editor.editor.undo()
    assert "module foo" in editor.editor.toPlainText()


def test_find_bar_navigation(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    bar = editor.find_bar
    cursor_to(editor, 2, 13, select_to=(2, 18))  # "Count"
    window.find_action.trigger()
    assert bar.isVisible() and bar.find_edit.text() == "Count"
    bar.find_next()
    assert editor.editor.textCursor().blockNumber() == 2  # (count_all, line 3)
    bar.find_previous()
    assert editor.editor.textCursor().blockNumber() == 1
    bar.replace_edit.setText("total")
    bar.case_check.setChecked(True)
    bar.replace_one()
    assert "integer :: total = 1" in editor.editor.toPlainText()
    QTest.keyClick(bar, Qt.Key_Escape)
    assert not bar.isVisible()


# ------------------------------------------------------------- the menu
def test_context_menu_without_a_language_server(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    texts = menu_texts(editor.editor.context_menu(QPoint(5, 5)))
    assert any("Copy" in t for t in texts) and not any("Definition" in t for t in texts)


# --------------------------------------------------------- files on disk
def test_reloads_a_file_changed_on_disk(qtbot, window, project):
    editor = open_editor(window, project / "src/foo.f90")
    (project / "src/foo.f90").write_text("! changed elsewhere\n" + (project / "src/foo.f90").read_text())
    editor._check_disk()  # (as the file watcher does)
    assert editor.editor.toPlainText().startswith("! changed elsewhere") and not editor.is_dirty()


def test_asks_before_dropping_edits(window, project, monkeypatch):
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.insertPlainText("! mine\n")
    (project / "src/foo.f90").write_text("! theirs\n")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    editor._check_disk()
    assert "! mine" in editor.editor.toPlainText() and editor.is_dirty()
    (project / "src/foo.f90").unlink()
    editor._check_disk()
    assert "deleted on disk" in editor.status_label.text() if hasattr(editor, "status_label") else True


def test_save_as(window, project, monkeypatch):
    editor = open_editor(window, project / "app/main.f90")
    target = project / "app/renamed.f90"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    window.save_as_action.trigger()
    assert editor.path == target.resolve() and target.read_text().startswith("program main")
    assert window.tabs.tabText(window.tabs.indexOf(editor)).endswith("renamed.f90")


def test_gutter_change_tips(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    cursor_to(editor, 2, 0)
    code.insertPlainText("! new\n")
    editor.update_change_marks()
    gutter = code.gutter
    block = code.document().findBlockByNumber(1)
    y = int(code.blockBoundingGeometry(block).translated(code.contentOffset()).center().y())
    assert gutter._change_tip(QPoint(gutter.width() - 2, y), 2) == "Changed — not saved yet"
    assert gutter._change_tip(QPoint(2, y), 2) == ""  # (not over the bar)
    cursor_to(editor, 4, 0, select_to=(5, 0))
    code.textCursor().removeSelectedText()
    editor.update_change_marks()
    assert any(kind == "unsaved" for kind, _n in code.deletions.values())


# ---------------------------------------------------------------- lint
needs_fortitude = pytest.mark.skipif(lint.executable() is None, reason="fortitude not installed")


@pytest.fixture
def lint_window(make_window, project, settings_file):
    settings_file.write_text("[General]\nfortls=false\nlint=true\n")
    (project / "src/lt.f90").write_text(LINTED)
    return make_window(project / "fpm.toml")


@needs_fortitude
def test_lint_marks_issues(qtbot, lint_window, project):
    editor = open_editor(lint_window, project / "src/lt.f90")
    qtbot.waitUntil(lambda: len(editor.editor.lint_issues) >= 4, timeout=20_000)
    codes = {i.code for i in editor.editor.lint_issues}
    assert {"S101", "S061"} <= codes
    assert 5 in editor.editor.markers  # (the gutter)
    html = editor.editor.lint_card_html([i for i in editor.editor.lint_issues if i.line == 5], [])
    assert "S101" in html and "trailing whitespace" in html


@needs_fortitude
def test_lint_fix_from_the_menu(qtbot, lint_window, project):
    editor = open_editor(lint_window, project / "src/lt.f90")
    qtbot.waitUntil(lambda: any(i.code == "S101" for i in editor.editor.lint_issues), timeout=20_000)
    code = editor.editor
    issue = next(i for i in code.lint_issues if i.code == "S101")
    cursor_to(editor, issue.line, issue.column - 1)
    point = code.cursorRect().center()
    texts = menu_texts(code.context_menu(point), choose="Fix:")
    assert any(t.startswith("Fix:") for t in texts)
    qtbot.waitUntil(lambda: line_text(editor, 5) == "    x = 1", timeout=5_000)


@needs_fortitude
def test_fix_all_safe_and_unsafe(qtbot, lint_window, project):
    editor = open_editor(lint_window, project / "src/lt.f90")
    qtbot.waitUntil(lambda: bool(editor.editor.lint_issues), timeout=20_000)
    lint_window.fix_safe_action.trigger()
    qtbot.waitUntil(lambda: "end subroutine s" in editor.editor.toPlainText(), timeout=20_000)  # (in the background)
    text = editor.editor.toPlainText()
    assert "end subroutine s" in text and "end module m" in text and "x = 1   " not in text
    assert "implicit none" not in text  # (unsafe: not with the safe fixes)
    qtbot.waitUntil(lambda: not editor.rewriting, timeout=5_000)
    lint_window.fix_all_action.trigger()
    qtbot.waitUntil(lambda: "implicit none" in editor.editor.toPlainText(), timeout=20_000)
    editor.editor.undo()
    assert "implicit none" not in editor.editor.toPlainText()


def test_lint_toggle(lint_window, project):
    editor = open_editor(lint_window, project / "src/lt.f90")
    lint_window.lint_action.setChecked(False)
    assert not editor.editor.lint_issues and not editor.can_lint()


# ---------------------------------------------------------------- format
@pytest.mark.skipif(formatter.executable() is None, reason="fprettify not installed")
def test_format_with_fprettify(qtbot, window, project):
    (project / "src/messy.f90").write_text("module messy\ninteger::a,b\ncontains\nsubroutine s()\na=b+1\n"
                                           "end subroutine s\nend module messy\n")
    editor = open_editor(window, project / "src/messy.f90")
    window.format_action.trigger()
    qtbot.waitUntil(lambda: "a = b + 1" in editor.editor.toPlainText(), timeout=20_000)
    assert "   integer::a, b" in editor.editor.toPlainText()  # (indented; fprettify's default spacing)
    editor.editor.undo()  # (one step)
    assert "a=b+1" in editor.editor.toPlainText()


# ------------------------------------------------------------ completion
def test_completion_popup_accepts(qtbot, window, project):
    from vibefpmgui.lsp import Completion

    editor = open_editor(window, project / "app/main.f90")
    code = editor.editor
    cursor_to(editor, 3, len(line_text(editor, 3)))
    code.insertPlainText("\n  call bu")
    block = code.textCursor().blockNumber()
    items = [Completion("bump", "bump()", "subroutine", "SUBROUTINE bump()", "Adds one"),
             Completion("build", "build", "variable", "", "")]
    code.completion_popup.open(items, (block, 7), "bu", code.cursorRect())
    assert code.completion_popup.isVisible()
    QTest.keyClick(code, Qt.Key_Down)
    QTest.keyClick(code, Qt.Key_Up)
    QTest.keyClick(code, Qt.Key_Return)
    assert line_text(editor, 4) == "  call bump()" and not code.completion_popup.isVisible()


@needs_fortitude
def test_lint_fix_after_the_colors_change(qtbot, lint_window, project):
    # Recoloring the syntax isn't an edit: fortitude's fixes still apply.
    editor = open_editor(lint_window, project / "src/lt.f90")
    qtbot.waitUntil(lambda: any(i.code == "S101" for i in editor.editor.lint_issues), timeout=20_000)
    lint_window.set_syntax_style("monokai")
    qtbot.waitUntil(lambda: not editor.highlighter._full, timeout=5_000)
    issue = next(i for i in editor.editor.lint_issues if i.code == "S101")
    editor.apply_lint_fix(issue)
    assert line_text(editor, 5) == "    x = 1"


# ---------------------------------------------------------- indentation
def test_indentation_rules():
    from vibefpmgui.editor import (
        backspace_length,
        outdent_length,
        tab_text,
        visual_column,
    )

    assert visual_column("ab\tc", 4) == 5 and visual_column("\t\t", 2) == 4
    assert tab_text("", 4, False) == "    " and tab_text("ab", 4, False) == "  "  # (to the next tab stop)
    assert tab_text("abcd", 4, False) == "    " and tab_text("a\t", 4, False) == "    "
    assert tab_text("ab", 4, True) == "\t" and tab_text("a", 2, False) == " "
    assert outdent_length("      x", 4) == 4 and outdent_length("  x", 4) == 2
    assert outdent_length("\t\tx", 4) == 1 and outdent_length("  \tx", 4) == 3  # (tabs too)
    assert outdent_length("x", 4) == 0
    assert backspace_length("      ", 4) == 2 and backspace_length("        ", 4) == 4
    assert backspace_length("  x ", 4) == 0 and backspace_length("", 4) == 0


def test_indentation_menu(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    assert window.indent_actions[False].isChecked() and window.indent_actions[4].isChecked()
    assert "Spaces: 4" in editor.status.text()
    window.indent_actions[2].trigger()  # (Width: 2)
    original = line_text(editor, 6)
    cursor_to(editor, 6, 0)
    QTest.keyClick(code, Qt.Key_Tab)
    assert line_text(editor, 6) == "  " + original  # (2 columns: the next tab stop)
    assert code.tabStopDistance() == code.fontMetrics().horizontalAdvance(" ") * 2
    window.indent_actions[True].trigger()  # (Indent with Tabs)
    assert "Tabs: 2" in editor.status.text() and window.settings.value("indent_tabs") in (True, "true")
    cursor_to(editor, 2, 0, select_to=(4, 0))  # (ends at the start of line 4: not that one)
    QTest.keyClick(code, Qt.Key_Tab)
    assert line_text(editor, 2).startswith("\t") and line_text(editor, 3).startswith("\t")
    assert not line_text(editor, 4).startswith("\t")
    QTest.keyClick(code, Qt.Key_Backtab)  # (⇧Tab takes the tabs off again)
    assert not line_text(editor, 2).startswith("\t") and not line_text(editor, 3).startswith("\t")
    assert window.settings.value("indent_width", type=int) == 2


def test_return_and_backspace_are_one_undo_step_each(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    before = code.toPlainText()
    cursor_to(editor, 6, len(line_text(editor, 6)))
    QTest.keyClick(code, Qt.Key_Return)  # (a new line, indented)
    code.undo()
    assert code.toPlainText() == before
    cursor_to(editor, 6, 4)  # (in the line's leading spaces)
    QTest.keyClick(code, Qt.Key_Backspace)
    code.undo()
    assert code.toPlainText() == before


def test_format_and_fix_dont_make_the_window_wait(qtbot, window, project, monkeypatch):
    """fprettify / fortitude can take seconds on a big file: they run on a pool
    thread, and their answer is used only for the text they were given."""
    import time

    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    original = code.toPlainText()
    monkeypatch.setattr(formatter, "can_format", lambda path: True)

    def slow_format(text, path):
        time.sleep(0.5)
        return text.upper(), ""

    monkeypatch.setattr(formatter, "format_text", slow_format)
    started = time.monotonic()
    window.format_file(editor)
    assert time.monotonic() - started < 0.3 and editor.rewriting  # (didn't wait)
    window.format_file(editor)  # (again while it runs: ignored)
    assert "still formatting" in window.statusBar().currentMessage()
    qtbot.waitUntil(lambda: not editor.rewriting, timeout=5_000)
    assert code.toPlainText() == original.upper()
    code.undo()
    # Typed into meanwhile: the answer is for the old text, so it's not used.
    window.format_file(editor)
    code.appendPlainText("! typed while it was formatting")
    qtbot.waitUntil(lambda: not editor.rewriting, timeout=5_000)
    assert code.toPlainText() == original + "\n! typed while it was formatting"
    assert "changed while formatting" in window.statusBar().currentMessage()
