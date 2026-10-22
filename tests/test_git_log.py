"""The Git panel's History: the commits, their branches and tags, the graph
of how they join, and a commit's details, files and diffs."""

from __future__ import annotations

import shutil
from itertools import pairwise
from pathlib import Path

import pytest
from test_git import git, make_repo

from vibefpmgui import git_blame as gb
from vibefpmgui import git_log as gl
from vibefpmgui import git_log_view as glv
from vibefpmgui import git_panel as gp
from vibefpmgui import git_status as gs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git needed")


def c(sha: str, *parents: str) -> gl.LogCommit:
    return gl.LogCommit(sha * 8, [p * 8 for p in parents], "A", "a@x", 0, sha)


def lanes_below(row: gl.GraphRow) -> set[int]:
    return {i for i, _ in row.through} | {i for i, _ in row.parents}


def lanes_above(row: gl.GraphRow) -> set[int]:
    return {i for i, _ in row.through} | {i for i, _ in row.joining} | ({row.lane} if row.from_above else set())


def assert_joined_up(rows: list[gl.GraphRow]):
    """Every line leaving a row's bottom goes on from the top of the next one (and only those)."""
    for upper, lower in pairwise(rows):
        assert lanes_below(upper) == lanes_above(lower)


def make_history(tmp_path: Path) -> Path:
    """main: first, second, (feature merged in), fourth; feature: f1; other: o1 (not merged); a tag on first."""
    root = make_repo(tmp_path / "repo", {"a.txt": "one\n", "gone.txt": "bye\n", "old.txt": "x\n" * 20})
    git(root, "tag", "v1")
    (root / "a.txt").write_text("one\ntwo\n")
    git(root, "commit", "-qam", "second")
    git(root, "checkout", "-qb", "feature")
    (root / "b.txt").write_text("b\n")
    git(root, "add", "b.txt")
    git(root, "commit", "-qm", "f1")
    git(root, "checkout", "-q", "main")
    git(root, "checkout", "-qb", "other")
    (root / "c.txt").write_text("c\n")
    git(root, "add", "c.txt")
    git(root, "commit", "-qm", "o1")
    git(root, "checkout", "-q", "main")
    (root / "a.txt").write_text("one\ntwo\nthree\n")
    git(root, "commit", "-qam", "third")
    git(root, "merge", "-q", "--no-ff", "-m", "merge feature", "feature")
    git(root, "rm", "-q", "gone.txt")
    git(root, "mv", "old.txt", "new.txt")
    (root / "a.txt").write_text("one\nTWO\nthree\n")
    git(root, "commit", "-qam", "fourth")
    return root


# ------------------------------------------------------------ reading
def test_the_refs():
    refs = gl.parse_refs("HEAD -> refs/heads/main, tag: refs/tags/v1, refs/remotes/origin/main, "
                         "refs/remotes/origin/HEAD, refs/heads/dev")
    assert [(r.name, r.kind, r.current) for r in refs] == [
        ("main", gl.BRANCH, True), ("dev", gl.BRANCH, False), ("origin/main", gl.REMOTE, False),
        ("v1", gl.TAG, False)]
    assert [(r.name, r.kind) for r in gl.parse_refs("HEAD, refs/heads/x")] == [("HEAD", gl.HEAD), ("x", gl.BRANCH)]
    assert gl.parse_refs("") == []


def test_a_straight_line():
    rows = gl.graph([c("c", "b"), c("b", "a"), c("a")])
    assert [(r.lane, r.from_above, r.parents) for r in rows] == [(0, False, [(0, 0)]), (0, True, [(0, 0)]),
                                                                  (0, True, [])]
    assert all(r.width == 1 for r in rows)


def test_a_merge_and_a_branch_that_joins():
    # m merges b into a's line; x and y are two tips on a; a's the first commit.
    rows = gl.graph([c("x", "a"), c("m", "a", "b"), c("b", "a"), c("y", "a"), c("a")])
    x, m, b, y, a = rows
    assert x.lane == 0 and not x.from_above
    assert m.lane == 1 and not m.from_above and [i for i, _ in m.parents] == [1, 2]  # (its second parent: a lane)
    assert b.lane == 2 and b.from_above
    assert y.lane == 3 and not y.from_above
    assert a.lane == 0 and {i for i, _ in a.joining} == {1, 2, 3} and a.parents == []
    assert len({x.color, m.color, b.color, y.color}) == 4  # (each lane its own color)
    assert_joined_up(rows)


def test_read_the_log(tmp_path):
    root = make_history(tmp_path)
    log = gl.read_log(root)
    subjects = [commit.subject for commit in log.commits]
    assert subjects[0] == "fourth" and "o1" in subjects and subjects[-1] == "first" and not log.limited
    head = log.commits[0]
    assert head.sha == log.head and [(r.name, r.current) for r in head.refs] == [("main", True)]
    first = log.commits[-1]
    assert [r.name for r in first.refs] == ["v1"] and first.parents == []
    merge = next(commit for commit in log.commits if commit.subject == "merge feature")
    assert len(merge.parents) == 2
    assert_joined_up(log.rows)
    only_head = gl.read_log(root, all_branches=False)  # (what's checked out: not other's o1)
    assert "o1" not in [commit.subject for commit in only_head.commits] and not only_head.all_branches
    newest = gl.read_log(root, limit=3)
    assert len(newest.commits) == 3 and newest.limited and newest.total == 7  # (and how many there are)
    every = gl.read_log(root, limit=None)
    assert len(every.commits) == every.total == 7 and not every.limited


