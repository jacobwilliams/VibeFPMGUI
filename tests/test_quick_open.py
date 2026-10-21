"""Edit → Go To → Go to File… (⌘P): any file the Project files tree lists,
found by typing part of its name; git-ignored ones only with Ignored ticked."""

from __future__ import annotations

from qtpy.QtCore import Qt
from qtpy.QtTest import QTest

from vibefpmgui.project_tree import ProjectFile
from vibefpmgui.quick_open import FILE_ROLE, ranked, score


def pf(relative: str, dependency: bool = False) -> ProjectFile:
    return ProjectFile(f"/p/{relative}", relative, "dep" if dependency else "proj", dependency)


FILES = [pf("src/demo_stats.f90"), pf("src/demo.f90"), pf("app/main.f90"), pf("test/check_stats.f90"),
         pf("src/stats.f90", dependency=True), pf("README.md")]


def names(files) -> list[str]:
    return [f.relative for f in files]


def test_the_name_first_then_the_path():
    assert score("demo", FILES[1]) == 1000  # (the name's start)
    assert score("stat", FILES[0]) == 800   # (in the name)
    assert score("dstat", FILES[0]) == 600  # (its letters in order)
    assert score("src/", FILES[1]) == 400   # (in the path)
    assert score("zzz", FILES[1]) is None
    assert names(ranked("demo", FILES)) == ["src/demo.f90", "src/demo_stats.f90"]
    # (a name starting so first, a dependency's too; then names with it inside, the shorter path first)
    assert names(ranked("stats", FILES)) == ["src/stats.f90", "src/demo_stats.f90", "test/check_stats.f90"]


def test_ties_go_to_the_project_s_own_files():
    own, dep = pf("src/stats.f90"), pf("src/stats.f90", dependency=True)
    assert ranked("stats", [dep, own]) == [own, dep]


def test_nothing_typed_the_open_files_first():
    assert names(ranked("", FILES, ["/p/app/main.f90"]))[0] == "app/main.f90"


def test_the_tree_s_files_and_its_ignored_checkbox(window, project):
    tree = window.project_tree
    tree.include_ignored.setChecked(False)
    shown = {f.relative: f for f in tree.all_files()}
    assert {"src/foo.f90", "app/main.f90", "test/check.f90", "fpm.toml"} <= set(shown)
    assert shown["src/dl.f90"].dependency and shown["src/dl.f90"].package == "dl"  # (the local dependency's)
    assert not {"out/x.f90", "run.log", ".hidden/h.f90"} & set(shown)  # (ignored by git, or hidden)
    tree.include_ignored.setChecked(True)
    shown = {f.relative: f for f in tree.all_files()}
    assert {"out/x.f90", "run.log"} <= set(shown) and shown["run.log"].hidden


def test_command_p_opens_a_file(window, project, qtbot):
    window.project_tree.include_ignored.setChecked(False)
    assert window.go_to_file_action.shortcut().toString() == "Ctrl+P"  # (⌘P on macOS)
    window.go_to_file_action.trigger()
    palette = window._quick_open
    qtbot.waitUntil(palette.isVisible, timeout=3000)
    palette.field.setText("foo")
    assert palette.list.item(0).data(FILE_ROLE).relative == "src/foo.f90"
    assert "left out" in palette.note.text()
    QTest.keyClick(palette.field, Qt.Key_Return)
    editor = window.current_editor()
    assert editor is not None and editor.path == (project / "src/foo.f90").resolve()
    assert not palette.isVisible()


def test_one_palette_at_a_time(window, qtbot):
    """Go to File and the Command Palette: opening one closes the other."""
    window.show_command_palette()
    commands = window._command_palette
    qtbot.waitUntil(commands.isVisible, timeout=3000)
    window.go_to_file_action.trigger()
    files = window._quick_open
    qtbot.waitUntil(files.isVisible, timeout=3000)
    assert not commands.isVisible()
    window.show_command_palette()
    qtbot.waitUntil(commands.isVisible, timeout=3000)
    assert not files.isVisible()
    commands.close()


def test_its_icon_in_the_menu_and_the_palette(window, qtbot):
    from qtpy.QtGui import QPalette
    from qtpy.QtWidgets import QApplication

    from vibefpmgui.icons import make_icon

    action = window.go_to_file_action
    color = QApplication.palette().color(QPalette.WindowText)
    assert action.icon().pixmap(18, 18).toImage() == make_icon("find-file", color).pixmap(18, 18).toImage()
    action.trigger()
    palette = window._quick_open
    qtbot.waitUntil(palette.isVisible, timeout=3000)
    assert palette.icon_action.icon().cacheKey() == action.icon().cacheKey()  # (at the field's start)
    palette.close()
