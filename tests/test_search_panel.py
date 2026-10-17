"""Find in Files: the Search tab in the window."""

from __future__ import annotations

from conftest import open_editor
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import QMessageBox


def search_for(qtbot, window, text: str, **checks):
    panel = window.search_panel
    for name, value in checks.items():
        getattr(panel, name).setChecked(value)
    panel.find_edit.setText(text)
    panel.start_search()
    qtbot.waitUntil(lambda: panel._worker is None and not panel._incoming, timeout=10_000)  # (all listed)
    return panel


def result_files(panel) -> list[str]:
    return sorted(str(p.name) for p in panel._results)


def test_shortcut_searches_the_selection(qtbot, window, project):
    assert window.find_files_action.shortcut().toString() == "Ctrl+Shift+F"
    editor = open_editor(window, project / "src/foo.f90")
    cursor = editor.editor.textCursor()
    block = editor.editor.document().findBlockByNumber(2)
    cursor.setPosition(block.position() + 13)
    cursor.setPosition(block.position() + 18, QTextCursor.KeepAnchor)  # "count"
    editor.editor.setTextCursor(cursor)
    window.find_files_action.trigger()
    panel = window.search_panel
    assert window.panel_in_front(panel)
    assert panel.find_edit.text() == "count"
    qtbot.waitUntil(lambda: panel._worker is None and bool(panel._results), timeout=10_000)
    assert result_files(panel) == ["check.f90", "dl.f90", "foo.f90"]
    assert panel.status.text().startswith("7 results in 3 files")


def test_options_and_errors(qtbot, window):
    panel = search_for(qtbot, window, "count", word_button=True)
    assert sum(len(r.matches) for r in panel._results.values()) == 6
    panel = search_for(qtbot, window, "count", word_button=False, deps_check=False)
    assert result_files(panel) == ["check.f90", "foo.f90"]
    panel.regex_button.setChecked(True)
    panel.find_edit.setText("count(")
    panel.start_search()
    assert "Bad regular expression" in panel.status.text()
    assert not panel._results


def test_options_are_remembered(qtbot, window, make_window, project):
    window.search_panel.case_button.setChecked(True)
    window.search_panel.include_edit.setText("*.f90")
    window.search_panel._save_options()
    again = make_window(project / "fpm.toml")
    assert again.search_panel.case_button.isChecked()
    assert again.search_panel.include_edit.text() == "*.f90"


def test_results_follow_unsaved_edits(qtbot, window, project):
    panel = search_for(qtbot, window, "count", deps_check=False)
    editor = open_editor(window, project / "app/main.f90")
    editor.editor.moveCursor(QTextCursor.End)
    editor.editor.insertPlainText("! count again\n")
    qtbot.waitUntil(lambda: "main.f90" in result_files(panel), timeout=5_000)


def test_clicking_a_result_opens_the_match(qtbot, window, project):
    panel = search_for(qtbot, window, "count", deps_check=False)
    item = panel.tree.items[(project / "test/check.f90").resolve()]
    panel.tree.open_item(item.child(0))
    editor = window.current_editor()
    assert editor.path.name == "check.f90"
    cursor = editor.editor.textCursor()
    assert cursor.selectedText() == "count" and cursor.blockNumber() == 2


def test_replace_all(qtbot, window, project, monkeypatch):
    open_editor(window, project / "src/foo.f90")  # an open file: changed in the editor only
    panel = search_for(qtbot, window, "count", word_button=True)
    panel.set_replace_visible(True)
    panel.replace_edit.setText("n_count")
    asked = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: asked.append(box.text()) or QMessageBox.Yes)
    panel.replace_all()
    assert asked and "Replace 6 matches in 3 files" in asked[0]
    assert not panel._results  # (nothing left to find)
    assert "n_count" in (project / "test/check.f90").read_text()   # saved
    assert "n_count" in (project / "dep_local/src/dl.f90").read_text()
    foo = window.current_editor()
    assert foo.is_dirty() and "n_count = n_count + 1" in foo.editor.toPlainText()
    assert "n_count" not in (project / "src/foo.f90").read_text()   # not saved
    foo.editor.undo()
    qtbot.waitUntil(lambda: "foo.f90" in result_files(panel), timeout=5_000)


