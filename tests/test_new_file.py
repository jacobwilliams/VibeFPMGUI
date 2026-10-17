"""File → New File: an editor tab for a file not saved yet."""

from __future__ import annotations

import json
from pathlib import Path

from qtpy.QtWidgets import QFileDialog, QMessageBox


def new_tab(window):
    window.new_file_action.trigger()
    return window.current_editor()


def test_a_new_file_has_no_type_yet(window, project):
    editor = new_tab(window)
    assert editor.untitled and editor.path.name == "Untitled-1" and not editor.path.exists()
    assert editor.path.parent == project.resolve()  # (saved in the project's folder, unless another is chosen)
    assert window.tabs.tabText(window.tabs.indexOf(editor)) == "Untitled-1"
    assert "not saved yet" in window.tabs.tabToolTip(window.tabs.indexOf(editor))
    assert editor.highlighter.lexer.name == "Text only" and not editor.can_lint()  # (no colors, no checks)
    window._update_editor_actions()
    assert not window.comment_action.isEnabled() and not window.format_action.isEnabled()
    assert new_tab(window).path.name == "Untitled-2"
    window._save_session()
    saved = json.loads(window.settings.value(window._session_key(), "{}"))
    assert not any("Untitled" in f["path"] for f in saved.get("files", []))  # (nothing to open next time)


def test_closing_asks_only_when_something_was_typed(window, project, monkeypatch):
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Cancel)
    editor = new_tab(window)
    window.close_editor(window.tabs.indexOf(editor))
    assert asked == [] and window.tabs.indexOf(editor) < 0  # (empty: closed)
    editor = new_tab(window)
    editor.editor.insertPlainText("program p\nend program p\n")
    assert editor.is_dirty()
    window.close_editor(window.tabs.indexOf(editor))
    assert len(asked) == 1 and window.tabs.indexOf(editor) >= 0  # (Cancel: still open)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    window.close_editor(window.tabs.indexOf(editor))
    assert window.tabs.indexOf(editor) < 0 and not editor.path.exists()


def test_saving_gives_it_a_name_and_a_type(window, project, monkeypatch):
    editor = new_tab(window)
    editor.editor.insertPlainText("program p\n  print *, 'hi'\nend program p\n")
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: ("", ""))
    assert not editor.save()  # (Save: Save As first; cancelled)
    assert editor.untitled and not editor.path.exists()
    target = project / "src" / "hello.f90"
    asked = []
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: asked.append(a[2]) or (str(target), ""))
    window.save_action.trigger()
    assert Path(asked[0]).name.startswith("Untitled-")  # (the dialog starts at its name)
    assert target.read_text() == "program p\n  print *, 'hi'\nend program p\n"
    assert not editor.untitled and editor.path == target.resolve() and not editor.is_dirty()
    assert editor.highlighter.lexer.name.startswith("Fortran") and editor.editor.comment_prefix == "!"
    assert window.tabs.tabText(window.tabs.indexOf(editor)) == "hello.f90"
    assert str(target.resolve()) in window.project_tree._items  # (in Project files)
    window._save_session()
    saved = json.loads(window.settings.value(window._session_key(), "{}"))
    assert str(target.resolve()) in [f["path"] for f in saved["files"]]


def test_going_to_a_new_file_stays_in_the_app(window, project, monkeypatch):
    from qtpy.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    editor = new_tab(window)
    editor.editor.insertPlainText("a\nb\nc\n")
    window.open_file(str(project / "src/foo.f90"), force_editor=True)
    window.navigate_to(str(editor.path), 2, 0)  # (e.g. from the Problems tab, or Go Back)
    assert opened == [] and window.current_editor() is editor and editor.editor.textCursor().blockNumber() == 1


def test_no_spelling_until_it_has_a_type(window, project, monkeypatch):
    from vibefpmgui import spelling

    checked = []
    monkeypatch.setattr(spelling, "check", lambda *a, **k: checked.append(1) or [])
    editor = new_tab(window)
    editor.editor.insertPlainText("integer :: nelems  ! a mispeled word\n")
    editor.check_spelling()
    assert checked == []
