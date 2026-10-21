"""View → Show Git Blame: each line's last commit beside the editor, read
from the text in the editor; a click opens the commit on the web."""

from __future__ import annotations

import shutil

import pytest
from conftest import open_editor, write_files
from qtpy.QtWidgets import QApplication
from test_git import git

from vibefpmgui import git_blame
from vibefpmgui.git_blame import NOT_COMMITTED, Commit, parse_porcelain, short_name

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git needed")

A, B = "a" * 40, "b" * 40
PORCELAIN = f"""\
{A} 1 1 2
author Ada Lovelace
author-mail <ada@example.com>
author-time 1700000000
author-tz +0000
summary The first one
filename x.f90
\tprogram x
{A} 2 2
\t  implicit none
{NOT_COMMITTED} 3 3 1
author Not Committed Yet
author-time 1800000000
summary Version of x.f90 from x.f90
filename x.f90
\t  print *, 1
{B} 3 4 1
author Grace Hopper
author-time 1750000000
summary The second
filename x.f90
\tend program x
"""


def test_the_porcelain_read():
    lines, commits = parse_porcelain(PORCELAIN)
    assert lines == [A, A, NOT_COMMITTED, B]
    assert commits[A].author == "Ada Lovelace" and commits[A].summary == "The first one"
    assert commits[A].time == 1700000000 and commits[B].author == "Grace Hopper"
    assert not commits[NOT_COMMITTED].committed and commits[NOT_COMMITTED].author == "You"


def test_short_names():
    assert short_name("Ada Lovelace") == "Ada Lovelace"
    assert short_name("Augusta Ada King-Noel Lovelace") == "Augusta L."
    assert short_name("Supercalifragilisticexpialidocious") == "Supercalifrag…"


@pytest.fixture
def repo(tmp_path):
    root = write_files(tmp_path / "repo", {"fpm.toml": 'name = "r"\n', "src/x.f90": "program x\nend program x\n"})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "The first one")
    git(root, "remote", "add", "origin", "https://github.com/example/r.git")
    return root


def test_the_editor_s_text_blamed(repo):
    path = repo / "src/x.f90"
    blame = git_blame.blame(path, "program x\n  print *, 1\nend program x\n")  # (an edit, not saved)
    first = blame.commits[blame.lines[0]]
    assert first.summary == "The first one" and blame.lines[2] == first.sha
    assert blame.lines[1] == NOT_COMMITTED  # (the line added in the editor)
    assert blame.remote == "https://github.com/example/r.git"
    (repo / "src/new.f90").write_text("x\n")
    assert git_blame.blame(repo / "src/new.f90", "x\n") is None  # (git doesn't know it yet)


def test_the_column_beside_the_editor(make_window, repo, qtbot, monkeypatch):
    window = make_window(repo / "fpm.toml")
    editor = open_editor(window, repo / "src/x.f90")
    assert not editor.blame_column.isVisible()  # (off unless asked for)
    window.blame_action.setChecked(True)
    qtbot.waitUntil(lambda: editor.blame_column.blame is not None, timeout=10_000)
    column = editor.blame_column
    assert column.isVisible() and window.settings.value("git_blame", type=bool)
    layout = editor.layout().itemAt(editor.layout().count() - 3).layout()  # (the editor, the column, the minimap)
    assert layout.indexOf(column) == layout.indexOf(editor.editor) + 1 == layout.indexOf(editor.minimap) - 1
    top = editor.editor.cursorRect(editor.editor.textCursor()).center().y()
    commit = column.commit_at(top)
    assert commit is not None and commit.summary == "The first one"
    # A click: the commit's page on GitHub.
    opened = []
    monkeypatch.setattr(git_blame, "open_web_page", opened.append)
    from qtpy.QtCore import QPoint, Qt
    from qtpy.QtTest import QTest

    QTest.mouseClick(column, Qt.LeftButton, Qt.NoModifier, QPoint(20, top))
    assert opened == [f"https://github.com/example/r/commit/{commit.sha}"]
    # Typing: read again once it pauses; the new line not committed.
    editor.editor.moveCursor(editor.editor.textCursor().MoveOperation.End)
    editor.editor.insertPlainText("! more\n")
    qtbot.waitUntil(lambda: NOT_COMMITTED in column.blame.lines, timeout=10_000)
    editor.editor.document().setModified(False)
    window.blame_action.setChecked(False)
    assert not column.isVisible()


