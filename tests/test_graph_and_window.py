"""The dependency graph, the package menu and the window's menus."""

from __future__ import annotations

import pytest
from conftest import open_editor
from qtpy.QtCore import Qt
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication, QFileDialog, QMenu

from vibefpmgui import graph_view as gv
from vibefpmgui import highlighter as hl


@pytest.fixture
def conflict_window(make_window, conflict_project):
    return make_window(conflict_project / "fpm.toml")


def overlapping(view) -> list[tuple[str, str]]:
    names = sorted(view.nodes)
    rects = {n: view.nodes[n].scene_rect() for n in names}
    return [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if rects[a].intersects(rects[b])]


# ------------------------------------------------------------------- graph
@pytest.mark.parametrize("layout", list(gv.LAYOUTS))
def test_layouts_place_every_package_apart(conflict_window, layout):
    view = conflict_window.graph_view
    view.set_layout(layout, animate=False)
    assert set(view.nodes) == {"conflictdemo", "libA", "libB", "test-drive"}
    assert not overlapping(view)


def test_animated_relayout(qtbot, conflict_window):
    view = conflict_window.graph_view
    view.set_layout(gv.RADIAL, animate=True)
    qtbot.waitUntil(lambda: not view._anim_timer.isActive(), timeout=5_000)
    assert not overlapping(view)


@pytest.mark.parametrize("theme", list(gv.THEME_CHOICES))
def test_themes_and_edges_draw(conflict_window, theme):
    view = conflict_window.graph_view
    conflict_window._set_graph_theme(theme)
    for style in gv.EDGE_STYLES:
        conflict_window._set_graph_edges(style)
        assert not view.grab().isNull()
    assert conflict_window.settings.value("graph_theme") == theme
    assert "Git" in view.legend_html() or "git" in view.legend_html().lower()


def test_select_and_chains(qtbot, conflict_window):
    view = conflict_window.graph_view
    selected = []
    view.package_selected.connect(selected.append)
    view.select("libA")
    assert selected == ["libA"]
    dimmed = {n for n, node in view.nodes.items() if node.dimmed}
    assert dimmed == {"libB"}  # (libA's chain: the project above it, test-drive below)
    assert "libA" in conflict_window.details.toPlainText()
    view.select(None)
    assert not any(node.dimmed for node in view.nodes.values())
    view.show_chains("test-drive", [["conflictdemo", "libA", "test-drive"]])
    assert {n for n, node in view.nodes.items() if node.dimmed} == {"libB"}


def test_search_box(qtbot, conflict_window):
    view = conflict_window.graph_view
    QTest.keyClicks(view.search, "lib")
    assert {n for n, node in view.nodes.items() if not node.dimmed} == {"libA", "libB"}
    view._jump_to_match(None)
    first = view.selected
    view._jump_to_match(None)
    assert {first, view.selected} == {"libA", "libB"}
    QTest.keyClick(view.search, Qt.Key_Escape)
    assert view.search.text() == ""
    conflict_window.find_action.trigger()  # (Find with the graph in front: its search box)


def test_conflict_badge(conflict_window):
    node = conflict_window.graph_view.nodes["test-drive"]
    assert node.conflict  # (asked for in different ways)


def test_export_image(conflict_window, tmp_path, monkeypatch):
    for name in ("graph.png", "graph.svg"):
        target = tmp_path / name
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, t=target, **k: (str(t), ""))
        conflict_window.export_graph()
        assert target.stat().st_size > 100
    assert "<svg" in (tmp_path / "graph.svg").read_text()


# ------------------------------------------------------------ package menu
def texts(menu: QMenu) -> dict:
    return {a.text(): a for a in menu.actions() if a.text()}


def test_package_menu_for_a_git_dependency(conflict_window, conflict_project):
    actions = texts(conflict_window.package_menu("test-drive"))
    assert {"Open Repository", "Check for Updates", "Copy Dependency Line", "Why Is This Here?"} <= set(actions)
    actions["Why Is This Here?"].trigger()
    details = conflict_window.details.toPlainText()
    assert "Why is test-drive here?" in details and "libA → test-drive" in details
    actions["Copy Dependency Line"].trigger()
    assert QApplication.clipboard().text().startswith("test-drive = {")
    assert conflict_window.package_menu("no-such-package") is None