def test_the_versions_a_commit_changed(tmp_path):
    root = make_history(tmp_path)
    log = gl.read_log(root)
    fourth = log.commits[0]
    files = {f.path: f for f in gb.commit_details(root, fourth.sha).files}
    assert gl.file_versions(root, fourth, files["a.txt"]) == ("one\ntwo\nthree\n", "one\nTWO\nthree\n")
    assert gl.file_versions(root, fourth, files["gone.txt"]) == ("bye\n", None)
    renamed = files["new.txt"]
    assert renamed.old_path == "old.txt" and gl.file_versions(root, fourth, renamed) == ("x\n" * 20,) * 2
    first = log.commits[-1]
    assert gl.file_versions(root, first, gb.FileChange("A", "a.txt")) == (None, "one\n")


def test_a_merge_s_files_are_what_it_brought_in(tmp_path):
    """(git's "combined" list for a merge leaves out what came from one side only.)"""
    root = make_history(tmp_path)
    merge = next(commit for commit in gl.read_log(root).commits if commit.subject == "merge feature")
    assert [f.path for f in gb.commit_details(root, merge.sha).files] == ["b.txt"]


# ------------------------------------------------------------ the panel
def history_panel(qtbot, root: Path) -> gp.GitPanel:
    panel = gp.GitPanel()
    qtbot.addWidget(panel)
    panel.resize(1200, 800)
    panel.show_status(gs.read_status(root))
    panel.view_buttons[gp.HISTORY].click()
    qtbot.waitUntil(lambda: panel.log_view.log is not None, timeout=10000)
    return panel


def test_the_history_view(qtbot, tmp_path):
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    view = panel.log_view
    assert panel.view() == gp.HISTORY and not panel.unified_button.isHidden()
    assert view.model.rowCount() == 7 and "7 commits of every branch" in view.count.text()
    assert view.current_commit().subject == "fourth"  # (what's checked out, chosen)
    qtbot.waitUntil(lambda: view.files.topLevelItemCount() == 3, timeout=10000)
    assert "fourth" in view.card.toPlainText()
    paths = [view.files.topLevelItem(i).data(0, glv.CHANGE_ROLE).path for i in range(3)]
    assert set(paths) == {"a.txt", "gone.txt", "new.txt"}
    item = next(view.files.topLevelItem(i) for i in range(3) if paths[i] == "a.txt")
    view.files.setCurrentItem(item)
    qtbot.waitUntil(lambda: "a.txt" in view.file_diff.title.text() and "→" in view.file_diff.title.text(), timeout=10000)
    with qtbot.waitSignal(panel.open_requested) as opened:
        view.file_diff.open_requested.emit(2)
    assert opened.args == [str(root.resolve() / "a.txt"), 2]


def test_all_branches_or_what_s_checked_out(qtbot, tmp_path):
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    view = panel.log_view
    with qtbot.waitSignal(view.all_branches_changed) as said:
        view.all_branches.click()
    assert said.args == [False]
    qtbot.waitUntil(lambda: view.log is not None and not view.log.all_branches, timeout=10000)
    assert "o1" not in [commit.subject for commit in view.log.commits] and "checked out" in view.count.text()


def test_read_again_when_head_moves(qtbot, tmp_path):
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    first = panel.log_view.log
    (root / "a.txt").write_text("five\n")
    git(root, "commit", "-qam", "fifth")
    panel.show_status(gs.read_status(root))
    qtbot.waitUntil(lambda: panel.log_view.log is not first, timeout=10000)
    assert panel.log_view.log.commits[0].subject == "fifth"


def test_the_graph_is_drawn(qtbot, tmp_path):
    """(The Commit column: its lanes, dots, badges and subject, painted without errors.)"""
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    panel.show()
    image = panel.log_view.table.viewport().grab().toImage()
    assert not image.isNull() and image.width() > 0


def test_the_window_remembers_all_branches(qtbot, window, project, make_window):
    window.git_panel.view_buttons[gp.HISTORY].click()
    assert window.settings.value("git_view") == gp.HISTORY
    window.git_panel.log_view.all_branches.click()
    assert window.settings.value("git_all_branches", type=bool) is False
    other = make_window(project / "fpm.toml")
    assert other.git_panel.view() == gp.HISTORY and not other.git_panel.log_view.all_branches.isChecked()


