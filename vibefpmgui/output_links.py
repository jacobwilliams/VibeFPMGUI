"""Clickable file locations in the Output panel.

Runtime errors and compiler messages name where they happened:
gfortran's "At line 1753 of file build/dependencies/slsqp/src/slsqp_core.f90",
its backtraces' "… at src/foo.f90:12", and compiler messages'
"src/foo.f90:12:5:". `OutputLinks` underlines those whose file exists
(relative paths are the project's, where fpm runs) and opens the file at
that line when one is clicked.
"""

from __future__ import annotations

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
]
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
        return path if self._exists[path] else None

    def locations(self, text: str) -> list[Location]:
        """The locations in one line of output whose files exist."""
        if ":" not in text and "(" not in text and "At line" not in text:
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
                found.append(Location(match.start(), match.end(), path, line, column))
        return found

    def forget(self):
        """Look again whether files exist (a new command's output)."""
        self._exists.clear()

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
