"""The editor's hover card: a fortitude issue's or a word's (with its fixes), or fortls's hover."""

from __future__ import annotations

from typing import TYPE_CHECKING

from qtpy.QtCore import QPoint, Qt, QTimer
from qtpy.QtGui import QColor, QCursor, QPalette
from qtpy.QtWidgets import QApplication, QFrame, QTextBrowser, QToolTip, QVBoxLayout

if TYPE_CHECKING:
    from .code_editor import CodeEditor


class HoverPopup(QFrame):
    """A tooltip-like popup for hover information that can scroll: fortls's
    answer for a long declaration or doc comment can be taller than the
    screen, and QToolTip can't scroll. It stays open while the mouse is on
    the name or inside the popup (so it can be scrolled), and closes when
    the mouse leaves both, on a key press, or when the editor scrolls."""

    MAX_WIDTH, MAX_HEIGHT = 720, 420  # also at most ~60% / 45% of the screen

    def __init__(self, editor: CodeEditor):
        super().__init__(editor, Qt.ToolTip | Qt.FramelessWindowHint)
        self.editor = editor
        self.anchor = None  # what the popup is about (see CodeEditor.hover_key)
        # A rounded card: the window is transparent, the card inside is drawn.
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.card = QFrame()
        self.card.setObjectName("HoverCard")
        self.browser = QTextBrowser()
        self.browser.setFrameShape(QFrame.NoFrame)
        self.browser.setOpenLinks(False)
        self.browser.anchorClicked.connect(self._link_clicked)
        self.browser.setFont(QToolTip.font())
        self.browser.document().setDocumentMargin(9)
        self.browser.viewport().setAutoFillBackground(False)
        inner = QVBoxLayout(self.card)
        inner.setContentsMargins(1, 3, 1, 3)
        inner.addWidget(self.browser)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.card)
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.setInterval(300)  # time to move the mouse into the popup
        self._hide_timer.timeout.connect(self._hide_unless_hovered)

    def show_at(self, global_pos: QPoint, text_html: str, anchor):
        self.anchor = anchor
        self._hide_timer.stop()
        # The editor's colors (Qt's tooltip colors don't follow dark mode on macOS).
        pal = self.editor.palette()
        base, text = pal.color(QPalette.Base), pal.color(QPalette.Text)
        bg = base.lighter(115) if base.lightness() < 128 else base.darker(103)
        border = QColor(text.red(), text.green(), text.blue(), 70).name(QColor.HexArgb)
        self.setStyleSheet(f"#HoverCard {{ background: {bg.name()}; border: 1px solid {border}; "
                           f"border-radius: 9px; }} QTextBrowser {{ background: transparent; "
                           f"color: {text.name()}; }}")
        self.browser.setHtml(text_html)
        screen = (QApplication.screenAt(global_pos) or QApplication.primaryScreen()).availableGeometry()
        max_w = min(self.MAX_WIDTH, int(screen.width() * 0.6))
        max_h = min(self.MAX_HEIGHT, int(screen.height() * 0.45))
        doc = self.browser.document()
        doc.setTextWidth(-1)
        bar = self.browser.verticalScrollBar().sizeHint().width()
        width = min(int(doc.idealWidth()) + 4, max_w)
        doc.setTextWidth(width)
        height = int(doc.size().height()) + 10
        if height > max_h:  # it will scroll: leave room for the scroll bar
            width = min(width + bar, max_w)
            height = max_h
        self.resize(width + 4, height + 8)  # (the card's border and margins)
        # Below the mouse; above it if there's no room; always on screen.
        x = min(max(global_pos.x(), screen.left()), screen.right() - self.width())
        y = global_pos.y() + 18
        if y + self.height() > screen.bottom():
            y = max(screen.top(), global_pos.y() - 8 - self.height())
        self.move(x, y)
        self.browser.verticalScrollBar().setValue(0)
        self.show()

    def _link_clicked(self, url):
        """The card's links: "Apply fix" (fix:N = the editor's Nth lint
        issue), a spelling suggestion (spell:N:word) or "Add to Dictionary"
        (addword:N); a web page (opened in the browser: a git blame card's
        commit, issue, file), or copy:TEXT (to the clipboard)."""
        if url.scheme() in ("http", "https"):
            from .git_blame import open_web_page

            self.hide()
            open_web_page(url.toString())
            return
        if url.scheme() == "copy":
            QApplication.clipboard().setText(url.path())
            self.hide()
            return
        if url.scheme() in ("spell", "addword"):
            number, _, word = url.path().partition(":")
            try:
                issue = self.editor.lint_issues[int(number)]
            except (ValueError, IndexError):
                return
            self.hide()
            if url.scheme() == "spell":
                self.editor.replace_word(issue, word)
            else:
                self.editor.word_added.emit(issue.word)
            return
        if url.scheme() == "fix":
            try:
                issue = self.editor.lint_issues[int(url.path())]
            except (ValueError, IndexError):
                return
            self.hide()
            self.editor.lint_fix_requested.emit(issue)

    def schedule_hide(self):
        if self.isVisible() and not self._hide_timer.isActive():
            self._hide_timer.start()

    def keep(self):
        self._hide_timer.stop()

    def _hide_unless_hovered(self):
        if not self.geometry().contains(QCursor.pos()):
            self.hide()

    def leaveEvent(self, event):
        self.schedule_hide()
        super().leaveEvent(event)

    def enterEvent(self, event):
        self.keep()
        super().enterEvent(event)