def test_show_a_commit_s_file_in_project_files(qtbot, tmp_path):
    """Only for a file still where the commit left it: not one it deleted, nor one moved since."""
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    view = panel.log_view

    def menu_for(subject: str, path: str) -> dict:
        row = next(i for i, commit in enumerate(view.log.commits) if commit.subject == subject)
        view.table.setCurrentIndex(view.model.index(row, 0))
        qtbot.waitUntil(lambda: any(view.files.topLevelItem(i).data(0, glv.CHANGE_ROLE).path == path
                                    for i in range(view.files.topLevelItemCount())), timeout=10000)
        item = next(view.files.topLevelItem(i) for i in range(view.files.topLevelItemCount())
                    if view.files.topLevelItem(i).data(0, glv.CHANGE_ROLE).path == path)
        view._file_menu(view.files.visualItemRect(item).center())
        menu = view.file_menu
        menu.close()
        return {a.text(): a for a in menu.actions()}

    assert not menu_for("fourth", "gone.txt")["Show in Project Files (not there now)"].isEnabled()  # (deleted)
    assert not menu_for("first", "old.txt")["Show in Project Files (not there now)"].isEnabled()  # (renamed since)
    show = menu_for("fourth", "new.txt")["Show in Project Files"]
    assert show.isEnabled()
    with qtbot.waitSignal(panel.reveal_requested) as said:
        show.trigger()
    assert said.args == [str(root.resolve() / "new.txt")]


def test_a_theme_change_keeps_the_commit_and_file_chosen(qtbot, tmp_path, monkeypatch):
    """New colors: the card drawn again from what was read (git not asked again), the file and its diff kept."""
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    view = panel.log_view
    qtbot.waitUntil(lambda: view.files.topLevelItemCount() == 3, timeout=10000)
    view.files.setCurrentItem(view.files.topLevelItem(2))
    chosen = view.files.currentItem().data(0, glv.CHANGE_ROLE).path
    qtbot.waitUntil(lambda: "→" in view.file_diff.title.text(), timeout=10000)
    diff_title = view.file_diff.title.text()
    monkeypatch.setattr(gb, "commit_details", lambda *args: pytest.fail("read again"))
    monkeypatch.setattr(gl, "file_versions", lambda *args: pytest.fail("read again"))
    before = view.card.toHtml()
    panel.refresh_palette()
    view.refresh_palette(gp.PALETTES["dark"], True)
    assert view.card.toHtml() != before and "fourth" in view.card.toPlainText()  # (its new colors)
    assert view.files.topLevelItemCount() == 3 and view.files.currentItem().data(0, glv.CHANGE_ROLE).path == chosen
    assert view.file_diff.title.text() == diff_title


def test_each_page_read_through_the_same_few_methods():
    """(The Git panel needs nothing of a page's own: see git_panel.)"""
    for page in (gp.GitLogView, gp.GitStatsView):
        for method in ("reload_key", "reader", "show_result", "show_message", "refresh_palette"):
            assert callable(getattr(page, method)), (page, method)
        assert hasattr(page, "reload_requested")


def test_load_older_commits(qtbot, tmp_path, monkeypatch):
    """At first the newest MAX_COMMITS; Load Older Commits: the rest, back to the first (and on later reads)."""
    monkeypatch.setattr(gl, "MAX_COMMITS", 3)
    root = make_history(tmp_path)
    panel = history_panel(qtbot, root)
    view = panel.log_view
    assert view.model.rowCount() == 3 and "The newest 3 commits of 7" in view.count.text()
    assert view.older_button.isVisibleTo(view) and view.older_button.text() == "Load the 4 Older Commits"
    view.older_button.click()
    qtbot.waitUntil(lambda: view.log is not None and not view.log.limited, timeout=10000)
    assert view.model.rowCount() == 7 and view.log.commits[-1].subject == "first"
    assert not view.older_button.isVisibleTo(view) and view.count.text().startswith("7 commits")
    (root / "a.txt").write_text("five\n")
    git(root, "commit", "-qam", "fifth")
    panel.show_status(gs.read_status(root))  # (HEAD moved: read again, every commit still)
    qtbot.waitUntil(lambda: view.log is not None and view.model.rowCount() == 8, timeout=10000)


def test_the_graph_has_room_for_every_lane(qtbot, tmp_path):
    """(A column of its own, as wide as its widest row's lanes: none cut off.)"""
    root = make_repo(tmp_path / "repo", {"a.txt": "a\n"})
    for n in range(20):  # (20 branches off the first commit, none merged: 21 lanes)
        git(root, "checkout", "-q", "-b", f"b{n}", "main")
        (root / f"f{n}.txt").write_text(f"{n}\n")
        git(root, "add", "-A")
        git(root, "commit", "-qm", f"on b{n}")
    git(root, "checkout", "-q", "main")
    panel = gp.GitPanel()
    qtbot.addWidget(panel)
    panel.resize(2400, 900)
    panel.show()
    panel.show_status(gs.read_status(root))
    panel.view_buttons[gp.HISTORY].click()
    qtbot.waitUntil(lambda: panel.log_view.log is not None, timeout=10000)
    view = panel.log_view
    assert view.model.lanes == max(row.width for row in view.log.rows) > 16  # (no longer capped)
    assert view.table.columnWidth(glv.GRAPH) >= glv.graph_width(view.model.lanes, view.table.font())
    assert view.table.columnWidth(glv.SUBJECT) > 100  # (the subject still has room)
    assert view.model.index(0, glv.SUBJECT).data() == view.log.commits[0].subject
