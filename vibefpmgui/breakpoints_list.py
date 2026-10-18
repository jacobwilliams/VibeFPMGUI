"""The Debug panel's list of breakpoints: every one in the project, each
with its state drawn as in the editor's gutter (a red dot; hollow: disabled;
grey: the debugger couldn't place it), its place and its line of code.

A click opens the line; the right-click menu enables, disables or removes
one (and, from the Debug menu, all of them); Delete removes the selected
one. Where the program stopped at a breakpoint, it's selected, and the
gutter's yellow arrow is drawn over its dot. The code is in the editor's
font and syntax colors.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from pygments.styles import get_style_by_name
from qtpy.QtCore import QPointF, QRect, QRectF, Qt, Signal
from qtpy.QtGui import (
    QAction,
    QColor,
    QFont,
    QFontMetrics,
    QIcon,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QPolygonF,
)
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHeaderView,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
)

from . import highlighter as hl
from .editor_shared import BREAKPOINT_COLOR, BREAKPOINT_UNPLACED, EXECUTION_COLOR
from .result_tree import syntax_formats

ENABLED, DISABLED, UNPLACED = "enabled", "disabled", "unplaced"
KEY_ROLE = Qt.UserRole  # an item's (path, line)


@dataclass(frozen=True)
class BreakpointEntry:
    path: str      # (absolute: as the breakpoints are kept)
    line: int
    state: str     # ENABLED, DISABLED or UNPLACED (enabled, but the debugger couldn't place it)
    shown: str     # its place as shown: the path in the project, and the line ("src/x.f90:12")
    code: str = ""  # the line's code (as in the file: its indentation not shown)
    why: str = ""   # why it wasn't placed


_icons: dict[tuple[str, bool], QIcon] = {}


def breakpoint_icon(state: str, here: bool = False) -> QIcon:
    """The gutter's dot for a state; ``here``: the program is stopped at it
    (the gutter's yellow arrow over the dot)."""
    if (state, here) not in _icons:
        icon = QIcon()
        for ratio in (1, 2, 3):
            pixmap = QPixmap(12 * ratio, 12 * ratio)
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            if state == DISABLED:
                painter.setPen(QPen(QColor(BREAKPOINT_COLOR), 1.5))
                painter.setBrush(Qt.NoBrush)
            else:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(BREAKPOINT_UNPLACED if state == UNPLACED else BREAKPOINT_COLOR))
            painter.drawEllipse(QRectF(1.5, 1.5, 9, 9))
            if here:  # (as editor_gutter draws it, in 12 pixels)
                painter.setPen(QPen(QColor(EXECUTION_COLOR).darker(140), 1))
                painter.setBrush(QColor(EXECUTION_COLOR))
                painter.drawPolygon(QPolygonF([QPointF(0.5, 1.5), QPointF(5.5, 1.5), QPointF(11.5, 6),
                                               QPointF(5.5, 10.5), QPointF(0.5, 10.5)]))
            painter.end()
            icon.addPixmap(pixmap)
        _icons[(state, here)] = icon
    return _icons[(state, here)]


class _CodeDelegate(QStyledItemDelegate):
    """The code column: the line in the editor's font and syntax colors
    (plain on the selected row: its text color reads best there)."""

    def __init__(self, tree: BreakpointsList):
        super().__init__(tree)
        self.tree = tree

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        entry = self.tree.entry_at(index.row())
        if entry is None:
            super().paint(painter, option, index)
            return
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(opt.state & QStyle.State_Selected)
        group = QPalette.Active if opt.state & QStyle.State_Active else QPalette.Inactive
        plain = opt.palette.color(group, QPalette.HighlightedText if selected else QPalette.Text)
        if entry.state == DISABLED and not selected:
            plain = opt.palette.color(group, QPalette.PlaceholderText)
        code = entry.code.lstrip()
        column = len(entry.code) - len(code)  # (where the code shown starts in the line)
        formats = None if selected or entry.state == DISABLED else self.tree.line_formats(entry)
        rect = opt.rect.adjusted(4, 0, 0, 0)
        x = rect.left()
        painter.save()
        painter.setClipRect(opt.rect)
        start = 0
        for i in range(1, len(code) + 1):  # (a run of text in one color at a time)
            fmt = formats[column + start] if formats and column + start < len(formats) else None
            if i < len(code) and formats and column + i < len(formats) and formats[column + i] is fmt:
                continue
            font = QFont(self.tree.mono)
            if fmt is not None:
                font.setBold(fmt.fontWeight() >= QFont.Bold)
                font.setItalic(fmt.fontItalic())
            brush = fmt.foreground() if fmt is not None else None
            painter.setFont(font)
            painter.setPen(brush.color() if brush is not None and brush.style() != Qt.NoBrush else plain)
            piece = code[start:i]
            width = QFontMetrics(font).horizontalAdvance(piece)
            painter.drawText(QRect(x, rect.top(), width, rect.height()), Qt.AlignVCenter | Qt.AlignLeft, piece)
            x += width
            if x > rect.right():
                break
            start = i
        painter.restore()


def state_text(entry: BreakpointEntry) -> str:
    if entry.state == DISABLED:
        return "Disabled: the debugger doesn't stop here"
    if entry.state == UNPLACED:
        return f"Not placed: {entry.why}" if entry.why else "Not placed by the debugger"
    return "Enabled"


