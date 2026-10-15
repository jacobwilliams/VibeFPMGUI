"""The language server (fortls) for the editor: starting it, symbols, references, rename."""

from __future__ import annotations

from pathlib import Path

from qtpy.QtWidgets import QDialog, QMessageBox

from .. import lsp
from ..editor import EditorTab
from ..rename_dialog import RenamePreviewDialog
from ..source_text import read_source
from ..symbol_search import SymbolSearchDialog
from ..widgets import run_dialog


class LanguageMixin:
    """The language server's features (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _update_language_server(self):
        """Run fortls for the open project (restarting it if the project or
        its folders changed), and connect the open Fortran tabs to it."""
        if not self.fortls_action.isChecked() or lsp.executable() is None or self.manifest_path is None:
            self._stop_language_server()
            return
        root = self.manifest_path.parent.resolve()
        args = lsp.server_args(self.graph, root)
        client = self.lsp_client
        if client.is_running() and client.root == root and client.args == args:
            return
        for editor in self.editors():
            editor.detach_lsp()
        client.start(root, args)
        for editor in self.editors():
            editor.attach_lsp(client)
        self._update_editor_actions()

    def _stop_language_server(self):
        for editor in self.editors():
            editor.detach_lsp()
        self.lsp_client.stop()
        self._update_editor_actions()

    def _language_server_stopped(self, why: str):
        for editor in self.editors():
            editor.detach_lsp()
        self.statusBar().showMessage(f"{why} — hover, Go to Definition and the outline are off "
                                     "until the project is reloaded", 10000)
        self._update_editor_actions()

    def set_fortls_enabled(self, on: bool):
        self.settings.setValue("fortls", on)
        self._update_language_server()

    def go_to_symbol(self):
        if not self.lsp_client.is_running():
            return
        dialog = SymbolSearchDialog(self.lsp_client, self.manifest_path.parent if self.manifest_path else None,
                                    self)
        dialog.chosen.connect(self.navigate_to)
        run_dialog(dialog)

    def _open_editor_for(self, path: Path) -> EditorTab | None:
        path = path.resolve()
        return next((e for e in self.editors() if e.path == path), None)

    def _file_text(self, path: Path) -> str:
        """A file's current text: the editor's (maybe unsaved) if it's open."""
        editor = self._open_editor_for(path)
        if editor is not None:
            return editor.editor.toPlainText()
        return read_source(path).text

    def _show_references(self, name: str, results: list):
        roots, _texts, project_dir = self._search_sources()
        self.references_panel.show_results(name, results, roots, project_dir)
        self.show_panel(self.references_panel)
        self.statusBar().showMessage(f"{len(results)} reference{'s' * (len(results) != 1)} to {name}", 5000)

    def _preview_rename(self, old: str, new: str, edits: dict):
        edits = lsp.add_result_variable_edits(edits, old, new, self._file_text)
        open_files = {e.path for e in self.editors()}
        dialog = RenamePreviewDialog(old, new, edits, self._file_text, open_files,
                                     self.manifest_path.parent if self.manifest_path else None,
                                     self._mono_font(), self)
        if run_dialog(dialog) != QDialog.Accepted:
            return
        changed_open, written, failed, places = 0, 0, [], 0
        for path in dialog.selected():  # (deleted later: still there now)
            file_edits = edits.get(path, [])
            editor = self._open_editor_for(path)
            if editor is not None:
                editor.apply_text_edits(file_edits)  # unsaved; Undo reverts it
                changed_open += 1
            else:
                try:
                    text, encoding, newline, _mixed = read_source(path)
                    text = lsp.apply_edits(text, file_edits)
                    path.write_bytes(text.replace("\n", newline).encode(encoding, errors="replace"))
                except OSError as exc:
                    failed.append(f"{path}: {exc}")
                    continue
                self.lsp_client.file_changed_on_disk(path)
                written += 1
            places += len(file_edits)
        if failed:
            QMessageBox.warning(self, "Rename Symbol", "Some files could not be changed:\n\n"
                                + "\n".join(failed))
        parts = []
        if written:
            parts.append(f"{written} file{'s' * (written != 1)} saved")
        if changed_open:
            parts.append(f"{changed_open} open file{'s' * (changed_open != 1)} changed (not saved yet)")
        self.statusBar().showMessage(f"Renamed {old} → {new} in {places} place{'s' * (places != 1)}"
                                     + (f": {', '.join(parts)}" if parts else ""), 10000)
        self.project_tree.refresh()

    def _lsp_diagnostics(self, path: Path, issues: list):
        """fortls checked a file (opened or saved): show it in its tab."""
        self._lsp_diagnostics_by_file[path] = issues
        editor = self._open_editor_for(path)
        if editor is not None:
            editor.show_lsp_diagnostics(issues)
