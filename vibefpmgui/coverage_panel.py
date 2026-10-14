"""The *Coverage* tab: how much of the code the last test run ran (with
Build options → Coverage on): the totals for lines, functions and branches,
each file's numbers, and the selected file's missed lines and functions.
Clicking a file opens it at its first missed line; the editor's gutter
shows which lines ran (see `vibefpmgui.coverage`)."""

from __future__ import annotations

import html
import re
from pathlib import Path

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QColor, QFont
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import coverage as cov
from .build_monitor import (
    PALETTES,
    ProgressLine,
    TitleIcon,
    app_is_light,
    panel_stylesheet,
)
from .icons import symbol_icon
from .widgets import StatusLabel

GOOD, FAIR, POOR = "#2f9e44", "#f08c00", "#e03131"
MANGLED = re.compile(r"^_*(\w+?)_MOD_(\w+)$")  # gfortran: __module_MOD_procedure


def function_name(name: str) -> str:
    """gfortran's name for a procedure, as written: ``add (module calc)``."""
    found = MANGLED.match(name)
    if found:
        return f"{found.group(2)} (module {found.group(1)})"
    if name == "MAIN__":
        return "(main program)"
    return name


def shown_functions(file: cov.FileCoverage) -> list[cov.FunctionCoverage]:
    """A file's functions without the ``main`` gfortran adds to call a
    program (it has no line of its own)."""
    has_program = any(f.name == "MAIN__" for f in file.functions)
    return [f for f in file.functions if not (has_program and f.name == "main")]


def row_tooltip(f: cov.FileCoverage) -> str:
    """A file's row's tooltip: its path, and every range of lines the tests
    missed (wrapped: a tooltip of one very long line runs off the screen)."""
    ranges = f.missed()
    if not ranges:
        missed = "Every line with code ran."
    else:
        lines = sum(1 for n in f.lines.values() if n == 0)
        missed = (f"<b>Missed lines</b> ({lines:,} in {len(ranges):,} range{'s' * (len(ranges) != 1)}):<br>"
                  f"{html.escape(cov.ranges_text(ranges, limit=len(ranges)))}")
    return f"<p style='white-space:normal'>{html.escape(str(f.path))}<br><br>{missed}</p>"


def color_for(percent: float) -> str:
    return GOOD if percent >= 80 else FAIR if percent >= 50 else POOR


