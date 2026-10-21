"""A file's diff with a line over it saying what's compared ("staged →
working tree", "3f9db510 → 930ec19c"): the Git panel's Diff page and its
History's. The two versions are read on a pool thread; a file chosen
meanwhile wins (the older answer is dropped).
"""

from __future__ import annotations

from collections.abc import Callable, Hashable

from qtpy.QtCore import Signal
from qtpy.QtGui import QFont
from qtpy.QtWidgets import QVBoxLayout, QWidget

from .background import call_in_background
from .diff_view import DiffView
from .git_status import BINARY
from .widgets import StatusLabel

# What load() reads: (the older text, the newer one, what's compared). A text
# is None where there's no such version (added, deleted), BINARY where it
# isn't text. It raises OSError, or RuntimeError (git_status.GitError), if
# it can't be read.
Versions = tuple[str | None, str | None, str]


class FileDiff(QWidget):
    open_requested = Signal(int)  # a line of the newer version (1-based), double-clicked

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = StatusLabel()
        self.diff = DiffView()
        self.diff.open_requested.connect(self.open_requested)
        self._loading: Hashable | None = None  # (what's being read: see load)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.title)
        layout.addWidget(self.diff, 1)

    def load(self, key: Hashable, path: str, read: Callable[[], Versions]):
        """Show ``path``'s diff, its versions from ``read()`` (on a pool
        thread); ``key`` tells this file apart from the next one chosen."""
        self._loading = key
        self.title.setText(f"<b>{path}</b> · reading…")

        def work():
            try:
                return read()
            except (OSError, RuntimeError) as error:
                return str(error) or type(error).__name__

        call_in_background(work, lambda result: self._read(key, path, result), self)

    def _read(self, key: Hashable, path: str, result):
        if key != self._loading:
            return  # (another file chosen meanwhile)
        if isinstance(result, str):
            self.title.setText(f"<b>{path}</b> · couldn't read it: {result}")
            self.diff.clear()
            return
        old, new, compared = result
        if BINARY in (old, new):
            self.title.setText(f"<b>{path}</b> · binary or very big: no diff shown")
            self.diff.clear()
            return
        self.title.setText(f"<b>{path}</b> · {compared}")
        self.diff.show_diff(old, new, path)

    def clear(self):
        self._loading = None
        self.title.setText("")
        self.diff.clear()

    def set_unified(self, on: bool):
        self.diff.set_unified(on)

    def set_font(self, font: QFont):
        self.diff.set_font(font)

    def refresh_style(self, muted: str):
        """The colors: the title's (muted), the diff's (the syntax style's)."""
        self.title.setStyleSheet(f"color: {muted};")
        self.diff.refresh_style()
