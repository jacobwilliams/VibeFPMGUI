"""Graph → Show Dependencies' Dev-Dependencies: what a dependency lists as
its dev-dependencies, which fpm never downloads (only the project's own)."""

from __future__ import annotations

import pytest
from conftest import write_files

from vibefpmgui import dep_updates
from vibefpmgui import manifest as m

URL = "https://github.com/fortran-lang/test-drive"


@pytest.fixture
def dev_project(tmp_path):
    """The project: libA (on disk) and its own dev-dependency, tool; libA's
    dev-dependencies are test-drive (not downloaded) and shared (which the
    project uses, so it is)."""
    return write_files(tmp_path / "devdemo", {
        "fpm.toml": 'name = "devdemo"\n[dependencies]\nlibA = { path = "libA" }\nshared = { path = "shared" }\n'
                    '[dev-dependencies]\ntool = { path = "tool" }\n',
        "libA/fpm.toml": f'name = "libA"\n[dev-dependencies]\ntest-drive = {{ git = "{URL}", tag = "v0.4.0" }}\n'
                         'shared = { path = "../shared" }\n',
        "shared/fpm.toml": 'name = "shared"\n',
        "tool/fpm.toml": 'name = "tool"\n[dev-dependencies]\nhelper = "1.2"\nface = { git = "https://x/FACE" }\n'
                         'slsqp = { git = "https://x/slsqp", tag = "1.6.1" }\n'
                         'mine = { path = "../mine" }\n',
    })


def edges(graph):
    return {(e.parent, e.child, e.dev) for e in graph.edges}


def test_left_out_unless_asked_for(dev_project):
    graph = m.resolve(dev_project / "fpm.toml")
    assert set(graph.packages) == {"devdemo", "libA", "shared", "tool"}
    assert ("devdemo", "tool", True) in edges(graph)  # (the project's own: fpm fetches it)
    assert not any(e.parent == "libA" for e in graph.edges)


def test_shown_as_dev_edges(dev_project):
    graph = m.resolve(dev_project / "fpm.toml", dependencies_dev=True)
    assert {("libA", "test-drive", True), ("libA", "shared", True), ("tool", "helper", True)} <= edges(graph)
    drive = graph.packages["test-drive"]
    assert drive.kind == m.DEV_ONLY
    assert drive.directory is None and drive.manifest is None
    assert drive.git == URL and drive.git_ref == "tag v0.4.0"
    assert "libA" in drive.error and "doesn't download" in drive.error
    assert graph.packages["helper"].kind == m.DEV_ONLY and graph.packages["helper"].version == "1.2"
    # Also a dependency fpm uses: that package, as it was.
    assert graph.packages["shared"].kind == m.PATH and graph.packages["shared"].manifest


def test_no_conflicts_or_updates_from_them(dev_project):
    graph = m.resolve(dev_project / "fpm.toml", dependencies_dev=True)
    assert "test-drive" not in graph.declarations
    assert m.conflicts(graph) == {}
    assert "test-drive" not in {check.name for check in dep_updates.git_dependencies(graph)}


def test_the_graph_menu_option(make_window, dev_project):
    window = make_window(dev_project / "fpm.toml")
    view = window.graph_view
    assert not window.dependencies_dev_action.isChecked()
    assert "test-drive" not in view.nodes
    assert view.nodes["tool"].dev and not view.nodes["libA"].dev  # (the project's own dev-dependency)
    assert "only a dev-dependency" in window.legend.text()
    assert gv_label(m.DEV_ONLY) not in window.legend.text()

    window.dependencies_dev_action.trigger()
    assert window.settings.value("graph_dependencies_dev", type=bool)
    node = view.nodes["test-drive"]
    assert node.package.kind == m.DEV_ONLY and node.dev
    assert node.subtitle == "v0.4.0"  # (the tag it asks for, as a downloaded one shows its version)
    assert view.nodes["helper"].subtitle == "v1.2"
    assert view.nodes["face"].subtitle == "Git"  # (nothing pinned: the kind it would be)
    assert view.nodes["mine"].subtitle == "Local path"
    assert view.nodes["slsqp"].subtitle == "v1.6.1"
    assert not view.nodes["shared"].dev  # (the project uses it too)
    assert gv_label(m.DEV_ONLY) in window.legend.text()
    assert "dev-dependencies shown" in window.statusBar().currentMessage()

    # Nothing to open or fetch: fpm hasn't downloaded it.
    actions = {a.text(): a for a in window.package_menu("test-drive").actions() if a.text()}
    assert not actions["Open fpm.toml"].isEnabled()
    assert not actions["Show in Project Files"].isEnabled()
    assert "Check for Updates" not in actions and "Update This Dependency" not in actions
    assert "Open Repository" in actions
    opened = []
    window.open_file = lambda *a, **k: opened.append(a)
    window.open_package_manifest("test-drive")
    assert not opened
    window.show_package("test-drive")
    assert "doesn't download" in window.details.toPlainText()

    window.dependencies_dev_action.trigger()
    assert "test-drive" not in view.nodes


def gv_label(kind: str) -> str:
    from vibefpmgui.graph_view import KIND_LABELS
    return KIND_LABELS[kind]
