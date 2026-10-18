"""Small widgets shared by the window's parts."""

from qtpy.QtCore import QSize, Qt, Signal
from qtpy.QtWidgets import QDialog, QLabel, QLineEdit, QMenu, QPushButton, QSizePolicy


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


class HistoryLineEdit(QLineEdit):
    """A field for commands, as a shell's: Enter sends its text
    (``submitted``), remembers it, and clears the field; Up and Down go
    through what was sent (Down past the last one: what was being typed).
    The history is this field's own, for as long as it lives (not saved)."""

    submitted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.history: list[str] = []
        self._index = 0  # (in history; len(history): the text being typed, not sent yet)
        self._draft = ""
        self.returnPressed.connect(self._submit)

    def _submit(self):
        text = self.text().strip()
        if not text:
            return
        if not self.history or self.history[-1] != text:  # (the same twice in a row: kept once)
            self.history.append(text)
        self._index, self._draft = len(self.history), ""
        self.clear()
        self.submitted.emit(text)

    def keyPressEvent(self, event):
        key = event.key()
        if key == Qt.Key_Up and self._index > 0:
            if self._index == len(self.history):
                self._draft = self.text()
            self._index -= 1
            self.setText(self.history[self._index])
        elif key == Qt.Key_Down and self._index < len(self.history):
            self._index += 1
            self.setText(self._draft if self._index == len(self.history) else self.history[self._index])
        elif key in (Qt.Key_Up, Qt.Key_Down):
            pass  # (no earlier, or later, entry)
        else:
            super().keyPressEvent(event)


def run_dialog(dialog: QDialog) -> int:
    """Show a dialog until it's closed, then let it go (a dialog that's a
    child of the window would otherwise stay, hidden, until the app quits)."""
    try:
        return dialog.exec()
    finally:
        dialog.deleteLater()


class StayOpenMenu(QMenu):
    """A menu that stays open when a checkable item is clicked (to tick several)."""

    def mouseReleaseEvent(self, event):
        action = self.activeAction()
        if action is not None and action.isCheckable() and action.isEnabled():
            action.trigger()
            return
        super().mouseReleaseEvent(event)


class MenuButton(QPushButton):
    """A button that opens a menu, its text cut short with "…" to fit (it
    doesn't make the panel it's in wider); the whole text is its tooltip."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_full_text(self, text: str):
        self._full_text = text
        self._elide()

    def full_text(self) -> str:
        return self._full_text

    def minimumSizeHint(self):
        hint = super().minimumSizeHint()
        return QSize(60, hint.height())

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(60, hint.height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self):
        room = max(10, self.width() - 40)  # (the menu arrow and the margins)
        self.setText(self.fontMetrics().elidedText(self._full_text, Qt.ElideRight, room))
