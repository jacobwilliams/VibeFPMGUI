"""Live table of the files fpm is compiling, fed from fpm's console output.

When its output is not a terminal, fpm prints one line when it starts a
target and another when it finishes, e.g.::

    [ 11%]            json_parameters.F90
    [ 22%]            json_parameters.F90  done.

With an OpenMP-enabled fpm several files compile at once, so start and finish
lines interleave.  fpm prints "done." even for a file that failed to compile,
so failures and warnings come from the compiler diagnostics instead
(``path:line:col:`` followed by ``Error:``/``Warning:``) and from fpm's
``<ERROR> Compilation failed for object " app_main.f90.o "`` line.

fpm only echoes compiler output when a compile fails; otherwise it is kept in
``build/<compiler>_<hash>/<package>/<object>.o.log``, so that log is read when
a file finishes to pick up its warnings.

The table is drawn by `RowDelegate`: status badges (with a spinner
while compiling), package tags, and per-file timing bars scaled to the slowest
file.  The summary chips above it filter the table by status.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar, NamedTuple

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from qtpy.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPalette,
    QPen,
)
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import manifest as m
from .ansi import strip as strip_ansi
from .icons import make_icon
from .widgets import StatusLabel

PROGRESS_RE = re.compile(r"^\[\s*(\d+)%\]\s+(\S+)(?:\s+(done|failed)\.)?\s*$")
# gfortran/gcc: "src/a.f90:3:15:" (message on later lines) or "a.c:3:5: error: ..."
# (An optional "C:" drive prefix, so Windows paths aren't cut at the drive colon.)
GNU_LOC_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\s]+?):(\d+):(?:(\d+):)?\s*"
                        r"(?:(fatal error|error|warning)\b:?\s*(.*))?$", re.IGNORECASE)
# Intel: "src/a.f90(3): error #5082: ..."
INTEL_LOC_RE = re.compile(r"^((?:[A-Za-z]:)?[^:\s(]+?)\((\d+)\):\s*(error|warning|remark)\b[^:]*:\s*(.*)$",
                          re.IGNORECASE)
GNU_SEVERITY_RE = re.compile(r"^(Fatal Error|Error|Warning):\s*(.*)$")
# lfortran: "semantic error: ..." / "warning: ..." / "style suggestion: ...",
# then " --> src/a.f90:3:15" on the next line.
LFORTRAN_SEVERITY_RE = re.compile(r"^(?:[a-z]+ )?(error|warning|style suggestion):\s*(.*)$")
LFORTRAN_LOC_RE = re.compile(r"^\s*-->\s*((?:[A-Za-z]:)?.+?):(\d+)(?::\d+)?\s*$")
FAILED_OBJECT_RE = re.compile(r'Compilation failed for object\s+"\s*(.+?)\s*"')
UP_TO_DATE_RE = re.compile(r"Project is up to date")

SOURCE_EXTS = {".f", ".for", ".ftn", ".f77", ".f90", ".f95", ".f03", ".f08", ".fpp",
               ".c", ".cc", ".cpp", ".cxx", ".h", ".inc"}

COMPILING, DONE, WARNING, FAILED, ABORTED = "Compiling", "Done", "Warnings", "Failed", "Aborted"
STATUSES = (COMPILING, DONE, WARNING, FAILED, ABORTED)
STATUS_ICONS = {COMPILING: "⟳", DONE: "✓", WARNING: "⚠", FAILED: "✕", ABORTED: "–"}
LIBRARY = "library"  # package kind used for link targets

PALETTES = {
    "light": {"bg": "#ffffff", "text": "#1f2328", "muted": "#8b949e", "line": "#eaeef2", "hover": "#f3f6f9",
                  "track": "#eaeef2", "accent1": "#4c6ef5", "accent2": "#15aabf",
                  "status": {COMPILING: "#e8a317", DONE: "#2f9e44", WARNING: "#e8590c",
                          FAILED: "#e03131", ABORTED: "#868e96"}},
    "dark": {"bg": "#1e1f22", "text": "#e6e8eb", "muted": "#8b929b", "line": "#2f3237", "hover": "#2a2d31",
                 "track": "#33363b", "accent1": "#748ffc", "accent2": "#3bc9db",
                 "status": {COMPILING: "#fcc419", DONE: "#51cf66", WARNING: "#ff922b",
                         FAILED: "#ff6b6b", ABORTED: "#868e96"}},
}
# Package-tag dot colors; the main window replaces these with the graph theme's.
DEFAULT_PACKAGE_COLORS = {m.ROOT: "#f6c85f", m.GIT: "#74c0fc", m.PATH: "#8ce99a",
                          m.REGISTRY: "#d7b8f3", m.META: "#f3c6a5", m.MISSING: "#ced4da",
                          LIBRARY: "#ced4da"}

ROW_HEIGHT = 44  # at the normal text size; see scaled() (the Review tab: two lines a row)
COMPACT_ROW_HEIGHT = 30  # the Build, Tests and Run tabs: one line a row

# Text-size factor set by the main window's View menu (1.0 = normal).
_ui_scale = 1.0


def set_ui_scale(factor: float):
    global _ui_scale
    _ui_scale = factor


def scaled(pixels: float) -> int:
    """A pixel size adjusted for the current text size."""
    return round(pixels * _ui_scale)
FLASH_SECONDS = 0.6  # a row briefly glows in its status color when it finishes


class Diagnostic(NamedTuple):
    file: str      # as the compiler printed it
    line: int
    severity: str  # "error", "fatal error", "warning" or "remark"
    message: str   # one-line summary including the location


class DiagnosticParser:
    """Turns compiler output, one line at a time, into `Diagnostic`."""

    def __init__(self):
        self._location: tuple[str, int] | None = None  # gfortran prints the severity later
        self._pending: tuple[str, str] | None = None  # lfortran prints the location later

    def feed(self, line: str) -> Diagnostic | None:
        match = LFORTRAN_SEVERITY_RE.match(line)
        if match:
            severity = "remark" if match.group(1) == "style suggestion" else match.group(1)
            self._pending = (severity, line)
            return None
        if self._pending:
            match = LFORTRAN_LOC_RE.match(line)
            if match:
                (severity, text), self._pending = self._pending, None
                return Diagnostic(match.group(1), int(match.group(2)), severity,
                                  f"{match.group(1)}:{match.group(2)}: {text}")
        match = INTEL_LOC_RE.match(line)
        if match:
            return Diagnostic(match.group(1), int(match.group(2)), match.group(3).lower(), line)
        match = GNU_LOC_RE.match(line)
        if match and Path(match.group(1)).suffix.lower() in SOURCE_EXTS:
            if match.group(4):  # severity on the same line (gcc/clang style)
                return Diagnostic(match.group(1), int(match.group(2)), match.group(4).lower(), line)
            self._location = (match.group(1), int(match.group(2)))
            return None
        match = GNU_SEVERITY_RE.match(line)
        if match and self._location:
            (file, number), self._location = self._location, None
            return Diagnostic(file, number, match.group(1).lower(), f"{file}:{number}: {line}")
        return None


LOG_READ_LIMIT = 1_000_000  # bytes of a compiler log read (half from each end)


def read_log_text(log: Path) -> str:
    """A compiler log's text. A huge one (an lfortran crash writes the whole
    compiled program, 100+ MB) is read as its start and end only, which is
    where the messages are; reading and showing all of it would freeze the app."""
    size = log.stat().st_size
    if size <= LOG_READ_LIMIT:
        return log.read_text(errors="replace")
    half = LOG_READ_LIMIT // 2
    with open(log, "rb") as f:
        head = f.read(half)
        f.seek(size - half)
        tail = f.read()
    head = head[:head.rfind(b"\n") + 1] or head  # whole lines only
    tail = tail[tail.find(b"\n") + 1:] or tail
    skipped = size - len(head) - len(tail)
    return (head.decode("utf-8", errors="replace")
            + f"\n[… {skipped / 1e6:.1f} MB of compiler output not shown — full log: {log} …]\n\n"
            + tail.decode("utf-8", errors="replace"))


def target_kind(name: str) -> str:
    suffix = Path(name).suffix.lower()
    if suffix in SOURCE_EXTS:
        return "Source"
    if suffix in (".a", ".so", ".dylib", ".lib", ".dll"):
        return "Library"
    return "Executable"


@dataclass
class Target:
    name: str
    row: int
    seq: int = 0  # original (start) order, for "unsorted"
    status: str = COMPILING
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    messages: list[str] = field(default_factory=list)
    warnings: int = 0
    errors: int = 0
    candidates: list[tuple[str, Path]] = field(default_factory=list)  # (package, source)
    path: Path | None = None  # resolved source file
    log_text: str = ""  # the compiler's full output (fpm's .o.log)
    diagnostics: list[Diagnostic] = field(default_factory=list)
    package: str = ""
    package_kind: str = LIBRARY

    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started


def index_sources(graph: m.DependencyGraph | None) -> dict[str, list[tuple[str, Path]]]:
    """Map source file basename -> [(package, full path)] for every package
    on disk, so table rows can say which package a file belongs to."""
    index: dict[str, list[tuple[str, Path]]] = {}
    if graph is None:
        return index
    packages = [p for p in graph.packages.values()
                if p.directory and p.directory.is_dir() and p.kind != m.MISSING]
    package_dirs = {p.directory.resolve() for p in packages}
    for pkg in packages:
        own = pkg.directory.resolve()
        for dirpath, dirnames, filenames in os.walk(own):
            # Skip build trees (they hold other packages' sources), VCS dirs,
            # and nested packages (e.g. a path dependency inside the project),
            # which are indexed under their own name.
            dirnames[:] = [d for d in dirnames if d != "build" and not d.startswith(".")
                           and (Path(dirpath) / d).resolve() not in package_dirs]
            for f in filenames:
                if Path(f).suffix.lower() in SOURCE_EXTS:
                    index.setdefault(f, []).append((pkg.name, Path(dirpath) / f))
    return index


# ----------------------------------------------------------------- drawing
def _tint(color: QColor, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def panel_stylesheet(object_name: str, pal: dict) -> str:
    """Flat table/header/scrollbar styling shared by the build and test panels."""
    n = f"#{object_name}"
    return f"""
        {n} {{ background: {pal['bg']}; }}
        {n} QLabel {{ color: {pal['text']}; background: transparent; }}
        {n} QTableWidget {{ background: {pal['bg']}; border: none; }}
        {n} QHeaderView {{ background: {pal['bg']}; }}
        {n} QAbstractScrollArea::corner {{ background: transparent; border: none; }}
        {n} QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        {n} QScrollBar::handle:vertical {{ background: {pal['track']}; border-radius: 3px;
            min-height: 24px; }}
        {n} QScrollBar::handle:vertical:hover {{ background: {pal['muted']}; }}
        {n} QScrollBar:horizontal {{ background: transparent; height: 8px; margin: 2px; }}
        {n} QScrollBar::handle:horizontal {{ background: {pal['track']}; border-radius: 3px;
            min-width: 24px; }}
        {n} QScrollBar::handle:horizontal:hover {{ background: {pal['muted']}; }}
        {n} QScrollBar::add-line, {n} QScrollBar::sub-line,
        {n} QScrollBar::add-page, {n} QScrollBar::sub-page {{ background: none; height: 0; }}
        {n} QScrollBar::add-line:horizontal, {n} QScrollBar::sub-line:horizontal,
        {n} QScrollBar::add-page:horizontal, {n} QScrollBar::sub-page:horizontal {{ width: 0; }}
        {n} QHeaderView::section {{
            background: {pal['bg']}; color: {pal['muted']}; border: none;
            border-bottom: 1px solid {pal['line']}; padding: 6px 12px;
            font-size: {scaled(10)}px; font-weight: 600; }}
    """


def chip_stylesheet(pal: dict, color: str) -> str:
    """A rounded, tinted, checkable summary chip."""
    c = QColor(color)
    rgb = f"{c.red()},{c.green()},{c.blue()}"
    return f"""
        QPushButton {{ background: rgba({rgb},40); color: {pal['text']}; border: 1px solid transparent;
            border-radius: 10px; padding: 2px 10px; font-weight: 600; }}
        QPushButton:hover {{ background: rgba({rgb},70); }}
        QPushButton:checked {{ background: rgba({rgb},90); border: 1px solid rgb({rgb}); }}
    """


def app_is_light() -> bool:
    return QApplication.palette().color(QPalette.Base).lightness() >= 128


def paint_name_and_detail(p: QPainter, r: QRectF, name: str, detail: str, bold: QFont, small: QFont,
                          color: QColor, muted: QColor, detail_elide=Qt.ElideRight):
    """A row's name (bold) and, on the same line after it, its detail in
    small grey text (a file's folder, a test's exit code), each shortened
    to fit: the name keeps at least 60% of the room when both don't fit."""
    gap = 8
    name_metrics = QFontMetricsF(bold)
    name_width = name_metrics.horizontalAdvance(name)
    if detail:
        detail_width = QFontMetricsF(small).horizontalAdvance(detail)
        if name_width + gap + detail_width > r.width():
            name_width = min(name_width, max(r.width() * 0.6, r.width() - gap - detail_width))
    shown = name_metrics.elidedText(name, Qt.ElideMiddle, min(name_width, r.width()) + 1)
    p.setFont(bold)
    p.setPen(color)
    p.drawText(r, Qt.AlignLeft | Qt.AlignVCenter, shown)
    if not detail:
        return
    left = r.left() + name_metrics.horizontalAdvance(shown) + gap
    room = r.right() - left
    if room < 20:
        return
    p.setFont(small)
    p.setPen(muted)
    p.drawText(QRectF(left, r.top(), room, r.height()), Qt.AlignLeft | Qt.AlignVCenter,
               QFontMetricsF(small).elidedText(detail, detail_elide, room))


