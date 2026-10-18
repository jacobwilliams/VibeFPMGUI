"""Clickable file locations in the Output panel (and the Debug panel's console).

Runtime errors and compiler messages name where they happened:
gfortran's "At line 1753 of file build/dependencies/slsqp/src/slsqp_core.f90",
its backtraces' "… at src/foo.f90:12", compiler messages' "src/foo.f90:12:5:",
ifx's traceback rows ("test  00007FF7010C7F25  ROUTINE  1266  x.f90"), and
gdb's "Breakpoint 3 at 0x…: file test/x.f90, line 155." and "No compiled
code for line 187 in file "/x/y.f90"." in the Debug panel.
`OutputLinks` underlines those whose file exists (relative paths are the
project's, where fpm runs; a bare file name, as ifx prints, is looked for in
the project and its dependencies) and opens the file at that line when one
is clicked.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from qtpy.QtCore import QEvent, QObject, QPoint, Qt, Signal
from qtpy.QtGui import QPalette, QSyntaxHighlighter, QTextCharFormat, QTextCursor
from qtpy.QtWidgets import QPlainTextEdit, QToolTip

_EXTENSIONS = r"f|for|ftn|f77|f90|f95|f03|f08|f18|fpp|fypp|inc|c|h|cc|cpp|cxx|hpp"
LOCATION_RES = [
    # gfortran at run time: "At line 1753 of file src/x.f90" (then maybe " (unit = 6, …)").
    re.compile(r"At line (?P<line>\d+) of file (?P<path>[^\s()]+)"),
    # Compiler messages and backtraces: "src/x.f90:12:5:", "at /abs/x.f90:12", "x.f90(12)" (Intel).
    re.compile(r"(?P<path>(?:[A-Za-z]:[\\/])?[^\s:'\"()<>]*\.(?i:" + _EXTENSIONS + r"))"
               r"(?::(?P<line2>\d+)(?::(?P<col>\d+))?|\((?P<line3>\d+)\))"),
    # gdb (the Debug panel's console): "Breakpoint 3 at 0x…: file test/x.f90, line 155."
    re.compile(r"\bfile (?P<path>[^\s,'\"()<>]+\.(?i:" + _EXTENSIONS + r")), line (?P<line>\d+)"),
    # gdb, a breakpoint on a line with no code: 'No compiled code for line 187 in file "/x/y.f90".'
    re.compile(r"\bline (?P<line>\d+) in file \"(?P<path>[^\"]+\.(?i:" + _EXTENSIONS + r"))\""),
    # ifx's traceback (image, PC, routine, line, source): "x  00007FF7010C7F25  ROUTINE  1266  x.f90".
    # The link is the line and the source.
    re.compile(r"^\s*\S+\s+[0-9A-Fa-f]{8,16}\s+\S+\s+(?P<link>(?P<line>\d+)\s+"
               r"(?P<path>[^\s:'\"()<>]+\.(?i:" + _EXTENSIONS + r")))\s*$"),
]
SKIPPED_FOLDERS = {".git", ".hg", ".svn", "__pycache__", "node_modules"}  # (when looking for a bare name)
CACHE_LIMIT = 5000


@dataclass(frozen=True)
class Location:
    start: int   # (in the line of output)
    end: int
    path: Path
    line: int
    column: int  # 0-based


class LinkHighlighter(QSyntaxHighlighter):
    """Underlines the locations in each line of output."""

    def __init__(self, links: OutputLinks):
        super().__init__(links.view.document())
        self.links = links

    def highlightBlock(self, text: str):
        found = self.links.locations(text)
        if not found:
            return
        fmt = QTextCharFormat()
        fmt.setForeground(self.links.view.palette().color(QPalette.Link))
        fmt.setFontUnderline(True)
        for loc in found:
            self.setFormat(loc.start, loc.end - loc.start, fmt)


class OutputLinks(QObject):
    """Makes the file locations in ``view`` clickable; ``base`` gives the
    folder relative paths are in (None: only absolute ones)."""

    open_requested = Signal(str, int, int)  # path, line (1-based), column (0-based)

    def __init__(self, view: QPlainTextEdit, base: Callable[[], Path | None]):
        super().__init__(view)
        self.view, self.base = view, base
        self._exists: dict[Path, bool] = {}
        self._by_name: dict[str, list[Path]] | None = None  # the project's files by name (see find_by_name)
        self._press: QPoint | None = None
        self.highlighter = LinkHighlighter(self)
        view.viewport().setMouseTracking(True)
        view.viewport().installEventFilter(self)

    # ------------------------------------------------------------ finding
    def resolve(self, text: str) -> Path | None:
        path = Path(text)
        if not path.is_absolute():
            base = self.base()
            if base is None:
                return None
            path = base / path
        if path not in self._exists:
            if len(self._exists) > CACHE_LIMIT:
                self._exists.clear()
            self._exists[path] = path.is_file()
        if self._exists[path]:
            return path
        if len(Path(text).parts) == 1:  # (a bare name, as ifx prints: the project's file of that name)
            return self.find_by_name(text)
        return None

    def find_by_name(self, name: str) -> Path | None:
        """The project's file called ``name`` (its own before a dependency's;
        the name's case only matters if two differ just by it), or None."""
        if self._by_name is None:
            self._by_name = self._index()
        found = self._by_name.get(name.lower(), [])
        exact = [p for p in found if p.name == name] or found
        return exact[0] if exact else None

    def _index(self) -> dict[str, list[Path]]:
        """The project's files by lowercase name: its own first, then the
        dependencies' (build/dependencies); not the rest of the build folder."""
        base = self.base()
        index: dict[str, list[Path]] = {}
        if base is None or not base.is_dir():
            return index
        own, deps = [], []
        for folder, dirs, files in os.walk(base):
            here = Path(folder)
            relative = here.relative_to(base).parts
            in_build = bool(relative) and relative[0] == "build"
            dirs[:] = sorted(d for d in dirs if not d.startswith(".") and d not in SKIPPED_FOLDERS
                             and (not in_build or len(relative) > 1 or d == "dependencies"))
            (deps if in_build else own).extend(here / f for f in sorted(files))
        for path in own + deps:
            index.setdefault(path.name.lower(), []).append(path)
        return index

    def locations(self, text: str) -> list[Location]:
        """The locations in one line of output whose files exist."""
        if "." not in text:  # (every location names a file: name.ext)
            return []
        found: list[Location] = []
        for pattern in LOCATION_RES:
            for match in pattern.finditer(text):
                if any(loc.start < match.end() and match.start() < loc.end for loc in found):
                    continue  # (already part of one)
                path = self.resolve(match.group("path"))
                if path is None:
                    continue
                groups = match.groupdict()
                line = int(groups.get("line") or groups.get("line2") or groups.get("line3") or 1)
                column = max(int(groups.get("col") or 1) - 1, 0)
                start, end = match.span("link") if groups.get("link") else match.span()
                found.append(Location(start, end, path, line, column))
        return found

    def forget(self):
        """Look again whether files exist (a new command's output)."""
        self._exists.clear()
        self._by_name = None

    def location_at(self, pos: QPoint) -> Location | None:
        """The location under the mouse, if any (``pos``: in the viewport)."""
        cursor = self.view.cursorForPosition(pos)
        column = cursor.positionInBlock()
        for loc in self.locations(cursor.block().text()):
            if loc.start <= column <= loc.end:
                end = QTextCursor(cursor.block())
                end.setPosition(cursor.block().position() + loc.end)
                if pos.x() <= self.view.cursorRect(end).right():  # (not past the end of the line)
                    return loc
        return None

    # ------------------------------------------------------------ the mouse
    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.MouseMove:
            over = event.buttons() == Qt.NoButton and self.location_at(event.position().toPoint())
            self.view.viewport().setCursor(Qt.PointingHandCursor if over else Qt.IBeamCursor)
        elif kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            self._press = event.position().toPoint()
        elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and self._press is not None:
            pos, self._press = event.position().toPoint(), None
            if not self.view.textCursor().hasSelection():  # (not the end of selecting text)
                loc = self.location_at(pos)
                if loc is not None:
                    self.open_requested.emit(str(loc.path), loc.line, loc.column)
        elif kind == QEvent.ToolTip:
            loc = self.location_at(event.pos())
            if loc is not None:
                QToolTip.showText(event.globalPos(), f"Open {loc.path.name} at line {loc.line}", self.view)
            else:
                QToolTip.hideText()
            return True
        return False