def test_package_menu_for_the_project_and_a_path_dependency(conflict_window, monkeypatch):
    root = texts(conflict_window.package_menu("conflictdemo"))
    assert "Why Is This Here?" not in root and "Copy Dependency Line" not in root
    started = []

    class FakeProcess:  # (records the new window's command instead of starting it)
        def __init__(self, *args):
            self.args = []

        def setProgram(self, program):
            self.program = program

        def setArguments(self, args):
            self.args = args

        def setWorkingDirectory(self, folder):
            pass

        def setProcessEnvironment(self, env):
            self.env = env

        def startDetached(self):
            started.append((self.program, self.args, self.env.value("PYTHONPATH")))
            return True

    monkeypatch.setattr("vibefpmgui.main_window.QProcess", FakeProcess)
    lib = texts(conflict_window.package_menu("libA"))
    lib["Open as Project in New Window"].trigger()
    program, args, pythonpath = started[0]
    assert program.endswith(("python", "python3", "python3.14", "python.exe")) or "python" in program
    assert args[:3] == ["-m", "vibefpmgui", "--new-window"] and args[3].endswith("libA/fpm.toml")
    assert pythonpath  # (the new process finds this package, installed or not)


# ------------------------------------------------------------- window menus
def test_recent_manifests(make_window, project, conflict_project):
    window = make_window(project / "fpm.toml")
    window.open_manifest(conflict_project / "fpm.toml")
    window._fill_recent_menu()
    labels = [a.text() for a in window.recent_menu.actions()]
    assert any(label.startswith("proj") for label in labels) and any("conflict" in label for label in labels)
    window.open_recent(str(project / "fpm.toml"))
    assert window.manifest_path == (project / "fpm.toml").resolve() or window.manifest_path.name == "fpm.toml"
    window.clear_recent_manifests()
    window._fill_recent_menu()
    assert window.recent_menu.actions()[0].text() == "No recent manifests"


