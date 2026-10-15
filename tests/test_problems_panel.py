"""The Problems tab: everything flagged in the file in the editor."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from conftest import open_editor, write_files
from qtpy.QtGui import QTextCursor

from vibefpmgui import spelling
from vibefpmgui.result_tree import PARTS_ROLE

pytestmark = pytest.mark.skipif(not spelling.available(), reason="pyspellchecker not installed")

SOURCE = """\
! Teh first line
module m
  real :: x  ! the tolerence
end module m
"""


@pytest.fixture(autouse=True)
def no_user_words(monkeypatch):
    monkeypatch.setattr(spelling, "user_words", set())


@pytest.fixture
def editor(qtbot, window, project):
    write_files(project, {"src/m.f90": SOURCE, "src/other.f90": "! all fine here\n"})
    editor = open_editor(window, project / "src/m.f90")
    qtbot.waitUntil(lambda: len(editor._spelling_issues) == 2, timeout=5_000)
    editor.show_diagnostics([SimpleNamespace(line=3, severity="warning", message="Unused variable 'x'")])
    editor.show_review([SimpleNamespace(line=2, category="naming", comment="Name the module better")])
    window.show_panel(window.problems_panel)  # (it's only kept up to date while it's shown)
    qtbot.waitUntil(lambda: len(rows(window.problems_panel)) == 4, timeout=5_000)
    return editor


def rows(panel) -> list[tuple]:
    tree = panel.tree
    if not tree.topLevelItemCount():
        return []
    item = tree.topLevelItem(0)
    return [(p.line, p.badge, p.source) for p in
            (item.child(i).data(0, PARTS_ROLE)[2] for i in range(item.childCount()))]


def test_lists_the_file_s_problems_in_line_order(window, editor):
    panel = window.problems_panel
    assert rows(panel) == [(1, "spelling", "spelling"), (2, "review", "review"), (3, "warning", "build"),
                           (3, "spelling", "spelling")]
    assert panel.buttons["spelling"].text() == "Spelling 2" and panel.buttons["build"].text() == "Build 1"
    assert panel.buttons["fortls"].isHidden()  # (none: no button)
    assert panel.title.text() == "<b>4</b> problems"
    file_row = panel.tree.topLevelItem(0).data(0, PARTS_ROLE)
    assert file_row[2] == "m.f90" and file_row[3] == "src"


def test_hiding_a_kind(window, editor):
    panel = window.problems_panel
    panel.buttons["spelling"].setChecked(False)
    assert [r[2] for r in rows(panel)] == ["review", "build"]
    assert "(2 hidden)" in panel.title.text()
    assert window.settings.value("problems/spelling") in (False, "false")  # (remembered)


def test_clicking_a_problem_goes_there(qtbot, window, editor):
    panel = window.problems_panel
    window.show_panel(panel)
    item = panel.tree.topLevelItem(0)
    spelled = next(item.child(i) for i in range(item.childCount())
                   if item.child(i).data(0, PARTS_ROLE)[2].line == 3
                   and item.child(i).data(0, PARTS_ROLE)[2].source == "spelling")
    panel.tree.open_item(spelled)
    cursor = editor.editor.textCursor()
    assert cursor.blockNumber() == 2 and cursor.selectedText() == "tolerence"


def test_follows_the_editor(qtbot, window, editor, project):
    panel = window.problems_panel
    cursor = QTextCursor(editor.editor.document())
    cursor.insertText("! one mistaek\n")  # (typing: checked again)
    qtbot.waitUntil(lambda: "mistaek" in [i.word for i in editor._spelling_issues], timeout=5_000)
    qtbot.waitUntil(lambda: rows(panel)[0] == (1, "spelling", "spelling"), timeout=2_000)
    open_editor(window, project / "src/other.f90")  # (another tab: its problems)
    assert rows(panel) == [] or panel.tree.topLevelItem(0).childCount() == 0
    assert "No problems in <b>other.f90</b>" in panel.title.text()
    window.tabs.setCurrentWidget(editor)
    assert len(rows(panel)) == 5
    assert editor.save()  # (closing an unsaved file would ask first)
    window.close_all_editors()
    assert panel.tree.topLevelItemCount() == 0 and "Open a file" in panel.title.text()


def test_long_messages_wrap(qtbot, window, editor):
    from types import SimpleNamespace

    long = ("The loop recomputes the norm of the whole array on every iteration; compute it once before "
            "the loop and reuse it, which also avoids allocating the temporary each time. ") * 2
    editor.show_review([SimpleNamespace(line=3, category="performance", comment=long)])
    panel = window.problems_panel
    tree = panel.tree
    dock = window.docks["problems"]
    dock.setFloating(True)  # (a size of its own, to try narrow and wide)
    dock.resize(500, 400)
    qtbot.waitUntil(lambda: panel.width() < 520, timeout=2_000)

    def review_row():
        item = tree.topLevelItem(0)
        return next((item.child(i) for i in range(item.childCount())
                     if item.child(i).data(0, PARTS_ROLE)[2].message.endswith(long[-30:])), None)

    qtbot.waitUntil(lambda: review_row() is not None, timeout=2_000)  # (the update is batched)

    def short_row():
        item = tree.topLevelItem(0)
        return next(item.child(i) for i in range(item.childCount())
                    if item.child(i).data(0, PARTS_ROLE)[2].source == "build")

    def height(row):  # (rows are looked up each time: an update makes new ones)
        return tree.visualItemRect(row()).height()

    assert height(review_row) > 2 * height(short_row)  # (several lines)
    narrow = height(review_row)
    dock.resize(1000, 400)
    qtbot.waitUntil(lambda: height(review_row) < narrow, timeout=2_000)  # (wrapped again, wider)
    assert tree.horizontalScrollBar().maximum() == 0  # (wraps rather than scrolling sideways)


def test_hidden_it_waits_until_shown(qtbot, window, editor):
    """Not shown (another panel in front, or closed), it isn't rebuilt as the
    file changes; shown again, it catches up."""
    from types import SimpleNamespace

    panel = window.problems_panel
    window.show_panel(window.build_monitor)  # (in front of Problems)
    qtbot.waitUntil(lambda: not window._problems_visible, timeout=2_000)
    built = []
    real = panel.show_problems
    panel.show_problems = lambda *a, **k: built.append(1) or real(*a, **k)
    editor.show_diagnostics([SimpleNamespace(line=1, severity="error", message="Something's wrong")])
    qtbot.wait(300)  # (past the update's delay)
    assert built == []
    window.show_panel(panel)
    qtbot.waitUntil(lambda: (1, "error", "build") in rows(panel), timeout=2_000)
    assert built  # (caught up)


def test_apply_a_fortitude_fix_from_the_problems(qtbot, window, project):
    """Right-click a fortitude problem that has a fix → Apply Fix: the same
    as its hover card's Apply fix (one Undo step)."""
    from vibefpmgui import lint

    write_files(project, {"src/fix.f90": "program p\n  x = 1\nend program p\n"})
    editor = open_editor(window, project / "src/fix.f90")
    fixable = lint.LintIssue(2, 3, 2, 8, "S101", "missing implicit none", "add implicit none", True,
                             (lint.FixEdit(1, 0, 1, 0, "  implicit none\n"),))
    plain = lint.LintIssue(3, 1, 3, 4, "C999", "a remark without a fix", "")
    editor._lint_text = editor.editor.toPlainText()  # (what fortitude checked: still the text)
    editor._show_lint([fixable, plain])
    window.show_panel(window.problems_panel)
    tree = window.problems_panel.tree
    qtbot.waitUntil(lambda: tree.topLevelItemCount() and tree.topLevelItem(0).childCount() == 2, timeout=5_000)

    def menu_for(badge: str):
        item = tree.topLevelItem(0)
        row = next(item.child(i) for i in range(item.childCount())
                   if item.child(i).data(0, PARTS_ROLE)[2].badge == badge)
        return window.problems_panel.context_menu(tree.visualItemRect(row).center())

    assert not any(a.text().startswith("Apply Fix") for a in menu_for("C999").actions())  # (no fix: no item)
    action = next(a for a in menu_for("S101").actions() if a.text().startswith("Apply Fix"))
    assert action.text() == "Apply Fix: add implicit none"
    action.trigger()
    assert editor.editor.toPlainText() == "program p\n  implicit none\n  x = 1\nend program p\n"
    editor.editor.undo()  # (one step back)
    assert editor.editor.toPlainText() == "program p\n  x = 1\nend program p\n"