class RowDelegate(QStyledItemDelegate):
    """Paints every cell of the build table from its `Target`."""

    def __init__(self, monitor: BuildMonitor):
        super().__init__(monitor.table)
        self.monitor = monitor

    def sizeHint(self, option, index):
        return QSize(100, scaled(COMPACT_ROW_HEIGHT))

    def paint(self, p: QPainter, option, index):
        mon = self.monitor
        if index.row() >= len(mon.targets):
            return
        target = mon.targets[index.row()]
        pal = mon.palette_colors
        r = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        status_color = QColor(pal["status"][target.status])

        if index.row() == mon.selected_row:
            p.fillRect(r, _tint(QColor(pal["accent1"]), 28))
            if index.column() == 0:
                p.fillRect(QRectF(r.left(), r.top() + 6, 3, r.height() - 12), QColor(pal["accent1"]))
        elif index.row() == mon.hover_row:
            p.fillRect(r, QColor(pal["hover"]))
        if target.finished is not None:  # fade-out flash when a file finishes
            age = time.monotonic() - target.finished
            if age < FLASH_SECONDS:
                p.fillRect(r, _tint(status_color, int(35 * (1 - age / FLASH_SECONDS))))
        p.setPen(QPen(QColor(pal["line"]), 1))
        p.drawLine(r.bottomLeft(), r.bottomRight())

        small = QFont(option.font)
        small.setPointSizeF(max(option.font.pointSizeF() - 2, 7))
        column = index.column()
        if column == 0:
            self._paint_file(p, r, target, option.font, small, pal)
        elif column == 1:
            self._paint_package(p, r, target, small, pal)
        elif column == 2:
            self._paint_status(p, r, target, small, pal, status_color)
        else:
            self._paint_time(p, r, target, small, pal, status_color)
        p.restore()

    def _paint_file(self, p, r, target, font, small, pal):
        bold = QFont(font)
        bold.setWeight(QFont.DemiBold)
        paint_name_and_detail(p, r.adjusted(14, 0, -6, 0), target.name, self.monitor.display_folder(target),
                              bold, small, QColor(pal["text"]), QColor(pal["muted"]), Qt.ElideLeft)

    def _paint_package(self, p, r, target, small, pal):
        if not target.package:
            return
        color = QColor(self.monitor.package_colors.get(target.package_kind, pal["muted"]))
        fm = QFontMetricsF(small)
        text = fm.elidedText(target.package, Qt.ElideRight, r.width() - 46)
        pill = QRectF(r.left() + 8, r.center().y() - 9, fm.horizontalAdvance(text) + 28, 18)
        p.setPen(Qt.NoPen)
        p.setBrush(_tint(color, 50))
        p.drawRoundedRect(pill, 9, 9)
        p.setBrush(color)
        p.drawEllipse(QRectF(pill.left() + 7, pill.center().y() - 3, 6, 6))
        p.setFont(small)
        p.setPen(QColor(pal["text"]))
        p.drawText(pill.adjusted(17, 0, 0, 0), Qt.AlignVCenter | Qt.AlignLeft, text)

    def _paint_status(self, p, r, target, small, pal, color):
        text = f"{target.warnings} warning{'s' * (target.warnings != 1)}" \
            if target.status == WARNING else target.status
        font = QFont(small)
        font.setWeight(QFont.DemiBold)
        pill = QRectF(r.left() + 8, r.center().y() - 10,
                      QFontMetricsF(font).horizontalAdvance(text) + 36, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(_tint(color, 40))
        p.drawRoundedRect(pill, 10, 10)
        icon = QRectF(pill.left() + 8, pill.center().y() - 6, 12, 12)
        if target.status == COMPILING:  # spinning arc
            p.setPen(QPen(_tint(color, 70), 2))
            p.drawEllipse(icon)
            p.setPen(QPen(color, 2, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(icon, int(-self.monitor.spinner_angle * 16), -110 * 16)
        else:
            p.setFont(font)
            p.setPen(color)
            p.drawText(icon.adjusted(-3, -3, 3, 3), Qt.AlignCenter, STATUS_ICONS[target.status])
        p.setFont(font)
        p.setPen(color.darker(115) if self.monitor.is_light else color)
        p.drawText(pill.adjusted(27, 0, -6, 0), Qt.AlignVCenter | Qt.AlignLeft, text)

    def _paint_time(self, p, r, target, small, pal, color):
        elapsed = target.elapsed()
        slowest = self.monitor.slowest()
        track = QRectF(r.left() + 10, r.center().y() - 3, max(r.width() - 74, 10), 6)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["track"]))
        p.drawRoundedRect(track, 3, 3)
        # Live bars use the status color; finished ones are grey.
        bar_color = color if target.status == COMPILING else QColor(pal["muted"])
        fraction = elapsed / slowest if slowest > 0 else 0
        p.setBrush(bar_color)
        p.drawRoundedRect(QRectF(track.left(), track.top(), max(track.width() * fraction, 3), 6), 3, 3)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(small.pointSizeF())
        p.setFont(mono)
        p.setPen(QColor(pal["text"] if target.status == COMPILING else pal["muted"]))
        p.drawText(r.adjusted(0, 0, -12, 0), Qt.AlignVCenter | Qt.AlignRight, f"{elapsed:.1f} s")


class SortableTable:
    """Click-to-sort for the custom-drawn status tables.

    The delegates paint row *n* from ``rows()[n]``, so instead of Qt's own
    sorting (which would reorder the items but not our list) the list itself
    is sorted and every row's items are rewritten. Clicking a header cycles
    through: sorted, reversed, original order. The order is kept while rows
    are added or change (re-sorted a few times a second at most).

    Subclasses provide ``table``, ``COLUMNS``, ``rows()``, ``_sync_row(i)``
    and ``_reapply_filter()``, and may override ``_selected_object`` /
    ``_restore_selection`` to keep the selection on the same object.
    """

    def _init_sorting(self, keys: dict, descending_first: set[int] = frozenset()):
        self._sort_keys = keys
        self._sort_desc_first = set(descending_first)
        self.sort_column = -1
        self.sort_descending = False
        self._sort_clicks = 0
        header = self.table.horizontalHeader()
        header.setSectionsClickable(True)
        header.sectionClicked.connect(self._header_clicked)
        self._sort_timer = QTimer(self)
        self._sort_timer.setSingleShot(True)
        self._sort_timer.setInterval(150)
        self._sort_timer.timeout.connect(self.apply_sort)
        for column in keys:
            item = self.table.horizontalHeaderItem(column)
            if item is not None:
                item.setToolTip("Click to sort (again to reverse, a third time for the original order)")

    def _header_clicked(self, column: int):
        if column not in self._sort_keys:
            return
        if column != self.sort_column:
            self.sort_column = column
            self.sort_descending = column in self._sort_desc_first
            self._sort_clicks = 1
        elif self._sort_clicks == 1:
            self.sort_descending = not self.sort_descending
            self._sort_clicks = 2
        else:
            self.sort_column = -1  # back to the original order
            self._sort_clicks = 0
        self.apply_sort()

    def schedule_sort(self):
        """Call when rows were added or changed; re-sorts shortly if sorted."""
        if self.sort_column >= 0 and not self._sort_timer.isActive():
            self._sort_timer.start()

    def apply_sort(self):
        rows = self.rows()
        selected = self._selected_object()
        rows.sort(key=lambda o: getattr(o, "seq", 0))  # stable base: original order
        if self.sort_column >= 0:
            rows.sort(key=self._sort_keys[self.sort_column], reverse=self.sort_descending)
        for index, obj in enumerate(rows):
            if hasattr(obj, "row"):
                obj.row = index
            self._sync_row(index)
        self._restore_selection(selected)
        self._reapply_filter()
        self._update_sort_header()
        self.table.viewport().update()

    def _update_sort_header(self):
        labels = []
        for column, name in enumerate(self.COLUMNS):
            if column == self.sort_column:
                name += " ▼" if self.sort_descending else " ▲"
            labels.append(name)
        self.table.setHorizontalHeaderLabels(labels)

    def _selected_object(self):
        row = getattr(self, "selected_row", -1)
        rows = self.rows()
        return rows[row] if 0 <= row < len(rows) else None

    def _restore_selection(self, obj):
        if hasattr(self, "selected_row"):
            self.selected_row = self.rows().index(obj) if obj is not None else -1


class ProgressLine(QWidget):
    """Thin rounded progress bar with a gradient fill. In "busy" mode
    (`set_busy`) it shows a segment sliding back and forth instead of
    a fill, for work whose length isn't known."""

    SEGMENT = 0.28  # busy segment width, as a fraction of the bar

    def __init__(self, monitor: BuildMonitor):
        super().__init__()
        self.monitor = monitor
        self.value = 0.0
        self.state = "running"  # running | ok | failed | stopped
        self.busy = False
        self._busy_started = 0.0
        self._busy_timer = QTimer(self)
        self._busy_timer.setInterval(16)
        self._busy_timer.timeout.connect(self.update)
        self.setFixedHeight(6)

    def set(self, value: float, state: str | None = None):
        """Show a known fraction (0..1); ends busy mode."""
        self._stop_busy()
        self.value = max(0.0, min(1.0, value))
        if state:
            self.state = state
        self.update()

    def set_busy(self, busy: bool = True):
        """Indeterminate mode: something is happening, amount unknown."""
        if busy and not self.busy:
            self.busy = True
            self.state = "running"
            self._busy_started = time.monotonic()
            self._busy_timer.start()
        elif not busy:
            self._stop_busy()
        self.update()

    def _stop_busy(self):
        self.busy = False
        self._busy_timer.stop()

    def paintEvent(self, _event):
        pal = self.monitor.palette_colors
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["track"]))
        p.drawRoundedRect(r, 3, 3)
        if self.busy:
            self._paint_busy(p, r, pal)
            return
        if self.value <= 0:
            return
        if self.state == "failed":
            a = b = QColor(pal["status"][FAILED])
        elif self.state == "stopped":
            a = b = QColor(pal["status"][ABORTED])
        elif self.state == "ok":
            a = b = QColor(pal["status"][DONE])
        else:
            a, b = QColor(pal["accent1"]), QColor(pal["accent2"])
        gradient = QLinearGradient(r.topLeft(), r.topRight())
        gradient.setColorAt(0, a)
        gradient.setColorAt(1, b)
        p.setBrush(gradient)
        p.drawRoundedRect(QRectF(r.left(), r.top(), max(r.width() * self.value, 6), r.height()), 3, 3)

    def _paint_busy(self, p: QPainter, r: QRectF, pal: dict):
        # Ease back and forth across the track, ~1.6 s per sweep.
        t = ((time.monotonic() - self._busy_started) / 1.6) % 2.0
        t = t if t <= 1 else 2 - t
        t = t * t * (3 - 2 * t)  # smoothstep
        seg = r.width() * self.SEGMENT
        x = r.left() + (r.width() - seg) * t
        gradient = QLinearGradient(x, 0, x + seg, 0)
        a, b = QColor(pal["accent1"]), QColor(pal["accent2"])
        edge = QColor(a)
        edge.setAlpha(0)
        gradient.setColorAt(0, edge)
        gradient.setColorAt(0.3, a)
        gradient.setColorAt(0.7, b)
        end = QColor(b)
        end.setAlpha(0)
        gradient.setColorAt(1, end)
        p.setBrush(gradient)
        p.drawRoundedRect(QRectF(x, r.top(), seg, r.height()), 3, 3)


