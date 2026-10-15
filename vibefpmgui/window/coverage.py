"""Test coverage: collecting it after a test run, and showing it.

In the Coverage panel and the editors' gutters (see `vibefpmgui.coverage`).
"""

from __future__ import annotations

import time
from pathlib import Path

from .. import coverage as cov
from ..background import call_in_background
from ..editor import EditorTab
from ..project_tree import fpm_folders


class CoverageMixin:
    """Test coverage (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _collect_coverage(self):
        """After a test run with coverage: gcov reads the counts the tests
        wrote (in the background), then the Coverage panel and the editors
        show them."""
        compiler = self.effective_compiler()[0]
        gcov = cov.find_gcov(compiler)
        if gcov is None:
            self.coverage_panel.show_message(f"Coverage needs gcov (for {compiler}): it isn't on the PATH. "
                                             "(It comes with gfortran.)")
            return
        if self._coverage_collector is not None:
            return
        self.coverage_panel.show_message("Collecting coverage…")
        self._coverage_description = f"{time.strftime('%H:%M')} · {self._build_description()}"
        collector = self._coverage_collector = cov.Collector(gcov, self.manifest_path.parent / "build")
        call_in_background(collector.run, lambda result: self._coverage_collected(collector, *result), self)

    def _stop_coverage(self):
        """Stop gcov if it's still reading the counts (quitting, or another
        project); its result is then dropped."""
        collector, self._coverage_collector = self._coverage_collector, None
        if collector is not None:
            collector.stop()

    def _coverage_collected(self, collector: cov.Collector, files: dict, error: str):
        if collector is not self._coverage_collector:
            return
        self._coverage_collector = None
        if error:
            self.coverage_panel.show_message(f"gcov couldn't read the coverage: {error}")
            return
        self._coverage_read(files)

    def _coverage_read(self, files: dict):
        if not self.manifest_path:
            return
        if not files:
            self.coverage_panel.show_message("The tests wrote no coverage counts: was the project built with "
                                             "gfortran? (Coverage works with gfortran's --coverage.)")
            return
        self._apply_coverage(files)
        tests = [Path(folder) for folder, role in fpm_folders(self.manifest_path).items() if role == "test"]
        self.coverage_panel.show_report(files, self.manifest_path.parent, self._coverage_description, tests)
        hit, found = cov.totals(self.coverage_panel.shown_files())["lines"]
        if found:
            self.statusBar().showMessage(f"Coverage: {100 * hit / found:.1f}% of the lines with code ran "
                                         "(see the Coverage panel)", 8000)

    def _apply_coverage(self, files: dict):
        """The coverage shown in the editors' gutters."""
        self.coverage = files
        for editor in self.editors():
            self._show_coverage(editor)

    def _go_to_missed(self, editor: EditorTab, forward: bool):
        if not editor.editor.go_to_missed(forward):
            self.statusBar().showMessage("No missed lines in this file" if editor.editor.coverage else
                                         "No coverage for this file: turn on Build options → Coverage "
                                         "and run the tests", 5000)

    def _show_coverage(self, editor: EditorTab):
        found = self._coverage_for(editor)
        editor.editor.set_coverage(found.lines if found else None, found.line_branches if found else None)

    def _coverage_for(self, editor: EditorTab) -> cov.FileCoverage | None:
        """A file's coverage for its gutter, if its text is still what the
        tests ran: not changed on disk since the run started, and no unsaved
        changes (its line numbers could differ: the bars would be on the
        wrong lines). An open file edited later drops its bars itself."""
        found = self.coverage.get(editor.path)
        if found is None or editor.is_dirty():
            return None
        try:
            if editor.path.stat().st_mtime > self._coverage_started:
                return None
        except OSError:
            return None
        return found
