"""The References tab: shown like the Search tab (icons, syntax colors)."""

from __future__ import annotations

from vibefpmgui.result_tree import PARTS_ROLE


def show_count_references(window, project):
    """What Find References on "count" gives (as fortls would report it)."""
    foo, dl, check = (project / "src/foo.f90", project / "dep_local/src/dl.f90", project / "test/check.f90")
    window._show_references("count", [(dl, 2, 13, 18), (foo, 6, 4, 9), (foo, 2, 13, 18), (check, 3, 11, 16)])
    return window.references_panel


def test_results_grouped_like_the_search_tab(window, project):
    panel = show_count_references(window, project)
    assert window.monitor_tabs.currentWidget() is panel
    assert "4 references in 3 files" in panel.title.text()
    tree = panel.tree
    files = [tree.topLevelItem(i).data(0, PARTS_ROLE) for i in range(tree.topLevelItemCount())]
    # (kind, path, name, folder, package, count): the project's files first, then the dependency's.
    assert [(f[2], f[3], f[4], f[5]) for f in files] == [
        ("foo.f90", "src", "", 2), ("check.f90", "test", "", 1), ("dl.f90", "src", "· dl", 1)]
    foo = tree.items[(project / "src/foo.f90").resolve()]
    rows = [foo.child(i).data(0, PARTS_ROLE)[2] for i in range(foo.childCount())]
    assert [(r.line, r.before, r.text, r.after) for r in rows] == [
        (2, "integer :: ", "Count", " = 1"), (6, "", "count", " = count + 1 ! count")]
    assert tree.icon_for == window.project_tree.icon_for
    formats = tree.line_formats((project / "src/foo.f90").resolve(), 2)  # "  integer :: Count = 1"
    assert formats[2] is not None  # (integer: a keyword)


def test_clicking_a_reference_selects_it(window, project):
    panel = show_count_references(window, project)
    check = panel.tree.items[(project / "test/check.f90").resolve()]
    panel.tree.open_item(check.child(0))
    editor = window.current_editor()
    assert editor.path.name == "check.f90"
    assert editor.editor.textCursor().selectedText() == "count"


def test_no_references(window):
    window._show_references("nothing", [])
    assert "No references" in window.references_panel.title.text()
    assert window.references_panel.tree.topLevelItemCount() == 0
