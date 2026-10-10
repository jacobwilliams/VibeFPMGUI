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

The table is drawn by :class:`RowDelegate`: status badges (with a spinner
while compiling), package tags, and per-file timing bars scaled to the slowest
file.  The summary chips above it filter the table by status.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, QTimer, QUrl
from qtpy.QtGui import (
    QColor, QDesktopServices, QFont, QFontDatabase, QFontMetricsF, QLinearGradient, QPainter,
    QPalette, QPen,
)
from qtpy.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import manifest as m

PROGRESS_RE = re.compile(r"^\[\s*(\d+)%\]\s+(\S+)(?:\s+(done|failed)\.)?\s*$")
# gfortran/gcc: "src/a.f90:3:15:" (message on later lines) or "a.c:3:5: error: ..."
GNU_LOC_RE = re.compile(r"^(\S+?):(\d+):(?:(\d+):)?\s*(?:(fatal error|error|warning)\b:?\s*(.*))?$",
                        re.IGNORECASE)
# Intel: "src/a.f90(3): error #5082: ..."
INTEL_LOC_RE = re.compile(r"^(\S+?)\((\d+)\):\s*(error|warning|remark)\b[^:]*:\s*(.*)$", re.IGNORECASE)
GNU_SEVERITY_RE = re.compile(r"^(Fatal Error|Error|Warning):\s*(.*)$")
FAILED_OBJECT_RE = re.compile(r'Compilation failed for object\s+"\s*(.+?)\s*"')
UP_TO_DATE_RE = re.compile(r"Project is up to date")

SOURCE_EXTS = {".f", ".for", ".ftn", ".f77", ".f90", ".f95", ".f03", ".f08", ".fpp",
               ".c", ".cc", ".cpp", ".cxx", ".h", ".inc"}

COMPILING, DONE, WARNING, FAILED, ABORTED = "Compiling", "Done", "Warnings", "Failed", "Aborted"
STATUSES = (COMPILING, DONE, WARNING, FAILED, ABORTED)
STATUS_ICONS = {COMPILING: "⟳", DONE: "✓", WARNING: "⚠", FAILED: "✕", ABORTED: "–"}
LIBRARY = "library"  # package kind used for link targets

PALETTES = {
    "light": dict(bg="#ffffff", text="#1f2328", muted="#8b949e", line="#eaeef2", hover="#f3f6f9",
                  track="#eaeef2", accent1="#4c6ef5", accent2="#15aabf",
                  status={COMPILING: "#e8a317", DONE: "#2f9e44", WARNING: "#e8590c",
                          FAILED: "#e03131", ABORTED: "#868e96"}),
    "dark": dict(bg="#1e1f22", text="#e6e8eb", muted="#8b929b", line="#2f3237", hover="#2a2d31",
                 track="#33363b", accent1="#748ffc", accent2="#3bc9db",
                 status={COMPILING: "#fcc419", DONE: "#51cf66", WARNING: "#ff922b",
                         FAILED: "#ff6b6b", ABORTED: "#868e96"}),
}
# Package-tag dot colours; the main window replaces these with the graph theme's.
DEFAULT_PACKAGE_COLORS = {m.ROOT: "#f6c85f", m.GIT: "#74c0fc", m.PATH: "#8ce99a",
                          m.REGISTRY: "#d7b8f3", m.META: "#f3c6a5", m.MISSING: "#ced4da",
                          LIBRARY: "#ced4da"}

ROW_HEIGHT = 44
FLASH_SECONDS = 0.6  # a row briefly glows in its status colour when it finishes


