"""Preview tabs: one click in Project files shows a file in a tab of its
own (name in italics) that the next click replaces; editing it, or opening
it for real, keeps it."""

from __future__ import annotations

from conftest import write_files
from qtpy.QtCore import Qt
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication


def italic(window, editor) -> str | None:
    return window.tabs.tabBar().italic_name(window.tabs.indexOf(editor))


def test_one_click_in_the_tree_previews(qtbot, window, project):
    tree = window.project_tree
    tree.reveal(str(project / "src/foo.f90"))
    rect = tree.view.visualRect(tree.view.currentIndex())
    tree.view.setFocus()
    QTest.mouseClick(tree.view.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    editor = window.current_editor()
    assert editor is not None and editor.path.name == "foo.f90"
    assert italic(window, editor) == "foo.f90" and window.tabs.tabText(window.tabs.indexOf(editor)) == ""
    assert QApplication.focusWidget() is tree.view  # (the keyboard stays in the tree)


def test_the_next_preview_replaces_it(window, project):
    window.open_file(str(project / "app/main.f90"), force_editor=True)  # (open for real)
    window.preview_file(str(project / "src/foo.f90"))
    first = window.current_editor()
    window.preview_file(str(project / "test/check.f90"))
    second = window.current_editor()
    assert first not in window.editors() and second is window._preview
    assert [e.path.name for e in window.editors()] == ["main.f90", "check.f90"]  # (in its place)
    window.preview_file(str(project / "app/main.f90"))  # (already open for real: just shown)
    assert window.current_editor().path.name == "main.f90" and italic(window, window.current_editor()) is None
    assert second in window.editors()


def test_editing_keeps_it(window, project):
    window.preview_file(str(project / "src/foo.f90"))
    editor = window.current_editor()
    editor.editor.appendPlainText("! an edit")
    assert window._preview is None and italic(window, editor) is None
    assert window.tabs.tabText(window.tabs.indexOf(editor)) == "• foo.f90"
    window.preview_file(str(project / "test/check.f90"))
    assert [e.path.name for e in window.editors()] == ["foo.f90", "check.f90"]  # (both open)


def test_opening_it_for_real_keeps_it(window, project):
    window.preview_file(str(project / "src/foo.f90"))
    editor = window.current_editor()
    window.project_tree.open_requested.emit(str(project / "src/foo.f90"))  # (double-click in the tree)
    assert window._preview is None and italic(window, editor) is None
    window.preview_file(str(project / "test/check.f90"))
    other = window.current_editor()
    window.tabs.tabBarDoubleClicked.emit(window.tabs.indexOf(other))  # (double-click the tab)
    assert window._preview is None and italic(window, other) is None
    assert len(window.editors()) == 2


def test_not_for_files_the_editor_doesnt_open(window, project, monkeypatch):
    from qtpy.QtGui import QDesktopServices

    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url))
    write_files(project, {"src/data.bin": b"\x00\x01binary"})
    window.preview_file(str(project / "src/data.bin"))
    window.use_editor_action.setChecked(False)
    window.preview_file(str(project / "src/foo.f90"))
    assert window.editors() == [] and opened == []  # (one click never starts another app)


def test_tab_list_and_close(window, project):
    window.preview_file(str(project / "src/foo.f90"))
    editor = window.current_editor()
    window._fill_tab_list(window.tab_list_button.menu())
    assert "foo.f90" in [a.text() for a in window.tab_list_button.menu().actions()]
    window.close_editor(window.tabs.indexOf(editor))
    assert window.editors() == [] and window._preview is None


def test_arrow_keys_preview_once_the_selection_rests(qtbot, window, project):
    write_files(project, {"src/zz_a.f90": "! a\n", "src/zz_b.f90": "! b\n", "src/zz_c.f90": "! c\n"})
    tree = window.project_tree
    tree.refresh()
    tree.reveal(str(project / "src/zz_a.f90"))  # (moved by the app: no preview)
    qtbot.wait(300)
    assert window.editors() == []
    asked = []
    tree.preview_requested.connect(asked.append)
    window.activateWindow()
    tree.view.setFocus()
    qtbot.waitUntil(lambda: QApplication.focusWidget() is tree.view, timeout=2_000)
    for _ in range(2):  # (↓ ↓ quickly: only where it stops is previewed)
        QTest.keyClick(tree.view, Qt.Key_Down)
    qtbot.waitUntil(lambda: bool(asked), timeout=2_000)
    qtbot.wait(300)
    assert [p.rsplit("/", 1)[-1] for p in asked] == ["zz_c.f90"]
    editor = window.current_editor()
    assert editor.path.name == "zz_c.f90" and italic(window, editor) == "zz_c.f90"
    # The keyboard stays in the tree: the window's focus is still there, not in
    # the editor. (window.focusWidget(): on the offscreen test platform the
    # window itself loses activation when an editor tab is built.)
    assert window.focusWidget() is tree.view and not editor.editor.hasFocus()
    window.activateWindow()  # (see above)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is tree.view, timeout=2_000)
    QTest.keyClick(tree.view, Qt.Key_Up)
    qtbot.waitUntil(lambda: window.current_editor().path.name == "zz_b.f90", timeout=2_000)
    assert len(window.editors()) == 1  # (b's preview took c's place)


def test_search_and_references_results_preview(window, project):
    from pathlib import Path

    from vibefpmgui.result_tree import make_match

    foo, check, main = (str(project / p) for p in ("src/foo.f90", "test/check.f90", "app/main.f90"))
    window.open_file(main, force_editor=True)  # (open for real)
    window.search_panel.open_requested.emit(foo, 2, 2, 2, 9)  # (a result clicked)
    editor = window.current_editor()
    assert editor.path.name == "foo.f90" and editor is window._preview and italic(window, editor) == "foo.f90"
    assert editor.editor.textCursor().selectedText() == "integer"  # (the match, selected)
    window.references_panel.open_requested.emit(check, 1, 0, 1, 7)  # (the next one: in its place)
    assert editor not in window.editors() and window.current_editor().path.name == "check.f90"
    window.search_panel.open_requested.emit(main, 1, 0, 1, 7)  # (open already: shown as it is)
    assert window.current_editor().path.name == "main.f90" and italic(window, window.current_editor()) is None

    tree = window.search_panel.tree  # (double-clicked: kept open)
    row = tree.set_file(Path(check).resolve(), [make_match("program check", 1, 0, 1, 7)])
    tree.itemClicked.emit(row.child(0), 0)
    assert window.current_editor() is window._preview
    tree.itemDoubleClicked.emit(row.child(0), 0)
    kept = window.current_editor()
    assert kept.path.name == "check.f90" and window._preview is not kept and italic(window, kept) is None


def test_problems_still_open_files_for_real(window, project):
    window.problems_panel.open_requested.emit(str(project / "src/foo.f90"), 2, 0, 2, 3)
    assert window.current_editor().path.name == "foo.f90" and window._preview is None
