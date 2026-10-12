"""Gremlins: non-ASCII and invisible characters, marked in the gutter and
listed in the Problems tab."""

from __future__ import annotations

import pytest
from conftest import open_editor, write_files
from qtpy.QtCore import QEvent, QPoint, QRectF, Qt
from qtpy.QtGui import QHelpEvent, QImage, QPainter, QTextCursor
from qtpy.QtWidgets import QToolTip

from vibefpmgui import gremlins

NBSP, ZWSP = " ", "\u200b"
SOURCE = f"program p\n  ! 21°C\n  x = 1.0{NBSP}+ 2.0\n\tprint *, 'ok'{ZWSP}\nend program\n"


def test_find():
    found = gremlins.find(SOURCE)
    assert [(g.line, g.column, g.code) for g in found] == [(2, 6, "U+00B0"), (3, 9, "U+00A0"), (4, 14, "U+200B")]
    assert gremlins.find("tabs\tand\nnew lines are fine ~\n") == []
    assert [g.code for g in gremlins.find("a\rb\x0cc\x00")] == ["U+000D", "U+000C", "U+0000"]
    assert len(gremlins.find("é" * 50, limit=10)) == 10


def test_describe():
    assert gremlins.describe(NBSP) == "non-breaking space, not ASCII"
    assert gremlins.describe(ZWSP) == "zero-width space, not ASCII"
    assert gremlins.describe("é") == "latin small letter e with acute (é), not ASCII"
    assert gremlins.describe("\x0c") == "form feed (a control character)"
    assert gremlins.describe("\x01").endswith("(a control character)")


def test_the_gremlin_is_drawn():
    image = QImage(24, 24, QImage.Format_ARGB32)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    gremlins.paint_gremlin(painter, QRectF(0, 0, 24, 24))
    painter.end()
    assert image.pixelColor(12, 13).alpha() == 255  # (the head)
    assert image.pixelColor(0, 23).alpha() == 0     # (a corner: nothing)


@pytest.fixture
def editor(window, project):
    write_files(project, {"src/g.f90": SOURCE})
    return open_editor(window, project / "src/g.f90")


def test_marked_in_the_editor(window, editor):
    code = editor.editor
    assert sorted(code.gremlins) == [2, 3, 4]
    plain_width = code.gutter_width() - 16
    assert code.gutter.width() == code.gutter_width() > plain_width  # (a column for the gremlins)
    assert "3 gremlins" in editor.status.text()
    problems = [p for p in editor.problems() if p.source == "gremlins"]
    assert [(p.line, p.badge) for p in problems] == [(2, "U+00B0"), (3, "U+00A0"), (4, "U+200B")]
    assert window.problems_panel.buttons["gremlins"].text() == "Gremlins 3"
    assert code.gremlin_notes(3) == ["Gremlin at column 10: non-breaking space, not ASCII (U+00A0)"]


def test_gutter_tooltip(editor, monkeypatch):
    code = editor.editor
    shown = []
    monkeypatch.setattr(QToolTip, "showText", lambda pos, text, *a: shown.append(text))
    y = code.cursorRect(QTextCursor(code.document().findBlockByNumber(3))).center().y()
    point = QPoint(4, y)
    code.gutter.event(QHelpEvent(QEvent.ToolTip, point, code.gutter.mapToGlobal(point)))
    assert shown and "zero-width space" in shown[-1]


def test_typing_one_and_removing_them(qtbot, editor):
    code = editor.editor
    cursor = QTextCursor(code.document().findBlockByNumber(0))
    cursor.insertText("é")
    qtbot.waitUntil(lambda: 1 in code.gremlins, timeout=3_000)
    code.setPlainText("program p\nend program\n")
    qtbot.waitUntil(lambda: not code.gremlins, timeout=3_000)
    assert code.gutter.width() == code.gutter_width()  # (the column is gone again)


def test_turned_off_from_the_view_menu(window, editor):
    window.gremlins_action.setChecked(False)
    assert not editor.editor.gremlins and "gremlin" not in editor.status.text()
    assert not [p for p in editor.problems() if p.source == "gremlins"]
    assert window.settings.value("gremlins") in (False, "false")
    window.gremlins_action.setChecked(True)
    assert len(editor.editor.gremlins) == 3


def test_saving_keeps_non_breaking_spaces(editor, project):
    """Qt's toPlainText() turns them into plain spaces: saving that changed the file."""
    path = project / "src/g.f90"
    assert editor.editor.toPlainText() == SOURCE
    editor.update_change_marks()
    assert not editor.editor.change_marks  # (the line with one isn't "changed")
    editor.editor.appendPlainText("! more")
    assert editor.save()
    assert NBSP in path.read_text(encoding="utf-8").splitlines()[2]