class DiagnosticParser:
    """Turns compiler output, one line at a time, into
    ``(file, severity, message)`` tuples."""

    def __init__(self):
        self._location: str | None = None  # gfortran prints severity lines later

    def feed(self, line: str) -> tuple[str, str, str] | None:
        match = INTEL_LOC_RE.match(line)
        if match:
            return match.group(1), match.group(3).lower(), line
        match = GNU_LOC_RE.match(line)
        if match and Path(match.group(1)).suffix.lower() in SOURCE_EXTS:
            if match.group(4):  # severity on the same line (gcc/clang style)
                return match.group(1), match.group(4).lower(), line
            self._location = f"{match.group(1)}:{match.group(2)}"
            return None
        match = GNU_SEVERITY_RE.match(line)
        if match and self._location:
            location, self._location = self._location, None
            return location.split(":")[0], match.group(1).lower(), f"{location}: {line}"
        return None


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
    status: str = COMPILING
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    messages: list[str] = field(default_factory=list)
    warnings: int = 0
    errors: int = 0
    candidates: list[tuple[str, Path]] = field(default_factory=list)  # (package, source)
    path: Path | None = None  # resolved source file
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
    for pkg in graph.packages.values():
        if not pkg.directory or not pkg.directory.is_dir() or pkg.kind == m.MISSING:
            continue
        for dirpath, dirnames, filenames in os.walk(pkg.directory):
            # Skip build trees (they hold other packages' sources) and VCS dirs.
            dirnames[:] = [d for d in dirnames if d != "build" and not d.startswith(".")]
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
        {n} QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
        {n} QScrollBar::handle:vertical {{ background: {pal['track']}; border-radius: 3px;
            min-height: 24px; }}
        {n} QScrollBar::handle:vertical:hover {{ background: {pal['muted']}; }}
        {n} QScrollBar::add-line, {n} QScrollBar::sub-line,
        {n} QScrollBar::add-page, {n} QScrollBar::sub-page {{ background: none; height: 0; }}
        {n} QHeaderView::section {{
            background: {pal['bg']}; color: {pal['muted']}; border: none;
            border-bottom: 1px solid {pal['line']}; padding: 6px 12px;
            font-size: 10px; font-weight: 600; }}
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


class RowDelegate(QStyledItemDelegate):
    """Paints every cell of the build table from its :class:`Target`."""

    def __init__(self, monitor: "BuildMonitor"):
        super().__init__(monitor.table)
        self.monitor = monitor

    def sizeHint(self, option, index):
        return QSize(100, ROW_HEIGHT)

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

        if index.row() == mon.hover_row:
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
        folder = self.monitor.display_folder(target)
        p.setFont(bold)
        p.setPen(QColor(pal["text"]))
        text_r = r.adjusted(14, 0, -6, 0)
        name = QFontMetricsF(bold).elidedText(target.name, Qt.ElideMiddle, text_r.width())
        if not folder:
            p.drawText(text_r, Qt.AlignLeft | Qt.AlignVCenter, name)
            return
        half = r.height() / 2
        p.drawText(text_r.adjusted(0, 4, 0, -half + 2), Qt.AlignLeft | Qt.AlignBottom, name)
        p.setFont(small)
        p.setPen(QColor(pal["muted"]))
        folder = QFontMetricsF(small).elidedText(folder, Qt.ElideLeft, text_r.width())
        p.drawText(text_r.adjusted(0, half + 1, 0, -3), Qt.AlignLeft | Qt.AlignTop, folder)

    def _paint_package(self, p, r, target, small, pal):
        if not target.package:
            return
        color = QColor(self.monitor.package_colors.get(target.package_kind, pal["muted"]))
        fm = QFontMetricsF(small)
        text = fm.elidedText(target.package, Qt.ElideRight, r.width() - 46)
        pill = QRectF(r.left() + 8, r.center().y() - 10, fm.horizontalAdvance(text) + 28, 20)
        p.setPen(Qt.NoPen)
        p.setBrush(_tint(color, 50))
        p.drawRoundedRect(pill, 10, 10)
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
        pill = QRectF(r.left() + 8, r.center().y() - 11,
                      QFontMetricsF(font).horizontalAdvance(text) + 36, 22)
        p.setPen(Qt.NoPen)
        p.setBrush(_tint(color, 40))
        p.drawRoundedRect(pill, 11, 11)
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
        if target.status == COMPILING:
            bar_color = color
        elif not self.monitor.running and elapsed >= slowest and len(self.monitor.targets) > 1:
            bar_color = QColor(pal["status"][WARNING])  # the bottleneck, once the build is over
        else:
            bar_color = QColor(pal["muted"])
        fraction = elapsed / slowest if slowest > 0 else 0
        p.setBrush(bar_color)
        p.drawRoundedRect(QRectF(track.left(), track.top(), max(track.width() * fraction, 3), 6), 3, 3)
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(small.pointSizeF())
        p.setFont(mono)
        p.setPen(QColor(pal["text"] if target.status == COMPILING else pal["muted"]))
        p.drawText(r.adjusted(0, 0, -12, 0), Qt.AlignVCenter | Qt.AlignRight, f"{elapsed:.1f} s")


