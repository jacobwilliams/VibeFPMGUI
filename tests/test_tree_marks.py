"""Project files: files colored by the last build's errors and warnings."""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import Qt

from vibefpmgui.project_tree import FILE_COLORS, KIND_ROLE


def item_of(window, path: Path):
    tree = window.project_tree
    tree.reveal(str(path))
    return next(item for p, item in tree._items.items() if Path(p).resolve() == path.resolve())


def test_marks_color_files_and_resolve_folders_once(window, project, monkeypatch):
    tree = window.project_tree
    foo, check = item_of(window, project / "src/foo.f90"), item_of(window, project / "test/check.f90")
    # (The build's paths are resolved; the tree's may not be: on macOS a temporary folder is under a link.)
    resolved = []
    real_resolve = Path.resolve
    monkeypatch.setattr(Path, "resolve", lambda self, *a, **k: resolved.append(self) or real_resolve(self, *a, **k))
    tree.set_marks({str((project / "src/foo.f90").resolve()): "error",
                    str((project / "test/check.f90").resolve()): "warning"})
    monkeypatch.setattr(Path, "resolve", real_resolve)
    assert foo.foreground().color().name() == FILE_COLORS["error"] and "errors in the last build" in foo.toolTip()
    assert check.foreground().color().name() == FILE_COLORS["warning"]
    files = sum(1 for item in tree._items.values() if item.data(KIND_ROLE) == "file")
    assert len(resolved) < files  # (a folder at a time, not each file)
    tree.set_marks({})
    assert foo.data(Qt.ForegroundRole) is None and "last build" not in foo.toolTip()
