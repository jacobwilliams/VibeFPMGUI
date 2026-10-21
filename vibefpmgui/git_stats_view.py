"""The Git panel's *Stats* page (see `vibefpmgui.git_stats`): the
repository's numbers on tiles at the top, its people (their commits, share,
first and latest) on the left, and on the right a plot of each one's commits
so far over time, from the first commit to now (*Total*), or their commits
in each month as stacked bars (*Per month*: the month's commits, by whom,
when the mouse is over it). pyqtgraph: wheel zooms, drag pans, double-click
fits it again. The people with the most commits get a line (a color) each,
the rest one together; choosing people in the list highlights theirs.
"""

from __future__ import annotations

import os
import time
from bisect import bisect_right
from datetime import datetime
from itertools import pairwise
from typing import ClassVar

import qtpy

os.environ.setdefault("PYQTGRAPH_QT_LIB", qtpy.API_NAME)  # (pyqtgraph: qtpy's Qt binding)

import pyqtgraph as pg
from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QBrush, QColor, QCursor, QFont, QPen
from qtpy.QtWidgets import (
    QApplication,
    QButtonGroup,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSplitter,
    QStackedLayout,
    QToolButton,
    QToolTip,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import git_stats as gst
from .build_outputs import human_size

MOST = 8  # people with a line of their own (the rest: "Others", together)
OTHERS = "Others"
TOTAL, MONTHLY = "total", "monthly"  # the plot: commits so far, or each month's (stacked bars)
MODE_TITLES = {TOTAL: "Total", MONTHLY: "Per month"}
MODE_TIPS = {TOTAL: "Each person's commits so far, over time",
             MONTHLY: "Everyone's commits in each month, stacked"}
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


def month_number(seconds: float) -> int:
    """Months since year 0 of a date (local time): consecutive months, consecutive numbers."""
    when = datetime.fromtimestamp(seconds)
    return when.year * 12 + when.month - 1


def month_start(number: int) -> int:
    """When a month (`month_number`) starts (seconds since 1970)."""
    return int(datetime(number // 12, number % 12 + 1, 1).timestamp())


def per_month(stats: gst.RepoStats, now: float) -> tuple[list[int], list[tuple[str, list[int]]]]:
    """(the months' edges: when each starts, and when the last ends; for each
    line of `lines`, its commits in each month), from the first commit's
    month to this one."""
    first, last = month_number(stats.first), month_number(max(now, stats.last))
    edges = [month_start(n) for n in range(first, last + 2)]
    counts = []
    for who, times in lines(stats):
        months = [0] * (last - first + 1)
        for t in times:
            months[month_number(t) - first] += 1
        counts.append((who, months))
    return edges, counts


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
    mode_changed = Signal(str)  # TOTAL or MONTHLY chosen above the plot

    TILES: ClassVar[list[tuple[str, str]]] = [("commits", "Commits"), ("people", "People"), ("age", "Age"), ("latest", "Latest commit"),
             ("recent", "Last 30 days"), ("branches", "Branches"), ("tags", "Tags"), ("files", "Files"),
             ("size", "Size (.git)")]

    def __init__(self, parent=None):
        super().__init__(parent)
        self.stats: gst.RepoStats | None = None
        self.pal: dict = {}
        self.dark = False
        self.curves: dict[str, pg.PlotDataItem | pg.BarGraphItem] = {}  # each line's (or bars'), by whom
        self.colors: dict[str, QColor] = {}
        self.mode = TOTAL
        self.months: tuple[list[int], list[tuple[str, list[int]]]] | None = None  # (Per month's: see per_month)
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
        self.plot.scene().sigMouseMoved.connect(self._hovered)
        self.legend = self.plot.addLegend(offset=(10, 10))
        self.mode_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(self)
        modes = QHBoxLayout()
        modes.setContentsMargins(0, 0, 0, 0)
        modes.setSpacing(2)
        modes.addStretch(1)
        for mode in (TOTAL, MONTHLY):
            button = self.mode_buttons[mode] = QToolButton()
            button.setObjectName("GitView")  # (as the panel's Diff and Stats)
            button.setText(MODE_TITLES[mode])
            button.setToolTip(MODE_TIPS[mode])
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.clicked.connect(lambda _checked=False, m=mode: self.set_mode(m, user=True))
            group.addButton(button)
            modes.addWidget(button)
        self.mode_buttons[TOTAL].setChecked(True)
        plot_side = QWidget()
        plot_layout = QVBoxLayout(plot_side)
        plot_layout.setContentsMargins(0, 0, 0, 0)
        plot_layout.setSpacing(4)
        plot_layout.addLayout(modes)
        plot_layout.addWidget(self.plot, 1)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.people)
        self.splitter.addWidget(plot_side)
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
        self._label()
        self.legend.setLabelTextColor(pal["text"])
        self.legend.setBrush(QColor(pal["bg"]))
        if self.stats is not None:
            self._draw()

    def _label(self):
        self.plot.setLabel("left", "Commits a month" if self.mode == MONTHLY else "Commits",
                           color=self.pal.get("muted"))

    # -------------------------------------------------------------- state
    def set_mode(self, mode: str, user: bool = False):
        """The plot: TOTAL or MONTHLY (``user``: chosen with its buttons: said
        with mode_changed, to be remembered)."""
        mode = mode if mode in MODE_TITLES else TOTAL
        self.mode = mode
        self.mode_buttons[mode].setChecked(True)
        self._label()
        if self.stats is not None:
            self._draw()
        if user:
            self.mode_changed.emit(mode)

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
            "recent": (f"{stats.since(30 * gst.DAY, now):,}",
                       f"commits in the last 30 days; {stats.since(365 * gst.DAY, now):,} in the last year"),
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
        self.curves, self.colors, self.months = {}, {}, None
        palette = LINE_COLORS["dark" if self.dark else "light"]
        shown = lines(stats)
        for n, (who, _times) in enumerate(shown):
            self.colors[who] = QColor(palette[min(n, len(palette) - 1)])
        if self.mode == MONTHLY:
            self._draw_months(now)
        else:
            start, end = stats.first, max(int(now), stats.last)
            for who, times in shown:
                xs, ys = steps(times, start, end)
                self.curves[who] = self.plot.plot(xs, ys, pen=pg.mkPen(self.colors[who], width=2),
                                                  name=f"{who} · {len(times):,}")
        self.plot.enableAutoRange()
        self._highlight()

    def _draw_months(self, now: float):
        """Each month's commits: a bar, its people's parts stacked (the most
        active at the bottom)."""
        edges, counts = self.months = per_month(self.stats, now)
        gap = 0.08  # (of a month: between the bars)
        x0 = [a + (b - a) * gap / 2 for a, b in pairwise(edges)]
        x1 = [b - (b - a) * gap / 2 for a, b in pairwise(edges)]
        bottom = [0] * len(x0)
        for who, months in counts:
            top = [b + c for b, c in zip(bottom, months)]
            bars = pg.BarGraphItem(x0=x0, x1=x1, y0=bottom, y1=top, pen=pg.mkPen(None),
                                   brush=QBrush(self.colors[who]))
            self.plot.addItem(bars)
            self.legend.addItem(bars, f"{who} · {sum(months):,}")
            self.curves[who] = bars
            bottom = top

    def _highlight(self):
        """The chosen people's lines thicker, the others' faint (none chosen: all as they are)."""
        chosen = {item.data(0, PERSON_ROLE) for item in self.people.selectedItems()}
        for who, curve in self.curves.items():
            mine = who in chosen or (who.startswith(OTHERS) and chosen - set(self.curves))
            color = QColor(self.colors[who])
            color.setAlphaF(1.0 if not chosen or mine else 0.25)
            if isinstance(curve, pg.BarGraphItem):
                curve.setOpts(brush=QBrush(color))
            else:
                pen = QPen(color)
                pen.setWidthF(3.5 if chosen and mine else 2)
                pen.setCosmetic(True)
                curve.setPen(pen)

    def month_text(self, x: float) -> str:
        """What a Per month bar holds (the one at ``x``, seconds since 1970): ""
        for none."""
        if self.months is None:
            return ""
        edges, counts = self.months
        index = bisect_right(edges, x) - 1
        if not 0 <= index < len(edges) - 1:
            return ""
        month = datetime.fromtimestamp(edges[index]).strftime("%B %Y")
        parts = [(who, months[index]) for who, months in counts if months[index]]
        total = sum(n for _who, n in parts)
        rows = "".join(f"<br>{who}: {n:,}" for who, n in sorted(parts, key=lambda p: -p[1]))
        return f"<b>{month}</b>: {total:,} commit{'s' * (total != 1)}{rows}"

    def _hovered(self, pos):
        if self.mode != MONTHLY or not self.plot.sceneBoundingRect().contains(pos):
            return
        text = self.month_text(self.plot.getViewBox().mapSceneToView(pos).x())
        if text:
            QToolTip.showText(QCursor.pos(), text, self.plot)
        else:
            QToolTip.hideText()

    def _clicked(self, event):
        if event.double():
            self.plot.enableAutoRange()