class BreakpointsList(QTreeWidget):
    open_requested = Signal(str, int)       # path, line: a breakpoint clicked
    enabled_toggled = Signal(str, int)      # path, line: enable it if disabled, else disable it
    remove_requested = Signal(str, int)     # path, line

    def __init__(self, more_actions: Iterable[QAction] = (), parent=None):
        """``more_actions``: added to every right-click menu (the Debug menu's
        Disable All and Remove All)."""
        super().__init__(parent)
        self.more_actions = list(more_actions)
        self.entries: list[BreakpointEntry] = []
        self.setHeaderLabels(["Breakpoint", "Code"])
        self.setRootIsDecorated(False)
        self.setUniformRowHeights(True)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)  # (the code: the rest)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu_at)
        self.itemClicked.connect(self._open)
        self.itemActivated.connect(self._open)  # (Enter, a double-click)
        self._hit: tuple[str, int] | None = None  # where the program stopped (selected, the arrow on it)
        self.mono = QFont()  # the code's font: the editor's (set by the window)
        self.lines_for: Callable[[str], list[str]] | None = None  # a file's lines (set by the window)
        self._style = get_style_by_name(hl.style_name(QApplication.palette()))
        self._formats: dict = {}  # token type -> format
        self._line_formats: dict[tuple[str, int, str], list | None] = {}
        self.setItemDelegateForColumn(1, _CodeDelegate(self))

    # ------------------------------------------------------------ contents
    def set_entries(self, entries: list[BreakpointEntry]):
        """The breakpoints now (the selection kept, if its breakpoint still is)."""
        if entries == self.entries:
            return
        current = self.current_key()
        self.entries = list(entries)
        self._line_formats.clear()
        self.clear()
        for entry in entries:
            item = QTreeWidgetItem([entry.shown, entry.code.strip()])
            item.setIcon(0, breakpoint_icon(entry.state, (entry.path, entry.line) == self._hit))
            item.setData(0, KEY_ROLE, (entry.path, entry.line))
            tip = f"{entry.path}:{entry.line}\n{state_text(entry)}"
            item.setToolTip(0, tip)
            item.setToolTip(1, tip)
            if entry.state == DISABLED:
                item.setForeground(0, self.palette().placeholderText())
                item.setForeground(1, self.palette().placeholderText())
            self.addTopLevelItem(item)
        self._select(self._hit or current)

    def entry_at(self, row: int) -> BreakpointEntry | None:
        item = self.topLevelItem(row)
        if item is None:
            return None
        key = tuple(item.data(0, KEY_ROLE))
        return next((e for e in self.entries if (e.path, e.line) == key), None)

    def line_formats(self, entry: BreakpointEntry) -> list | None:
        """The syntax format of each column of a breakpoint's line (None: plain)."""
        key = (entry.path, entry.line, entry.code)
        if key not in self._line_formats:
            lines = self.lines_for(entry.path) if self.lines_for is not None else [entry.code]
            line = entry.line if self.lines_for is not None else 1
            self._line_formats[key] = syntax_formats(lines, line, Path(entry.path), self._style, self._formats)
        return self._line_formats[key]

    def refresh_style(self):
        """The editor's syntax colors (View → Syntax Colors; light or dark)."""
        self._style = get_style_by_name(hl.style_name(QApplication.palette()))
        self._formats.clear()
        self._line_formats.clear()
        self.viewport().update()

    def item_for(self, path: str, line: int) -> QTreeWidgetItem | None:
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if tuple(item.data(0, KEY_ROLE)) == (path, line):
                return item
        return None

    def current_key(self) -> tuple[str, int] | None:
        item = self.currentItem()
        return tuple(item.data(0, KEY_ROLE)) if item is not None and item.isSelected() else None

    def show_hit(self, where: tuple[str, int] | None):
        """The breakpoint the program stopped at, selected (None: it's not
        stopped at one: nothing selected)."""
        for entry in self.entries:  # (the arrow: off the one it was on, onto this one)
            if (entry.path, entry.line) in (self._hit, where):
                self.item_for(entry.path, entry.line).setIcon(
                    0, breakpoint_icon(entry.state, (entry.path, entry.line) == where))
        self._hit = where
        self._select(where)

    def _select(self, key: tuple[str, int] | None):
        item = self.item_for(*key) if key else None
        if item is None:
            self.clearSelection()
            self.setCurrentItem(None)
            return
        self.setCurrentItem(item)
        item.setSelected(True)
        self.scrollToItem(item)

    # ------------------------------------------------------------ using it
    def _open(self, item: QTreeWidgetItem, _column: int = 0):
        path, line = item.data(0, KEY_ROLE)
        self.open_requested.emit(path, line)

    def menu_for(self, item: QTreeWidgetItem | None) -> QMenu:
        menu = QMenu(self)
        if item is not None:
            path, line = item.data(0, KEY_ROLE)
            entry = next(e for e in self.entries if (e.path, e.line) == (path, line))
            menu.addAction("Go to Line", lambda: self.open_requested.emit(path, line))
            menu.addAction("Enable Breakpoint" if entry.state == DISABLED else "Disable Breakpoint",
                           lambda: self.enabled_toggled.emit(path, line))
            menu.addAction("Remove Breakpoint", lambda: self.remove_requested.emit(path, line))
            if self.more_actions:
                menu.addSeparator()
        menu.addActions(self.more_actions)
        return menu

    def _menu_at(self, pos):
        item = self.itemAt(pos)
        if item is None and not self.entries:
            return
        menu = self.menu_for(item)
        menu.exec(self.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def keyPressEvent(self, event):
        key = self.current_key()
        if event.key() in (Qt.Key_Delete, Qt.Key_Backspace) and key is not None:
            self.remove_requested.emit(*key)
            return
        super().keyPressEvent(event)
