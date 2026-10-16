"""The Complexity panel: measuring the project's Fortran files (on a pool
thread, while the panel is on view), again when one (or a file one
includes) is saved; a file that hasn't changed since it was last measured,
nor have the files it includes, isn't read again.
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import complexity as cx
from ..background import call_in_background
from ..complexity_panel import ComplexityPanel
from ..project_search import Query, iter_files, search_roots
from ..project_tree import fpm_folders

FORTRAN_GLOBS = ", ".join(f"*{ext}" for ext in sorted(cx.FORTRAN))


def _stamp(path: Path) -> tuple[int, int] | None:
    """A file's (modification time, size); None if it's gone."""
    try:
        stat = path.stat()
    except OSError:
        return None
    return stat.st_mtime_ns, stat.st_size


def _key(path: Path, metrics: cx.FileMetrics) -> tuple:
    return _stamp(path), tuple(_stamp(p) for p in metrics.includes)


def measure(roots, dependencies: bool, cache: dict) -> tuple[list[cx.FileMetrics], dict]:
    """Every Fortran file's metrics, and the cache for next time ({path:
    (key, metrics)}: a file isn't read again while it and the files it
    includes are unchanged, their modification times and sizes the same)."""
    files, kept = [], {}
    query = Query("", include=FORTRAN_GLOBS, dependencies=dependencies)
    for _root, path in iter_files(roots, query):
        if _stamp(path) is None:
            continue
        old = cache.get(path)
        if old is not None and old[0] == _key(path, old[1]):
            metrics = old[1]
        else:
            metrics = cx.analyze_file(path)
        kept[path] = (_key(path, metrics), metrics)
        files.append(metrics)
    return files, kept


class ComplexityMixin:
    """The Complexity panel (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_complexity_panel(self):
        """The Complexity panel."""
        self.complexity_panel = panel = ComplexityPanel()
        panel.icon_for = self.project_tree.icon_for
        panel.set_code_font(self._mono_font(editor=True))
        panel.tests_check.setChecked(self.settings.value("complexity_tests", False, type=bool))
        panel.tests_check.toggled.connect(lambda on: self.settings.setValue("complexity_tests", on))
        panel.dependencies_check.setChecked(self.settings.value("complexity_dependencies", False, type=bool))
        panel.dependencies_check.toggled.connect(lambda on: self.settings.setValue("complexity_dependencies", on))
        panel.refresh_requested.connect(self.refresh_complexity)
        panel.open_requested.connect(lambda path, line: self.navigate_to(path, line, 0, preview=True))
        self._complexity_cache: dict = {}
        self._complexity_request = 0
        self._complexity_stale = True

    def refresh_complexity(self):
        """Measure the project's code again (or once the panel is on view)."""
        panel = self.complexity_panel
        if self.manifest_path is None:
            panel.show_message("Open a project to see the complexity of its Fortran code here.")
            return
        if not self.panel_in_front(panel):
            self._complexity_stale = True
            return
        self._complexity_stale = False
        self._complexity_request += 1
        request = self._complexity_request
        project = self.manifest_path.parent
        roots = search_roots(self.graph, project)
        dependencies = panel.dependencies_check.isChecked()
        cache = dict(self._complexity_cache)
        if not panel.files:
            panel.show_message("Measuring the code…")
        call_in_background(lambda: measure(roots, dependencies, cache),
                           lambda result: self._complexity_measured(request, roots, *result), self)

    def _complexity_measured(self, request: int, roots, files: list[cx.FileMetrics], cache: dict):
        if request != self._complexity_request or self.manifest_path is None:
            return
        self._complexity_cache = cache
        project = self.manifest_path.parent
        tests = [Path(folder) for folder, role in fpm_folders(self.manifest_path).items() if role == "test"]
        dependency_dirs = [r.directory for r in roots if r.dependency]
        self.complexity_panel.show_metrics(files, project, tests, dependency_dirs, time.strftime("%H:%M"))

    def _complexity_panel_shown(self, shown: bool):
        if shown and self._complexity_stale:
            self.refresh_complexity()

    def _complexity_file_saved(self, path: str):
        saved = Path(path)
        if cx.is_fortran(saved) or any(saved.resolve() == p.resolve()
                                       for f in self.complexity_panel.files for p in f.includes):
            self.refresh_complexity()
