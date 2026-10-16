"""The *Complexity* tab: each procedure's complexity, as lizard measures it
(see `vibefpmgui.complexity`): CCN, lines of code, tokens, parameters,
length and nesting depth, the values over lizard's thresholds in red; and
each file's totals. Click a procedure to open it; click a file to list only
its procedures.
"""

from __future__ import annotations

import html
from pathlib import Path

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QBrush, QColor, QFont
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import complexity as cx
from .build_monitor import PALETTES, TitleIcon, app_is_light, panel_stylesheet
from .coverage_panel import NumberItem
from .icons import symbol_icon
from .widgets import StatusLabel

OVER = "#e03131"  # a value over its threshold
PROCEDURE_COLUMNS = ("FILE", "PROCEDURE", "CCN", "NLOC", "TOKENS", "PARAMS", "LENGTH", "DEPTH")
FILE_COLUMN, NAME_COLUMN = 0, 1  # (in the procedures' table: the file's column stretches, and shrinks)
MIN_FILE_WIDTH = 60
FILE_COLUMNS = ("FILE", "NLOC", "PROCEDURES", "AVG CCN", "MAX CCN", "OVER")
COLUMN_TIPS = {
    "CCN": "Cyclomatic complexity: 1 + its decisions (if, else if, loops, case, type is, where, .and., .or.)",
    "NLOC": "Lines of code (not blank, not only a comment)",
    "TOKENS": "Tokens in its code",
    "PARAMS": "Dummy arguments",
    "LENGTH": "Lines, first to last (with comments and its internal procedures)",
    "DEPTH": "How deeply its block constructs (do, if…then, select…) nest",
    "OVER": "Procedures with a value over its threshold",
}
METRIC_OF_COLUMN = {"CCN": "ccn", "PARAMS": "params", "LENGTH": "length", "DEPTH": "depth"}