def test_no_web_page_copies_the_hash(qtbot, make_window, repo, monkeypatch):
    git(repo, "remote", "remove", "origin")
    window = make_window(repo / "fpm.toml")
    window.blame_action.setChecked(True)
    editor = open_editor(window, repo / "src/x.f90")
    qtbot.waitUntil(lambda: editor.blame_column.blame is not None, timeout=10_000)
    column = editor.blame_column
    commit = next(c for c in column.blame.commits.values() if c.committed)
    from qtpy.QtCore import QPoint, Qt
    from qtpy.QtTest import QTest

    top = editor.editor.cursorRect(editor.editor.textCursor()).center().y()
    QTest.mouseClick(column, Qt.LeftButton, Qt.NoModifier, QPoint(20, top))
    assert QApplication.clipboard().text() == commit.sha


def test_age_bands():
    """Fixed bands, the same in every file: this week, this month, this year, older."""
    from qtpy.QtGui import QColor

    from vibefpmgui.git_blame import age_band, age_words

    now = 2_000_000_000
    day = 86_400
    assert [age_band(now - days * day, now)[0] for days in (0, 6, 7, 30, 31, 364, 365, 5000)] == [
        "this week", "this week", "this month", "this month", "this year", "this year", "older", "older"]
    dark = QColor("#202020")
    colors = {git_blame.BlameColumn._age_color(Commit(A, time=now - days * day), dark, now).name()
              for days in (1, 10, 100, 1000)}
    assert len(colors) == 4  # (a color each)
    assert git_blame.BlameColumn._age_color(Commit(NOT_COMMITTED), dark, now).name() == "#e5484d"
    assert age_words(now - 12 * day, now) == "this month (12 days ago)"
    assert age_words(now - 3 * 365 * day, now) == "older (3 years ago)"
    assert age_words(now - 3600, now) == "this week (today)"


def test_cards_hover_and_press_as_buttons(make_window, repo, qtbot, monkeypatch):
    """A run of lines from one commit: one card; it lights up under the mouse, and a click is a press and a
    release on the same card (released on another: nothing, as a button)."""
    from qtpy.QtCore import QPoint, Qt
    from qtpy.QtTest import QTest

    (repo / "src/x.f90").write_text("program x\n  implicit none\n  print *, 1\nend program x\n")
    git(repo, "commit", "-q", "-am", "The second")  # (lines 2-3 from the second commit, 1 and 4 from the first)
    window = make_window(repo / "fpm.toml")
    window.blame_action.setChecked(True)
    editor = open_editor(window, repo / "src/x.f90")
    column = editor.blame_column
    qtbot.waitUntil(lambda: column.blame is not None, timeout=10_000)
    runs = list(column.runs())
    assert [(first, commit.summary) for first, commit, _top, _bottom in runs] == [
        (0, "The first one"), (1, "The second"), (3, "The first one")]  # (lines 2 and 3: one card)
    second = runs[1]
    middle = QPoint(30, int((second[2] + second[3]) / 2))
    QTest.mouseMove(column, middle)
    assert column._hover == 1
    opened = []
    monkeypatch.setattr(git_blame, "open_web_page", opened.append)
    QTest.mousePress(column, Qt.LeftButton, Qt.NoModifier, middle)
    assert column._pressed == 1
    QTest.mouseRelease(column, Qt.LeftButton, Qt.NoModifier, QPoint(30, int(runs[0][2] + 2)))  # (on another card)
    assert opened == [] and column._pressed is None
    QTest.mouseClick(column, Qt.LeftButton, Qt.NoModifier, middle)
    assert opened == [f"https://github.com/example/r/commit/{second[1].sha}"]


def test_the_wheel_over_the_column_scrolls_the_text(make_window, repo, qtbot):
    """(And the column and the minimap with it.)"""
    from qtpy.QtCore import QPoint, QPointF, Qt
    from qtpy.QtGui import QWheelEvent
    from qtpy.QtWidgets import QApplication

    (repo / "src/x.f90").write_text("".join(f"! line {n}\n" for n in range(400)))
    git(repo, "commit", "-q", "-am", "Long")
    window = make_window(repo / "fpm.toml")
    window.blame_action.setChecked(True)
    editor = open_editor(window, repo / "src/x.f90")
    column = editor.blame_column
    qtbot.waitUntil(lambda: column.blame is not None, timeout=10_000)
    bar = editor.editor.verticalScrollBar()
    assert bar.value() == 0
    first = next(column.runs())
    at = QPointF(30, 30)
    wheel = QWheelEvent(at, QPointF(column.mapToGlobal(QPoint(30, 30))), QPoint(0, 0), QPoint(0, -360),
                        Qt.NoButton, Qt.NoModifier, Qt.NoScrollPhase, False)
    QApplication.sendEvent(column, wheel)
    assert bar.value() > 0  # (the text scrolled)
    qtbot.waitUntil(lambda: next(column.runs())[2] < first[2] or next(column.runs())[0] > first[0], timeout=3000)
