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
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from qtpy.QtCore import Qt, QTimer, QUrl
from qtpy.QtGui import QBrush, QColor, QDesktopServices
from qtpy.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QProgressBar, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
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
STATUS_COLORS = {
    COMPILING: "#ffe08a",
    DONE: "#b2f2bb",
    WARNING: "#ffd8a8",
    FAILED: "#ffa8a8",
    ABORTED: "#dee2e6",
}


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


class BuildMonitor(QWidget):
    """Table + progress bar showing per-file compile status."""

    COLUMNS = ["File", "Package", "Status", "Time (s)"]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.targets: list[Target] = []
        self.sources: dict[str, list[tuple[str, Path]]] = {}
        self.diagnostics = DiagnosticParser()
        self.project_dir: Path | None = None
        self._build_started = 0.0
        self._active = False

        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Build status</b>"))
        header.addStretch(1)
        self.summary = QLabel("")
        header.addWidget(self.summary)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setAlternatingRowColors(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for col in range(1, len(self.COLUMNS)):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._open_row)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(header)
        layout.addWidget(self.progress)
        layout.addWidget(self.table)

        self._timer = QTimer(self)
        self._timer.setInterval(200)
        self._timer.timeout.connect(self._tick)

    # ----------------------------------------------------------- lifecycle
    def start(self, graph: m.DependencyGraph | None, project_dir: Path):
        self.targets.clear()
        self.table.setRowCount(0)
        self.sources = index_sources(graph)
        self.diagnostics = DiagnosticParser()
        self.project_dir = project_dir
        self._build_started = time.time()
        self._active = True
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self._timer.start()
        self._update_summary()

    def finish(self, exit_code: int, stopped: bool = False):
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        for t in self.targets:
            if t.status == COMPILING:
                # A sibling's failure (or Stop) kills files still compiling.
                self._set_status(t, ABORTED if (stopped or exit_code != 0) else DONE)
        if exit_code == 0 and not stopped:
            self.progress.setValue(100)
            if not self.targets:
                self.progress.setFormat("Up to date")
        elif stopped:
            self.progress.setFormat("Stopped at %p%")
        else:
            self.progress.setFormat("Failed at %p%")
        self._tick()
        self._update_summary()

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
            self.progress.setValue(max(self.progress.value(), percent))
            if end:
                target = self._find(name, running=True) or self._add(name)
                if target.status == COMPILING:
                    target.finished = time.monotonic()
                    self._read_log(target)
                    self._set_status(target, FAILED if end == "failed" or target.errors
                                     else WARNING if target.warnings else DONE)
            else:
                self._add(name)
            self._update_summary()
            return

        match = FAILED_OBJECT_RE.search(line)
        if match:
            target = self._find_object(match.group(1))
            if target:
                self._mark_failed(target)
            self._update_summary()
            return

        if UP_TO_DATE_RE.search(line):
            self.progress.setValue(100)
            self.progress.setFormat("Up to date")
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
                self.table.item(target.row, 1).setText(package)
                file_item = self.table.item(target.row, 0)
                file_item.setData(Qt.UserRole, str(path))
                file_item.setToolTip(str(path))
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
        self._update_summary()

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

        kind = target_kind(name)
        file_item = QTableWidgetItem(name)
        file_item.setToolTip(kind)
        package = ""
        if kind == "Source":
            owners = self.sources.get(name, [])
            target.candidates = owners
            package = ", ".join(sorted({p for p, _ in owners}))
            if len(owners) == 1:
                file_item.setData(Qt.UserRole, str(owners[0][1]))
                file_item.setToolTip(str(owners[0][1]))
        else:
            package = kind.lower()
        time_item = QTableWidgetItem("0.0")
        time_item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.table.setItem(row, 0, file_item)
        self.table.setItem(row, 1, QTableWidgetItem(package))
        self.table.setItem(row, 2, QTableWidgetItem())
        self.table.setItem(row, 3, time_item)
        self._set_status(target, COMPILING)
        self.table.scrollToItem(file_item)
        return target

    def _set_status(self, target: Target, status: str):
        target.status = status
        item = self.table.item(target.row, 2)
        item.setText(status if status != WARNING else f"Warnings ({target.warnings})")
        brush = QBrush(QColor(STATUS_COLORS[status]))
        for col in range(len(self.COLUMNS)):
            cell = self.table.item(target.row, col)
            cell.setBackground(brush)
            cell.setForeground(QBrush(QColor("#1b1b1b")))
        self._update_time(target)

    def _update_tooltip(self, target: Target):
        if target.messages:
            text = "\n".join(target.messages[:10])
            if len(target.messages) > 10:
                text += f"\n… {len(target.messages) - 10} more"
            self.table.item(target.row, 2).setToolTip(text)

    def _update_time(self, target: Target):
        self.table.item(target.row, 3).setText(f"{target.elapsed():.1f}")

    def _tick(self):
        for t in self.targets:
            if t.status == COMPILING:
                self._update_time(t)

    def _update_summary(self):
        counts = {s: 0 for s in STATUS_COLORS}
        for t in self.targets:
            counts[t.status] += 1
        parts = [f"{counts[COMPILING]} compiling", f"{counts[DONE] + counts[WARNING]} done"]
        if counts[WARNING]:
            parts.append(f"{counts[WARNING]} with warnings")
        if counts[FAILED]:
            parts.append(f"{counts[FAILED]} failed")
        if counts[ABORTED]:
            parts.append(f"{counts[ABORTED]} aborted")
        self.summary.setText(" · ".join(parts) if self.targets else "")

    def _open_row(self, row: int, _column: int):
        path = self.table.item(row, 0).data(Qt.UserRole)
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