def test_replace_cancelled_changes_nothing(qtbot, window, project, monkeypatch):
    panel = search_for(qtbot, window, "count", word_button=True)
    panel.set_replace_visible(True)
    panel.replace_edit.setText("n")
    monkeypatch.setattr(QMessageBox, "exec", lambda box: QMessageBox.Cancel)
    panel.replace_all()
    assert "n" + " = " not in (project / "src/foo.f90").read_text()
    assert len(panel._results) == 3


def test_replace_one_match_and_dismiss(qtbot, window, project):
    panel = search_for(qtbot, window, "count", word_button=True, deps_check=False)
    panel.replace_edit.setText("total")
    path = (project / "src/foo.f90").resolve()
    first = panel._results[path].matches[0]
    panel._replace({path: {(first.line, first.column)}})  # (right-click → Replace: no question)
    text = (project / "src/foo.f90").read_text()
    assert "integer :: total = 1" in text and "count = count + 1" in text
    assert len(panel._results[path].matches) == 3
    panel._dismiss(path, None)
    assert path not in panel._results


def test_result_lines_are_syntax_colored(qtbot, window, project):
    panel = search_for(qtbot, window, "bump", deps_check=False)
    path = (project / "src/foo.f90").resolve()
    formats = panel.tree.line_formats(path, 5)  # "  subroutine bump()"
    keyword, name = formats[2], formats[13]
    assert keyword is not None and keyword.foreground().color() != name.foreground().color()
    # A line inside a C block comment is colored as a comment (lexed with the lines before it).
    c_file = project / "src/x.c"
    c_file.write_text("int a;\n/* start\n   bump inside\n*/\nint bump;\n")
    inside, code = panel.tree.line_formats(c_file, 3), panel.tree.line_formats(c_file, 5)
    assert inside[3].foreground().color() != code[0].foreground().color()


def test_another_project_starts_a_new_search(qtbot, window, conflict_project):
    panel = search_for(qtbot, window, "count", case_button=True)
    panel.replace_edit.setText("total")
    assert panel._results
    window.open_manifest(conflict_project / "fpm.toml")
    assert panel.find_edit.text() == panel.replace_edit.text() == ""
    assert not panel._results and panel.tree.topLevelItemCount() == 0
    assert panel.case_button.isChecked()  # (the options stay)
    qtbot.wait(400)  # (past the typing pause: no search for the cleared text)
    assert panel._worker is None and not panel._results


def test_replace_writes_a_mixed_file_s_most_common_line_end(qtbot, window, project, monkeypatch):
    """(Replace in Files and Rename used CRLF for a file with any CRLF line,
    the editor the most common line end: one reader for both now.)"""
    mixed = project / "test/mixed.f90"
    mixed.write_bytes(b"program mixed\n  integer :: count\n  count = 1\nend program mixed\r\n")
    panel = search_for(qtbot, window, "count", word_button=True, deps_check=False)
    panel.set_replace_visible(True)
    panel.replace_edit.setText("total")
    path = mixed.resolve()
    panel._replace({path: {(m.line, m.column) for m in panel._results[path].matches}})
    assert mixed.read_bytes() == b"program mixed\n  integer :: total\n  total = 1\nend program mixed\n"


def test_many_results_are_listed_a_slice_at_a_time(qtbot, window, project, monkeypatch):
    """(A batch of a thousand files at once held the window a tenth of a second.)"""
    from pathlib import Path

    from vibefpmgui.project_search import FileResult, Match, Root, SearchPanel

    panel = search_for(qtbot, window, "count")
    found = dict(panel._results)
    root = Root("proj", project, False)
    many = [FileResult(Path(project / f"src/f{i}.f90"), root, [Match(1, 0, 1, 1, "", "x", "")]) for i in range(300)]
    added = []
    real = panel._add_file_item
    monkeypatch.setattr(panel, "_add_file_item", lambda result: added.append(result) or real(result))
    monkeypatch.setattr(SearchPanel, "ADD_SECONDS", 0.0)  # (one at a time)
    panel._worker = object()  # (as if still searching)
    panel._add_results(many)
    assert len(added) == 1 and panel._incoming and "searching" in panel.status.text()
    panel._worker = None
    panel._finished(500, False)
    assert not panel.replace_all_button.isEnabled()  # (not until all are listed: it would miss them)
    qtbot.waitUntil(lambda: not panel._incoming, timeout=10_000)
    assert len(added) == 300 and len(panel._results) == len(found) + 300
    assert panel.status.text().startswith(f"{7 + 300} results") and panel.replace_all_button.isEnabled()
