"""The *Complexity* tab: each procedure's complexity, as lizard measures it
(see `vibefpmgui.complexity`): CCN, lines of code, tokens, parameters,
length and nesting depth, the values over lizard's thresholds in red; each
file's totals; and the selected procedure's control-flow graph (see
`vibefpmgui.control_flow`, `vibefpmgui.cfg_view`). Click a procedure to open
it; click a file to list only its procedures (its first one selected and
drawn); click a node of the graph to open its lines.
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
from . import control_flow as cf
from .build_monitor import PALETTES, TitleIcon, app_is_light, panel_stylesheet
from .cfg_view import CfgView
from .coverage_panel import NumberItem
from .icons import symbol_icon
from .source_text import read_source
from .widgets import StatusLabel

OVER = "#e03131"  # a value over its threshold
PROCEDURE_COLUMNS = ("FILE", "PROCEDURE", "CCN", "NLOC", "TOKENS", "PARAMS", "LENGTH", "DEPTH")
FILE_COLUMN, NAME_COLUMN = 0, 1  # (in the procedures' table)
MAX_TEXT_WIDTH = 320  # a file's or procedure's column at most (wider: drag it; a long path elided at its start)
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
PROCEDURE_ROLE = Qt.UserRole + 1  # a procedure's row: its complexity.Procedure


class ComplexityPanel(QWidget):
    """The files (top), their procedures (below), their totals, and the
    selected procedure's control-flow graph (beside them)."""

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
        self.filter_edit.setMinimumWidth(160)  # (narrow: the totals beside it are cut off, not the filter)
        self.filter_edit.textChanged.connect(self._fill_procedures)
        self.totals = StatusLabel()
        totals_row = QHBoxLayout()
        totals_row.setSpacing(10)
        totals_row.addWidget(self.filter_edit, 1)
        totals_row.addWidget(self.totals)

        self.procedures = self._table(PROCEDURE_COLUMNS)
        self.procedures.horizontalHeader().setSortIndicator(2, Qt.DescendingOrder)  # (the most complex first)
        self.procedures.cellClicked.connect(self._procedure_clicked)
        self.file_table = self._table(FILE_COLUMNS)
        self.file_table.horizontalHeader().setSortIndicator(3, Qt.DescendingOrder)
        # A file chosen (clicked, or with the arrow keys): only its procedures;
        # the chosen file clicked again: all of them.
        self.file_table.currentCellChanged.connect(self._file_chosen)
        self.file_table.cellClicked.connect(self._file_clicked)
        self._file_just_chosen = False
        self.splitter = QSplitter(Qt.Vertical)  # (the files above their procedures)
        self.splitter.addWidget(self.file_table)
        self.splitter.addWidget(self.procedures)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)
        # The selected procedure's control flow, beside the tables.
        self.flow_title = StatusLabel()
        self.flow = CfgView()
        self.flow.line_clicked.connect(self._flow_line_clicked)
        self.flow_shown: cx.Procedure | None = None
        flow_box = QWidget()
        flow_layout = QVBoxLayout(flow_box)
        flow_layout.setContentsMargins(0, 0, 0, 0)
        flow_layout.setSpacing(4)
        flow_layout.addWidget(self.flow_title)
        flow_layout.addWidget(self.flow, 1)
        self.body = QSplitter(Qt.Horizontal)
        self.body.addWidget(self.splitter)
        self.body.addWidget(flow_box)
        self.body.setStretchFactor(0, 3)
        self.body.setStretchFactor(1, 2)
        self.procedures.currentCellChanged.connect(lambda row, *_: self._show_flow(row))

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(totals_row)
        layout.addWidget(self.body, 1)
        layout.addWidget(self.empty, 1)
        self.refresh_fonts()
        self.show_message("Open a project to see the complexity of its Fortran code here.")

    def _table(self, columns: tuple[str, ...]) -> QTableWidget:
        """A table whose columns are as wide as their contents, scrolling
        sideways if the panel is narrower (not squeezing the first column)."""
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
        table.setTextElideMode(Qt.ElideLeft)  # (a long path: the end of its file name still shows)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        hh = table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(QHeaderView.Interactive)
        hh.setStretchLastSection(False)
        hh.sortIndicatorChanged.connect(lambda *_: self._fit_columns(table))
        return table

    @staticmethod
    def _fit_columns(table: QTableWidget):
        """Each column as wide as its values and its title, room for the sort
        arrow only in the sorted one (Qt leaves room for it in every title)."""
        header = table.horizontalHeader()
        header.setSortIndicatorShown(False)
        table.resizeColumnsToContents()
        header.setSortIndicatorShown(True)
        column = header.sortIndicatorSection()
        if 0 <= column < header.count():
            header.resizeSection(column, max(header.sectionSize(column), header.sectionSizeHint(column)))
        for column in range(header.count()):
            if header.sectionSize(column) > MAX_TEXT_WIDTH:
                header.resizeSection(column, MAX_TEXT_WIDTH)

    # ------------------------------------------------------------ styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal)
                           + f"#{self.objectName()} QHeaderView::section {{ padding: 6px 8px; }}")
        self.title_icon.draw(pal["text"], self.title)
        for label in (self.subtitle, self.empty, self.totals, self.flow_title):
            label.setStyleSheet(f"color: {pal['muted']};")
        self.flow.set_dark(not self.is_light)
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
        self.body.hide()
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
        self.body.show()
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

    def chosen_file(self) -> str | None:
        """The file chosen in the files' table (its label), if the filter is
        still that file's."""
        row = self.file_table.currentRow()
        item = self.file_table.item(row, 0) if row >= 0 else None
        label = item.data(Qt.UserRole) if item is not None else None
        return label if label and label == self.filter_edit.text() else None

    def _fill_procedures(self):
        needle = self.filter_edit.text().strip().lower()
        chosen = self.chosen_file()
        rows = []
        for f in self.shown_files():
            label = self.label(f.path)
            for proc in f.procedures:
                if chosen is not None:
                    if label != chosen:  # (only that file's: not another path it's a part of)
                        continue
                elif needle and needle not in proc.qualified.lower() and needle not in label.lower():
                    continue
                rows.append((proc, label))
        self.procedures.setColumnHidden(FILE_COLUMN, chosen is not None)  # (one file: no need to say which)
        table = self.procedures
        table.blockSignals(True)  # (refilling isn't choosing a procedure: see the end)
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
                cell.setData(PROCEDURE_ROLE, proc)
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
        self._fit_columns(table)
        table.blockSignals(False)
        self._keep_flow()

    # ------------------------------------------------------------ control flow
    def set_code_font(self, font: QFont):
        """The graph's font (the editor's)."""
        self.flow.set_code_font(font)

    def _row_of(self, path: Path, qualified: str) -> int | None:
        for row in range(self.procedures.rowCount()):
            proc = self.procedures.item(row, NAME_COLUMN).data(PROCEDURE_ROLE)
            if proc.path == path and proc.qualified == qualified:
                return row
        return None

    def _keep_flow(self):
        """After the tables are filled again (a save, a filter, a file chosen):
        the procedure shown stays selected, its graph drawn again from its file
        (it may have changed); filtered out, or a file just chosen, the first
        procedure listed is selected and shown instead; gone from the project,
        the graph goes too."""
        shown = self.flow_shown
        row = self._row_of(shown.path, shown.qualified) if shown is not None else None
        if row is None and self.procedures.rowCount() and (shown is not None or self.chosen_file()):
            row = 0  # (filtered out, or a file chosen: its most complex procedure)
        if row is None and shown is None:
            return
        if row is None:
            if not any(p.path == shown.path and p.qualified == shown.qualified
                       for f in self.files for p in f.procedures):
                self.flow_shown = None
                self.flow_title.setText("")
                self.flow.show_message("Select a procedure to see its control flow.")
            return
        self.procedures.blockSignals(True)
        self.procedures.setCurrentCell(row, NAME_COLUMN)
        self.procedures.blockSignals(False)
        self._show_flow(row, again=True)

    def _show_flow(self, row: int, again: bool = False):
        """The control-flow graph of the procedure in ``row``."""
        item = self.procedures.item(row, NAME_COLUMN) if row >= 0 else None
        proc = item.data(PROCEDURE_ROLE) if item is not None else None
        if proc is None or (not again and proc is self.flow_shown):
            return
        self.flow_shown = proc
        try:
            text = read_source(proc.path).text
        except OSError as error:
            self.flow.show_message(f"Couldn't read {proc.path.name}: {error}")
            return
        graph = cf.build(text, proc.path, proc)
        decisions = graph.complexity - 1
        self.flow_title.setText(f"<b>{html.escape(proc.name)}</b> · lines {proc.start}–{proc.end} · "
                                f"{decisions} decision{'s' * (decisions != 1)} · click a node to open it, "
                                "right-click for more")
        self.flow.show_graph(graph, text)

    def _flow_line_clicked(self, line: int):
        if self.flow_shown is not None:
            self.open_requested.emit(str(self.flow_shown.path), line)

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
        chosen = self.chosen_file()
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
        self._fit_columns(table)
        table.setCurrentCell(-1, -1)
        for row in range(table.rowCount()):  # (still chosen: a save refills the tables)
            if table.item(row, 0).data(Qt.UserRole) == chosen:
                table.setCurrentCell(row, 0)
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
