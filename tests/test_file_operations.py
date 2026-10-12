"""Renaming and deleting files from the Project files tree."""

from __future__ import annotations

import pytest
from conftest import open_editor, write_files
from qtpy.QtCore import QFile
from qtpy.QtWidgets import QInputDialog, QMessageBox


@pytest.fixture(autouse=True)
def no_unexpected_dialogs(monkeypatch):
    """A dialog a test doesn't answer fails it (instead of waiting for a click)."""
    def unexpected(*args, **kwargs):
        raise AssertionError(f"unexpected dialog: {args[1:3]}")

    for name in ("question", "warning", "information", "critical"):
        monkeypatch.setattr(QMessageBox, name, unexpected)
    monkeypatch.setattr(QInputDialog, "getText", unexpected)


@pytest.fixture
def trash(monkeypatch):
    """Files "moved to the trash" (with their text then): removed, not really trashed."""
    trashed: dict[str, str] = {}

    def move_to_trash(path):
        from pathlib import Path
        trashed[Path(path).name] = Path(path).read_text()
        Path(path).unlink()
        return True, "/trash/" + Path(path).name

    monkeypatch.setattr(QFile, "moveToTrash", move_to_trash)
    return trashed


@pytest.fixture
def notes(project):
    write_files(project, {"src/notes.f90": "! notes\n"})
    return project / "src/notes.f90"


def answer(monkeypatch, *answers):
    replies = iter(answers)
    asked = []

    def ask(_parent, title, text, *rest):
        asked.append(text)
        return next(replies)

    monkeypatch.setattr(QMessageBox, "question", ask)
    return asked


def rename_to(monkeypatch, name):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (name, True))


def test_menu_items_for_files_only(window, notes):
    tree = window.project_tree
    tree.refresh()
    tree.reveal(str(notes))
    rect = tree.view.visualRect(tree.view.currentIndex())
    texts = [a.text() for a in tree.context_menu(rect.center()).actions()]
    assert "Rename…" in texts and "Move to Trash…" in texts
    tree.reveal(str(notes.parent))  # (a folder: neither)
    rect = tree.view.visualRect(tree.view.currentIndex())
    texts = [a.text() for a in tree.context_menu(rect.center()).actions()]
    assert "Rename…" not in texts and "Move to Trash…" not in texts


def test_menu_items_ask_the_window(qtbot, window, notes, monkeypatch):
    rename_to(monkeypatch, "")  # (the window's questions: Cancel)
    answer(monkeypatch, QMessageBox.Cancel)
    tree = window.project_tree
    tree.reveal(str(notes))
    menu = tree.context_menu(tree.view.visualRect(tree.view.currentIndex()).center())
    for text, signal in (("Rename…", tree.rename_requested), ("Move to Trash…", tree.delete_requested)):
        with qtbot.waitSignal(signal) as blocker:
            next(a for a in menu.actions() if a.text() == text).trigger()
        assert blocker.args == [str(notes)]


def test_rename_an_open_file_with_unsaved_changes(qtbot, window, notes, monkeypatch):
    editor = open_editor(window, notes)
    editor.editor.appendPlainText("! not saved yet")
    rename_to(monkeypatch, "journal.f90")
    window.rename_file(str(notes))
    new = notes.with_name("journal.f90")
    assert new.read_text() == "! notes\n" and not notes.exists()
    assert editor.path == new.resolve() and editor.is_dirty()  # (the tab follows it, edits and all)
    assert "! not saved yet" in editor.editor.toPlainText()
    index = window.tabs.indexOf(editor)
    assert window.tabs.tabText(index) == "• journal.f90" and window.tabs.tabToolTip(index) == str(new.resolve())
    assert editor.save() and "! not saved yet" in new.read_text()  # (saves under the new name)
    assert not notes.exists()


def test_rename_problems(window, notes, project, monkeypatch):
    write_files(project, {"src/taken.f90": "! taken\n"})
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *a: warned.append(text))
    for name in ("taken.f90", "a/b.f90"):
        rename_to(monkeypatch, name)
        window.rename_file(str(notes))
    assert len(warned) == 2 and "already a file named taken.f90" in warned[0]
    assert notes.exists() and (project / "src/taken.f90").read_text() == "! taken\n"
    rename_to(monkeypatch, "")  # (Cancel, or nothing typed: nothing happens)
    window.rename_file(str(notes))
    assert notes.exists() and len(warned) == 2


def test_rename_a_file_that_isnt_open(window, notes, monkeypatch):
    rename_to(monkeypatch, "Notes.F90")  # (only the case changes: the same file on macOS and Windows)
    window.rename_file(str(notes))
    assert "Notes.F90" in [p.name for p in notes.parent.iterdir()]
    assert window.current_editor() is None


def test_delete_asks_first(window, notes, trash, monkeypatch):
    asked = answer(monkeypatch, QMessageBox.Cancel, QMessageBox.Yes)
    editor = open_editor(window, notes)
    window.delete_file(str(notes))
    assert notes.exists() and not trash  # (Cancel)
    window.delete_file(str(notes))
    assert not notes.exists() and trash == {"notes.f90": "! notes\n"}
    assert "Move notes.f90 to the trash?" in asked[0]
    assert window.tabs.indexOf(editor) == -1  # (its tab is closed)


def test_delete_with_unsaved_changes(window, notes, trash, monkeypatch):
    editor = open_editor(window, notes)
    editor.editor.appendPlainText("! edit")
    asked = answer(monkeypatch, QMessageBox.Cancel, QMessageBox.Save)
    window.delete_file(str(notes))
    assert notes.exists() and window.tabs.indexOf(editor) >= 0  # (Cancel: nothing changes)
    assert "unsaved changes" in asked[0]
    window.delete_file(str(notes))
    assert "! edit" in trash["notes.f90"]  # (saved first: the edits are in the trash too)
    assert window.tabs.indexOf(editor) == -1


def test_delete_discarding_unsaved_changes(window, notes, trash, monkeypatch):
    editor = open_editor(window, notes)
    editor.editor.appendPlainText("! edit")
    answer(monkeypatch, QMessageBox.Discard)
    window.delete_file(str(notes))
    assert trash["notes.f90"] == "! notes\n" and window.tabs.indexOf(editor) == -1


def test_delete_that_fails(window, notes, monkeypatch):
    answer(monkeypatch, QMessageBox.Yes)
    monkeypatch.setattr(QFile, "moveToTrash", lambda path: (False, ""))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _p, _t, text, *a: warned.append(text))
    editor = open_editor(window, notes)
    window.delete_file(str(notes))
    assert notes.exists() and warned and window.tabs.indexOf(editor) >= 0