# ------------------------------------------------------------------ widget
STATUS_SORT = {FAILED: 0, WARNING: 1, COMPILING: 2, ABORTED: 3, DONE: 4}


class LogFinder:
    """Finds a source file's compile log, which fpm writes as
    ``build/<compiler>_<hash>/<package>/<source path>.o.log``: the path is
    relative to the project, with "/" as "_" (``src_utils.f90.o.log``,
    ``build_dependencies_fmin_src_fmin.F90.o.log``). There is a folder per
    compiler and flag combination, and a build may create new ones.

    Called for each file a build finishes, so it doesn't list folders: it
    checks for the names the file's known paths give (one stat per folder).
    Only when that finds nothing (the path isn't known) does it list the
    folders, and each listing is kept until its folder changes."""

    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self._root = project_dir.resolve()
        self.build = project_dir / "build"
        self._folders: tuple[tuple, list[Path]] = ((), [])  # (what they were listed from, the folders)
        self._listings: dict[Path, tuple[int, list[str]]] = {}  # folder -> (its mtime, its log names)
        self.listed = 0  # folders listed (for the tests)

    def folders(self) -> list[Path]:
        """``build/*_*/*``: listed again only when a folder is added or removed."""
        try:
            configs = sorted(e.path for e in os.scandir(self.build) if e.is_dir() and "_" in e.name)
            key = (self.build.stat().st_mtime_ns, *((c, os.stat(c).st_mtime_ns) for c in configs))
        except OSError:
            return []
        if key != self._folders[0]:
            folders = []
            for config in configs:
                try:
                    folders += [Path(e.path) for e in os.scandir(config) if e.is_dir()]
                except OSError:
                    continue
            self._folders = (key, folders)
        return self._folders[1]

    def log_names(self, name: str, paths: list[Path]) -> list[str]:
        """The log names fpm would give a file with this name and these paths."""
        names = [f"{name}.o.log"]
        for path in paths:
            try:
                rel = os.path.relpath(Path(path).resolve(), self._root)
            except ValueError:  # (Windows: another drive)
                continue
            names.append(rel.replace("\\", "_").replace("/", "_") + ".o.log")
        return names

    def find(self, name: str, paths: list[Path], since: float) -> list[tuple[float, Path]]:
        """(modification time, log) of the logs for a source file written
        since ``since`` (the build's start)."""
        folders = self.folders()
        found = []
        for folder in folders:
            for log_name in self.log_names(name, paths):
                found += self._if_new(folder / log_name, since)
        if found:
            return found
        for folder in folders:  # (not found by its path, or that isn't known: any log of that file name)
            for log_name in self._listing(folder):
                if log_name == f"{name}.o.log" or log_name.endswith(f"_{name}.o.log"):
                    found += self._if_new(folder / log_name, since)
        return found

    @staticmethod
    def _if_new(log: Path, since: float) -> list[tuple[float, Path]]:
        try:
            mtime = log.stat().st_mtime
        except OSError:
            return []
        return [(mtime, log)] if mtime >= since else []

    def _listing(self, folder: Path) -> list[str]:
        try:
            mtime = folder.stat().st_mtime_ns
        except OSError:
            return []
        cached = self._listings.get(folder)
        if cached is None or cached[0] != mtime:
            self.listed += 1
            try:
                names = [e.name for e in os.scandir(folder) if e.name.endswith(".o.log")]
            except OSError:
                names = []
            cached = self._listings[folder] = (mtime, names)
        return cached[1]


