"""Popups for the language server: the completion list and argument hints.

Both are tooltip-style windows that never take the keyboard focus, so typing
continues in the editor; :class:`~vibefpmgui.editor.CodeEditor` passes them
the keys they use (Up/Down/Enter/Tab/Escape for the completion list).
"""

from __future__ import annotations

from qtpy.QtCore import QRect, QSize, Qt, Signal
from qtpy.QtGui import QColor, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
)

from . import lsp as lsp_mod

MAX_ROWS = 10
ITEM_ROLE = Qt.UserRole + 1


def popup_colors(editor) -> tuple[QColor, QColor, QColor]:
    """(background, text, border) matching the editor's colors."""
    pal = editor.palette()
    base, text = pal.color(QPalette.Base), pal.color(QPalette.Text)
    bg = base.lighter(115) if base.lightness() < 128 else base.darker(103)
    border = QColor(text.red(), text.green(), text.blue(), 90)
    return bg, text, border


def place(popup, anchor: QRect, below: bool = True):
    """Put a popup just below (or above) a global rectangle, on screen."""
    screen = (QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()).availableGeometry()
    x = min(max(anchor.left(), screen.left()), screen.right() - popup.width())
    y = anchor.bottom() + 2 if below else anchor.top() - popup.height() - 2
    if below and y + popup.height() > screen.bottom():
        y = anchor.top() - popup.height() - 2
    elif not below and y < screen.top():
        y = anchor.bottom() + 2
    popup.move(x, max(screen.top(), y))


