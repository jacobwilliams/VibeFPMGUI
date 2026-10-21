"""The Git panel: reading the project's repository's status (git, on a
pool thread) when it may have changed, and only while the panel is on view.

It may have changed when the project is opened or reloaded, a file is
saved, the app becomes active again after a while away (git used in a
terminal, another app), or Refresh is clicked. One read at a time: asked
again meanwhile, it's read once more when that one is done.
"""

from __future__ import annotations

import time
from pathlib import Path

from qtpy.QtCore import QFile, Qt
from qtpy.QtWidgets import QApplication, QMessageBox

from .. import git_status as gs
from ..background import call_in_background
from ..git_panel import GitPanel
from ..widgets import run_dialog


def _read(path: Path) -> tuple[gs.RepoStatus | None, str]:
    """(status, error message)."""
    try:
        return gs.read_status(path), ""
    except (gs.GitError, OSError) as error:
        return None, str(error) or type(error).__name__


def move_to_trash(path: Path) -> bool:
    """Move a file to the Trash (the Recycle Bin on Windows); False: it couldn't be."""
    return QFile.moveToTrash(str(path))


class GitMixin:
    """The Git panel (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_git_panel(self):
        """The Git panel."""
        self.git_panel = GitPanel()
        self.git_panel.icon_for = self.project_tree.icon_for
        self.git_panel.set_code_font(self._mono_font(editor=True))
        self.git_panel.refresh_requested.connect(self.refresh_git)
        self.git_panel.open_requested.connect(lambda path, line: self.navigate_to(path, line, 0, preview=True))
        self.git_panel.reveal_requested.connect(self._show_in_project_files)
        self.git_panel.discard_requested.connect(self.discard_git_change)
        self._git_request = 0     # (an answer to an older request is dropped)
        self._git_stale = True    # (read again once the panel is on view)
        self._git_reading = False  # (a read is under way)
        self._git_again = False    # (asked again meanwhile: read again once it's done)
        self._inactive_since: float | None = None
        QApplication.instance().applicationStateChanged.connect(self._app_state_changed)

    def _show_in_project_files(self, path: str):
        """A changed file's Show in Project Files: Project files in front, the file selected in it."""
        self.show_panel(self._content(self.docks["files"]))
        if not self.project_tree.reveal(path):
            name = Path(path).name
            self.statusBar().showMessage(f"{name} isn't in Project files (it's outside the project, or filtered "
                                         "out)", 5000)

    def discard_git_change(self, change: gs.Change):
        """The Git panel's Discard Changes… (Delete File… for an untracked
        one): asked first, saying what's lost; files git has no copy of go to
        the Trash (not deleted: they can be got back)."""
        status = self.git_panel.status
        if status is None:
            return
        root, name = status.root, change.path
        new = change.area == gs.UNTRACKED or (change.area == gs.STAGED and change.kind in ("added", "renamed"))
        both = change.area == gs.STAGED and any(c.path == name and c.area == gs.CHANGED for c in status.changes)
        if change.area == gs.UNTRACKED:
            question = (f"Move {name} to the Trash?\n\nIt isn't in git: this is its only copy (until the "
                        "Trash is emptied).")
        elif change.area == gs.CHANGED:
            staged = any(c.path == name and c.area == gs.STAGED for c in status.changes)
            question = (f"Discard the changes to {name}?\n\nIt goes back to its "
                        + ("staged version (what's staged is kept)" if staged else "version in the last commit")
                        + ": the changes are lost.")
        elif new:
            question = (f"Discard {name}, new in the staged changes?\n\nIt's unstaged and moved to the Trash"
                        + (f", and {change.old_path} gets its name back" if change.kind == "renamed" else "") + ".")
        else:
            question = (f"Discard the changes to {name}, staged" + (" and not" if both else "")
                        + "?\n\nIt goes back to its version in the last commit: the changes are lost.")
        editor = self._open_editor_for(root / name)
        if editor is not None and editor.editor.document().isModified():
            question += "\n\nIt's open in the editor with unsaved edits: they're kept there, to save or not."
        box = QMessageBox(QMessageBox.Warning, "Discard Changes", question, QMessageBox.Cancel, self)
        discard = box.addButton("Move to Trash" if new else "Discard", QMessageBox.DestructiveRole)
        box.setDefaultButton(QMessageBox.Cancel)
        run_dialog(box)
        if box.clickedButton() is not discard:
            return

        def work():
            try:
                return gs.discard(root, change), ""
            except gs.GitError as error:
                return [], str(error)

        def done(result):
            paths, error = result
            if error:
                QMessageBox.warning(self, "Discard Changes", f"git couldn't discard the changes to {name}:\n\n{error}")
            kept = [p for p in paths if p.exists() and not move_to_trash(p)]
            if kept:
                QMessageBox.warning(self, "Discard Changes", "Couldn't move to the Trash (left where it is):\n"
                                    + "\n".join(str(p) for p in kept))
            elif not error:
                self.statusBar().showMessage(f"Discarded the changes to {name}"
                                             + (" (moved to the Trash)" if paths else ""), 5000)
            self.refresh_git()

        call_in_background(work, done, self)

    def refresh_git(self):
        """Read the repository's status again (or once the panel is on view)."""
        panel = self.git_panel
        if self.manifest_path is None:
            panel.show_message("Open a project in a git repository to see its changes here.")
            return
        if not gs.available():
            panel.show_message("The Git panel needs git: install it (https://git-scm.com) and make sure "
                               "it's on the PATH, then click Refresh.")
            return
        if not self.panel_in_front(panel):
            self._git_stale = True
            return
        self._git_stale = False
        if self._git_reading:
            self._git_again = True
            return
        self._git_reading = True
        self._git_request += 1
        request, folder = self._git_request, self.manifest_path.parent
        call_in_background(lambda: _read(folder), lambda result: self._git_read(request, *result), self)

    def _git_read(self, request: int, status: gs.RepoStatus | None, error: str):
        self._git_reading = False
        if self._git_again:
            self._git_again = False
            self.refresh_git()
            return  # (that one's answer is the one shown)
        if request != self._git_request or self.manifest_path is None:
            return
        if error:
            self.git_panel.show_message(f"Couldn't read the repository: {error}")
        elif status is None:
            self.git_panel.show_message(f"{self.manifest_path.parent.name} isn't in a git repository.")
        else:
            self.git_panel.show_status(status)

    def _git_panel_shown(self, shown: bool):
        if shown and self._git_stale:
            self.refresh_git()

    AWAY_SECONDS = 1.0  # (shorter: a dialog, a popup, a focus change; not time to use git elsewhere)

    def _app_state_changed(self, state):
        if state != Qt.ApplicationActive:
            if self._inactive_since is None:
                self._inactive_since = time.monotonic()
            return
        since, self._inactive_since = self._inactive_since, None
        if since is not None and time.monotonic() - since >= self.AWAY_SECONDS:
            self.refresh_git()  # (back from a terminal, or another app, that may have used git)