class TitleIcon(QLabel):
    """A panel's icon, the toolbar's for its command (see `icons.make_icon`),
    drawn in front of the panel's title at the title's height."""

    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.name = name

    def draw(self, color: str, title: QLabel):
        size = round(title.fontMetrics().height() * 1.15)
        self.setPixmap(make_icon(self.name, color).pixmap(QSize(size, size)))
        self.setFixedSize(size, size)


class BuildMonitor(SortableTable, QWidget):
    """Per-file compile status: header with filter chips, a slim progress bar
    and a custom-drawn table."""

    COLUMNS: ClassVar[list[str]] = ["FILE", "PACKAGE", "STATUS", "TIME"]
    # Row clicked: show that file's compiler messages (-1: show all output).
    details_requested = Signal(int)
    # Double-click: open (path, [Diagnostic]) in an editor.
    open_file_requested = Signal(str, list)
    # Right-click → Show Compile Command (source path).
    compile_command_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.targets: list[Target] = []
        self.sources: dict[str, list[tuple[str, Path]]] = {}
        self.package_kinds: dict[str, str] = {}
        self.package_colors = dict(DEFAULT_PACKAGE_COLORS)
        self.diagnostics = DiagnosticParser()
        self.project_dir: Path | None = None
        self._logs: LogFinder | None = None  # (where the compile logs are: see _read_log)
        self.hover_row = -1
        self.selected_row = -1
        self.spinner_angle = 0.0
        self.peak_parallel = 0
        self.filter_status: str | None = None
        self.description = ""
        self._build_started = 0.0  # wall clock, to compare with log mtimes
        self._started = 0.0        # monotonic, for the elapsed time
        self._finished_at: float | None = None
        self._percent = 0
        self._result = ""          # "", "ok", "failed", "stopped", "uptodate"
        self._active = False
        self.is_light = True
        self.palette_colors = PALETTES["light"]

        self.setObjectName("BuildMonitor")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.title_icon = TitleIcon("build")
        self.title = QLabel("Build")
        title_font = self.title.font()
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.subtitle = StatusLabel()
        self.chips: dict[str, QPushButton] = {}
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_icon)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle)
        header.addStretch(1)
        for status in STATUSES:
            chip = QPushButton()
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(f"Show only “{status}” files (click again to show all)")
            chip.clicked.connect(lambda checked, s=status: self.set_filter(s if checked else None))
            chip.hide()
            header.addWidget(chip)
            self.chips[status] = chip

        self.progress = ProgressLine(self)
        self.progress_label = StatusLabel()
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setItemDelegate(RowDelegate(self))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(scaled(COMPACT_ROW_HEIGHT))
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.cellEntered.connect(self._set_hover)
        self.table.viewport().installEventFilter(self)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._context_menu)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            hh.setSectionResizeMode(column, QHeaderView.Fixed)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fit_columns()
        self._init_sorting({
            0: lambda t: t.name.lower(),
            1: lambda t: (t.package.lower(), t.name.lower()),
            2: lambda t: (STATUS_SORT.get(t.status, 9), -t.warnings),
            3: lambda t: t.elapsed(),
        }, descending_first={3})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.table, 1)
        # Shown instead of the table (and its column headers) while it's empty.
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty, 1)

        # Drives the spinner, the live times and the finish flash.
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

        self.refresh_palette()
        self.update_header()

    # ------------------------------------------------------------- styling
    def refresh_palette(self):
        """Follow the application's light/dark palette."""
        self.is_light = app_is_light()
        self.palette_colors = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), self.palette_colors))
        self.empty.setStyleSheet(f"color: {self.palette_colors['muted']};")
        self.title_icon.draw(self.palette_colors["text"], self.title)
        self._style_chips()
        self.update_header()
        self.table.viewport().update()
        self.progress.update()

    def set_package_colors(self, colors: dict[str, str]):
        """Use the dependency graph's colors for the package tags."""
        self.package_colors = {**DEFAULT_PACKAGE_COLORS, **colors}
        self.table.viewport().update()

    def _style_chips(self):
        pal = self.palette_colors
        for status, chip in self.chips.items():
            chip.setStyleSheet(chip_stylesheet(pal, pal["status"][status]))

    # ----------------------------------------------------------- lifecycle
    def start(self, graph: m.DependencyGraph | None, project_dir: Path, description: str = ""):
        self.targets.clear()
        self.selected_row = -1
        self.table.setRowCount(0)
        self.sources = index_sources(graph)
        self.package_kinds = {n: p.kind for n, p in graph.packages.items()} if graph else {}
        self.diagnostics = DiagnosticParser()
        self.project_dir = project_dir
        self._logs = LogFinder(project_dir)  # (fresh for each build: new folders)
        self.description = description
        self.peak_parallel = 0
        self._build_started = time.time()
        self._started = time.monotonic()
        self._finished_at = None
        self._percent = 0
        self._result = ""
        self._active = True
        self.set_filter(None)
        self.progress.set(0, "running")
        self._timer.start()
        self.update_header()

    def reset(self):
        """Back to the empty state (e.g. when another project is opened)."""
        self._timer.stop()
        self.targets.clear()
        self.selected_row = -1
        self.table.setRowCount(0)
        self.project_dir = None
        self.description = ""
        self.peak_parallel = 0
        self._started = 0.0
        self._finished_at = None
        self._percent = 0
        self._result = ""
        self._active = False
        self.set_filter(None)
        self.progress.set(0, "running")
        self.update_header()

    @property
    def percent(self) -> int:
        """How far the build is (0–100)."""
        return self._percent

    def finish(self, exit_code: int, stopped: bool = False):
        if not self._active:
            return
        self._active = False
        self._finished_at = time.monotonic()
        for t in self.targets:
            if t.status == COMPILING:
                # A sibling's failure (or Stop) kills files still compiling.
                t.finished = self._finished_at
                self._set_status(t, ABORTED if (stopped or exit_code != 0) else DONE)
        if stopped:
            self._result = "stopped"
        elif exit_code != 0:
            self._result = "failed"
        elif self._result != "uptodate" or self.targets:
            self._result = "ok"
            self._percent = 100
        self.progress.set(self._percent / 100, {"uptodate": "ok"}.get(self._result, self._result))
        self.update_header()
        self.table.viewport().update()

    # ------------------------------------------------------------- parsing
    def feed_line(self, line: str):
        """Process one complete line of fpm output."""
        if not self._active:
            return
        line = line.rstrip()
        if not line:
            return

        match = PROGRESS_RE.match(line)
        if match:
            percent, name, end = int(match.group(1)), match.group(2).strip(), match.group(3)
            self._percent = max(self._percent, percent)
            self.progress.set(self._percent / 100)
            if end:
                target = self._find(name, running=True) or self._add(name)
                if target.status == COMPILING:
                    target.finished = time.monotonic()
                    self._read_log(target)
                    self._set_status(target, FAILED if end == "failed" or target.errors
                                     else WARNING if target.warnings else DONE)
            else:
                self._add(name)
            self.update_header()
            return

        match = FAILED_OBJECT_RE.search(line)
        if match:
            target = self._find_object(match.group(1))
            if target:
                self._mark_failed(target)
            self.update_header()
            return

        if UP_TO_DATE_RE.search(line):
            self._percent = 100
            self._result = "uptodate"
            self.progress.set(1.0, "ok")
            self.update_header()
            return

        self._parse_diagnostic(line)

    def _parse_diagnostic(self, line: str):
        found = self.diagnostics.feed(line)
        if found:
            target = self._find(Path(found.file).name, running=False)
            if target:
                self._record(target, found)

    def _read_log(self, target: Target):
        """Pick up diagnostics for a finished source file from fpm's log."""
        if target_kind(target.name) != "Source":
            return
        if self.project_dir is None:
            return
        if self._logs is None or self._logs.project_dir != self.project_dir:
            self._logs = LogFinder(self.project_dir)
        logs = self._logs.find(target.name, [path for _package, path in target.candidates], self._build_started)
        if not logs:
            return
        log = max(logs)[1]
        self._resolve_owner(target, log.name)
        try:
            text = read_log_text(log)
        except OSError:
            return
        target.log_text = text  # shown with its colors
        parser = DiagnosticParser()
        for line in strip_ansi(text).splitlines():
            found = parser.feed(line.rstrip())
            if found:
                # Diagnostics in included files still belong to this target.
                self._record(target, found)

    def _resolve_owner(self, target: Target, log_name: str):
        """Several packages may have a file of this name; fpm names the log
        after the source path relative to the project ("/" -> "_"), which
        tells them apart."""
        if len(target.candidates) < 2 or self.project_dir is None:
            return
        for package, path in target.candidates:
            try:
                rel = os.path.relpath(path, self.project_dir)
            except ValueError:
                continue
            if log_name == rel.replace("\\", "_").replace("/", "_") + ".o.log":
                self._set_owner(target, package, path)
                return

    def _record(self, target: Target, diag: Diagnostic):
        if diag.message in target.messages:  # fpm echoes the log on failure
            return
        target.messages.append(diag.message)
        target.diagnostics.append(diag)
        severity = diag.severity
        if severity in ("error", "fatal error"):
            target.errors += 1
            self._mark_failed(target)
        elif severity == "warning":
            target.warnings += 1
            if target.status in (DONE, WARNING):
                self._set_status(target, WARNING)
        self._update_tooltip(target)
        self.update_header()

    # ------------------------------------------------------------- helpers
    def _find(self, name: str, running: bool) -> Target | None:
        """Most suitable row for a file name: the oldest one still compiling,
        otherwise (unless ``running``) the most recent one."""
        for t in self.targets:
            if t.name == name and t.status == COMPILING:
                return t
        if running:
            return None
        for t in reversed(self.targets):
            if t.name == name:
                return t
        return None

    def _find_object(self, obj: str) -> Target | None:
        # fpm names objects after the source path with "/" -> "_", plus ".o".
        stem = obj.removesuffix(".o")
        candidates = [t for t in self.targets if stem == t.name or stem.endswith("_" + t.name)]
        return max(candidates, key=lambda t: len(t.name), default=None)

    def _mark_failed(self, target: Target):
        if target.finished is None:
            target.finished = time.monotonic()
        self._set_status(target, FAILED)

    def _add(self, name: str) -> Target:
        row = self.table.rowCount()
        self.table.insertRow(row)
        target = Target(name, row, seq=len(self.targets))
        self.targets.append(target)
        for column in range(len(self.COLUMNS)):
            self.table.setItem(row, column, QTableWidgetItem())

        kind = target_kind(name)
        self.table.item(row, 0).setText(name)
        self.table.item(row, 0).setToolTip(kind)
        if kind == "Source":
            target.candidates = self.sources.get(name, [])
            if len(target.candidates) == 1:
                self._set_owner(target, *target.candidates[0])
            else:
                names = sorted({p for p, _ in target.candidates})
                target.package = ", ".join(names)
                target.package_kind = self.package_kinds.get(names[0], LIBRARY) if names else LIBRARY
        else:
            target.package = kind.lower()
            target.package_kind = LIBRARY
        self._set_status(target, COMPILING)
        running = sum(1 for t in self.targets if t.status == COMPILING)
        self.peak_parallel = max(self.peak_parallel, running)
        self._apply_filter(target)
        if not self.table.isRowHidden(row):
            self.table.scrollToItem(self.table.item(row, 0))
        self.schedule_sort()
        return target

    def _set_owner(self, target: Target, package: str, path: Path):
        target.package = package
        target.package_kind = self.package_kinds.get(package, LIBRARY)
        target.path = path
        item = self.table.item(target.row, 0)
        item.setData(Qt.UserRole, str(path))
        item.setToolTip(str(path))
        self.table.item(target.row, 1).setText(package)

    def display_folder(self, target: Target) -> str:
        if target.path is None:
            return ""
        folder = target.path.parent
        if self.project_dir is not None:
            try:
                return os.path.relpath(folder, self.project_dir)
            except ValueError:
                pass
        return str(folder)

    def _set_status(self, target: Target, status: str):
        target.status = status
        self.table.item(target.row, 2).setText(status)
        self._apply_filter(target)
        self.table.viewport().update()
        self.schedule_sort()

    # SortableTable hooks
    def rows(self) -> list[Target]:
        return self.targets

    def _sync_row(self, row: int):
        """Rewrite a row's items (text, tooltips, data) from its target."""
        t = self.targets[row]
        file_item = self.table.item(row, 0)
        file_item.setText(t.name)
        file_item.setToolTip(str(t.path) if t.path else target_kind(t.name))
        file_item.setData(Qt.UserRole, str(t.path) if t.path else None)
        self.table.item(row, 1).setText(t.package)
        status_item = self.table.item(row, 2)
        status_item.setText(t.status)
        status_item.setToolTip("")
        self._update_tooltip(t)

    def _reapply_filter(self):
        for t in self.targets:
            self._apply_filter(t)

    def _update_tooltip(self, target: Target):
        if target.messages:
            text = "\n".join(target.messages[:10])
            if len(target.messages) > 10:
                text += f"\n… {len(target.messages) - 10} more"
            self.table.item(target.row, 2).setToolTip(text)

    def slowest(self) -> float:
        return max((t.elapsed() for t in self.targets), default=0.0)

    # ------------------------------------------------------------- display
    def set_filter(self, status: str | None):
        self.filter_status = status
        for s, chip in self.chips.items():
            chip.setChecked(s == status)
        for t in self.targets:
            self._apply_filter(t)

    def _apply_filter(self, target: Target):
        hide = self.filter_status is not None and target.status != self.filter_status
        self.table.setRowHidden(target.row, hide)

    def counts(self) -> dict[str, int]:
        counts = {s: 0 for s in STATUSES}
        for t in self.targets:
            counts[t.status] += 1
        return counts

    def update_header(self):
        pal = self.palette_colors
        parts = [self.description] if self.description else []
        if self.peak_parallel > 1:
            parts.append(f"{self.peak_parallel} parallel")
        self.subtitle.setText(f"<span style='color:{pal['muted']}'>{' · '.join(parts)}</span>")

        counts = self.counts()
        for status, chip in self.chips.items():
            chip.setText(f"{STATUS_ICONS[status]} {counts[status]}")
            chip.setVisible(counts[status] > 0)
        if self.filter_status and counts[self.filter_status] == 0:
            self.set_filter(None)

        end = self._finished_at or time.monotonic()
        seconds = f"{end - self._started:.1f} s" if self._started else ""
        finished = counts[DONE] + counts[WARNING]
        muted = f"<span style='color:{pal['muted']}'>"
        if self._result in ("uptodate", "ok") and not self.targets:  # nothing needed compiling
            text = "<b>Up to date</b>"
        elif self._result == "ok":
            text = f"<b>Finished</b> {muted}· {finished} built · {seconds}</span>"
        elif self._result == "failed":
            text = f"<b>Failed at {self._percent}%</b> {muted}· {seconds}</span>"
        elif self._result == "stopped":
            text = f"<b>Stopped at {self._percent}%</b> {muted}· {seconds}</span>"
        elif self._active:
            text = f"<b>{self._percent}%</b> {muted}· {finished} done · {seconds}</span>"
        else:
            text = f"{muted}Run Build, Run or Test to see progress</span>"
        self.progress_label.setText(text)
        self._update_empty()

    def _update_empty(self):
        """While nothing has been compiled, a message instead of the table."""
        if self.table.rowCount():
            message = ""
        elif self._result == "uptodate" or (self._result == "ok" and not self.targets):
            message = "Nothing needed compiling — the project is up to date."
        elif self._result in ("failed", "stopped"):
            message = "Nothing was compiled — see the output."
        elif self._active:
            message = "Waiting for fpm…"
        else:
            message = "Run Build, Run or Test to see each file fpm compiles here."
        if self.empty.text() != message:
            self.empty.setText(message)
        self.empty.setVisible(bool(message))
        self.table.setVisible(not message)

    def _tick(self):
        self.spinner_angle = (self.spinner_angle + 18) % 360
        now = time.monotonic()
        flashing = any(t.finished is not None and now - t.finished < FLASH_SECONDS for t in self.targets)
        self.table.viewport().update()  # also clears the last flash frame
        self.update_header()
        if not (self._active or flashing):
            self._timer.stop()

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
        """Share the width out without a horizontal scrollbar; drop the
        package column when space is tight (the file tooltip still has it)."""
        width = self.table.viewport().width()
        narrow = width < scaled(520)
        self.table.setColumnHidden(1, narrow)
        self.table.setColumnWidth(1, scaled(130))
        self.table.setColumnWidth(2, scaled(140 if width >= scaled(600) else 128))
        self.table.setColumnWidth(3, scaled(150 if width >= scaled(700) else 112))

    def refresh_fonts(self):
        """Re-apply sizes after the application font changed."""
        title_font = QFont(QApplication.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.table.verticalHeader().setDefaultSectionSize(scaled(COMPACT_ROW_HEIGHT))
        self.refresh_palette()  # the stylesheet has pixel sizes
        self._fit_columns()

    def _row_clicked(self, row: int, _column: int):
        """Click a file to see its compiler messages; click again for all output."""
        if row >= len(self.targets):
            return
        self.selected_row = -1 if self.selected_row == row else row
        self.table.viewport().update()
        self.details_requested.emit(self.selected_row)

    def clear_selection(self):
        if self.selected_row != -1:
            self.selected_row = -1
            self.table.viewport().update()

    def details_title(self, row: int) -> str:
        t = self.targets[row]
        what = {WARNING: f"{t.warnings} warning{'s' * (t.warnings != 1)}",
                FAILED: "errors"}.get(t.status, t.status.lower())
        package = f" ({t.package})" if t.package else ""
        return f"{t.name}{package} — {what}"

    def details_text(self, row: int) -> str:
        """The compiler output for one file, as fpm logged it."""
        t = self.targets[row]
        if t.status == COMPILING:
            return "(still compiling)"
        if t.log_text.strip():
            return t.log_text
        if t.messages:
            return "\n".join(t.messages)
        return "No compiler messages for this file."

    def _open_row(self, row: int, _column: int):
        """Double-click: open the source, at its first error or warning."""
        path = self.table.item(row, 0).data(Qt.UserRole)
        if path:
            self.open_file_requested.emit(path, self.diagnostics_for(row))

    def _context_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if not 0 <= row < len(self.targets):
            return
        target = self.targets[row]
        menu = QMenu(self)
        if target.path is not None:
            path = str(target.path)
            menu.addAction("Show Compile Command…", lambda: self.compile_command_requested.emit(path))
            menu.addAction("Open in Editor",
                           lambda: self.open_file_requested.emit(path, self.diagnostics_for(row)))
        menu.addAction("Show Compiler Messages", lambda: self._row_clicked(row, 0)
                       if self.selected_row != row else None)
        if menu.actions():
            menu.exec(self.table.viewport().mapToGlobal(pos))

    def diagnostics_for(self, row: int) -> list[Diagnostic]:
        """Diagnostics that point into the row's own source file (errors first)."""
        t = self.targets[row]
        own = [d for d in t.diagnostics if Path(d.file).name == t.name]
        return sorted(own, key=lambda d: (d.severity == "warning", d.line))
