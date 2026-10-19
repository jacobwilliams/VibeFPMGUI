"""The Build panel's Package column: which package each compiled file is
from, the dependencies' too, those fpm downloads during the build among
them (not on disk when it starts)."""

from __future__ import annotations

from conftest import write_files

from vibefpmgui import manifest as m
from vibefpmgui.build_monitor import BuildMonitor


def compiled(monitor: BuildMonitor, *names: str) -> dict[str, tuple[str, str]]:
    """fpm's progress lines for these files; each row's (package, kind)."""
    for i, name in enumerate(names, 1):
        monitor.feed_line(f"[{100 * i // (len(names) + 1):>3}%] {name:>30}")
        monitor.feed_line(f"[{100 * i // (len(names) + 1):>3}%] {name:>30}  done.")
    return {t.name: (t.package, t.package_kind) for t in monitor.targets}


def test_a_dependency_downloaded_during_the_build(qtbot, tmp_path):
    root = write_files(tmp_path / "proj", {
        "fpm.toml": 'name = "proj"\n[dependencies]\nfmin = { git = "https://example.com/fmin" }\n',
        "src/proj.f90": "module proj\nend module proj\n"})
    graph = m.resolve(root / "fpm.toml")
    assert graph.packages["fmin"].kind == m.MISSING  # (not downloaded yet)
    monitor = BuildMonitor()
    qtbot.addWidget(monitor)
    monitor.start(graph, root)
    # fpm downloads it (and the dependency it has, which the graph didn't know of), then compiles.
    write_files(root / "build/dependencies", {
        "fmin/fpm.toml": 'name = "fmin"\n[dependencies]\nkinds = { git = "https://example.com/kinds" }\n',
        "fmin/src/fmin_module.F90": "", "fmin/.git/HEAD": "",
        "kinds/fpm.toml": 'name = "kinds"\n', "kinds/src/kinds.f90": "", "kinds/.git/HEAD": ""})
    assert compiled(monitor, "kinds.f90", "fmin_module.F90", "proj.f90") == {
        "kinds.f90": ("kinds", m.GIT), "fmin_module.F90": ("fmin", m.GIT), "proj.f90": ("proj", m.ROOT)}


def test_each_new_folder_is_indexed_once(qtbot, tmp_path, monkeypatch):
    root = write_files(tmp_path / "proj", {"fpm.toml": 'name = "proj"\n', "src/proj.f90": ""})
    monitor = BuildMonitor()
    qtbot.addWidget(monitor)
    monitor.start(m.resolve(root / "fpm.toml"), root)
    write_files(root / "build/dependencies", {"dep/fpm.toml": 'name = "dep"\n', "dep/src/dep.f90": ""})
    from vibefpmgui import build_monitor as bm

    walked = []
    real = bm.index_package
    monkeypatch.setattr(bm, "index_package", lambda index, name, *a: (walked.append(name), real(index, name, *a)))
    assert compiled(monitor, "dep.f90", "unknown.f90", "other.f90")["dep.f90"][0] == "dep"
    assert walked == ["dep"]  # (files of no package: the folders aren't walked again)
