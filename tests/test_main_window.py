"""The main window: opening a project, the graph, the file tree, sessions."""

from __future__ import annotations

from conftest import close_window, open_editor

from vibefpmgui.icons import fortran_file_icon


def test_opens_the_project(window, project):
    assert window.graph is not None
    assert set(window.graph.packages) == {"proj", "dl"}
    assert "proj" in window.windowTitle() or window.manifest_path == (project / "fpm.toml")


def test_without_a_project(make_window):
    window = make_window(None)
    assert window.graph is None
    window.find_in_files()
    window.search_panel.find_edit.setText("x")
    window.search_panel.start_search()  # (nothing to search: no error)
    assert not window.search_panel._results


def test_menu_shortcuts(window):
    from qtpy.QtWidgets import QMenu

    # (every menu as the window's child, not through action.menu(): PySide
    # invalidates a menu fetched that way)
    found = {action.text(): action.shortcut().toString()
             for menu in window.findChildren(QMenu) for action in menu.actions() if action.text()}
    assert found["Find in Files…"] == "Ctrl+Shift+F"
    assert found["Replace in Files…"] == "Ctrl+Shift+H"
    assert found["Word Wrap"] == "Alt+Z"


def test_project_tree_icons(window, project):
    tree = window.project_tree
    fortran = fortran_file_icon().cacheKey()
    assert tree.icon_for(project / "src/foo.f90").cacheKey() == fortran
    assert tree.icon_for(project / "src/FOO.F90").cacheKey() == fortran
    assert tree.icon_for(project / "src/defs.inc").cacheKey() == fortran_file_icon(include=True).cacheKey()
    assert tree.icon_for(project / "README.md").cacheKey() != fortran


def test_version_conflict_box(make_window, conflict_project):
    window = make_window(conflict_project / "fpm.toml")
    window.show_package("test-drive")
    html = window.details.toHtml()
    assert "Version conflict" in html
    assert "libA" in html and "v0.4.0" in html


def test_session_reopens_files(make_window, project):
    first = make_window(project / "fpm.toml")
    open_editor(first, project / "src/foo.f90")
    open_editor(first, project / "app/main.f90")
    close_window(first)
    second = make_window(project / "fpm.toml")
    assert sorted(e.path.name for e in second.editors()) == ["foo.f90", "main.f90"]


def visible_files(tree) -> set[str]:
    """Names of the files the Project files tree shows (through its filter)."""
    from vibefpmgui.project_tree import KIND_ROLE

    names, model = set(), tree.proxy

    def walk(parent):
        for row in range(model.rowCount(parent)):
            index = model.index(row, 0, parent)
            if index.data(KIND_ROLE) == "file":
                names.add(index.data())
            walk(index)

    walk(tree.view.rootIndex())
    return names


def test_project_tree_filter(qtbot, window):
    tree = window.project_tree
    tree.filter_edit.setText("check")
    qtbot.waitUntil(lambda: visible_files(tree) == {"check.f90"}, timeout=5_000)
    tree.filter_edit.setText("x.f90")  # (out/ is ignored by git: only with "Ignored")
    qtbot.waitUntil(lambda: visible_files(tree) == set(), timeout=5_000)
    tree.include_ignored.setChecked(True)
    qtbot.waitUntil(lambda: visible_files(tree) == {"x.f90"}, timeout=5_000)


def test_clear_settings_and_quit(qtbot, window, project, monkeypatch):
    from qtpy.QtWidgets import QApplication, QMessageBox

    from vibefpmgui.main_window import MainWindow

    quits = []
    monkeypatch.setattr(QApplication, "quit", lambda: quits.append(True))
    answers = iter([QMessageBox.Cancel, QMessageBox.Yes])
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: next(answers))
    window.settings.setValue("spelling_words", "wombat")
    window.clear_settings_and_quit()  # (Cancel: nothing happens)
    assert window.isVisible() and not quits and window.settings.value("spelling_words") == "wombat"
    window.clear_settings_and_quit()
    assert not window.isVisible() and quits == [True]
    assert window.settings.allKeys() == []  # (not even what closing the window would save)
    again = MainWindow(None)
    qtbot.addWidget(again)
    assert again.manifest_path is None  # (like the first start: no last project)


def test_word_wrap_reaches_the_output(window, make_window, project):
    from qtpy.QtWidgets import QPlainTextEdit

    def modes(w):
        return {w.console.lineWrapMode(), w.test_output.lineWrapMode()}

    window.word_wrap_action.setChecked(False)
    assert modes(window) == {QPlainTextEdit.NoWrap}  # (long lines scroll sideways)
    window.word_wrap_action.setChecked(True)
    assert modes(window) == {QPlainTextEdit.WidgetWidth}
    assert modes(make_window(project / "fpm.toml")) == {QPlainTextEdit.WidgetWidth}  # (remembered)
