"""The Git panel's Discard Changes… (Delete File… for an untracked file):
each area's meaning, asked first, and files git has no copy of to the Trash."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from qtpy.QtWidgets import QMessageBox
from test_git import git, make_repo

from vibefpmgui import git_status as gs
from vibefpmgui.git_panel import CHANGE_ROLE
from vibefpmgui.window import git as window_git

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git needed")

A = "program a\nend program a\n"


def status(root: Path) -> dict[tuple[str, str], gs.Change]:
    return {(c.path, c.area): c for c in gs.read_status(root).changes}


@pytest.fixture
def repo(tmp_path) -> Path:
    return make_repo(tmp_path / "repo", {"a.f90": A, "b.f90": "b\n"})


def test_changes_go_back_to_the_last_commit(repo):
    (repo / "a.f90").write_text("program a\n  print *, 1\nend program a\n")
    assert gs.discard(repo, status(repo)[("a.f90", gs.CHANGED)]) == []
    assert (repo / "a.f90").read_text() == A and not status(repo)


def test_changes_go_back_to_what_s_staged(repo):
    (repo / "a.f90").write_text("staged\n")
    git(repo, "add", "a.f90")
    (repo / "a.f90").write_text("staged, then more\n")
    gs.discard(repo, status(repo)[("a.f90", gs.CHANGED)])
    assert (repo / "a.f90").read_text() == "staged\n"  # (what's staged: kept)
    assert list(status(repo)) == [("a.f90", gs.STAGED)]


def test_staged_changes_go_back_to_the_last_commit_staged_and_not(repo):
    (repo / "a.f90").write_text("staged\n")
    git(repo, "add", "a.f90")
    (repo / "a.f90").write_text("staged, then more\n")
    (repo / "b.f90").unlink()
    git(repo, "add", "b.f90")  # (a deletion, staged)
    changes = status(repo)
    for key in (("a.f90", gs.STAGED), ("b.f90", gs.STAGED)):
        assert gs.discard(repo, changes[key]) == []
    assert (repo / "a.f90").read_text() == A and (repo / "b.f90").read_text() == "b\n" and not status(repo)


def test_a_new_file_staged_is_unstaged_and_given_to_the_trash(repo):
    (repo / "new.f90").write_text("new\n")
    git(repo, "add", "new.f90")
    assert gs.discard(repo, status(repo)[("new.f90", gs.STAGED)]) == [repo / "new.f90"]
    assert (repo / "new.f90").exists()  # (not deleted here: the window moves it to the Trash)
    assert list(status(repo)) == [("new.f90", gs.UNTRACKED)]


def test_a_rename_staged_gets_its_old_name_back(repo):
    git(repo, "mv", "a.f90", "renamed.f90")
    change = status(repo)[("renamed.f90", gs.STAGED)]
    assert change.kind == "renamed"
    assert gs.discard(repo, change) == [repo / "renamed.f90"]
    assert (repo / "a.f90").read_text() == A


def test_an_untracked_file_and_a_conflict(repo):
    (repo / "scratch.txt").write_text("mine\n")
    assert gs.discard(repo, status(repo)[("scratch.txt", gs.UNTRACKED)]) == [repo / "scratch.txt"]
    with pytest.raises(gs.GitError, match="conflict"):
        gs.discard(repo, gs.Change("a.f90", gs.CONFLICT, "modified"))


# ----------------------------------------------------------------- the panel and the window
@pytest.fixture
def git_window(window, project, qtbot, monkeypatch):
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "first")
    trashed = []

    def to_trash(path):  # (not the real Trash: the test's files would end up in it)
        trashed.append(path)
        path.unlink()
        return True

    monkeypatch.setattr(window_git, "move_to_trash", to_trash)
    window.show_panel(window.git_panel)
    window.trashed = trashed
    return window


def answer(monkeypatch, discard: bool) -> list[str]:
    """The confirmation: Discard (or Move to Trash) clicked, or Cancel; its text."""
    asked = []

    def run(box):
        asked.append(box.text())
        role = QMessageBox.DestructiveRole if discard else QMessageBox.RejectRole
        next(b for b in box.buttons() if box.buttonRole(b) == role).click()
        return 0

    monkeypatch.setattr(window_git, "run_dialog", run)
    return asked


def refreshed(qtbot, window, condition):
    window.refresh_git()
    qtbot.waitUntil(lambda: window.git_panel.status is not None and condition(window.git_panel.status),
                    timeout=10_000)


def test_the_menu(git_window, project, qtbot):
    (project / "src/foo.f90").write_text("changed\n")
    (project / "scratch.txt").write_text("mine\n")
    refreshed(qtbot, git_window, lambda s: len(s.changes) == 2)
    panel = git_window.git_panel
    texts = {}
    for i in range(panel.files.topLevelItemCount()):
        group = panel.files.topLevelItem(i)
        for j in range(group.childCount()):
            item = group.child(j)
            panel._file_menu(panel.files.visualItemRect(item).center())
            texts[item.data(0, CHANGE_ROLE).area] = [a.text() for a in panel.file_menu.actions() if a.text()]
            panel.file_menu.hide()
    assert texts[gs.CHANGED][-1] == "Discard Changes…" and texts[gs.UNTRACKED][-1] == "Delete File…"


def test_discard_asks_first_and_cancel_keeps_it(git_window, project, qtbot, monkeypatch):
    (project / "src/foo.f90").write_text("changed\n")
    refreshed(qtbot, git_window, lambda s: bool(s.changes))
    asked = answer(monkeypatch, discard=False)
    git_window.discard_git_change(git_window.git_panel.status.changes[0])
    assert "Discard the changes to src/foo.f90?" in asked[0] and "the changes are lost" in asked[0]
    assert (project / "src/foo.f90").read_text() == "changed\n"  # (Cancel: kept)


def test_discard_and_an_untracked_file_to_the_trash(git_window, project, qtbot, monkeypatch):
    original = (project / "src/foo.f90").read_text()
    (project / "src/foo.f90").write_text("changed\n")
    (project / "scratch.txt").write_text("mine\n")
    refreshed(qtbot, git_window, lambda s: len(s.changes) == 2)
    asked = answer(monkeypatch, discard=True)
    changes = {c.area: c for c in git_window.git_panel.status.changes}
    git_window.discard_git_change(changes[gs.CHANGED])
    qtbot.waitUntil(lambda: (project / "src/foo.f90").read_text() == original, timeout=10_000)
    git_window.discard_git_change(changes[gs.UNTRACKED])
    qtbot.waitUntil(lambda: not (project / "scratch.txt").exists(), timeout=10_000)
    assert "Move scratch.txt to the Trash?" in asked[1] and git_window.trashed == [project / "scratch.txt"]
    refreshed(qtbot, git_window, lambda s: not s.changes)
