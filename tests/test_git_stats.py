"""The Git panel's Stats: the repository's numbers, its people, and each
one's commits over time."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from test_git import git, make_repo

from vibefpmgui import git_panel as gp
from vibefpmgui import git_stats as gst
from vibefpmgui import git_stats_view as gsv
from vibefpmgui import git_status as gs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git needed")

DAY = gst.DAY
START = 1_600_000_000  # (2020-09-13)


def commit_as(root: Path, name: str, when: int, file: str = "log.txt"):
    """A commit by ``name`` (name@example.com) at ``when``."""
    path = root / file
    path.write_text((path.read_text() if path.exists() else "") + f"{name} {when}\n")
    env = dict(os.environ, GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=f"{name.lower()}@example.com",
               GIT_AUTHOR_DATE=f"@{when} +0000", GIT_COMMITTER_DATE=f"@{when} +0000")
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "add", "-A"], cwd=root,
                   check=True, env=env)
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=T", "commit", "-q", "-m",
                    f"by {name}"], cwd=root, check=True, env=env)


def history(tmp_path: Path) -> Path:
    """Ada: 3 commits, Bob: 2, Cy: 1 (and T's first one)."""
    root = make_repo(tmp_path / "repo", {"README.md": "hi\n", "src/a.f90": "! a\n"})
    for n, name in enumerate(["Ada", "Bob", "Ada", "Cy", "Bob", "Ada"]):
        commit_as(root, name, START + n * DAY)
    return root


# ------------------------------------------------------------ reading
def test_parse_the_log():
    output = (f"{START + 20}\x1fBob\x1fbob@x\n{START + 10}\x1fAda\x1fada@new\n"
              f"{START}\x1fAda\x1fada@old\nnot a commit line\n").encode()
    ada, bob = gst.parse_log(output)
    assert (ada.name, ada.commits, ada.first, ada.last) == ("Ada", 2, START, START + 10)
    assert ada.email == "ada@new"  # (their latest commit's: git lists the newest first)
    assert (bob.name, bob.commits) == ("Bob", 1)


def test_parse_the_size_and_spans():
    assert gst.parse_size(b"count: 3\nsize: 12\nin-pack: 40\nsize-pack: 100\nprune-packable: 0\n") == 112 * 1024
    assert gst.span_words(0) == "less than a day"
    assert gst.span_words(5 * DAY) == "5 days"
    assert gst.span_words(40 * DAY) == "1 month, 10 days"
    assert gst.span_words(800 * DAY) == "2 years, 2 months"


def test_read_the_statistics(tmp_path):
    root = history(tmp_path)
    git(root, "tag", "v1")
    git(root, "branch", "other")
    stats = gst.read_stats(root / "src")  # (from a folder inside it)
    assert stats.root == root.resolve()
    assert [(a.name, a.commits) for a in stats.authors] == [("Ada", 3), ("Bob", 2), ("Cy", 1), ("T", 1)]
    assert stats.commits == 7 and stats.merges == 0
    assert (stats.branches, stats.tags, stats.remote_branches) == (2, 1, 0)
    assert stats.files == 3 and stats.size > 0
    assert stats.first == START and stats.last > START + 5 * DAY  # (T's first commit: now)
    assert stats.since(2.5 * DAY, now=START + 5 * DAY) == 3  # (the last three days' commits)


def test_no_repository_or_no_commits(tmp_path):
    (tmp_path / "plain").mkdir()
    assert gst.read_stats(tmp_path / "plain") is None or gs.find_repository(tmp_path) is not None
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    assert gst.read_stats(empty) is None


# ------------------------------------------------------------ the plot's lines
def test_a_staircase_of_commits_so_far():
    xs, ys = gsv.steps([10, 20], 0, 30)
    assert list(zip(xs, ys)) == [(0, 0), (10, 0), (10, 1), (20, 1), (20, 2), (30, 2)]


def test_the_most_active_people_get_a_line_the_rest_share_one():
    authors = [gst.Author(f"P{n}", "", [START + n] * (20 - n)) for n in range(gsv.MOST + 3)]
    stats = gst.RepoStats(Path("."), "h", authors)
    shown = gsv.lines(stats)
    assert [who for who, _times in shown[:gsv.MOST]] == [f"P{n}" for n in range(gsv.MOST)]
    who, times = shown[-1]
    assert who == f"{gsv.OTHERS} (3 people)" and len(times) == sum(20 - n for n in range(gsv.MOST, gsv.MOST + 3))
    assert len(gsv.lines(gst.RepoStats(Path("."), "h", authors[:2]))) == 2  # (few people: no Others)


# ------------------------------------------------------------ the panel
def test_switching_between_the_diff_and_the_stats(qtbot, tmp_path):
    root = history(tmp_path)
    panel = gp.GitPanel()
    qtbot.addWidget(panel)
    panel.show_status(gs.read_status(root))
    assert panel.view() == gp.DIFF and panel.view_buttons[gp.DIFF].isChecked()
    assert panel.stats_view.stats is None  # (not read until it's on show)
    with qtbot.waitSignal(panel.view_changed) as said:
        panel.view_buttons[gp.STATS].click()
    assert said.args == [gp.STATS] and panel.view() == gp.STATS
    assert panel.unified_button.isHidden()  # (the diff's)
    qtbot.waitUntil(lambda: panel.stats_view.stats is not None, timeout=10000)
    view = panel.stats_view
    assert view.tiles["commits"].value.text() == "7" and view.tiles["people"].value.text() == "4"
    assert view.people.topLevelItem(0).text(0) == "Ada" and view.people.topLevelItem(0).text(1) == "3"
    assert set(view.curves) == {"Ada", "Bob", "Cy", "T"}
    panel.set_view(gp.DIFF)
    assert panel.view() == gp.DIFF and not panel.unified_button.isHidden()


def test_read_again_when_head_moves_or_refresh_is_clicked(qtbot, tmp_path):
    root = history(tmp_path)
    panel = gp.GitPanel()
    qtbot.addWidget(panel)
    panel.set_view(gp.STATS)
    panel.show_status(gs.read_status(root))
    qtbot.waitUntil(lambda: panel.stats_view.stats is not None, timeout=10000)
    first = panel.stats_view.stats
    panel.show_status(gs.read_status(root))  # (HEAD the same: not read again)
    assert panel._stats_reading is None and panel.stats_view.stats is first
    commit_as(root, "Dee", START + 9 * DAY)
    panel.show_status(gs.read_status(root))  # (a new commit)
    qtbot.waitUntil(lambda: panel.stats_view.stats is not first, timeout=10000)
    assert panel.stats_view.tiles["commits"].value.text() == "8"
    second = panel.stats_view.stats
    panel.refresh_requested.connect(lambda: panel.show_status(gs.read_status(root)))
    panel.refresh_button.click()  # (read again whatever HEAD)
    qtbot.waitUntil(lambda: panel.stats_view.stats is not second, timeout=10000)


def test_choosing_people_highlights_their_lines(qtbot, tmp_path):
    root = history(tmp_path)
    view = gsv.GitStatsView()
    qtbot.addWidget(view)
    view.show_stats(gst.read_stats(root))
    view.people.topLevelItem(1).setSelected(True)  # (Bob)
    alpha = {who: curve.opts["pen"].color().alphaF() for who, curve in view.curves.items()}
    assert alpha["Bob"] == 1.0 and alpha["Ada"] < 0.5
    assert view.curves["Bob"].opts["pen"].widthF() > view.curves["Ada"].opts["pen"].widthF()
    view.people.clearSelection()
    assert all(curve.opts["pen"].color().alphaF() == 1.0 for curve in view.curves.values())


def test_messages_without_a_repository_or_commits(qtbot, tmp_path):
    panel = gp.GitPanel()
    qtbot.addWidget(panel)
    panel.set_view(gp.STATS)
    panel.show_message("Not in a git repository.")
    assert panel.stats_view.stack.currentWidget() is panel.stats_view.message
    assert "Not in a git repository" in panel.stats_view.message.text()
    empty = tmp_path / "empty"
    empty.mkdir()
    git(empty, "init", "-q")
    panel.show_status(gs.read_status(empty))
    assert "No commits" in panel.stats_view.message.text()


def test_the_window_remembers_the_view(qtbot, window, project, make_window):
    window.git_panel.view_buttons[gp.STATS].click()
    assert window.settings.value("git_view") == gp.STATS
    other = make_window(project / "fpm.toml")
    assert other.git_panel.view() == gp.STATS
    other.git_panel.view_buttons[gp.DIFF].click()
    assert other.settings.value("git_view") == gp.DIFF


# ------------------------------------------------------------ per month
def test_commits_in_each_month():
    from datetime import datetime

    def at(y, m, d):
        return int(datetime(y, m, d, 12).timestamp())

    authors = [gst.Author("Ada", "", [at(2024, 1, 5), at(2024, 1, 20), at(2024, 3, 2)]),
               gst.Author("Bob", "", [at(2024, 3, 9)])]
    edges, counts = gsv.per_month(gst.RepoStats(Path("."), "h", authors), now=at(2024, 4, 1))
    assert edges[0] == int(datetime(2024, 1, 1).timestamp()) and edges[-1] == int(datetime(2024, 5, 1).timestamp())
    assert len(edges) == 5  # (January to April, and when April ends)
    assert counts == [("Ada", [2, 0, 1, 0]), ("Bob", [0, 0, 1, 0])]
    assert gsv.month_number(at(2024, 12, 31)) + 1 == gsv.month_number(at(2025, 1, 1))  # (over a year's end)


def test_the_per_month_bars(qtbot, tmp_path):
    root = history(tmp_path)
    view = gsv.GitStatsView()
    qtbot.addWidget(view)
    view.show_stats(gst.read_stats(root))
    assert view.mode == gsv.TOTAL and all(isinstance(c, gsv.pg.PlotDataItem) for c in view.curves.values())
    with qtbot.waitSignal(view.mode_changed) as said:
        view.mode_buttons[gsv.MONTHLY].click()
    assert said.args == [gsv.MONTHLY] and view.mode_buttons[gsv.MONTHLY].isChecked()
    assert set(view.curves) == {"Ada", "Bob", "Cy", "T"}
    assert all(isinstance(c, gsv.pg.BarGraphItem) for c in view.curves.values())
    assert "a month" in view.plot.getAxis("left").labelText
    edges, counts = view.months
    assert sum(sum(months) for _who, months in counts) == 7
    text = view.month_text(START + DAY)  # (the history's month: Ada's, Bob's, Cy's commits)
    assert "6 commits" in text and "Ada: 3" in text and "Bob: 2" in text and "Cy: 1" in text
    assert view.month_text(edges[0] - DAY) == ""  # (before the first month)
    view.people.topLevelItem(0).setSelected(True)  # (Ada: her bars as they are, the others' faint)
    alpha = {who: bars.opts["brush"].color().alphaF() for who, bars in view.curves.items()}
    assert alpha["Ada"] == 1.0 and alpha["Bob"] < 0.5
    view.set_mode(gsv.TOTAL)
    assert all(isinstance(c, gsv.pg.PlotDataItem) for c in view.curves.values()) and view.months is None


def test_the_window_remembers_the_plot(qtbot, window, project, make_window):
    window.git_panel.stats_view.mode_buttons[gsv.MONTHLY].click()
    assert window.settings.value("git_stats_plot") == gsv.MONTHLY
    assert make_window(project / "fpm.toml").git_panel.stats_view.mode == gsv.MONTHLY


def test_clicking_the_chosen_person_again_unchooses_them(qtbot, tmp_path):
    from qtpy.QtCore import Qt
    from qtpy.QtTest import QTest

    view = gsv.GitStatsView()
    qtbot.addWidget(view)
    view.resize(900, 500)
    view.show_stats(gst.read_stats(history(tmp_path)))
    view.show()
    people = view.people

    def click(row: int, modifiers=Qt.NoModifier):
        rect = people.visualItemRect(people.topLevelItem(row))
        QTest.mouseClick(people.viewport(), Qt.LeftButton, modifiers, rect.center())

    def faded() -> set[str]:
        return {who for who, c in view.curves.items() if c.opts["pen"].color().alphaF() < 1}

    click(1)  # (Bob)
    assert [i.text(0) for i in people.selectedItems()] == ["Bob"] and "Ada" in faded()
    click(1)  # (again: no one)
    assert people.selectedItems() == [] and faded() == set()
    click(0)
    click(1, Qt.ControlModifier)  # (two people: a click on one of them chooses only that one)
    click(0)
    assert [i.text(0) for i in people.selectedItems()] == ["Ada"]