class NumberItem(QTableWidgetItem):
    """A cell shown as text, sorted by a number."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self.value = value

    def __lt__(self, other):
        return self.value < getattr(other, "value", 0)


class CoveragePanel(QWidget):
    """Totals, a table of files, and the selected file's details."""

    open_requested = Signal(str, int)  # file, line

    COLUMNS = ("FILE", "COVERAGE", "LINES", "FUNCTIONS")

    def __init__(self, parent=None):
        super().__init__(parent)
        self.files: dict[Path, cov.FileCoverage] = {}
        self.project_dir: Path | None = None
        self.test_dirs: list[Path] = []  # the project's test folders (fpm.toml's, test/ by default)
        self.icon_for = None  # a file's QIcon, as in Project files (set by the window)
        self.is_light = True
        self.palette_colors = PALETTES["light"]
        self.setObjectName("CoveragePanel")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.title_icon = TitleIcon("coverage")
        self.title = QLabel("Coverage")
        self.subtitle = StatusLabel()
        self.dependencies_check = QCheckBox("Include dependencies")
        self.dependencies_check.setToolTip("Also list the dependencies' files (and count them in the totals)")
        self.dependencies_check.toggled.connect(self._fill)
        self.tests_check = QCheckBox("Include tests")
        self.tests_check.setToolTip("Also list the test programs (usually only the code they test matters)")
        self.tests_check.toggled.connect(self._fill)
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_icon)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle, 1)
        header.addWidget(self.tests_check)
        header.addSpacing(12)
        header.addWidget(self.dependencies_check)

        self.progress = ProgressLine(self)
        self.totals = StatusLabel()
        totals_row = QHBoxLayout()
        totals_row.setSpacing(10)
        totals_row.addWidget(self.progress, 1)
        totals_row.addWidget(self.totals)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(list(self.COLUMNS))
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        hh = self.table.horizontalHeader()
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            hh.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSortIndicator(0, Qt.AscendingOrder)  # (by file, at first)
        self.table.itemSelectionChanged.connect(self._show_details)
        self.table.cellClicked.connect(self._row_clicked)

        self.detail_title = QLabel()
        self.functions = QTableWidget(0, 3)
        self.functions.setHorizontalHeaderLabels(["FUNCTION", "LINE", "CALLS"])
        self.functions.verticalHeader().setVisible(False)
        self.functions.setShowGrid(False)
        self.functions.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.functions.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.functions.setSortingEnabled(True)
        fh = self.functions.horizontalHeader()
        fh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        fh.setSectionResizeMode(0, QHeaderView.Stretch)
        fh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        fh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.functions.cellClicked.connect(self._function_clicked)
        details = QWidget()
        detail_layout = QVBoxLayout(details)
        detail_layout.setContentsMargins(0, 4, 0, 0)
        detail_layout.setSpacing(4)
        detail_layout.addWidget(self.detail_title)
        detail_layout.addWidget(self.functions, 1)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(details)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

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
        self.show_message("Turn on Build options → Coverage (gfortran) and run Test: which lines the tests "
                          "ran shows here, and in the editor's gutter.")

    # ------------------------------------------------------------ styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal))
        self.title_icon.draw(pal["text"], self.title)
        for label in (self.subtitle, self.empty, self.detail_title):
            label.setStyleSheet(f"color: {pal['muted']};")
        self.progress.update()
        self._fill()

    def refresh_fonts(self):
        font = QFont(QApplication.font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.Bold)
        self.title.setFont(font)
        self.refresh_palette()

    # -------------------------------------------------------------- state
    def show_message(self, message: str):
        """No numbers to show (yet): why, or what's happening."""
        self.empty.setText(message)
        self.empty.show()
        self.splitter.hide()

    def show_report(self, files: dict[Path, cov.FileCoverage], project_dir: Path, description: str = "",
                    test_dirs: list[Path] | None = None):
        self.files = files
        self.project_dir = project_dir.resolve()
        self.test_dirs = [Path(d).resolve() for d in test_dirs or []]
        self.subtitle.setText(description)
        self._fill()

    def clear(self):
        self.files = {}
        self.subtitle.clear()
        self.totals.clear()
        self.progress.set(0, "running")
        self.table.setRowCount(0)
        self.show_message("Turn on Build options → Coverage (gfortran) and run Test: which lines the tests "
                          "ran shows here, and in the editor's gutter.")

    def shown_files(self) -> list[cov.FileCoverage]:
        """The files listed: the project's (and the dependencies' or the
        test programs, if asked), each with code in it."""
        out = []
        for path, f in self.files.items():
            if not f.lines_found:
                continue
            inside = self.project_dir is not None and self.project_dir in path.parents
            dependency = not inside or "dependencies" in path.relative_to(self.project_dir).parts[:2]
            if dependency and not self.dependencies_check.isChecked():
                continue
            if not self.tests_check.isChecked() and any(d == path.parent or d in path.parents for d in self.test_dirs):
                continue
            out.append(f)
        return out

    def relative(self, path: Path) -> str:
        try:
            return str(path.relative_to(self.project_dir)) if self.project_dir else path.name
        except ValueError:
            return str(path)

    def _fill(self, *_):
        files = self.shown_files()
        if not self.files:
            return
        if not files:
            self.show_message("No coverage for the project's files: did the tests run code from them? "
                              "(Include tests and Include dependencies list the others.)")
            return
        self.empty.hide()
        self.splitter.show()
        muted = self.palette_colors["muted"]
        t = cov.totals(files)
        functions = [f for file in files for f in shown_functions(file)]
        parts = []
        for name, (hit, found) in (("lines", t["lines"]),
                                   ("functions", (sum(f.hits > 0 for f in functions), len(functions))),
                                   ("branches", t["branches"])):
            if found:
                percent = 100 * hit / found
                parts.append(f"<b style='color:{color_for(percent)}'>{percent:.1f}%</b> "
                             f"<span style='color:{muted}'>{name} ({hit:,} of {found:,})</span>")
        self.totals.setText("&nbsp;&nbsp;".join(parts))
        hit, found = t["lines"]
        self.progress.set(hit / found if found else 0, "ok" if found and hit == found else "running")
        selected = self._selected_path()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(files))
        for row, f in enumerate(sorted(files, key=lambda f: self.relative(f.path))):
            name = QTableWidgetItem(self.relative(f.path))  # (sorted by its text)
            if self.icon_for is not None:
                name.setIcon(self.icon_for(f.path))
            name.setData(Qt.UserRole, str(f.path))
            shown = shown_functions(f)
            fn_hit = sum(x.hits > 0 for x in shown)
            items = [
                name,
                NumberItem(f"{f.percent:.1f}%", f.percent),
                NumberItem(f"{f.lines_hit} / {f.lines_found}", f.lines_hit),
                NumberItem(f"{fn_hit} / {len(shown)}" if shown else "—", fn_hit / len(shown) if shown else -1),
            ]
            items[1].setForeground(QColor(color_for(f.percent)))
            tip = row_tooltip(f)  # (the whole row: its path and every missed line)
            for column, item in enumerate(items):
                item.setToolTip(tip)
                self.table.setItem(row, column, item)
        self.table.setSortingEnabled(True)  # (sorted again, as the header says: the user's choice kept)
        if selected is not None:
            for row in range(self.table.rowCount()):
                if self.table.item(row, 0).data(Qt.UserRole) == selected:
                    self.table.selectRow(row)
                    break
        self._show_details()

    # ---------------------------------------------------------- details
    def _selected_path(self) -> str | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def _show_details(self):
        path = self._selected_path()
        f = self.files.get(Path(path)) if path else None
        if f is None:
            self.detail_title.setText("Select a file for its functions.")
            self.functions.setRowCount(0)
            return
        self.detail_title.setText(f"<b>{html.escape(self.relative(f.path))}</b> · {f.percent:.1f}% of its "
                                  f"{f.lines_found} lines with code ran")
        shown = shown_functions(f)
        try:  # (what each function is, from its first line: the outline's badge for it)
            source = f.path.read_text(errors="replace").split("\n")
        except OSError:
            source = []
        self.functions.setSortingEnabled(False)
        self.functions.setRowCount(len(shown))
        for row, fn in enumerate(shown):
            name = QTableWidgetItem(function_name(fn.name))
            kind = cov.procedure_kind(source, fn.line)
            if kind:
                name.setIcon(symbol_icon(kind))
                name.setToolTip(kind)
            name.setData(Qt.UserRole, fn.line)
            calls = NumberItem(f"{fn.hits:,}" if fn.hits else "never", fn.hits)
            calls.setForeground(QColor(GOOD if fn.hits else POOR))
            for column, item in enumerate((name, NumberItem(str(fn.line), fn.line), calls)):
                self.functions.setItem(row, column, item)
        self.functions.setSortingEnabled(True)
        self.functions.sortItems(1)

    def _row_clicked(self, row: int, _column: int):
        path = self.table.item(row, 0).data(Qt.UserRole)
        f = self.files.get(Path(path))
        if f is not None:
            missed = f.missed()
            self.open_requested.emit(path, missed[0][0] if missed else min(f.lines, default=1))

    def _function_clicked(self, row: int, _column: int):
        path = self._selected_path()
        if path:
            self.open_requested.emit(path, self.functions.item(row, 0).data(Qt.UserRole))