class ProgressLine(QWidget):
    """Thin rounded progress bar with a gradient fill."""

    def __init__(self, monitor: "BuildMonitor"):
        super().__init__()
        self.monitor = monitor
        self.value = 0.0
        self.state = "running"  # running | ok | failed | stopped
        self.setFixedHeight(6)

    def set(self, value: float, state: str | None = None):
        self.value = max(0.0, min(1.0, value))
        if state:
            self.state = state
        self.update()

    def paintEvent(self, _event):
        pal = self.monitor.palette_colors
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(pal["track"]))
        p.drawRoundedRect(r, 3, 3)
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


# ------------------------------------------------------------------ widget
class BuildMonitor(QWidget):
    """Per-file compile status: header with filter chips, a slim progress bar
    and a custom-drawn table."""

    COLUMNS = ["FILE", "PACKAGE", "STATUS", "TIME"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.targets: list[Target] = []
        self.sources: dict[str, list[tuple[str, Path]]] = {}
        self.package_kinds: dict[str, str] = {}
        self.package_colors = dict(DEFAULT_PACKAGE_COLORS)
        self.diagnostics = DiagnosticParser()
        self.project_dir: Path | None = None
        self.hover_row = -1
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

        self.title = QLabel("Build")
        title_font = self.title.font()
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.subtitle = QLabel()
        self.chips: dict[str, QPushButton] = {}
        header = QHBoxLayout()
        header.setSpacing(6)
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
        self.progress_label = QLabel()
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setItemDelegate(RowDelegate(self))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.cellEntered.connect(self._set_hover)
        self.table.viewport().installEventFilter(self)
        self.table.cellDoubleClicked.connect(self._open_row)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            hh.setSectionResizeMode(column, QHeaderView.Fixed)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._fit_columns()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.table, 1)

        # Drives the spinner, the live times and the finish flash.
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)

        self.refresh_palette()
        self._update_header()

    # ------------------------------------------------------------- styling
    def refresh_palette(self):
        """Follow the application's light/dark palette."""
        self.is_light = app_is_light()
        self.palette_colors = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), self.palette_colors))
        self._style_chips()
        self._update_header()
        self.table.viewport().update()
        self.progress.update()

    def set_package_colors(self, colors: dict[str, str]):
        """Use the dependency graph's colours for the package tags."""
        self.package_colors = {**DEFAULT_PACKAGE_COLORS, **colors}
        self.table.viewport().update()

    def _style_chips(self):
        pal = self.palette_colors
        for status, chip in self.chips.items():
            chip.setStyleSheet(chip_stylesheet(pal, pal["status"][status]))

    # ----------------------------------------------------------- lifecycle
    def start(self, graph: m.DependencyGraph | None, project_dir: Path, description: str = ""):
        self.targets.clear()
        self.table.setRowCount(0)
        self.sources = index_sources(graph)
        self.package_kinds = {n: p.kind for n, p in graph.packages.items()} if graph else {}
        self.diagnostics = DiagnosticParser()
        self.project_dir = project_dir
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
        self._update_header()

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
        self._update_header()
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
            self._update_header()
            return

        match = FAILED_OBJECT_RE.search(line)
        if match:
            target = self._find_object(match.group(1))
            if target:
                self._mark_failed(target)
            self._update_header()
            return

        if UP_TO_DATE_RE.search(line):
            self._percent = 100
            self._result = "uptodate"
            self.progress.set(1.0, "ok")
            self._update_header()
            return

        self._parse_diagnostic(line)

    def _parse_diagnostic(self, line: str):
        found = self.diagnostics.feed(line)
        if found:
            target = self._find(Path(found[0]).name, running=False)
            if target:
                self._record(target, found[1], found[2])

    def _read_log(self, target: Target):
        """Pick up diagnostics for a finished source file from fpm's log."""
        if target_kind(target.name) != "Source":
            return
        if self.project_dir is None:
            return
        logs = []
        # Each compiler/flag combination gets its own build/<compiler>_<hash>
        # directory, which may only have been created by this build.
        for d in (self.project_dir / "build").glob("*_*/*"):
            for log in d.glob(f"*{target.name}.o.log"):
                if log.name == f"{target.name}.o.log" or log.name.endswith(f"_{target.name}.o.log"):
                    try:
                        mtime = log.stat().st_mtime
                    except OSError:
                        continue
                    if mtime >= self._build_started:
                        logs.append((mtime, log))
        if not logs:
            return
        log = max(logs)[1]
        self._resolve_owner(target, log.name)
        try:
            text = log.read_text(errors="replace")
        except OSError:
            return
        parser = DiagnosticParser()
        for line in text.splitlines():
            found = parser.feed(line.rstrip())
            if found:
                # Diagnostics in included files still belong to this target.
                self._record(target, found[1], found[2])

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
            if log_name == rel.replace(os.sep, "_") + ".o.log":
                self._set_owner(target, package, path)
                return

    def _record(self, target: Target, severity: str, message: str):
        if message in target.messages:  # fpm echoes the log on failure
            return
        target.messages.append(message)
        if severity in ("error", "fatal error"):
            target.errors += 1
            self._mark_failed(target)
        elif severity == "warning":
            target.warnings += 1
            if target.status in (DONE, WARNING):
                self._set_status(target, WARNING)
        self._update_tooltip(target)
        self._update_header()

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
        stem = obj[:-2] if obj.endswith(".o") else obj
        candidates = [t for t in self.targets if stem == t.name or stem.endswith("_" + t.name)]
        return max(candidates, key=lambda t: len(t.name), default=None)

    def _mark_failed(self, target: Target):
        if target.finished is None:
            target.finished = time.monotonic()
        self._set_status(target, FAILED)

    def _add(self, name: str) -> Target:
        row = self.table.rowCount()
        self.table.insertRow(row)
        target = Target(name, row)
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

    def _update_tooltip(self, target: Target):
        if target.messages:
            text = "\n".join(target.messages[:10])
            if len(target.messages) > 10:
                text += f"\n… {len(target.messages) - 10} more"
            self.table.item(target.row, 2).setToolTip(text)

    @property
    def running(self) -> bool:
        return self._active

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

    def summary_text(self) -> str:
        """Plain-text summary (also used by tests)."""
        counts = self.counts()
        return " · ".join(f"{n} {s.lower()}" for s, n in counts.items() if n)

    def _update_header(self):
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
        if self._result == "uptodate" and not self.targets:
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

    def _tick(self):
        self.spinner_angle = (self.spinner_angle + 18) % 360
        now = time.monotonic()
        flashing = any(t.finished is not None and now - t.finished < FLASH_SECONDS for t in self.targets)
        self.table.viewport().update()  # also clears the last flash frame
        self._update_header()
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
        narrow = width < 520
        self.table.setColumnHidden(1, narrow)
        self.table.setColumnWidth(1, 130)
        self.table.setColumnWidth(2, 140 if width >= 600 else 128)
        self.table.setColumnWidth(3, 150 if width >= 700 else 112)

    def _open_row(self, row: int, _column: int):
        path = self.table.item(row, 0).data(Qt.UserRole)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
