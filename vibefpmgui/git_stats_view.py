"""The Git panel's *Stats* page (see `vibefpmgui.git_stats`): the
repository's numbers on tiles at the top, its people (their commits, share,
first and latest) on the left, and on the right a plot of each one's commits
so far over time, from the first commit to now (pyqtgraph: wheel zooms, drag
pans, double-click fits it again). The people with the most commits get a
line each, the rest one line together; choosing people in the list
highlights theirs.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import qtpy

os.environ.setdefault("PYQTGRAPH_QT_LIB", qtpy.API_NAME)  # (pyqtgraph: qtpy's Qt binding)

import pyqtgraph as pg
from qtpy.QtCore import Qt
from qtpy.QtGui import QColor, QFont, QPen
from qtpy.QtWidgets import (
    QApplication,
    QGridLayout,
    QLabel,
    QSplitter,
    QStackedLayout,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import git_stats as gst
from .build_outputs import human_size

MOST = 8  # people with a line of their own (the rest: "Others", together)
OTHERS = "Others"
PERSON_ROLE = Qt.UserRole + 1  # a person's row: their name
LINE_COLORS = {
    "light": ["#4c6ef5", "#e8590c", "#2f9e44", "#ae3ec9", "#e03131", "#1098ad", "#f08c00", "#5c940d", "#868e96"],
    "dark": ["#748ffc", "#ff922b", "#51cf66", "#da77f2", "#ff6b6b", "#3bc9db", "#fcc419", "#94d82d", "#adb5bd"],
}


def date_words(seconds: int) -> str:
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d")


def steps(times: list[int], start: int, end: int) -> tuple[list[float], list[float]]:
    """A staircase of commits so far: 0 at ``start``, one more at each commit,
    level to ``end``."""
    xs, ys = [float(start)], [0.0]
    for n, t in enumerate(times, 1):
        xs += [t, t]
        ys += [n - 1, n]
    xs.append(float(max(end, times[-1] if times else start)))
    ys.append(float(len(times)))
    return xs, ys


def lines(stats: gst.RepoStats) -> list[tuple[str, list[int]]]:
    """(who, their commits' dates) for each line: the people with the most
    commits, then the rest together."""
    shown = [(a.name, a.times) for a in stats.authors[:MOST]]
    rest = sorted(t for a in stats.authors[MOST:] for t in a.times)
    if rest:
        shown.append((f"{OTHERS} ({len(stats.authors) - MOST} people)", rest))
    return shown


class _Tile(QWidget):
    """A number, and what it is under it."""

    def __init__(self, caption: str):
        super().__init__()
        self.value = QLabel()
        self.caption = QLabel(caption)
        self.setObjectName("StatTile")
        self.setAttribute(Qt.WA_StyledBackground, True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(0)
        layout.addWidget(self.value)
        layout.addWidget(self.caption)


class GitStatsView(QWidget):
    TILES = [("commits", "Commits"), ("people", "People"), ("age", "Age"), ("latest", "Latest commit"),
             ("recent", "Last 30 days"), ("branches", "Branches"), ("tags", "Tags"), ("files", "Files"),
             ("size", "Size (.git)")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stats: gst.RepoStats | None = None
        self.pal: dict = {}
        self.dark = False
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.tiles = {key: _Tile(caption) for key, caption in self.TILES}
        tiles = QWidget()
        grid = QGridLayout(tiles)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        for n, (key, _caption) in enumerate(self.TILES):
            grid.addWidget(self.tiles[key], 0, n)

        self.people = QTreeWidget()
        self.people.setHeaderLabels(["Person", "Commits", "Share", "First", "Latest"])
        self.people.setRootIsDecorated(False)
        self.people.setUniformRowHeights(True)
        self.people.setSortingEnabled(False)
        self.people.setSelectionMode(QTreeWidget.ExtendedSelection)
        self.people.itemSelectionChanged.connect(self._highlight)

        self.plot = pg.PlotWidget(axisItems={"bottom": pg.DateAxisItem()})
        self.plot.setMenuEnabled(False)
        self.plot.hideButtons()
        self.plot.showGrid(x=True, y=True, alpha=0.15)
        self.plot.setLabel("left", "Commits")
        self.plot.getViewBox().setMouseEnabled(x=True, y=True)
        self.plot.scene().sigMouseClicked.connect(self._clicked)
        self.legend = self.plot.addLegend(offset=(10, 10))

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.people)
        self.splitter.addWidget(self.plot)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(tiles)
        layout.addWidget(self.splitter, 1)
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.stack = QStackedLayout(self)
        self.stack.addWidget(self.message)
        self.stack.addWidget(content)
        self.content = content
        self.show_message("Reading the repository…")

    # ------------------------------------------------------------ styling
    def refresh_palette(self, pal: dict, dark: bool):
        """The panel's colors (`build_monitor.PALETTES`)."""
        self.pal, self.dark = pal, dark
        big = QFont(QApplication.font())
        big.setPointSizeF(big.pointSizeF() + 4)
        big.setWeight(QFont.Bold)
        for tile in self.tiles.values():
            tile.value.setFont(big)
            tile.caption.setStyleSheet(f"color: {pal['muted']};")
        self.setStyleSheet(f"#StatTile {{ background: {pal['hover']}; border-radius: 8px; }}"
                           f"QTreeWidget {{ background: {pal['bg']}; border: 1px solid {pal['line']};"
                           " border-radius: 6px; }")
        self.message.setStyleSheet(f"color: {pal['muted']};")
        self.plot.setBackground(pal["bg"])
        for name in ("left", "bottom"):
            axis = self.plot.getAxis(name)
            axis.setPen(pal["muted"])
            axis.setTextPen(pal["muted"])
        self.plot.setLabel("left", "Commits", color=pal["muted"])
        self.legend.setLabelTextColor(pal["text"])
        self.legend.setBrush(QColor(pal["bg"]))
        if self.stats is not None:
            self._draw()

    # -------------------------------------------------------------- state
    def show_message(self, text: str):
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def show_stats(self, stats: gst.RepoStats, now: float | None = None):
        """A repository's statistics."""
        self.stats = stats
        now = time.time() if now is None else now
        total = stats.commits
        values = {
            "commits": (f"{total:,}", f"{stats.merges:,} of them merges"),
            "people": (f"{len(stats.authors):,}", "who made commits (by name)"),
            "age": (gst.span_words(now - stats.first), f"the first commit: {date_words(stats.first)}"),
            "latest": (date_words(stats.last), f"{gst.span_words(now - stats.last)} ago"),
            "recent": (f"{stats.since(30 * gst.DAY, now):,}", "commits in the last 30 days; "
                       f"{stats.since(365 * gst.DAY, now):,} in the last year"),
            "branches": (f"{stats.branches:,}", f"local; {stats.remote_branches:,} remote"),
            "tags": (f"{stats.tags:,}", "tags"),
            "files": (f"{stats.files:,}", "files tracked by git"),
            "size": (human_size(stats.size), "the repository's history on disk"),
        }
        for key, (value, tip) in values.items():
            self.tiles[key].value.setText(value)
            self.tiles[key].setToolTip(tip)

        self.people.clear()
        for author in stats.authors:
            item = QTreeWidgetItem([author.name, f"{author.commits:,}", f"{100 * author.commits / total:.1f}%",
                                    date_words(author.first), date_words(author.last)])
            item.setData(0, PERSON_ROLE, author.name)
            item.setToolTip(0, f"{author.name} <{author.email}>")
            for column in (1, 2):
                item.setTextAlignment(column, Qt.AlignRight | Qt.AlignVCenter)
            self.people.addTopLevelItem(item)
        for column in range(5):
            self.people.resizeColumnToContents(column)
        self.people.setColumnWidth(0, min(self.people.columnWidth(0), 260))  # (a very long name: cut)
        wanted = sum(self.people.columnWidth(c) for c in range(5)) + 24  # (all the columns, and a scroll bar)
        sizes = self.splitter.sizes()
        if sizes[0] < wanted:
            total = sum(sizes) if sum(sizes) > wanted else max(self.width(), wanted + 400)
            self.splitter.setSizes([wanted, max(total - wanted, 200)])
        self._draw(now)
        self.stack.setCurrentWidget(self.content)

    def _draw(self, now: float | None = None):
        stats = self.stats
        now = time.time() if now is None else now
        self.plot.clear()
        self.legend.clear()
        self.curves = {}
        colors = LINE_COLORS["dark" if self.dark else "light"]
        start, end = stats.first, max(int(now), stats.last)
        for n, (who, times) in enumerate(lines(stats)):
            xs, ys = steps(times, start, end)
            curve = self.plot.plot(xs, ys, pen=pg.mkPen(colors[min(n, len(colors) - 1)], width=2),
                                   name=f"{who} · {len(times):,}")
            self.curves[who] = curve
        self.plot.enableAutoRange()
        self._highlight()

    def _highlight(self):
        """The chosen people's lines thicker, the others' faint (none chosen: all as they are)."""
        chosen = {item.data(0, PERSON_ROLE) for item in self.people.selectedItems()}
        for who, curve in self.curves.items():
            pen = QPen(curve.opts["pen"])
            mine = who in chosen or (who.startswith(OTHERS) and chosen - set(self.curves))
            pen.setWidthF(3.5 if chosen and mine else 2)
            color = QColor(pen.color())
            color.setAlphaF(1.0 if not chosen or mine else 0.25)
            pen.setColor(color)
            curve.setPen(pen)

    def _clicked(self, event):
        if event.double():
            self.plot.enableAutoRange()
