"""Edit → Trim Trailing Whitespace, and trimming on save."""

from __future__ import annotations

from conftest import open_editor, write_files

from vibefpmgui.editor import EditorTab

SPACED = "program p  \n  x = 1\t \n\n  y = 2\nend program \n"
TRIMMED = "program p\n  x = 1\n\n  y = 2\nend program\n"


def test_trim_trailing_whitespace(window, project):
    write_files(project, {"src/t.f90": SPACED})
    editor = open_editor(window, project / "src/t.f90")
    window.trim_action.trigger()
    assert editor.editor.toPlainText() == TRIMMED and editor.is_dirty()
    assert "from 3 lines" in window.statusBar().currentMessage()
    editor.editor.undo()  # (one step)
    assert editor.editor.toPlainText() == SPACED and not editor.is_dirty()
    window.trim_action.trigger()
    window.trim_action.trigger()
    assert "from no lines" in window.statusBar().currentMessage()


def test_trimmed_on_save(window, project):
    assert window.trim_on_save_action.isChecked() and EditorTab.trim_on_save  # (on by default)
    write_files(project, {"src/t.f90": SPACED, "notes.md": "a  \nb\n"})
    editor = open_editor(window, project / "src/t.f90")
    assert editor.save()
    assert (project / "src/t.f90").read_text() == TRIMMED and editor.editor.toPlainText() == TRIMMED
    notes = open_editor(window, project / "notes.md")  # (Markdown: two spaces are a line break)
    notes.editor.appendPlainText("c  ")
    assert notes.save()
    assert (project / "notes.md").read_text() == "a  \nb\n\nc  "


def test_not_trimmed_on_save_when_off(window, project):
    window.trim_on_save_action.setChecked(False)
    try:
        assert not EditorTab.trim_on_save and window.settings.value("trim_on_save", type=bool) is False
        write_files(project, {"src/t.f90": SPACED})
        editor = open_editor(window, project / "src/t.f90")
        editor.editor.appendPlainText("! edit ")
        assert editor.save()
        assert (project / "src/t.f90").read_text() == SPACED + "\n! edit "
    finally:
        window.trim_on_save_action.setChecked(True)


def test_trim_needs_a_file(window):
    window._update_editor_actions()
    assert window.current_editor() is None and not window.trim_action.isEnabled()
