"""The built-in editor: change bars, find, edits, word wrap, minimap."""

from __future__ import annotations

from conftest import open_editor
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import QPlainTextEdit

from vibefpmgui.editor_shared import line_changes
from vibefpmgui.lsp import TextEdit


def test_line_changes():
    opened = "a\nb\nc\nd"
    saved = "a\nB\nc\nd"           # line 2 changed and saved
    current = "a\nB\nc\nD\ne"      # then 4 changed and 5 added, not saved
    marks, deletions = line_changes(opened, saved, current)
    assert marks == {2: "saved", 4: "unsaved", 5: "unsaved"}
    assert deletions == {}
    marks, deletions = line_changes("a\nb\nc", "a\nb\nc", "a\nc")
    assert marks == {} and deletions == {2: ("unsaved", 1)}


def test_change_bars_follow_edits(qtbot, window, project):
    editor = open_editor(window, project / "src/foo.f90")
    cursor = QTextCursor(editor.editor.document().findBlockByNumber(1))
    cursor.insertText("! new\n")
    qtbot.waitUntil(lambda: editor.editor.change_marks.get(2) == "unsaved", timeout=5_000)
    editor.save()
    qtbot.waitUntil(lambda: editor.editor.change_marks.get(2) == "saved", timeout=5_000)


def test_find_bar(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    bar = editor.find_bar
    bar.open_bar()
    bar.find_edit.setText("count")
    bar.word_check.setChecked(True)
    assert len(editor.editor.find_selections) == 4
    bar.replace_edit.setText("n")
    bar.replace_all()
    assert editor.editor.toPlainText().count("count_all") == 1
    assert "n = n + 1 ! n" in editor.editor.toPlainText()
    editor.editor.undo()  # (one step)
    assert "count = count + 1" in editor.editor.toPlainText()


def test_apply_text_edits_is_one_undo_step(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    before = editor.editor.toPlainText()
    editor.apply_text_edits([TextEdit(0, 7, 0, 10, "bar"), TextEdit(7, 11, 7, 14, "bar")])
    text = editor.editor.toPlainText()
    assert text.startswith("module bar\n") and text.endswith("end module bar\n")
    editor.editor.undo()
    assert editor.editor.toPlainText() == before


def test_save_writes_the_file(window, project):
    editor = open_editor(window, project / "app/main.f90")
    editor.editor.moveCursor(QTextCursor.End)
    editor.editor.insertPlainText("! saved\n")
    assert editor.is_dirty()
    assert editor.save()
    assert not editor.is_dirty()
    assert (project / "app/main.f90").read_text().endswith("! saved\n")


def long_file(project):
    lines = ["module wrapdemo", "contains"]
    lines += [f"  ! line {i}: " + ("words that keep going past the edge " * 6 if i % 4 == 0 else "")
              for i in range(80)]
    path = project / "src/wrap.f90"
    path.write_text("\n".join(lines) + "\nend module wrapdemo\n")
    return path


def test_word_wrap(qtbot, window, make_window, project):
    editor = open_editor(window, long_file(project))
    code = editor.editor
    assert window.word_wrap_action.shortcut().toString() == "Alt+Z"
    assert code.lineWrapMode() == QPlainTextEdit.NoWrap
    assert code.horizontalScrollBar().maximum() > 0
    window.word_wrap_action.trigger()
    qtbot.waitUntil(lambda: code.horizontalScrollBar().maximum() == 0, timeout=2_000)
    assert code.lineWrapMode() == QPlainTextEdit.WidgetWidth
    # Remembered, and used by new windows' editors.
    again = make_window(project / "fpm.toml")
    assert again.word_wrap_action.isChecked()
    assert open_editor(again, project / "src/foo.f90").editor.lineWrapMode() == QPlainTextEdit.WidgetWidth


def test_wrapped_line_highlight_covers_every_row(qtbot, window, project):
    editor = open_editor(window, long_file(project))
    window.word_wrap_action.setChecked(True)
    code = editor.editor
    block = code.document().findBlockByNumber(2)  # "! line 0: …", long
    cursor = code.textCursor()
    cursor.setPosition(block.position())
    code.setTextCursor(cursor)
    qtbot.waitUntil(lambda: block.layout().lineCount() > 1, timeout=2_000)
    rows = block.layout().lineCount()
    qtbot.waitUntil(lambda: sum(s.cursor.block() == block for s in code.extraSelections()) == rows,
                    timeout=2_000)


def test_minimap_follows_wrapped_lines(qtbot, window, project):
    editor = open_editor(window, long_file(project))
    window.word_wrap_action.setChecked(True)
    minimap = editor.minimap
    minimap._scroll_to_line(20)
    qtbot.waitUntil(lambda: editor.editor.firstVisibleBlock().blockNumber() == 20, timeout=2_000)
    assert minimap._first_line() == 20
    assert minimap._view_box().top() == 20 * 2.0 - minimap._offset()