class ComplexityPanel(QWidget):
    """The procedures (top), the files (bottom), and their totals."""

    open_requested = Signal(str, int)  # file, line
    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: list[cx.FileMetrics] = []
        self.project_dir: Path | None = None
        self.test_dirs: list[Path] = []
        self.dependency_dirs: list[Path] = []
        self.thresholds = dict(cx.THRESHOLDS)
        self.description = ""  # (when it was measured)
        self.icon_for = None  # a file's QIcon, as in Project files (set by the window)
        self.is_light = True
        self.palette_colors = PALETTES["light"]
        self.setObjectName("ComplexityPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.title_icon = TitleIcon("complexity")
        self.title = QLabel("Complexity")
        self.subtitle = StatusLabel()
        self.tests_check = QCheckBox("Include tests")
        self.tests_check.setToolTip("Also measure the test programs")
        self.tests_check.toggled.connect(self._fill)
        self.dependencies_check = QCheckBox("Include dependencies")
        self.dependencies_check.setToolTip("Also measure the dependencies' code")
        self.dependencies_check.toggled.connect(self.refresh_requested)
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_icon)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle, 1)
        header.addWidget(self.tests_check)
        header.addSpacing(12)
        header.addWidget(self.dependencies_check)

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter by procedure or file")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._fill_procedures)
        self.totals = StatusLabel()
        totals_row = QHBoxLayout()
        totals_row.setSpacing(10)
        totals_row.addWidget(self.filter_edit, 1)
        totals_row.addWidget(self.totals)

        self.procedures = self._table(PROCEDURE_COLUMNS, stretch=-1)
        self.procedures.setTextElideMode(Qt.ElideLeft)  # (a path, shrunk: the end of its file name still shows)
        # The file's column takes what the others leave, and gives it up first
        # when the panel narrows (Qt's Stretch never goes below its text's width).
        self.procedures.horizontalHeader().setSectionResizeMode(FILE_COLUMN, QHeaderView.Interactive)
        self.procedures.viewport().installEventFilter(self)  # (the viewport: it has its new width by then)
        self.procedures.horizontalHeader().setSortIndicator(2, Qt.DescendingOrder)  # (the most complex first)
        self.procedures.cellClicked.connect(self._procedure_clicked)
        self.file_table = self._table(FILE_COLUMNS, stretch=0)
        self.file_table.horizontalHeader().setSortIndicator(3, Qt.DescendingOrder)
        # A file chosen (clicked, or with the arrow keys): only its procedures;
        # the chosen file clicked again: all of them.
        self.file_table.currentCellChanged.connect(self._file_chosen)
        self.file_table.cellClicked.connect(self._file_clicked)
        self._file_just_chosen = False
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.procedures)
        self.splitter.addWidget(self.file_table)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 1)

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(totals_row)
        layout.addWidget(self.splitter, 1)
        layout.addWidget(self.empty, 1)
        self.refresh_fonts()
        self.show_message("Open a project to see the complexity of its Fortran code here.")

    def _table(self, columns: tuple[str, ...], stretch: int) -> QTableWidget:
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(list(columns))
        for i, name in enumerate(columns):
            if name in COLUMN_TIPS:
                table.horizontalHeaderItem(i).setToolTip(COLUMN_TIPS[name])
        table.verticalHeader().setVisible(False)
        table.setShowGrid(False)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setSortingEnabled(True)
        table.setWordWrap(False)
        hh = table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        for i in range(len(columns)):
            hh.setSectionResizeMode(i, QHeaderView.Stretch if i == stretch else QHeaderView.ResizeToContents)
        return table

    def eventFilter(self, obj, event):
        if obj is self.procedures.viewport() and event.type() == event.Type.Resize:
            self._fit_file_column()
        return False

    def _fit_file_column(self):
        """The file's column: the width the other columns leave (at least a little)."""
        header = self.procedures.horizontalHeader()
        others = sum(header.sectionSize(i) for i in range(header.count()) if i != FILE_COLUMN)
        header.resizeSection(FILE_COLUMN, max(MIN_FILE_WIDTH, self.procedures.viewport().width() - others))

    # ------------------------------------------------------------ styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal))
        self.title_icon.draw(pal["text"], self.title)
        for label in (self.subtitle, self.empty, self.totals):
            label.setStyleSheet(f"color: {pal['muted']};")
        self._fill()

    def refresh_fonts(self):
        font = QFont(QApplication.font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.Bold)
        self.title.setFont(font)
        self.refresh_palette()

    # -------------------------------------------------------------- state
    def show_message(self, message: str):
        """Nothing to list: why, or what's happening."""
        self.empty.setText(message)
        self.empty.show()
        self.splitter.hide()
        self.filter_edit.setEnabled(False)
        self.totals.setText("")

    def show_metrics(self, files: list[cx.FileMetrics], project_dir: Path, test_dirs: list[Path] | None = None,
                     dependency_dirs: list[Path] | None = None, description: str = ""):
        """Every measured file's metrics (the project's tests and the
        dependencies' are listed only with their boxes ticked)."""
        self.files = files
        self.project_dir = project_dir.resolve()
        self.test_dirs = [Path(d).resolve() for d in test_dirs or []]
        self.dependency_dirs = [Path(d).resolve() for d in dependency_dirs or []]
        self.description = description
        self._fill()

    def shown_files(self) -> list[cx.FileMetrics]:
        """The files listed: the project's own, with its tests if ticked
        (the dependencies' are only measured when their box is ticked)."""
        def is_test(path: Path) -> bool:
            return any(path.is_relative_to(d) for d in self.test_dirs)
        return [f for f in self.files if self.tests_check.isChecked() or not is_test(f.path.resolve())]

    def label(self, path: Path) -> str:
        """A file's name as listed: its path in the project (or in its dependency)."""
        path = path.resolve()
        for top in (*self.dependency_dirs, self.project_dir):
            if top is not None and path.is_relative_to(top):
                relative = path.relative_to(top).as_posix()
                return relative if top == self.project_dir else f"{top.name}/{relative}"
        return str(path)

    def _fill(self):
        files = self.shown_files()
        if not self.files:
            if self.project_dir is not None:
                self.show_message("No Fortran files in this project.")
            return
        self.empty.hide()
        self.splitter.show()
        self.filter_edit.setEnabled(True)
        self.subtitle.setText(" · ".join(part for part in (self.description, f"{len(files)} files") if part))
        procedures = [p for f in files for p in f.procedures]
        over = [p for p in procedures if p.warnings(self.thresholds)]
        nloc = sum(f.nloc for f in files)
        average_ccn = sum(p.ccn for p in procedures) / len(procedures) if procedures else 0.0
        average_nloc = sum(p.nloc for p in procedures) / len(procedures) if procedures else 0.0
        self.totals.setText(f"{nloc:,} lines of code · {len(procedures):,} procedures · average CCN "
                            f"{average_ccn:.1f}, NLOC {average_nloc:.0f} · "
                            + (f"<span style='color:{OVER}'>{len(over)} over a threshold</span>" if over
                               else "none over a threshold"))
        self.totals.setToolTip("Thresholds (lizard's): " + ", ".join(
            f"{key.upper() if key == 'ccn' else key} {value}" for key, value in self.thresholds.items()))
        self._fill_files(files)
        self._fill_procedures()

    def _fill_procedures(self):
        needle = self.filter_edit.text().strip().lower()
        rows = []
        for f in self.shown_files():
            label = self.label(f.path)
            for proc in f.procedures:
                if needle and needle not in proc.qualified.lower() and needle not in label.lower():
                    continue
                rows.append((proc, label))
        table = self.procedures
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        muted = QColor(self.palette_colors["muted"])
        for row, (proc, label) in enumerate(rows):
            over = set(proc.warnings(self.thresholds))
            name = QTableWidgetItem(proc.name)
            name.setIcon(symbol_icon(proc.kind))
            file_cell = QTableWidgetItem(label)
            file_cell.setForeground(QBrush(muted))
            for cell in (file_cell, name):
                cell.setData(Qt.UserRole, (str(proc.path), proc.start))
            cells = [file_cell, name]
            for column in PROCEDURE_COLUMNS[2:]:
                value = {"CCN": proc.ccn, "NLOC": proc.nloc, "TOKENS": proc.tokens, "PARAMS": proc.params,
                         "LENGTH": proc.length, "DEPTH": proc.depth}[column]
                cell = NumberItem(f"{value:,}", value)
                if METRIC_OF_COLUMN.get(column) in over:
                    cell.setForeground(QBrush(QColor(OVER)))
                    bold = QFont(table.font())
                    bold.setWeight(QFont.Bold)
                    cell.setFont(bold)
                cells.append(cell)
            tip = self._procedure_tip(proc, label, over)
            for column, cell in enumerate(cells):
                cell.setToolTip(tip)
                table.setItem(row, column, cell)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        self._fit_file_column()

    def _procedure_tip(self, proc: cx.Procedure, label: str, over: set[str]) -> str:
        lines = [f"<b>{html.escape(proc.qualified)}</b> ({proc.kind})",
                 f"{html.escape(label)}, lines {proc.start}–{proc.end}"]
        if over:
            names = {"ccn": "CCN", "length": "length", "params": "parameters", "depth": "depth"}
            over_text = ", ".join(f"{names[key]} (over {self.thresholds[key]})" for key in sorted(over))
            lines.append(f"<span style='color:{OVER}'>Over: {over_text}</span>")
        return "<br>".join(lines)

    def _fill_files(self, files: list[cx.FileMetrics]):
        table = self.file_table
        table.blockSignals(True)  # (refilling isn't choosing a file)
        table.setSortingEnabled(False)
        table.setRowCount(len(files))
        for row, f in enumerate(files):
            label = self.label(f.path)
            name = QTableWidgetItem(label)
            if self.icon_for is not None:
                name.setIcon(self.icon_for(f.path))
            name.setData(Qt.UserRole, label)
            over = sum(1 for p in f.procedures if p.warnings(self.thresholds))
            highest = max((p.ccn for p in f.procedures), default=0)
            cells = [name, NumberItem(f"{f.nloc:,}", f.nloc), NumberItem(str(len(f.procedures)), len(f.procedures)),
                     NumberItem(f"{f.average_ccn:.1f}", f.average_ccn), NumberItem(str(highest), highest),
                     NumberItem(str(over), over)]
            if over:
                cells[-1].setForeground(QBrush(QColor(OVER)))
            for column, cell in enumerate(cells):
                cell.setToolTip(f"{html.escape(str(f.path))}<br>Click to list only its procedures")
                table.setItem(row, column, cell)
        table.setSortingEnabled(True)
        table.blockSignals(False)

    def _procedure_clicked(self, row: int, _column: int):
        item = self.procedures.item(row, NAME_COLUMN)
        if item is not None:
            path, line = item.data(Qt.UserRole)
            self.open_requested.emit(path, line)

    def _file_chosen(self, row: int, _column: int, previous_row: int, _previous_column: int):
        """The current file changed (a click, or an arrow key): only its procedures."""
        item = self.file_table.item(row, 0) if row >= 0 else None
        if item is None or row == previous_row:
            return
        # (The click that chose it doesn't also unchoose it; an arrow key isn't a click.)
        self._file_just_chosen = bool(QApplication.mouseButtons() & Qt.LeftButton)
        self.filter_edit.setText(item.data(Qt.UserRole))

    def _file_clicked(self, row: int, _column: int):
        """A click on the file already chosen: all the procedures again."""
        if self._file_just_chosen:
            self._file_just_chosen = False
            return
        item = self.file_table.item(row, 0)
        if item is not None and self.filter_edit.text() == item.data(Qt.UserRole):
            self.filter_edit.clear()
            self.file_table.blockSignals(True)
            self.file_table.setCurrentCell(-1, -1)
            self.file_table.blockSignals(False)
