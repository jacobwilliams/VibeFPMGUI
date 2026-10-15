"""An editor tab's checks: fortitude, spelling, gremlins, and the compiler's, fortls's and the AI's markers.

A part of `vibefpmgui.editor.EditorTab`."""

from __future__ import annotations

from qtpy.QtCore import QProcess

from . import gremlins as gremlins_mod
from . import lint, spelling
from .editor_shared import MARKER_COLORS, PROBLEM_SOURCES
from .result_tree import Problem


class EditorChecksMixin:
    """The tab's checks and markers (a part of `vibefpmgui.editor.EditorTab`)."""

    def can_lint(self) -> bool:
        return self.lint_enabled and lint.can_lint(self.path) and lint.executable() is not None

    def set_lint_enabled(self, on: bool):
        if on:
            self.lint()
        else:
            self._lint_timer.stop()
            self._show_lint([])

    def lint(self):
        """Run fortitude on the current text (in the background)."""
        if not self.can_lint():
            self._show_lint([])
            return
        if self._lint_process is not None:
            self._lint_again = True
            return
        self._lint_again = False
        process = QProcess(self)
        process.setWorkingDirectory(str(self.path.parent))
        # (Methods, not lambdas holding the process: see LspClient.start.)
        process.finished.connect(self._lint_process_finished)
        process.errorOccurred.connect(self._lint_process_error)
        self._lint_process = process
        process.start(lint.executable(), lint.command(self.path))
        # (The text itself, not the document's revision: recoloring the
        # syntax also counts as a revision.)
        self._lint_sent_text = self.editor.toPlainText()
        process.write(self._lint_sent_text.encode("utf-8"))
        process.closeWriteChannel()

    def _lint_process_finished(self, *_):
        if isinstance(self.sender(), QProcess):
            self._lint_finished(self.sender())

    def _lint_process_error(self, error):
        if error == QProcess.FailedToStart and isinstance(self.sender(), QProcess):
            self._lint_finished(self.sender())

    def _lint_finished(self, process: QProcess):
        if process is not self._lint_process:
            return
        self._lint_process = None
        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace")
        process.deleteLater()
        if self.can_lint():
            self._show_lint(lint.parse(output))
            self._lint_text = self._lint_sent_text  # the text the issues are about
        if self._lint_again:
            self._lint_timer.start()

    def apply_lint_fix(self, issue):
        """Apply fortitude's fix for one issue (one Undo step). Only if the
        text is still the one fortitude checked: otherwise the positions
        may be off, so check again first."""
        if not issue.fixable:
            return
        if getattr(self, "_lint_text", None) != self.editor.toPlainText():
            self.update_status("the file changed since fortitude checked it — try again in a moment")
            self._lint_timer.start()
            return
        self.apply_text_edits(issue.edits)
        self.update_status(f"applied fix: {issue.fix or issue.code}")

    def stop_lint(self):
        """Before the tab is closed: don't leave fortitude running."""
        self._lint_timer.stop()
        if self._lint_process is not None:
            process, self._lint_process = self._lint_process, None
            process.kill()
            process.waitForFinished(1000)

    def _show_lint(self, issues: list[lint.LintIssue]):
        self._fortitude_issues = list(issues)
        self._lint_markers = self._markers_for(issues)
        self._show_issues()

    def set_gremlins_enabled(self, on: bool):
        self.check_gremlins()

    def check_gremlins(self):
        """Look for gremlins (non-ASCII and invisible characters) again."""
        found = gremlins_mod.find(self.editor.toPlainText()) if self.gremlins_enabled else []
        before = self.editor.gremlins
        self.editor.set_gremlins(found)
        if self.editor.gremlins != before:
            self.update_status()
            self.problems_changed.emit()

    def set_spelling_enabled(self, on: bool):
        self.check_spelling() if on else self._show_spelling([])

    def check_spelling(self):
        """Spell check the comments and strings (as the highlighter last
        found them: it lexes again after each edit, then this runs)."""
        self._spelling_timer.stop()
        if not (self.spelling_enabled and spelling.available()):
            self._show_spelling([])
            return
        hl = self.highlighter
        if not hl.is_current():
            return  # (the highlighter is behind: it calls again when it's caught up)
        self._show_spelling(spelling.check(hl.text, hl.prose, hl.names))

    def _show_spelling(self, issues: list):
        if issues == self._spelling_issues:
            return  # (the same words: nothing to redraw, or to tell the Problems tab)
        self._spelling_issues = issues
        self._show_issues()

    def show_lsp_diagnostics(self, issues: list):
        """fortls's check of this file (it sends one when the file is opened
        or saved), shown like fortitude's issues (View menu: on or off)."""
        issues = list(issues) if self.lsp_diagnostics_enabled else []
        self._lsp_issues = issues
        self._lsp_markers = self._markers_for(issues)
        self._show_issues()

    @staticmethod
    def _markers_for(issues) -> dict[int, list[tuple[str, str]]]:
        markers: dict[int, list[tuple[str, str]]] = {}
        for issue in issues:
            markers.setdefault(issue.line, []).append((issue.severity, issue.describe()))
        return markers

    def _show_issues(self):
        self.editor.set_lint(self._fortitude_issues + self._lsp_issues + self._spelling_issues)
        self._apply_markers()

    def show_diagnostics(self, diagnostics):
        """Mark compiler messages (objects with .line, .severity, .message)."""
        markers: dict[int, list[tuple[str, str]]] = {}
        for d in diagnostics:
            markers.setdefault(d.line, []).append((d.severity, d.message))
        self._compiler_markers = markers
        self._apply_markers()

    def show_review(self, findings):
        """Mark AI review findings (objects with .line, .category, .comment)."""
        markers: dict[int, list[tuple[str, str]]] = {}
        for f in findings:
            markers.setdefault(f.line, []).append(("review", f"AI ({f.category}): {f.comment}"))
        self._review_markers = markers
        self._apply_markers()

    def clear_review(self):
        self._review_markers = {}
        self._apply_markers()

    def _apply_markers(self):
        merged: dict[int, list[tuple[str, str]]] = {}
        for source in (self._compiler_markers, self._review_markers, self._lint_markers, self._lsp_markers):
            for line, notes in source.items():
                merged.setdefault(line, []).extend(notes)
        self.editor.set_markers(merged)
        self.update_status()
        self.problems_changed.emit()

    def compiler_diagnostics(self) -> list[tuple[int, str, str]]:
        """The last build's messages about this file: (line, severity, message), by line."""
        return [(line, severity, message) for line, notes in sorted(self._compiler_markers.items())
                for severity, message in notes]

    def problems(self) -> list[Problem]:
        """Everything flagged in the file, in line order: the last build's
        compiler messages, the AI review's findings, and the underlined
        issues (fortitude, fortls, spelling)."""
        found = []
        for markers, source in ((self._compiler_markers, "build"), (self._review_markers, "review")):
            for line, notes in markers.items():
                for severity, message in notes:
                    found.append(Problem(line, 0, line, 0, severity, severity, message,
                                         MARKER_COLORS.get(severity, "#868e96"), source))
        for line_gremlins in self.editor.gremlins.values():
            for g in line_gremlins:
                found.append(Problem(g.line, g.column, g.line, g.column + 1, "gremlin", g.code, g.message,
                                     gremlins_mod.COLOR, "gremlins"))
        for issue in self.editor.lint_issues:
            found.append(Problem(issue.line, max(0, issue.column - 1), issue.end_line, max(0, issue.end_column - 1),
                                 issue.severity, issue.code, issue.message, MARKER_COLORS[issue.severity],
                                 PROBLEM_SOURCES.get(issue.severity, "fortitude"), issue))
        found.sort(key=lambda p: (p.line, p.column))
        return found

    def _explain_line(self, line: int):
        """Explain Error with AI on a line: its most serious compiler, fortitude
        or fortls problem."""
        rank = {"fatal error": 0, "error": 1, "lint error": 2, "fortls error": 3, "warning": 4}
        found = [p for p in self.problems() if p.line == line and p.source in ("build", "fortitude", "fortls")]
        if found:
            self.explain_requested.emit(min(found, key=lambda p: rank.get(p.severity, 9)))
