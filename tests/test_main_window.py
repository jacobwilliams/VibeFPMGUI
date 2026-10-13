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


# ------------------------------------------------- code review: step 1
def test_no_two_actions_share_a_shortcut(window):
    from qtpy.QtCore import Qt
    from qtpy.QtGui import QAction

    seen = {}
    for action in window.findChildren(QAction):
        if action.shortcutContext() == Qt.WidgetShortcut:
            continue  # (shown in a context menu only)
        for key in action.shortcuts():
            text = key.toString()
            assert text not in seen, f"{text}: {seen[text]!r} and {action.text()!r}"
            seen[text] = action.text()
    assert "Ctrl+Shift+N" in seen and "Ctrl+Shift+O" in seen


def busy(window):
    """A command "running" (a process that just waits), as fpm would be."""
    import sys

    from qtpy.QtCore import QProcess

    process = QProcess(window)
    process.start(sys.executable, ["-c", "import time; time.sleep(60)"])
    assert process.waitForStarted(5000)
    window.process = process
    return process


def spy(monkeypatch, obj, name, calls):
    real = getattr(obj, name)
    monkeypatch.setattr(obj, name, lambda *a, **k: calls.append(name) or real(*a, **k))


def test_no_to_quitting_while_running_leaves_everything_working(qtbot, window, project, monkeypatch):
    from qtpy.QtCore import QProcess
    from qtpy.QtWidgets import QMessageBox

    editor = open_editor(window, project / "src/foo.f90")
    process = busy(window)
    calls = []
    for obj, name in ((editor, "close_down"), (window.lsp_client, "stop"),
                      (window.search_panel, "shutdown"), (window.terminal_panel, "shutdown")):
        spy(monkeypatch, obj, name, calls)
    answers = iter([QMessageBox.No, QMessageBox.Yes])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: next(answers))
    assert not window.close()  # (No: still running, nothing shut down)
    assert calls == [] and window.isVisible() and process.state() == QProcess.Running
    assert window.close()  # (Yes: the command is stopped, then everything shuts down)
    assert set(calls) == {"close_down", "stop", "shutdown"}
    assert process.state() == QProcess.NotRunning


def test_no_to_opening_another_project_while_running_keeps_its_files(window, project, conflict_project,
                                                                      monkeypatch):
    from qtpy.QtWidgets import QMessageBox

    editor = open_editor(window, project / "src/foo.f90")
    busy(window)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    window.open_manifest(conflict_project / "fpm.toml")
    assert window.manifest_path == (project / "fpm.toml").resolve()
    assert window.tabs.indexOf(editor) >= 0  # (still open)
    window.stop()


def test_a_character_split_between_reads_is_shown_whole(qtbot, window, tmp_path):
    import os
    import sys

    program = tmp_path / ("show.exe" if os.name == "nt" else "show")
    script = tmp_path / "show.py"
    # "°C" with the two bytes of "°" written separately, half a second apart.
    script.write_text("import sys, time\nout = sys.stdout.buffer\n"
                      "out.write(b'temp 21\\xc2'); out.flush(); time.sleep(0.5)\n"
                      "out.write(b'\\xb0C\\n'); out.flush()\n")
    if os.name == "nt":
        import pytest
        pytest.skip("needs a script that runs as a program")
    program.write_text(f"#!{sys.executable}\nexec(open({str(script)!r}).read())\n")
    program.chmod(0o755)
    window.run_program(str(program))
    qtbot.waitUntil(lambda: window.process is None, timeout=15_000)
    text = window.console.toPlainText()
    assert "temp 21°C" in text and "�" not in text


# ------------------------------------------------- code review: step 2
def test_dialogs_are_let_go_when_closed(qtbot, window, monkeypatch):
    from qtpy.QtCore import QCoreApplication, QEvent
    from qtpy.QtWidgets import QDialog

    from vibefpmgui import dep_updates
    from vibefpmgui.about import AboutDialog
    from vibefpmgui.rename_dialog import RenamePreviewDialog

    for cls in (AboutDialog, dep_updates.UpdatesDialog, RenamePreviewDialog):
        monkeypatch.setattr(cls, "exec", lambda self: QDialog.Rejected)
    window.show_about()
    window.check_dependency_updates()
    window._preview_rename("old", "new", {})
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    assert window.findChildren(QDialog) == []


def test_the_full_output_log_is_removed_on_quit(window):
    window._begin_output()  # (a command starting: its log is opened)
    window._append("some output\n", colored=True)
    log = window._full_output_path
    assert log.exists()
    assert window.close()
    assert not log.exists()
