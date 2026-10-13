"""Live table of the tests ``fpm test`` runs, with an estimated progress bar.

The GUI first runs ``fpm test --list`` to learn the test names, then runs the
tests with ``--runner`` pointing at `vibefpmgui.test_runner`, which
prints a marker before and after each test (fpm runs them one at a time)::

    @@vibefpm:start:check_fast@@
    @@vibefpm:end:check_fast:0:0.012@@

How long each test took is remembered per project, so later runs can weight
the progress bar by expected duration and estimate the time left.
"""

from __future__ import annotations

import re
import statistics
import time
from dataclasses import dataclass, field
from typing import ClassVar

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QPainter, QPen
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .build_monitor import (
    ABORTED,
    COMPACT_ROW_HEIGHT,
    COMPILING,
    DONE,
    FAILED,
    PALETTES,
    ProgressLine,
    SortableTable,
    _tint,
    app_is_light,
    chip_stylesheet,
    paint_name_and_detail,
    panel_stylesheet,
    scaled,
)

MARKER_RE = re.compile(r"@@vibefpm:(start|end):([^:@]+)(?::(-?\d+):([\d.]+))?@@")
# "fpm test --list" prints " Matched names:" followed by the names.
MATCHED_RE = re.compile(r"^\s*Matched names:\s*(.*)$")
NO_TESTS_RE = re.compile(r"No tests to run")

QUEUED, RUNNING, PASSED, FAILED_T, NOT_RUN = "Queued", "Running", "Passed", "Failed", "Not run"
STATUSES = (RUNNING, PASSED, FAILED_T, NOT_RUN, QUEUED)
ICONS = {QUEUED: "·", RUNNING: "⟳", PASSED: "✓", FAILED_T: "✕", NOT_RUN: "–"}
# Test statuses borrow the build palette's colors.
COLOR_KEY = {RUNNING: COMPILING, PASSED: DONE, FAILED_T: FAILED, NOT_RUN: ABORTED, QUEUED: ABORTED}
FLASH_SECONDS = 0.6


def parse_test_list(text: str) -> list[str] | None:
    """Test names from ``fpm test --list`` output ([] if there are none,
    None if the output wasn't understood)."""
    if NO_TESTS_RE.search(text):
        return []
    lines = text.splitlines()
    for i, line in enumerate(lines):
        match = MATCHED_RE.match(line)
        if match:
            names = match.group(1).split()
            for rest in lines[i + 1:]:
                if not rest.strip() or rest.lstrip().startswith(("<", "STOP")):
                    break
                names += rest.split()
            return names
    return None


@dataclass
class TestCase:
    name: str
    row: int
    expected: float | None = None  # duration last time, if known
    seq: int = 0  # original (listed) order, for "unsorted"
    status: str = QUEUED
    started: float | None = None
    finished: float | None = None
    seconds: float | None = None  # as measured by the runner
    exit_code: int | None = None
    stopped: bool = False  # was running when Stop was pressed
    output: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if self.seconds is not None:
            return self.seconds
        if self.started is None:
            return 0.0
        return (self.finished or time.monotonic()) - self.started


