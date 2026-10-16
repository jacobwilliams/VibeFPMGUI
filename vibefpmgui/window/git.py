"""The Git panel: reading the project's repository's status (on a pool
thread) when it may have changed, and only while the panel is on view.

It may have changed when the project is opened or reloaded, a file is
saved, the app becomes active again after a while away (git used in a
terminal, another app), or Refresh is clicked. One read at a time: asked
again meanwhile, it's read once more when that one is done.
"""

from __future__ import annotations

import time
from pathlib import Path

from qtpy.QtCore import Qt
from qtpy.QtWidgets import QApplication

from .. import git_status as gs
from ..background import call_in_background
from ..git_panel import GitPanel


def _read(path: Path) -> tuple[gs.RepoStatus | None, str]:
    """(status, error message)."""
    try:
        return gs.read_status_apart(path), ""
    except Exception as error:  # noqa: BLE001 - shown in the panel (pygit2 raises several kinds)
        return None, str(error) or type(error).__name__


class GitMixin:
    """The Git panel (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_git_panel(self):
        """The Git panel."""
        self.git_panel = GitPanel()
        self.git_panel.icon_for = self.project_tree.icon_for
        self.git_panel.set_code_font(self._mono_font(editor=True))
        self.git_panel.refresh_requested.connect(self.refresh_git)
        self.git_panel.open_requested.connect(lambda path, line: self.navigate_to(path, line, 0, preview=True))
        self._git_request = 0     # (an answer to an older request is dropped)
        self._git_stale = True    # (read again once the panel is on view)
        self._git_reading = False  # (a read is under way)
        self._git_again = False    # (asked again meanwhile: read again once it's done)
        self._inactive_since: float | None = None
        QApplication.instance().applicationStateChanged.connect(self._app_state_changed)

    def refresh_git(self):
        """Read the repository's status again (or once the panel is on view)."""
        panel = self.git_panel
        if self.manifest_path is None:
            panel.show_message("Open a project in a git repository to see its changes here.")
            return
        if not gs.available():
            panel.show_message("The Git panel needs pygit2 (pixi add pygit2, or pip install pygit2).")
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
