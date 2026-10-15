"""The Git panel: a repository's status, the versions of a change, the diff."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from conftest import open_editor, write_files

from vibefpmgui import diff_view as dv
from vibefpmgui import git_status as gs

pytestmark = pytest.mark.skipif(shutil.which("git") is None or not gs.available(), reason="git and pygit2 needed")


def git(root: Path, *args: str) -> str:
    out = subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "-c", "init.defaultBranch=main",
                          *args], cwd=root, capture_output=True, text=True, check=False)
    return out.stdout


def make_repo(root: Path, files: dict[str, str]) -> Path:
    write_files(root, files)
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "first")
    return root


# ------------------------------------------------------------ status
def test_status_areas_and_kinds(tmp_path):
    root = make_repo(tmp_path / "repo", {"src/a.f90": "program a\nend program a\n", "b.txt": "b\n",
                                         "c.txt": "c\n"})
    (root / "src/a.f90").write_text("program a\n  print *, 1\nend program a\n")
    git(root, "add", "src/a.f90")
    (root / "src/a.f90").write_text("program a\n  print *, 2\nend program a\n")  # (staged, then changed again)
    (root / "b.txt").unlink()
    git(root, "rm", "-q", "--cached", "c.txt")
    (root / "new.f90").write_text("! new\n")
    status = gs.read_status(root / "src")  # (from a folder inside the repository)
    assert status.root == root.resolve() and status.branch == "main" and status.head
    found = {(c.path, c.area, c.kind) for c in status.changes}
    assert found == {("src/a.f90", gs.STAGED, "modified"), ("src/a.f90", gs.CHANGED, "modified"),
                     ("b.txt", gs.CHANGED, "deleted"), ("c.txt", gs.STAGED, "deleted"),
                     ("c.txt", gs.UNTRACKED, "untracked"), ("new.f90", gs.UNTRACKED, "untracked")}
    assert status.summary() == "main"
    assert gs.read_status(tmp_path / "elsewhere") is None or gs.find_repository(tmp_path) is None


def test_a_merge_conflict(tmp_path):
    root = make_repo(tmp_path / "repo", {"a.txt": "one\n"})
    git(root, "checkout", "-q", "-b", "other")
    (root / "a.txt").write_text("theirs\n")
    git(root, "commit", "-q", "-am", "theirs")
    git(root, "checkout", "-q", "main")
    (root / "a.txt").write_text("ours\n")
    git(root, "commit", "-q", "-am", "ours")
    git(root, "merge", "-q", "other")
    (change,) = gs.read_status(root).changes
    assert (change.area, change.kind, change.letter) == (gs.CONFLICT, "conflict", "!")
    old, new, *_ = gs.versions(root, change)
    assert old == "ours\n" and "<<<<<<<" in new


def test_branch_before_the_first_commit_and_detached(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    git(root, "init", "-q")
    (root / "a.txt").write_text("a\n")
    status = gs.read_status(root)
    assert status.branch == "main" and status.head is None and status.summary() == "main (no commits yet)"
    other = make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    git(other, "checkout", "-q", "--detach")
    status = gs.read_status(other)
    assert status.branch is None and status.summary() == f"detached at {status.head}"


def test_ahead_and_behind_the_upstream(tmp_path):
    origin = make_repo(tmp_path / "origin", {"a.txt": "a\n"})
    subprocess.run(["git", "clone", "-q", str(origin), str(tmp_path / "clone")], check=True)
    clone = tmp_path / "clone"
    (clone / "a.txt").write_text("mine\n")
    git(clone, "commit", "-q", "-am", "mine")
    (origin / "b.txt").write_text("theirs\n")
    git(origin, "add", "b.txt")
    git(origin, "commit", "-q", "-m", "theirs")
    git(clone, "fetch", "-q")
    status = gs.read_status(clone)
    assert (status.upstream, status.ahead, status.behind) == ("origin/main", 1, 1)
    assert status.summary() == "main → origin/main · 1 ahead, 1 behind"


def test_the_versions_compared(tmp_path):
    root = make_repo(tmp_path / "repo", {"a.txt": "one\n", "bin.dat": "x\n"})
    (root / "a.txt").write_text("two\n")
    git(root, "add", "a.txt")
    (root / "a.txt").write_text("three\r\n")  # (CRLF: compared as lines)
    (root / "bin.dat").write_bytes(b"\0\1\2")
    (root / "u.txt").write_text("new\n")
    changes = {(c.path, c.area): c for c in gs.read_status(root).changes}
    assert gs.versions(root, changes["a.txt", gs.STAGED]) == ("one\n", "two\n", "last commit", "staged")
    assert gs.versions(root, changes["a.txt", gs.CHANGED]) == ("two\n", "three\n", "staged", "working tree")
    assert gs.versions(root, changes["u.txt", gs.UNTRACKED]) == (None, "new\n", "", "working tree")
    assert gs.versions(root, changes["bin.dat", gs.CHANGED])[1] == gs.BINARY


# ------------------------------------------------------------ the diff's rows
def test_align_and_context():
    old = ["a", "b", "c", "d", "e", "f", "g", "h", "i", "j"]
    new = ["a", "b", "C", "d", "e", "f", "g", "h", "i", "j", "k"]
    rows = dv.align(old, new)
    assert [r.kind for r in rows] == [dv.SAME, dv.SAME, dv.CHANGED] + [dv.SAME] * 7 + [dv.ADDED]
    assert rows[2] == dv.Row(dv.CHANGED, 3, "c", 3, "C")
    shown = dv.with_context(rows, context=2)
    assert [r.kind for r in shown] == [dv.SAME, dv.SAME, dv.CHANGED, dv.SAME, dv.SAME, dv.GAP, dv.SAME,
                                       dv.SAME, dv.ADDED]
    assert shown[5].hidden == 3
    assert [r.kind for r in dv.align([], ["x", "y"])] == [dv.ADDED, dv.ADDED]  # (a new file)
    assert dv.lines_of(None) == [] and dv.lines_of("a\nb\n") == ["a", "b"] and dv.lines_of("a") == ["a"]


def test_side_by_side_and_unified_rows():
    rows = dv.with_context(dv.align(["keep", "old", "gone"], ["keep", "new", "added", "more"]))
    old, new = dv.side_by_side(rows)
    assert old.text == ["keep", "old", "gone", ""] and new.text == ["keep", "new", "added", "more"]
    assert old.kinds == [dv.SAME, dv.REMOVED, dv.REMOVED, dv.FILLER]
    assert new.kinds == [dv.SAME, dv.ADDED, dv.ADDED, dv.ADDED]
    assert new.gutter[1] == ("2", "+") and old.gutter[3] == ("", "")
    one = dv.unified(rows)
    assert one.text == ["keep", "old", "gone", "new", "added", "more"]
    assert one.kinds == [dv.SAME, dv.REMOVED, dv.REMOVED, dv.ADDED, dv.ADDED, dv.ADDED]
    assert one.new_lines == [1, None, None, 2, 3, 4]


def test_double_clicking_opens_the_newer_version_s_line(qtbot):
    view = dv.DiffView()
    qtbot.addWidget(view)
    view.show_diff("keep\nold\n", "keep\nnew\nadded\n", "a.f90")
    assert view.new_pane.line_at(2) == 3 and view.old_pane.line_at(2) == 3  # (the same row on both sides)
    view.set_unified(True)
    assert view.unified_pane.line_at(1) == 1  # (a removed line: the line before it)
    assert view.unified_pane.toPlainText() == "keep\nold\nnew\nadded"


# ------------------------------------------------------------ the panel and the window
def test_the_panel_lists_the_changes_and_shows_a_diff(qtbot, tmp_path):
    from vibefpmgui.git_panel import CHANGE_ROLE, GitPanel

    root = make_repo(tmp_path / "repo", {"a.f90": "program a\nend program a\n"})
    (root / "a.f90").write_text("program a\n  print *, 1\nend program a\n")
    (root / "new.txt").write_text("new\n")
    panel = GitPanel()
    qtbot.addWidget(panel)
    opened = []
    panel.open_requested.connect(lambda path, line: opened.append((Path(path).name, line)))
    panel.show_status(gs.read_status(root))
    groups = [panel.files.topLevelItem(i).text(0) for i in range(panel.files.topLevelItemCount())]
    assert groups == ["Changes (1)", "Untracked (1)"]
    assert panel.selected() == gs.Change("a.f90", gs.CHANGED, "modified")  # (the first file, shown)
    assert "staged → working tree" in panel.diff_title.text()
    assert panel.diff.new_pane.toPlainText() == "program a\n  print *, 1\nend program a"
    assert panel.diff.new_pane.rows.kinds == [dv.SAME, dv.ADDED, dv.SAME]
    panel.diff.open_requested.emit(2)
    assert opened == [("a.f90", 2)]
    untracked = panel.files.topLevelItem(1).child(0)
    panel.files.setCurrentItem(untracked)
    assert untracked.data(0, CHANGE_ROLE).path == "new.txt" and "new file" in panel.diff_title.text()
    panel.show_status(gs.read_status(root))  # (again: the same file stays selected)
    assert panel.selected().path == "new.txt"


def test_the_window_s_git_panel(qtbot, window, project):
    git(project, "init", "-q")
    git(project, "add", "-A")
    git(project, "commit", "-q", "-m", "first")
    window.show_panel(window.git_panel)
    window.refresh_git()
    panel = window.git_panel
    qtbot.waitUntil(lambda: panel.status is not None, timeout=5_000)
    assert "No changes" in panel.empty.text()
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.appendPlainText("! an edit")
    editor.save()  # (saving refreshes it)
    qtbot.waitUntil(lambda: panel.status is not None and bool(panel.status.changes), timeout=5_000)
    assert [c.path for c in panel.status.changes] == ["src/foo.f90"]
    assert window.dockWidgetArea(window.docks["git"]) is not None