class TestDelegate(QStyledItemDelegate):
    def __init__(self, monitor: TestMonitor):
        super().__init__(monitor.table)
        self.monitor = monitor

    def sizeHint(self, option, index):
        return QSize(100, scaled(COMPACT_ROW_HEIGHT))

    def paint(self, p: QPainter, option, index):
        mon = self.monitor
        if index.row() >= len(mon.tests):
            return
        test = mon.tests[index.row()]
        pal = mon.palette_colors
        color = QColor(pal["status"][COLOR_KEY[test.status]])
        r = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if test.name == mon.selected:
            p.fillRect(r, _tint(QColor(pal["accent1"]), 28))
        elif index.row() == mon.hover_row:
            p.fillRect(r, QColor(pal["hover"]))
        if test.finished is not None and time.monotonic() - test.finished < FLASH_SECONDS:
            age = time.monotonic() - test.finished
            p.fillRect(r, _tint(color, int(35 * (1 - age / FLASH_SECONDS))))
        if test.name == mon.selected and index.column() == 0:
            p.fillRect(QRectF(r.left(), r.top() + 6, 3, r.height() - 12), QColor(pal["accent1"]))
        p.setPen(QPen(QColor(pal["line"]), 1))
        p.drawLine(r.bottomLeft(), r.bottomRight())

        small = QFont(option.font)
        small.setPointSizeF(max(option.font.pointSizeF() - 2, 7))
        if index.column() == 0:
            self._paint_name(p, r, test, option.font, small, pal)
        elif index.column() == 1:
            self._paint_status(p, r, test, small, color)
        else:
            self._paint_time(p, r, test, small, pal, color)
        p.restore()

    def _paint_name(self, p, r, test, font, small, pal):
        bold = QFont(font)
        bold.setWeight(QFont.DemiBold)
        detail = {RUNNING: "running…", NOT_RUN: "not run"}.get(test.status, "")
        if test.stopped:
            detail = f"stopped after {test.elapsed():.1f} s"
        if test.status == FAILED_T and test.exit_code is not None:
            detail = f"exit code {test.exit_code}"
        lines = len(test.output)
        if lines and test.status != RUNNING:
            count = f"{lines} line{'s' * (lines != 1)} of output"
            detail = f"{detail} · {count}" if detail else count
        paint_name_and_detail(p, r.adjusted(14, 0, -6, 0), test.name, detail, bold, small,
                              QColor(pal["text"] if test.status != QUEUED else pal["muted"]), QColor(pal["muted"]))

    def _paint_status(self, p, r, test, small, color):
        font = QFont(small)
        font.setWeight(QFont.DemiBold)
        text = "Stopped" if test.stopped else self.monitor.word(test.status)
        pill = QRectF(r.left() + 8, r.center().y() - 10,
                      QFontMetricsF(font).horizontalAdvance(text) + 36, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(_tint(color, 40 if test.status != QUEUED else 22))
        p.drawRoundedRect(pill, 10, 10)
        icon = QRectF(pill.left() + 8, pill.center().y() - 6, 12, 12)
        if test.status == RUNNING:
            p.setPen(QPen(_tint(color, 70), 2))
            p.drawEllipse(icon)
            p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(icon, int(-self.monitor.spinner_angle * 16), -110 * 16)
        else:
            p.setFont(font)
            p.setPen(color)
            p.drawText(icon.adjusted(-3, -3, 3, 3), Qt.AlignCenter, ICONS[test.status])
        p.setFont(font)
        p.setPen(color.darker(115) if self.monitor.is_light else color)
        p.drawText(pill.adjusted(27, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, text)

    def _paint_time(self, p, r, test, small, pal, color):
        scale = self.monitor.time_scale()
        track = QRectF(r.left() + 10, r.center().y() - 3, max(r.width() - 74, 10), 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["track"]))
        p.drawRoundedRect(track, 3, 3)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(small.pointSizeF())
        p.setFont(mono)
        if test.status == QUEUED:
            if test.expected is not None and scale > 0:  # ghost bar: last run's time
                p.setBrush(_tint(QColor(pal["muted"]), 70))
                p.drawRoundedRect(QRectF(track.left(), track.top(),
                                         max(track.width() * test.expected / scale, 3), 6), 3, 3)
                p.setPen(QColor(pal["muted"]))
                p.drawText(r.adjusted(0, 0, -12, 0), Qt.AlignVCenter | Qt.AlignRight,
                           f"~{test.expected:.1f} s")
            return
        elapsed = test.elapsed()
        if test.status == RUNNING and test.expected:  # where it finished last time
            x = track.left() + track.width() * min(test.expected / scale, 1)
            p.setPen(QPen(QColor(pal["muted"]), 1, Qt.DotLine))
            p.drawLine(x, track.top() - 3, x, track.bottom() + 3)
            p.setPen(Qt.NoPen)
        p.setBrush(color if test.status == RUNNING else QColor(pal["muted"]))
        fraction = elapsed / scale if scale > 0 else 0
        p.drawRoundedRect(QRectF(track.left(), track.top(), max(track.width() * min(fraction, 1), 3), 6), 3, 3)
        p.setPen(QColor(pal["text"] if test.status == RUNNING else pal["muted"]))
        p.drawText(r.adjusted(0, 0, -12, 0), Qt.AlignVCenter | Qt.AlignRight, f"{elapsed:.1f} s")


STATUS_SORT = {FAILED_T: 0, RUNNING: 1, QUEUED: 2, NOT_RUN: 3, PASSED: 4}


class TestMonitor(SortableTable, QWidget):
    """Header with filter chips, progress bar and a table of tests. Clicking a
    test emits `test_selected` with its name ("" to show all output)."""

    COLUMNS: ClassVar[list[str]] = ["TEST", "STATUS", "TIME"]
    test_selected = Signal(str)
    # Everything the panel says, so a "Run" version can reuse it (see run_monitor).
    WORDS: ClassVar[dict[str, str]] = {
        "title": "Tests", "item": "test", "items": "tests",
        PASSED: "Passed", "passed": "passed",
        "idle": "Run Test to see each test's progress here.",
        "idle_label": "Run Test to see progress",
        "collecting": "Building and collecting tests… (per-file progress is in the Build tab)",
        "collecting_label": "collecting tests",
        "after_build": "tests start after the build",
        "none": "This project has no tests.", "none_label": "No tests",
        "unlisted": "The tests could not be built or listed — see the build panel and output.",
        "row_tip": "Click to show only this test's output",
    }

    def word(self, key: str) -> str:
        return self.WORDS.get(key, key)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.tests: list[TestCase] = []
        self.by_name: dict[str, TestCase] = {}
        self.current: TestCase | None = None
        self.selected = ""
        self.hover_row = -1
        self.spinner_angle = 0.0
        self.filter_status: str | None = None
        self.description = ""
        self.build_percent = None  # callable giving the build's % (set by the main window)
        self._started = 0.0
        self._finished_at: float | None = None
        self._result = ""  # "", "listing", "ok", "failed", "stopped", "none"
        self._active = False
        self.is_light = True
        self.palette_colors = PALETTES["light"]

        self.setObjectName("TestMonitor")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.title = QLabel(self.word("title"))
        title_font = self.title.font()
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.subtitle = QLabel()
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle)
        header.addStretch(1)
        self.chips: dict[str, QPushButton] = {}
        for status in (RUNNING, PASSED, FAILED_T, NOT_RUN):
            chip = QPushButton()
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(f"Show only “{self.word(status)}” {self.word('items')} "
                            "(click again to show all)")
            chip.clicked.connect(lambda checked, s=status: self.set_filter(s if checked else None))
            chip.hide()
            header.addWidget(chip)
            self.chips[status] = chip

        self.progress = ProgressLine(self)
        self.progress_label = QLabel()
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setItemDelegate(TestDelegate(self))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(scaled(COMPACT_ROW_HEIGHT))
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.cellEntered.connect(self._set_hover)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.viewport().installEventFilter(self)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2):
            hh.setSectionResizeMode(column, QHeaderView.Fixed)
        self._fit_columns()
        self._init_sorting({
            0: lambda t: t.name.lower(),
            1: lambda t: STATUS_SORT.get(t.status, 9),
            2: lambda t: t.elapsed() if t.status != QUEUED else (t.expected or 0.0),
        }, descending_first={2})

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self.refresh_palette()
        self._show_empty(self.word("idle"))
        self._update_header()

    # ------------------------------------------------------------- styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal))
        for status, chip in self.chips.items():
            chip.setStyleSheet(chip_stylesheet(pal, pal["status"][COLOR_KEY[status]]))
        self.empty.setStyleSheet(f"color: {pal['muted']};")
        self._update_header()
        self.table.viewport().update()
        self.progress.update()

    def _show_empty(self, message: str | None):
        self.empty.setText(message or "")
        self.empty.setVisible(bool(message))
        self.table.setVisible(not message)

    # ----------------------------------------------------------- lifecycle
    def reset(self):
        """Back to the empty state (e.g. when another project is opened)."""
        self._timer.stop()
        self.tests, self.by_name, self.current = [], {}, None
        self.table.setRowCount(0)
        self.selected = ""
        self.description = ""
        self._started = 0.0
        self._finished_at = None
        self._result = ""
        self._active = False
        self.set_filter(None)
        self.progress.set(0, "running")
        self._show_empty(self.word("idle"))
        self._update_header()

    def begin_listing(self, description: str = ""):
        """Phase 1: fpm is building and listing the tests."""
        self.tests, self.by_name, self.current = [], {}, None
        self.table.setRowCount(0)
        self.selected = ""
        self.description = description
        self._result = "listing"
        self._started = time.monotonic()
        self._finished_at = None
        self._active = True
        self.set_filter(None)
        self.progress.set_busy(True)  # the build's length is unknown here
        self._show_empty(self.word("collecting"))
        self._timer.start()
        self._update_header()

    def start(self, names: list[str], history: dict[str, float], building: bool = False):
        """Phase 2: run the tests (after fpm has built them, if ``building``)."""
        self.tests, self.by_name, self.current = [], {}, None
        self.table.setRowCount(0)
        for name in names:
            self._add(name, history.get(name))
        self._result = "building" if building else ""
        self._started = time.monotonic()
        self._active = True
        if building:
            self.progress.set_busy(True)
        else:
            self.progress.set(0, "running")
        self._show_empty(None if names else self.word("none"))
        self._timer.start()
        self._update_header()

    def build_finished(self):
        """fpm has finished building; the tests are starting."""
        if self._result == "building":
            self._result = ""
            self._started = time.monotonic()
            self.progress.set(0, "running")
            self._update_header()

    def finish(self, exit_code: int, stopped: bool = False, no_tests: bool = False):
        if not self._active:
            return
        build_failed = self._result == "building" and exit_code != 0 and not stopped
        self._active = False
        self._finished_at = time.monotonic()
        now = self._finished_at
        for test in self.tests:
            if test.status == RUNNING:
                test.status = FAILED_T if not stopped else NOT_RUN
                test.stopped = stopped
                test.finished = now
            elif test.status == QUEUED:
                test.status = NOT_RUN
        if no_tests:
            self._result = "none"
            self._show_empty(self.word("none"))
        elif stopped:
            self._result = "stopped"
        elif exit_code != 0 or any(t.status == FAILED_T for t in self.tests):
            self._result = "failed"
        else:
            self._result = "ok"
        if build_failed:
            self._result = "buildfailed"
        if self._result == "failed" and not self.tests:
            self._show_empty(self.word("unlisted"))
        state = {"ok": "ok", "failed": "failed", "stopped": "stopped", "none": "ok",
                 "buildfailed": "failed"}[self._result]
        self.progress.set(1.0 if self._result in ("ok", "none") else self.fraction(), state)
        self._update_header()
        self.table.viewport().update()
        self.schedule_sort()

    def durations(self) -> dict[str, float]:
        """Measured durations of the tests that ran to completion."""
        return {t.name: t.seconds for t in self.tests if t.seconds is not None}

    def output_for(self, name: str) -> str:
        test = self.by_name.get(name)
        return "\n".join(test.output) if test else ""

    # ------------------------------------------------------------- parsing
    def feed_line(self, line: str) -> str | None:
        """Process one line of ``fpm test`` output. Returns the text to show
        in the console (markers removed), or None to drop the line."""
        match = MARKER_RE.search(line)
        if not match:
            if self.current is not None:
                self.current.output.append(line)
            return line
        before = line[:match.start()]
        if before.strip() and self.current is not None:
            self.current.output.append(before)
        kind, name = match.group(1), match.group(2)
        test = self.by_name.get(name) or self._add(name, None)
        if kind == "start":
            test.status = RUNNING
            test.started = time.monotonic()
            test.output = []
            self.current = test
            if self.filter_status is None:
                self.table.scrollToItem(self.table.item(test.row, 0))
        else:
            test.exit_code = int(match.group(3))
            test.seconds = float(match.group(4))
            test.finished = time.monotonic()
            test.status = PASSED if test.exit_code == 0 else FAILED_T
            self.current = None
            if self.selected == name:  # refresh the filtered output view
                self.test_selected.emit(name)
        self._apply_filter(test)
        self._update_header()
        self.table.viewport().update()
        self.schedule_sort()
        return before if before.strip() else None

    # ------------------------------------------------------------- helpers
    def _add(self, name: str, expected: float | None) -> TestCase:
        row = self.table.rowCount()
        self.table.insertRow(row)
        for column in range(len(self.COLUMNS)):
            self.table.setItem(row, column, QTableWidgetItem())
        self.table.item(row, 0).setText(name)
        self.table.item(row, 0).setToolTip(self.word("row_tip"))
        test = TestCase(name, row, expected, seq=len(self.tests))
        self.tests.append(test)
        self.by_name[name] = test
        self._apply_filter(test)
        self._show_empty(None)
        self.schedule_sort()
        return test

    # SortableTable hooks (the selection is kept by name, so nothing to remap)
    def rows(self) -> list[TestCase]:
        return self.tests

    def _sync_row(self, row: int):
        item = self.table.item(row, 0)
        item.setText(self.tests[row].name)
        item.setToolTip(self.word("row_tip"))

    def _reapply_filter(self):
        for t in self.tests:
            self._apply_filter(t)

    def _selected_object(self):
        return None

    def _restore_selection(self, _obj):
        pass

    def _expected(self) -> dict[str, float]:
        """Expected duration for every test, or {} when nothing is known.

        Tests without a recorded time get the median of the known ones, and a
        running test that has already taken longer than expected is assumed
        to need a bit longer still, so the bar never sits at ~100%."""
        known = [t.expected for t in self.tests if t.expected is not None]
        if not known:
            return {}
        guess = statistics.median(known)
        expected = {}
        for t in self.tests:
            e = t.expected if t.expected is not None else guess
            if t.status == RUNNING:
                e = max(e, t.elapsed() * 1.25)
            expected[t.name] = e
        return expected

    def fraction(self) -> float:
        if not self.tests:
            return 0.0
        expected = self._expected()
        if not expected:  # nothing known yet: count tests
            done = sum(t.status in (PASSED, FAILED_T, NOT_RUN) for t in self.tests)
            return done / len(self.tests)
        total = sum(max(v, 1e-3) for v in expected.values())
        progress = 0.0
        for t in self.tests:
            e = max(expected[t.name], 1e-3)
            if t.status in (PASSED, FAILED_T, NOT_RUN):
                progress += e
            elif t.status == RUNNING:
                progress += min(t.elapsed(), 0.95 * e)
        return progress / total

    def remaining(self) -> float | None:
        """Estimated time left, only when every test has a recorded time."""
        if any(t.expected is None for t in self.tests):
            return None
        expected = self._expected()
        if not expected:
            return None
        left = 0.0
        for t in self.tests:
            if t.status == QUEUED:
                left += expected[t.name]
            elif t.status == RUNNING:
                left += max(expected[t.name] - t.elapsed(), 0.0)
        return left

    def time_scale(self) -> float:
        return max([t.elapsed() for t in self.tests] + [t.expected or 0 for t in self.tests] + [0.1])

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for t in self.tests:
            counts[t.status] += 1
        return counts

    def set_filter(self, status: str | None):
        self.filter_status = status
        for s, chip in self.chips.items():
            chip.setChecked(s == status)
        for t in self.tests:
            self._apply_filter(t)

    def _apply_filter(self, test: TestCase):
        hide = self.filter_status is not None and test.status != self.filter_status
        self.table.setRowHidden(test.row, hide)

    def _update_header(self):
        pal = self.palette_colors
        self.subtitle.setText(f"<span style='color:{pal['muted']}'>{self.description}</span>")
        counts = self.counts()
        for status, chip in self.chips.items():
            chip.setText(f"{ICONS[status]} {counts[status]}")
            chip.setToolTip(f"Show only “{self.word(status)}” {self.word('items')} "
                            "(click again to show all)")
            chip.setVisible(counts[status] > 0)
        if self.filter_status and counts[self.filter_status] == 0:
            self.set_filter(None)

        muted = f"<span style='color:{pal['muted']}'>"
        end = self._finished_at or time.monotonic()
        seconds = f"{end - self._started:.1f} s" if self._started else ""
        n = len(self.tests)
        finished = counts[PASSED] + counts[FAILED_T]
        failed = f" · {counts[FAILED_T]} failed" if counts[FAILED_T] else ""
        if self._result in ("listing", "building"):
            pct = self.build_percent() if self.build_percent else None
            done = f" {pct}%" if pct else ""
            what = self.word("collecting_label" if self._result == "listing" else "after_build")
            text = f"<b>Building…{done}</b> {muted}· {what} · {seconds}</span>"
        elif self._result == "buildfailed":
            text = f"<b>Build failed</b> {muted}· see the Build tab</span>"
        elif self._result == "none":
            text = f"<b>{self.word('none_label')}</b>"
        elif self._result == "ok" and counts[NOT_RUN]:
            text = (f"<b>{counts[PASSED]} {self.word('passed')}</b> {muted}· {counts[NOT_RUN]} not run"
                    f" · {seconds}</span>")
        elif self._result == "ok":
            done = self.word(PASSED) if n == 1 else f"All {n} {self.word('passed')}"
            text = f"<b>{done}</b> {muted}· {seconds}</span>"
        elif self._result == "failed":
            text = f"<b>{counts[FAILED_T]} of {n} failed</b> {muted}· {seconds}</span>" if n \
                else "<b>Failed</b>"
        elif self._result == "stopped":
            text = f"<b>Stopped</b> {muted}· {finished} of {n} run{failed}</span>"
        elif self._active:
            left = self.remaining()
            eta = f" · ~{left:.0f} s left" if left is not None and left >= 1 else ""
            text = f"<b>{finished} of {n}</b> {muted}{self.word('items')}{failed} · {seconds}{eta}</span>"
        else:
            text = f"{muted}{self.word('idle_label')}</span>"
        self.progress_label.setText(text)

    def _tick(self):
        self.spinner_angle = (self.spinner_angle + 18) % 360
        now = time.monotonic()
        flashing = any(t.finished is not None and now - t.finished < FLASH_SECONDS for t in self.tests)
        if self._active and self._result == "":
            self.progress.set(self.fraction())
        self.table.viewport().update()
        self._update_header()
        if not (self._active or flashing):
            self._timer.stop()

    def _row_clicked(self, row: int, _column: int):
        if row >= len(self.tests):
            return
        name = self.tests[row].name
        self.selected = "" if self.selected == name else name  # click again: show all
        self.table.viewport().update()
        self.test_selected.emit(self.selected)

    def _set_hover(self, row: int, _column: int):
        if row != self.hover_row:
            self.hover_row = row
            self.table.viewport().update()

    def eventFilter(self, obj, event):
        if obj is self.table.viewport():
            if event.type() == QEvent.Leave:
                self._set_hover(-1, 0)
            elif event.type() == QEvent.Resize:
                self._fit_columns()
        return super().eventFilter(obj, event)

    def _fit_columns(self):
        width = self.table.viewport().width()
        self.table.setColumnWidth(1, scaled(140 if width >= scaled(520) else 120))
        self.table.setColumnWidth(2, scaled(170 if width >= scaled(640) else 120))

    def refresh_fonts(self):
        """Re-apply sizes after the application font changed."""
        title_font = QFont(QApplication.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.table.verticalHeader().setDefaultSectionSize(scaled(COMPACT_ROW_HEIGHT))
        self.refresh_palette()
        self._fit_columns()
