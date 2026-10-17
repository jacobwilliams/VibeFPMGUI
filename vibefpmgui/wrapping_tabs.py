"""The editor's tabs on several rows (View → Editor Tabs → Several Rows),
instead of one row that scrolls.

Qt's tab bar has one row only. `WrappingTabBar` lays the editor's tabs out
in as many rows as they need, drawn as Qt draws tabs (the style's own tab
shapes), and stands in for the tab bar, which is hidden but stays the
tabs' model: the names, icons and tooltips are read from it, and choosing,
moving and closing tabs is done through it (so everything else about the
tabs works as before).
"""

from __future__ import annotations

from qtpy.QtCore import QPoint, QRect, QSize, Qt, Signal
from qtpy.QtGui import QColor, QFont, QPainter
from qtpy.QtWidgets import (
    QApplication,
    QSizePolicy,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTabWidget,
    QToolTip,
    QWidget,
)

from .icons import make_icon

CLOSE_SIZE = 12  # the close glyph (as the tabs' own close buttons)


class WrappingTabBar(QWidget):
    """A ``QTabWidget``'s tabs, wrapped onto rows."""

    close_requested = Signal(int)  # a tab's close glyph clicked

    def __init__(self, tabs: QTabWidget, parent=None):
        super().__init__(parent)
        self.tabs = tabs
        self.bar: QTabBar = tabs.tabBar()
        self._rects: list[QRect] = []
        self._pressed: int | None = None   # the tab pressed (being dragged to another place)
        self._press_pos = QPoint()
        self._closing: int | None = None   # a close glyph pressed
        self._hover: int | None = None     # (the close glyph under the mouse: highlighted)
        self.setMouseTracking(True)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        tabs.currentChanged.connect(self.refresh)
        self.bar.tabMoved.connect(self.refresh)

    # ------------------------------------------------------------ layout
    def refresh(self, *_):
        """The tabs changed (added, removed, renamed, moved, chosen)."""
        self._layout(self.width())
        self.updateGeometry()
        self.update()

    def _layout(self, width: int) -> int:
        """Place the tabs in rows ``width`` wide; the height they take."""
        rects, x, y, row_height = [], 0, 0, 0
        for index in range(self.bar.count()):
            size = self.bar.tabSizeHint(index)
            if x and x + size.width() > width:  # (no room left on this row: the next)
                x, y = 0, y + row_height
                row_height = 0
            rects.append(QRect(x, y, size.width(), size.height()))
            x += size.width()
            row_height = max(row_height, size.height())
        self._rects = rects
        return y + row_height

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        rects = self._rects
        height = self._layout(width)
        self._rects = rects  # (only asked: the tabs stay where they are)
        return height

    def sizeHint(self) -> QSize:
        return QSize(self.width() or 400, self.heightForWidth(self.width() or 400))

    def minimumSizeHint(self) -> QSize:
        return QSize(0, self.sizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        height = self._layout(event.size().width())
        if height != self.height():
            self.setFixedHeight(height)  # (more rows, or fewer: the editor below gets the rest)

    def index_at(self, pos: QPoint) -> int:
        return next((i for i, rect in enumerate(self._rects) if rect.contains(pos)), -1)

    def tab_rect(self, index: int) -> QRect:
        return QRect(self._rects[index]) if 0 <= index < len(self._rects) else QRect()

    # ------------------------------------------------------------ drawing
    def _option(self, index: int) -> QStyleOptionTab:
        option = QStyleOptionTab()
        self.bar.initStyleOption(option, index)
        option.rect = self._rects[index]
        row = [i for i, r in enumerate(self._rects) if r.top() == option.rect.top()]
        option.position = (QStyleOptionTab.OnlyOneTab if len(row) == 1 else QStyleOptionTab.Beginning
                           if index == row[0] else QStyleOptionTab.End if index == row[-1]
                           else QStyleOptionTab.Middle)
        option.state |= QStyle.State_Enabled
        if index == self.tabs.currentIndex():
            option.state |= QStyle.State_Selected
        else:
            option.state &= ~QStyle.State_Selected
        option.text = self.bar.tabText(index)
        return option

    def _button_rect(self, element, option: QStyleOptionTab) -> QRect:
        """Where the style puts a tab's button (``element``: its left or right
        one), for a tab on any row. The macOS style places them from the top
        of the bar whatever the tab's own top (one row's always 0): asked for
        the tab on the first row, then moved down to its own."""
        top = QStyleOptionTab(option)
        top.rect = QRect(option.rect)
        top.rect.moveTop(0)
        return self.style().subElementRect(element, top, self).translated(0, option.rect.top())

    def _close_rect(self, option: QStyleOptionTab) -> QRect:
        area = self._button_rect(QStyle.SE_TabBarTabRightButton, option)
        rect = QRect(0, 0, CLOSE_SIZE + 6, CLOSE_SIZE + 6)
        rect.moveCenter(area.center())
        return rect

    def paintEvent(self, event):
        if len(self._rects) != self.bar.count():
            self._layout(self.width())
        painter = QStylePainter(self)
        italic = QFont(self.font())
        italic.setItalic(True)
        close = make_icon("close", QColor(140, 140, 140)).pixmap(CLOSE_SIZE, CLOSE_SIZE)
        current = self.tabs.currentIndex()
        for index in [i for i in range(len(self._rects)) if i != current] + ([current] if current >= 0 else []):
            option = self._option(index)  # (the current tab last: drawn over its neighbours)
            painter.setClipRect(option.rect)  # (a style may draw the current tab down into the page: not the next row)
            painter.drawControl(QStyle.CE_TabBarTabShape, option)
            name = self.bar.tab_name(index) if hasattr(self.bar, "tab_name") else option.text
            is_italic = hasattr(self.bar, "italic_name") and self.bar.italic_name(index) is not None
            option.text = name
            painter.setFont(italic if is_italic else self.font())
            painter.drawControl(QStyle.CE_TabBarTabLabel, option)
            icon = self.bar.tabButton(index, QTabBar.LeftSide)
            if icon is not None and hasattr(icon, "pixmap") and icon.pixmap() is not None:
                area = self._button_rect(QStyle.SE_TabBarTabLeftButton, option)
                pixmap = icon.pixmap()
                # (Its size as drawn, not in device pixels: on a Retina screen a 16-point icon is 32 pixels.)
                target = QRect(QPoint(0, 0), pixmap.deviceIndependentSize().toSize())
                target.moveCenter(area.center())
                painter.drawPixmap(target, pixmap)
            rect = self._close_rect(option)
            if index == self._hover:
                painter.setRenderHint(QPainter.Antialiasing)
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(128, 128, 128, 80))
                painter.drawRoundedRect(rect, 4, 4)
            painter.drawPixmap(rect.center() - QPoint(CLOSE_SIZE // 2, CLOSE_SIZE // 2), close)
            painter.setClipping(False)

    # ------------------------------------------------------------ the mouse
    def _on_close(self, index: int, pos: QPoint) -> bool:
        return index >= 0 and self._close_rect(self._option(index)).contains(pos)

    def mousePressEvent(self, event):
        index = self.index_at(event.position().toPoint())
        if event.button() != Qt.LeftButton or index < 0:
            super().mousePressEvent(event)
            return
        if self._on_close(index, event.position().toPoint()):
            self._closing = index
            return
        self.tabs.setCurrentIndex(index)
        self._pressed, self._press_pos = index, event.position().toPoint()

    def mouseMoveEvent(self, event):
        pos = event.position().toPoint()
        index = self.index_at(pos)
        hover = index if self._on_close(index, pos) else None
        if hover != self._hover:
            self._hover = hover
            self.update()
        if self._pressed is None or not event.buttons() & Qt.LeftButton:
            return
        if (pos - self._press_pos).manhattanLength() < QApplication.startDragDistance():
            return
        if index >= 0 and index != self._pressed:  # (dragged over another tab: it takes that one's place)
            self.bar.moveTab(self._pressed, index)
            self._pressed = index

    def mouseReleaseEvent(self, event):
        closing, self._closing, self._pressed = self._closing, None, None
        index = self.index_at(event.position().toPoint())
        if closing is not None and index == closing and self._on_close(index, event.position().toPoint()):
            self.close_requested.emit(index)

    def mouseDoubleClickEvent(self, event):
        index = self.index_at(event.position().toPoint())
        if index >= 0 and not self._on_close(index, event.position().toPoint()):
            self.tabs.tabBarDoubleClicked.emit(index)

    def leaveEvent(self, event):
        if self._hover is not None:
            self._hover = None
            self.update()
        super().leaveEvent(event)

    def event(self, event):
        if event.type() == event.Type.ToolTip:
            pos = event.pos()
            index = self.index_at(pos)
            if index < 0:
                QToolTip.hideText()
            elif self._on_close(index, pos):
                button = self.bar.tabButton(index, QTabBar.RightSide)
                QToolTip.showText(event.globalPos(), button.toolTip() if button is not None else "Close", self)
            else:
                QToolTip.showText(event.globalPos(), self.bar.tabToolTip(index), self)
            return True
        return super().event(event)