def test_syntax_colors_menu(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    window._fill_style_menu()
    assert window.style_menu.actions()[0].data() == hl.AUTO
    window.set_syntax_style("monokai")
    assert editor.highlighter.style.__name__.lower().startswith("monokai")
    assert window.settings.value("syntax_style") == "monokai"
    window.set_syntax_style(hl.AUTO)


def test_text_size_and_editor_font(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    size = editor.editor.font().pointSizeF()
    window.bigger_action.trigger()
    assert editor.editor.font().pointSizeF() > size
    window.reset_font_action.trigger()
    assert editor.editor.font().pointSizeF() == size
    window.set_editor_font("Courier", 15)
    assert editor.editor.font().family() == "Courier" and window.reset_editor_font_action.isEnabled()
    assert window.settings.value("editor_font_family") == "Courier"


def test_tab_list_and_close_all(window, project):
    open_editor(window, project / "src/foo.f90")
    open_editor(window, project / "app/main.f90")
    menu = QMenu()
    window._fill_tab_list(menu)
    labels = [a.text() for a in menu.actions() if a.text()]
    assert "foo.f90" in labels and "main.f90" in labels and labels[-1] == "Close All Editor Tabs"
    next(a for a in menu.actions() if a.text() == "foo.f90").trigger()
    assert window.current_editor().path.name == "foo.f90"
    window.close_all_editor_tabs()
    assert window.editors() == []


def test_editor_in_the_middle(window, project):
    tabs = window.tabs
    assert tabs.count() == 0 and window.central.currentWidget() is window.no_file_hint  # (a hint, no tabs)
    open_editor(window, project / "fpm.toml")  # (the manifest: an ordinary, closable editor tab)
    assert window.central.currentWidget() is tabs and tabs.tabText(0).endswith("fpm.toml")
    menu = QMenu()
    window._fill_tab_list(menu)
    assert [a.text() for a in menu.actions() if a.text()][:1] == ["fpm.toml"]
    window.close_all_editor_tabs()
    assert window.central.currentWidget() is window.no_file_hint


def areas(window) -> dict[str, str]:
    names = {Qt.LeftDockWidgetArea: "left", Qt.RightDockWidgetArea: "right", Qt.BottomDockWidgetArea: "bottom",
             Qt.NoDockWidgetArea: "none"}
    return {name: names.get(window.dockWidgetArea(dock), "?") for name, dock in window.docks.items()}


def stack_of(window, name) -> set[str]:
    dock = window.docks[name]
    return {n for n, d in window.docks.items() if d in window.tabifiedDockWidgets(dock)} | {name}


def test_default_panels(window):
    assert areas(window) == {"options": "left", "files": "left", "graph": "right", "outputs": "right",
                             "build": "bottom", "tests": "bottom", "run": "bottom", "review": "bottom",
                             "references": "bottom", "search": "bottom", "terminal": "bottom",
                             "output": "bottom"}
    assert stack_of(window, "graph") == {"graph", "outputs"}
    assert stack_of(window, "build") == {"build", "tests", "run", "review", "references", "search", "terminal"}
    assert "output" not in stack_of(window, "build")  # (beside the stack)
    assert window.panel_in_front(window.docks["graph"].widget())  # (in front of Build outputs)
    assert window.panel_in_front(window.build_monitor)
    # (window.panels_menu, not action.menu(): PySide invalidates a menu fetched that way)
    assert [a.text() for a in window.panels_menu.actions()][:3] == ["Build options", "Project files",
                                                                     "Dependency graph"]


def test_moving_floating_and_reset(qtbot, window):
    graph = window.docks["graph"]
    window.addDockWidget(Qt.BottomDockWidgetArea, graph)  # (dragged to the bottom)
    window.docks["search"].setFloating(True)
    window.docks["review"].hide()  # (closed)
    assert areas(window)["graph"] == "bottom" and window.docks["search"].isFloating()
    window.show_panel(window.review_monitor)  # (a review brings it back)
    assert window.panel_in_front(window.review_monitor)
    window.reset_layout()
    assert areas(window)["graph"] == "right" and not window.docks["search"].isFloating()
    assert all(not d.isHidden() for d in window.docks.values())


def test_layout_is_remembered(make_window, project):
    from conftest import close_window

    first = make_window(project / "fpm.toml")
    first.addDockWidget(Qt.LeftDockWidgetArea, first.docks["terminal"])
    first.docks["outputs"].hide()
    close_window(first)
    second = make_window(project / "fpm.toml")
    assert areas(second)["terminal"] == "left" and second.docks["outputs"].isHidden()


def test_find_goes_to_the_graph_or_the_editor(window, project, monkeypatch):
    searched = []
    monkeypatch.setattr(window.graph_view, "focus_search", lambda: searched.append(True))
    window.show_panel(window.docks["graph"].widget())
    window.find_action.trigger()  # (no file open: the graph's search box)
    assert searched
    editor = open_editor(window, project / "src/foo.f90")
    window.find_action.trigger()
    assert editor.find_bar.isVisible()


def test_editor_tabs_have_the_file_icon(window, project, monkeypatch):
    from qtpy.QtWidgets import QTabBar

    from vibefpmgui.icons import fortran_file_icon

    def shown(editor):  # (the icon in the tab: a widget beside the name, not a tab icon)
        return editor.tab_icon.pixmap().toImage()

    def image(icon):
        return icon.pixmap(16, 16).toImage()

    editor = open_editor(window, project / "src/foo.f90")
    index = window.tabs.indexOf(editor)
    assert window.tabs.tabIcon(index).isNull()  # (macOS's style cuts the name off after a tab icon)
    assert shown(editor) == image(fortran_file_icon())
    bar = window.tabs.tabBar()
    assert bar.tabButton(index, QTabBar.LeftSide) is editor.tab_icon  # (the icon left of the name,
    assert bar.tabButton(index, QTabBar.RightSide) is editor.close_button  # the × right: one small widget each)
    manifest = open_editor(window, project / "fpm.toml")
    assert shown(manifest) == image(window.project_tree.icon_for(project / "fpm.toml"))  # (with the fpm badge)
    target = project / "src/foo.inc"  # (Save As another kind of file: its icon)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(target), ""))
    window.tabs.setCurrentWidget(editor)
    window.save_as_action.trigger()
    assert shown(editor) == image(fortran_file_icon(include=True))
    menu = QMenu()
    window._fill_tab_list(menu)
    assert not next(a for a in menu.actions() if a.text() == "foo.inc").icon().isNull()


def test_output_panel_heading(window):
    title = window.output_title
    assert title.isHidden()  # ("Output" is the panel's own title)
    window._show_single_output("check_add", "add works\n")
    text = title.text().replace("\u200b", "")  # (long names may wrap after "_")
    assert not title.isHidden() and "check_add" in text and "Output" not in text
    window.show_all_output()
    assert title.isHidden()
