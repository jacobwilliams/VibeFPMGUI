"""Small widgets shared by the window's parts."""

from qtpy.QtCore import QSize
from qtpy.QtWidgets import QDialog, QLabel


class StatusLabel(QLabel):
    """A label for a line of status whose text keeps changing (an editor's
    status line, a panel's progress): it asks for its text's width when
    there's room, but needs only a little. A plain label's minimum width is
    its whole text, so a longer status ("154 AI review notes", "… · context
    ~14k of 32k") made the window wider, past the screen's edge for a window
    filling it. When narrow, the text is cut off at the right."""

    MINIMUM_WIDTH = 40

    def minimumSizeHint(self) -> QSize:
        hint = super().minimumSizeHint()
        return QSize(min(hint.width(), self.MINIMUM_WIDTH), hint.height())


def run_dialog(dialog: QDialog) -> int:
    """Show a dialog until it's closed, then let it go (a dialog that's a
    child of the window would otherwise stay, hidden, until the app quits)."""
    try:
        return dialog.exec()
    finally:
        dialog.deleteLater()
