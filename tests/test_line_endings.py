"""Edit → Convert Line Endings."""

from __future__ import annotations

from conftest import open_editor, write_files


def tab_text(window, editor) -> str:
    return window.tabs.tabText(window.tabs.indexOf(editor))


def test_convert_to_windows_and_back(window, project):
    write_files(project, {"src/le.f90": b"program p\n  x = 1\nend program\n"})
    path = project / "src/le.f90"
    editor = open_editor(window, path)
    assert editor.newline == "\n" and not editor.is_dirty()
    window._check_line_endings()
    assert window.line_ending_actions["\n"].isChecked() and not window.line_ending_actions["\r\n"].isChecked()
    window.line_ending_actions["\r\n"].trigger()
    assert editor.is_dirty() and tab_text(window, editor).startswith("• ")  # (unsaved)
    assert "CRLF" in editor.status.text() and window.line_ending_actions["\r\n"].isChecked()
    assert "not saved yet" in window.statusBar().currentMessage()
    window.line_ending_actions["\n"].trigger()  # (back as it was on disk: nothing to save)
    assert not editor.is_dirty() and not tab_text(window, editor).startswith("• ")
    window.line_ending_actions["\r\n"].trigger()
    assert editor.save()
    assert path.read_bytes() == b"program p\r\n  x = 1\r\nend program\r\n"
    assert not editor.is_dirty() and not tab_text(window, editor).startswith("• ")
    window.line_ending_actions["\r\n"].trigger()  # (already)
    assert "already has Windows" in window.statusBar().currentMessage()


def test_a_conversion_stays_unsaved_through_undo(window, project):
    write_files(project, {"src/le.f90": b"program p\nend program\n"})
    editor = open_editor(window, project / "src/le.f90")
    editor.editor.appendPlainText("! edit")
    window.convert_line_endings("\r\n")
    editor.editor.undo()  # (the edit is gone: the line endings still differ from the file's)
    assert not editor.editor.document().isModified() and editor.is_dirty()


def test_mixed_and_old_mac_line_endings(window, project):
    write_files(project, {"src/mixed.f90": b"a\r\nb\nc\rd\n"})
    path = project / "src/mixed.f90"
    editor = open_editor(window, path)
    assert editor.newline == "\r\n"  # (it has some)
    assert editor.editor.toPlainText() == "a\nb\nc\nd\n"  # (Qt reads the lone one as a line break)
    window.convert_line_endings("\n")
    assert editor.save() and path.read_bytes() == b"a\nb\nc\nd\n"  # (the same everywhere)


def test_reload_question_declined_keeps_the_conversion(window, project, monkeypatch):
    from qtpy.QtWidgets import QMessageBox

    write_files(project, {"src/le.f90": b"program p\nend program\n"})
    path = project / "src/le.f90"
    editor = open_editor(window, path)
    window.convert_line_endings("\r\n")
    path.write_bytes(b"program q\nend program\n")  # (changed by another program)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    editor._check_disk()
    assert editor.newline == "\r\n" and editor.is_dirty()  # (still the user's choice)


def test_menu_needs_a_file(window):
    assert not window.line_endings_menu.isEnabled()
