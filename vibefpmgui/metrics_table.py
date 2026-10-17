"""A sortable table of rows of objects, drawn on demand (Qt asks for the
cells on view, not all of them): the Complexity tab's procedures and files.

A `QTableWidget` makes an item for each cell: a big project's 9000
procedures were 90 000 items, made again at each refill (more than half a
second). Here a row is an object (a procedure, a file) and each column says
how to show it; tooltips are made when asked for.

It has the few `QTableWidget` calls the panel uses (`item`, `rowCount`,
`currentRow`, `setCurrentCell`, `cellClicked`, `currentCellChanged`…).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NamedTuple

from qtpy.QtCore import QAbstractTableModel, QModelIndex, QRect, Qt, Signal
from qtpy.QtGui import QBrush, QColor
from qtpy.QtWidgets import QTableView


def _role(role) -> int:
    return int(getattr(role, "value", role))


# The roles as plain numbers: data() is asked for every cell and role, and
# comparing a number with one of Qt's enums is ~100 times slower (a Python Enum).
DISPLAY, FOREGROUND, FONT, DECORATION, TOOLTIP, USER = (
    _role(r) for r in (Qt.DisplayRole, Qt.ForegroundRole, Qt.FontRole, Qt.DecorationRole, Qt.ToolTipRole,
                       Qt.UserRole))
SAMPLED_ROWS = 200  # rows looked at to size a column to its contents (Qt's default: 1000)


class Column(NamedTuple):
    title: str
    text: Callable[[Any], str]            # a row's text in it
    value: Callable[[Any], Any] | None = None  # what it's sorted by (None: its text)
    tip: str = ""                         # the title's tooltip


class RowStyle(NamedTuple):
    """How rows look and what they hold, apart from their text."""
    color: Callable[[Any, int], QColor | None] = lambda row, column: None
    bold: Callable[[Any, int], bool] = lambda row, column: False
    icon: Callable[[Any], Any] | None = None   # the first column's icon (see icon_column)
    tip: Callable[[Any], str] | None = None
    data: Callable[[Any, int], Any] | None = None  # (row, role) -> a value for that role, or None
    icon_column: int = 0


class RowModel(QAbstractTableModel):
    def __init__(self, columns: list[Column], style: RowStyle, parent=None):
        super().__init__(parent)
        self.columns = columns
        self.style = style
        self.rows: list = []
        self._sorted: tuple[int, Qt.SortOrder] | None = None
        self.bold_font = None  # (set by the table)

    def set_rows(self, rows: list):
        self.beginResetModel()
        self.rows = list(rows)
        if self._sorted is not None:
            self._sort_rows(*self._sorted)
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: B008 - Qt's signature
        return 0 if parent.isValid() else len(self.rows)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008
        return 0 if parent.isValid() else len(self.columns)

    def headerData(self, section, orientation, role=DISPLAY):
        role = _role(role)
        if orientation != Qt.Horizontal or not 0 <= section < len(self.columns):
            return None
        if role == DISPLAY:
            return self.columns[section].title
        if role == TOOLTIP:
            return self.columns[section].tip or None
        return None

    def data(self, index, role=DISPLAY):
        role = _role(role)
        if role not in (DISPLAY, FOREGROUND, FONT, DECORATION, TOOLTIP) and role < USER:
            return None  # (alignment, size hints…: Qt's defaults)
        if not index.isValid():
            return None
        row, column = self.rows[index.row()], index.column()
        style = self.style
        if role == DISPLAY:
            return self.columns[column].text(row)
        if role == FOREGROUND:
            color = style.color(row, column)
            return QBrush(color) if color is not None else None
        if role == FONT:
            return self.bold_font if style.bold(row, column) else None
        if role == DECORATION:
            return style.icon(row) if style.icon is not None and column == style.icon_column else None
        if role == TOOLTIP:
            return style.tip(row) if style.tip is not None else None
        if style.data is not None:
            return style.data(row, role)
        return None

    def sort(self, column, order=Qt.AscendingOrder):
        self._sorted = (column, order)
        self.layoutAboutToBeChanged.emit()
        old = self.persistentIndexList()  # (the current row, the selection: they follow their rows)
        old_rows = [self.rows[i.row()] for i in old]
        self._sort_rows(column, order)
        place = {id(row): n for n, row in enumerate(self.rows)}
        self.changePersistentIndexList(old, [self.index(place[id(row)], i.column()) for row, i in zip(old_rows, old,
                                                                                                    strict=True)])
        self.layoutChanged.emit()

    def _sort_rows(self, column: int, order):
        spec = self.columns[column]
        key = spec.value or spec.text
        self.rows.sort(key=key, reverse=order == Qt.DescendingOrder)  # (stable: ties keep their order)


class Cell:
    """A cell, as `QTableWidget.item` gives it (what the panel and its tests ask of one)."""

    def __init__(self, model: RowModel, index):
        self.model, self.index = model, index

    def text(self) -> str:
        return self.model.data(self.index, DISPLAY) or ""

    def data(self, role):
        return self.model.data(self.index, role)

    def foreground(self) -> QBrush:
        return self.model.data(self.index, FOREGROUND) or QBrush()

    def toolTip(self) -> str:
        return self.model.data(self.index, TOOLTIP) or ""


class _Title:
    def __init__(self, text: str):
        self._text = text

    def text(self) -> str:
        return self._text


class MetricsTable(QTableView):
    """A `QTableView` of a `RowModel`, with `QTableWidget`'s cell signals."""

    cellClicked = Signal(int, int)
    currentCellChanged = Signal(int, int, int, int)  # row, column, previous row, previous column

    def __init__(self, columns: list[Column], style: RowStyle, parent=None):
        super().__init__(parent)
        self.row_model = RowModel(columns, style, self)
        self.setModel(self.row_model)
        self.verticalHeader().setResizeContentsPrecision(SAMPLED_ROWS)
        self.horizontalHeader().setResizeContentsPrecision(SAMPLED_ROWS)
        font = self.font()
        font.setBold(True)
        self.row_model.bold_font = font
        self.clicked.connect(lambda index: self.cellClicked.emit(index.row(), index.column()))
        self.selectionModel().currentChanged.connect(
            lambda now, before: self.currentCellChanged.emit(now.row(), now.column(), before.row(), before.column()))

    def set_rows(self, rows: list):
        self.row_model.set_rows(rows)

    def rows(self) -> list:
        """The rows, in the order shown."""
        return self.row_model.rows

    def rowCount(self) -> int:
        return len(self.row_model.rows)

    def item(self, row: int, column: int) -> Cell | None:
        index = self.row_model.index(row, column)
        return Cell(self.row_model, index) if index.isValid() else None

    def currentRow(self) -> int:
        return self.currentIndex().row()

    def setCurrentCell(self, row: int, column: int):
        self.setCurrentIndex(self.row_model.index(row, column) if row >= 0 else QModelIndex())

    def horizontalHeaderItem(self, column: int) -> _Title:
        return _Title(self.row_model.columns[column].title)

    def visualItemRect(self, cell: Cell) -> QRect:
        return self.visualRect(cell.index)