class _KindDelegate(QStyledItemDelegate):
    """Draws each completion's kind (function, variable…) greyed at the right."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        item: lsp_mod.Completion | None = index.data(ITEM_ROLE)
        if item is None or not item.kind:
            return
        painter.save()
        painter.setFont(option.font)
        color = option.palette.color(QPalette.HighlightedText if option.state & QStyle.State_Selected
                                     else QPalette.Text)
        color.setAlpha(140)
        painter.setPen(color)
        painter.drawText(option.rect.adjusted(0, 0, -8, 0), Qt.AlignRight | Qt.AlignVCenter, item.kind)
        painter.restore()


class CompletionPopup(QFrame):
    """The list of completions for the name being typed."""

    accepted = Signal(object)  # the Completion chosen (double-click)

    def __init__(self, editor):
        super().__init__(editor, Qt.ToolTip | Qt.FramelessWindowHint)
        self.editor = editor
        self.setFrameShape(QFrame.Box)
        self.items: list[lsp_mod.Completion] = []
        self.start: tuple[int, int] | None = None  # (block number, column) the name starts at
        self.list = QListWidget()
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setUniformItemSizes(True)
        self.list.setItemDelegate(_KindDelegate(self.list))
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.currentRowChanged.connect(self._show_detail)
        self.list.itemDoubleClicked.connect(lambda item: self.accepted.emit(item.data(ITEM_ROLE)))
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setContentsMargins(6, 3, 6, 4)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self.list)
        layout.addWidget(self.detail)

    def open(self, items: list[lsp_mod.Completion], start: tuple[int, int], prefix: str,
             anchor: QRect) -> bool:
        """Show ``items`` matching ``prefix``; False (and hidden) if none do."""
        self.items, self.start = items, start
        bg, text, border = popup_colors(self.editor)
        # The popup is never the active window, so give the selection the
        # active colors explicitly (the inactive ones are hard to see).
        pal = QApplication.palette()
        sel, sel_text = pal.color(QPalette.Active, QPalette.Highlight), pal.color(QPalette.Active,
                                                                                 QPalette.HighlightedText)
        self.setStyleSheet(f"CompletionPopup {{ background: {bg.name()}; border: 1px solid "
                           f"{border.name(QColor.HexArgb)}; }} QListWidget, QLabel {{ background: "
                           f"{bg.name()}; color: {text.name()}; }}"
                           f"QListWidget::item:selected {{ background: {sel.name()}; color: {sel_text.name()}; }}")
        for widget in (self, self.list, self.detail):
            widget.setFont(self.editor.font())
        if not self.filter(prefix):
            return False
        place(self, anchor)
        self.show()
        return True

    def filter(self, prefix: str) -> bool:
        """Show the items matching what has been typed (names starting with
        it first, then names containing it); False if nothing matches."""
        lower = prefix.lower()
        matches = [c for c in self.items if c.label.lower().startswith(lower)]
        matches += [c for c in self.items if lower and lower in c.label.lower()[1:]
                    and not c.label.lower().startswith(lower)]
        if not matches or (len(matches) == 1 and matches[0].label == prefix):
            self.hide()  # nothing to offer (or it's already typed out)
            return False
        self.list.clear()
        for c in matches:
            item = QListWidgetItem(c.label)
            item.setData(ITEM_ROLE, c)
            self.list.addItem(item)
        self.list.setCurrentRow(0)
        metrics = self.list.fontMetrics()
        row_h = self.list.sizeHintForRow(0)
        width = max(metrics.horizontalAdvance(c.label + "    " + c.kind) for c in matches[:200]) + 40
        width = min(max(width, 260), 560)
        self.list.setFixedSize(QSize(width, row_h * min(MAX_ROWS, len(matches)) + 4))
        self.detail.setFixedWidth(width)
        self._show_detail(0)
        return True

    def _fit(self):
        """Size the popup to the list plus the detail line. (adjustSize()
        only ever grows a window, so a shorter list left a gap.)"""
        detail_h = self.detail.heightForWidth(self.detail.width()) if self.detail.isVisibleTo(self) else 0
        self.detail.setFixedHeight(max(detail_h, 0))
        self.setFixedSize(self.list.width() + 2, self.list.height() + detail_h + 2)

    def _show_detail(self, row: int):
        item = self.list.item(row)
        c: lsp_mod.Completion | None = item.data(ITEM_ROLE) if item else None
        text = c.detail if c else ""
        self.detail.setText(text)
        self.detail.setVisible(bool(text))
        self._fit()

    def move_selection(self, step: int):
        rows = self.list.count()
        if rows:
            row = self.list.currentRow() + step
            self.list.setCurrentRow(max(0, min(rows - 1, row)))

    def current(self) -> lsp_mod.Completion | None:
        item = self.list.currentItem()
        return item.data(ITEM_ROLE) if item else None

    def page_rows(self) -> int:
        return MAX_ROWS - 1


class SignaturePopup(QLabel):
    """Argument hints for the call being typed: the routine's arguments with
    the current one in bold, and that argument's declaration."""

    def __init__(self, editor):
        super().__init__(editor, Qt.ToolTip | Qt.FramelessWindowHint)
        self.editor = editor
        self.setTextFormat(Qt.RichText)
        self.setWordWrap(False)
        self.setMargin(6)
        self.line: int | None = None  # the block the hint is for

    def open(self, text_html: str, line: int, anchor: QRect):
        bg, text, border = popup_colors(self.editor)
        self.setStyleSheet(f"SignaturePopup {{ background: {bg.name()}; color: {text.name()}; "
                           f"border: 1px solid {border.name(QColor.HexArgb)}; }}")
        self.setFont(self.editor.font())
        self.setText(text_html)
        self.line = line
        self.adjustSize()
        screen = (QApplication.screenAt(anchor.center()) or QApplication.primaryScreen()).availableGeometry()
        if self.width() > screen.width() * 0.8:
            self.setWordWrap(True)
            self.setFixedWidth(int(screen.width() * 0.8))
            self.adjustSize()
        place(self, anchor, below=False)  # above the line, so the completion list fits below
        self.show()

    def hideEvent(self, event):
        self.line = None
        super().hideEvent(event)


def global_rect(editor, cursor) -> QRect:
    """The cursor's rectangle in screen coordinates."""
    rect = editor.cursorRect(cursor)
    return QRect(editor.viewport().mapToGlobal(rect.topLeft()), rect.size())
