"""Find and Replace in Files, and the Problems panel.

Also what the lists of places in files (References, Search, Problems) share.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import QTimer
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import QMessageBox

from .. import lsp
from ..problems_panel import ProblemsPanel
from ..project_search import SearchPanel, search_roots
from ..references_panel import ReferencesPanel
from ..result_tree import file_labels, root_for
from ..source_text import read_source
from ..widgets import run_dialog


class SearchMixin:
    """Find and Replace in Files, and the Problems panel (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_search_panels(self):
        """The References, Search and Problems panels."""
        self.references_panel = ReferencesPanel()
        # (A result clicked: its file in the preview tab, unless it's open; double-clicked: kept open.)
        self.references_panel.open_requested.connect(lambda *match: self._open_search_match(*match, preview=True))
        self.references_panel.keep_requested.connect(self._keep_open)
        self.search_panel = SearchPanel(self.settings)
        self.search_panel.provider = self._search_sources
        self.search_panel.open_requested.connect(lambda *match: self._open_search_match(*match, preview=True))
        self.search_panel.keep_requested.connect(self._keep_open)
        self.search_panel.replace_requested.connect(self._replace_in_files)
        self.problems_panel = ProblemsPanel(self.settings)
        self.problems_panel.open_requested.connect(self._open_search_match)
        self.problems_panel.explain_requested.connect(
            lambda problem: self._with_editor(lambda e: self.explain_problem(e, problem)))
        self.problems_panel.fix_requested.connect(self.fix_with_ai)
        self.problems_panel.apply_fix_requested.connect(self._apply_problem_fix)
        # (A burst of changes, e.g. spelling and fortitude answering together: one update.)
        self._problems_timer = QTimer(self)
        self._problems_timer.setSingleShot(True)
        self._problems_timer.setInterval(150)
        self._problems_timer.timeout.connect(self._update_problems)
        self._problems_visible = False  # (in front of its stack, or on its own: see _problems_shown)

    def _link_file_lists(self):
        """The lists of places in files (vibefpmgui.result_tree), drawn alike:
        Project files' icons, the files' text as the editor has it."""
        self.coverage_panel.icon_for = self.project_tree.icon_for
        self.result_trees = [self.references_panel.tree, self.search_panel.tree, self.problems_panel.tree]
        for tree in self.result_trees:
            tree.icon_for = self.project_tree.icon_for
            tree.text_for = self._open_text
            tree.mono = self._mono_font()
            tree.refresh_style()

    def find_in_files(self, replace: bool = False):
        """Show the Search tab, with the editor's selection to search for."""
        text = ""
        editor = self.current_editor()
        if editor is not None:
            selected = editor.editor.textCursor().selectedText()
            if selected and "\u2029" not in selected:  # (one line only)
                text = selected
        self.show_panel(self.search_panel)
        self.search_panel.open_find(text, replace)

    def _open_text(self, path: Path) -> str | None:
        """The text of a file open in the editor (unsaved changes included)."""
        editor = self._open_editor_for(path)
        return editor.editor.toPlainText() if editor is not None else None

    def _search_sources(self):
        """For the Search tab: (packages to search, open files' text, project folder)."""
        if self.manifest_path is None:
            return [], {}, None
        project_dir = self.manifest_path.parent
        texts = {e.path: e.editor.toPlainText() for e in self.editors() if not e.untitled}
        return search_roots(self.graph, project_dir), texts, project_dir

    def _problems_shown(self, shown: bool):
        """The Problems panel came to the front (or went behind another, or
        closed). Qt says so here; isVisible() isn't reliable for a panel
        behind another in a stack."""
        self._problems_visible = shown
        if shown:
            self._update_problems()

    def _update_problems(self, *_):
        """The Problems tab: the file in the editor's problems (only while
        it's shown: it catches up when it is)."""
        if not hasattr(self, "problems_panel"):
            return  # (the window is still being built)
        self._problems_timer.stop()
        if not self._problems_visible:
            return
        editor = self.current_editor()
        if editor is None:
            self.problems_panel.show_problems(None, [])
            return
        project_dir = self.manifest_path.parent if self.manifest_path else None
        roots = search_roots(self.graph, project_dir) if project_dir else []
        folder, package = file_labels(editor.path, root_for(editor.path, roots), project_dir)
        self.problems_panel.show_problems(editor.path, editor.problems(), folder, package)

    def _apply_problem_fix(self, path, problem):
        """Problems → right-click → Apply Fix: fortitude's fix, as its hover
        card's Apply fix does (in the file's tab, which the panel is for)."""
        editor = next((e for e in self.editors() if e.path == Path(path)), None)
        if editor is not None and problem.issue is not None:
            editor.apply_lint_fix(problem.issue)

    def _open_search_match(self, path: str, line: int, column: int, end_line: int, end_column: int,
                           preview: bool = False):
        """Open a search result with the match selected (``preview``: in the
        preview tab, if the file isn't open already)."""
        self.navigate_to(path, line, column, preview=preview)
        editor = self.current_editor()
        if editor is None or editor.path != Path(path).resolve():
            return
        doc = editor.editor.document()
        start, end = doc.findBlockByNumber(line - 1), doc.findBlockByNumber(end_line - 1)
        if start.isValid() and end.isValid():
            cursor = editor.editor.textCursor()
            cursor.setPosition(start.position() + min(column, start.length() - 1))
            cursor.setPosition(end.position() + min(end_column, end.length() - 1), QTextCursor.KeepAnchor)
            editor.editor.setTextCursor(cursor)

    def _replace_in_files(self, plan: dict, ask: bool):
        """Apply Find in Files replacements: open files are changed in the
        editor (not saved; Undo reverts), the others are saved."""
        places = sum(len(edits) for edits in plan.values())
        on_disk = [p for p in plan if self._open_editor_for(p) is None]
        if ask:
            files = len(plan)
            text = (f"Replace {places:,} match{'es' * (places != 1)} in {files:,} "
                    f"file{'s' * (files != 1)} with “{self.search_panel.replace_edit.text()}”?")
            info = []
            if on_disk:
                info.append(f"{len(on_disk):,} file{'s' * (len(on_disk) != 1)} will be changed and saved "
                            "(this can't be undone).")
            if len(on_disk) < files:
                info.append("Files open in the editor are changed but not saved (Undo reverts them).")
            box = QMessageBox(QMessageBox.Question, "Replace in Files", text,
                              QMessageBox.Yes | QMessageBox.Cancel, self)
            box.setInformativeText(" ".join(info))
            box.button(QMessageBox.Yes).setText("Replace")
            if run_dialog(box) != QMessageBox.Yes:
                return
        changed_open, written, failed = 0, 0, []
        for path, edits in plan.items():
            editor = self._open_editor_for(path)
            if editor is not None:
                editor.apply_text_edits(edits)
                changed_open += 1
                continue
            try:
                text, encoding, newline, _mixed = read_source(path)
                text = lsp.apply_edits(text, edits)
                path.write_bytes(text.replace("\n", newline).encode(encoding, errors="replace"))
            except OSError as exc:
                failed.append(f"{path}: {exc}")
                continue
            self.lsp_client.file_changed_on_disk(path)
            written += 1
        if failed:
            QMessageBox.warning(self, "Replace in Files", "Some files could not be changed:\n\n"
                                + "\n".join(failed))
        self.search_panel.refresh_files(plan.keys())
        parts = []
        if written:
            parts.append(f"{written} file{'s' * (written != 1)} saved")
        if changed_open:
            parts.append(f"{changed_open} open file{'s' * (changed_open != 1)} changed (not saved yet)")
        self.statusBar().showMessage(f"Replaced {places:,} match{'es' * (places != 1)}"
                                     + (f": {', '.join(parts)}" if parts else ""), 10000)
