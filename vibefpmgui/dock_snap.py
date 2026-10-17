"""Dragging a panel's edge past its smallest size collapses it, as in VS Code.

Qt stops a dock separator at the panels' minimum sizes, so `SnapClosed`
does separator drags itself (they reach the main window, not a child
widget). Once the mouse is more than half a panel's minimum size past where
it would stop, the panel collapses to a thin `Grip`; dragging the grip (or
the separator beside it) back out opens it, and so does double-clicking it.
At the edge of a side of the window the whole side collapses; between two
panels, just the squeezed one, with the tabs stacked with it.

A command showing a collapsed panel (a test run showing Tests…), View →
Panels and Reset Layout open it too.
"""

from __future__ import annotations

from dataclasses import dataclass

from qtpy.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt, QTimer
from qtpy.QtWidgets import QDockWidget, QMainWindow, QStyle, QTabBar, QWidget

SLACK = 3  # (pixels around a separator still counted as on it)
SEPARATOR_NAME = "qt_qmainwindow_extended_splitter"  # (Qt's widget over a thin separator)
GRIP = 7  # a collapsed panel's thickness


class _NoTitle(QWidget):
    """A collapsed panel's title bar: nothing."""

    def sizeHint(self):
        return QSize(0, 0)

    def minimumSizeHint(self):
        return QSize(0, 0)


class Grip(QWidget):
    """A collapsed panel's content: a thin strip with the resize cursor (the
    style draws the dots on the separator beside it). Not its title bar:
    with no content, Qt keeps a panel only as big as its title, in a corner."""

    def __init__(self, snap: SnapClosed, side: Side):
        super().__init__()
        self.snap, self.side = snap, side
        self.setCursor(Qt.SplitHCursor if side.horizontal else Qt.SplitVCursor)
        self.setToolTip("Drag to open (or double-click)")

    def sizeHint(self):
        return QSize(GRIP, GRIP)

    def minimumSizeHint(self):
        return QSize(GRIP, GRIP)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            # The window takes the rest of the drag: the grip goes once the panel opens.
            window = self.snap.window
            self.snap.start_drag([self.side], self.mapTo(window, event.position().toPoint()))
            window.grabMouse(self.cursor())
        event.accept()  # (not the panel's: it would float it)

    def mouseDoubleClickEvent(self, event):
        self.snap.expand(self.side.docks[0])


@dataclass(eq=False)
class Side:
    """The panels on one side of a separator: what collapses together."""
    docks: list[QDockWidget]       # the ones in front along the separator
    group: list[QDockWidget]       # and every one stacked with them
    size: int                      # their extent across the separator (at the press, or before collapsing)
    minimum: int
    sign: int                      # +1: the mouse moving along the axis makes them bigger
    horizontal: bool               # (the drag's axis: a vertical separator moves sideways)
    press_size: int = 0            # (during a drag: the size at the press)
    cross: list[int] | None = None  # the docks' sizes along the separator, before collapsing

    def size_at(self, delta: int) -> int:
        return self.press_size + self.sign * delta


def along(rect, horizontal: bool) -> tuple[int, int, int, int]:
    """A rectangle's start and end along a drag, then across it."""
    return ((rect.left(), rect.right(), rect.top(), rect.bottom()) if horizontal
            else (rect.top(), rect.bottom(), rect.left(), rect.right()))


def touching(rect, pos: QPoint, before: bool, horizontal: bool, gap: int) -> bool:
    """Whether ``rect`` ends just before ``pos`` (``before``) or starts
    just after it, along the drag, and reaches it across."""
    start, end, low, high = along(rect, horizontal)
    p, q = (pos.x(), pos.y()) if horizontal else (pos.y(), pos.x())
    distance = p - end if before else start - p
    return 0 <= distance <= gap and low - gap <= q <= high + gap


class SnapClosed(QObject):
    def __init__(self, window: QMainWindow, docks: list[QDockWidget]):
        super().__init__(window)
        self.window = window
        self.docks = docks
        self.collapsed: list[Side] = []
        self.contents: dict[QDockWidget, QWidget] = {}  # a collapsed panel's content (its widget: the grip)
        self._press: QPoint | None = None
        self._sides: list[Side] = []
        for dock in docks:
            dock.toggleViewAction().toggled.connect(lambda on, d=dock: on and self.expand(d))
        window.installEventFilter(self)

    # ------------------------------------------------------------ finding
    def _gap(self) -> int:
        return self.window.style().pixelMetric(QStyle.PM_DockWidgetSeparatorExtent, None, self.window) + SLACK

    def _front(self) -> list[QDockWidget]:
        """The docked panels on view (behind a tab, or collapsed, one is
        "visible" but moved out of the window)."""
        rect = self.window.rect()
        return [d for d in self.docks if d.isVisible() and not d.isFloating() and rect.intersects(d.geometry())]

    def _tab_bars(self) -> list[QTabBar]:
        return [bar for bar in self.window.findChildren(QTabBar) if bar.parent() is self.window and bar.isVisible()]

    def _rect(self, widget: QWidget, tab_bars: list) -> QRect:
        """Where a panel is, with its stack's tab bar (which sits between it
        and the separator when the tabs are on that side)."""
        rect = widget.geometry()
        if isinstance(widget, QDockWidget):
            for bar in tab_bars:
                other = bar.geometry()
                # Its own stack's: starting where it starts and within it, along
                # its edge (tabs above or below it), or down its side (tabs beside
                # it). (Not another stack's that only touches it: with the macOS
                # style's thin separators, the bottom panels' tab bar touched the
                # right side's, which then reached across the window.)
                lined_up = (abs(other.left() - rect.left()) <= 4 and other.right() <= rect.right() + 4) or \
                    (abs(other.top() - rect.top()) <= 4 and other.bottom() <= rect.bottom() + 4)
                if lined_up and other.adjusted(-2, -2, 2, 2).intersects(rect):
                    rect = rect.united(other)
        return rect

    def _on_separator(self, pos: QPoint) -> bool:
        """No widget there but the window, or one of the invisible widgets Qt
        puts over thin separators to catch the mouse (the macOS style's are
        1 pixel)."""
        widget = self.window.childAt(pos)
        return widget is None or widget.objectName() == SEPARATOR_NAME

    def _sides_at(self, pos: QPoint) -> list[Side]:
        """The panels on each side of the separator at ``pos`` that could collapse."""
        gap, tab_bars = self._gap(), self._tab_bars()
        # A collapsed side: its separator is next to its grips. (Not "next to
        # what's beyond it": the right side's panels are beyond the left side,
        # and their edges are at the right side's separator too.)
        for side in self.collapsed:
            if any(d.isVisible() and touching(d.geometry(), pos, side.sign > 0, side.horizontal, gap)
                   for d in side.docks):
                return [side]
        front = self._front()
        rects = {dock: self._rect(dock, tab_bars) for dock in front}
        central = self.window.centralWidget()
        area = self.window.dockWidgetArea
        for horizontal in (True, False):
            before = [d for d in front if touching(rects[d], pos, True, horizontal, gap)]
            after = [d for d in front if touching(rects[d], pos, False, horizontal, gap)]
            if not before and not after:
                continue
            for side, check in ((before, True), (after, False)):
                if central is not None and touching(central.geometry(), pos, check, horizontal, gap):
                    side.append(central)
            sides = []
            for docks, others, sign in ((before, after, 1), (after, before, -1)):
                if not docks or central in docks:
                    continue  # (the editor's side can't collapse)
                areas = {area(d) for d in docks}
                if len(areas) != 1:
                    continue
                (here,) = areas
                if central in others or any(area(d) != here for d in others):
                    # The edge of a side of the window: all of that side
                    # collapses, moved the way it opens (the left one sideways).
                    if (here in (Qt.LeftDockWidgetArea, Qt.RightDockWidgetArea)) != horizontal:
                        continue
                    docks = [d for d in front if area(d) == here]
                group = []
                for dock in docks:
                    for member in [dock, *self.window.tabifiedDockWidgets(dock)]:
                        if member not in group:
                            group.append(member)
                spans = [along(rects[d], horizontal) for d in docks]
                size = max(span[1] for span in spans) - min(span[0] for span in spans) + 1
                minimum = max((d.minimumSizeHint().width() if horizontal else d.minimumSizeHint().height())
                              for d in group)
                sides.append(Side(docks, group, size, max(minimum, 1), sign, horizontal))
            if sides:
                return sides
        return []

    # ------------------------------------------------------------ collapsing
    def _collapse(self, side: Side):
        """The panels in front: no content or title, so no minimum size; the
        ones behind them in their stacks: hidden (with one panel left, Qt
        takes the stack's tab bar away, which would keep a strip open)."""
        side.cross = [d.height() if side.horizontal else d.width() for d in side.docks]
        for dock in side.group:
            if dock in side.docks:
                content = dock.widget()
                self.contents[dock] = content
                dock.setTitleBarWidget(_NoTitle())
                dock.setWidget(Grip(self, side))
                if content is not None:
                    content.hide()  # (still the panel's child, put back by _open)
            else:
                dock.hide()
        self.collapsed.append(side)

    def _open(self, side: Side):
        self.collapsed.remove(side)  # (first: showing them below mustn't open it again)
        for dock in side.docks:
            grip = dock.widget()
            content = self.contents.pop(dock, None)
            dock.setTitleBarWidget(None)
            if content is not None:
                dock.setWidget(content)
                content.show()
            if isinstance(grip, Grip):
                grip.hide()
                grip.deleteLater()
        for dock in side.group:
            dock.show()  # (back in their places in the stacks)
        for dock in side.docks:  # (the tabs that were in front; again once Qt has put the others back)
            dock.raise_()
            QTimer.singleShot(0, dock, dock.raise_)
        if side.cross and len(side.docks) > 1:  # (the panels beside each other: as they were)
            self.window.resizeDocks(side.docks, side.cross, Qt.Vertical if side.horizontal else Qt.Horizontal)

    def _resize(self, side: Side, size: int):
        orientation = Qt.Horizontal if side.horizontal else Qt.Vertical
        self.window.resizeDocks(side.docks, [max(size, 1)] * len(side.docks), orientation)

    def content(self, dock: QDockWidget) -> QWidget | None:
        """A panel's content, collapsed or not."""
        return self.contents.get(dock) or dock.widget()

    def is_collapsed(self, dock: QDockWidget) -> bool:
        return any(dock in side.group for side in self.collapsed)

    def expand(self, dock: QDockWidget):
        """Open a collapsed panel (and the others collapsed with it), at the
        size it had."""
        side = next((s for s in self.collapsed if dock in s.group), None)
        if side is None:
            return
        self._open(side)
        QTimer.singleShot(0, dock, dock.raise_)  # (in front: after _open's raising of the tabs that were)
        QTimer.singleShot(0, self.window, lambda: self._resize(side, side.size))

    def expand_all(self):
        """Open every collapsed panel, at its size (Reset Layout; quitting,
        so the saved layout has them open)."""
        for side in list(self.collapsed):
            self._open(side)
            self._resize(side, side.size)

    # ------------------------------------------------------------ the drag
    # On a separator next to panels that can collapse, the drag is ours (Qt's
    # own stops at the minimum sizes, and doesn't follow a panel just opened).
    def eventFilter(self, obj, event):
        kind = event.type()
        if kind == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
            pos = event.position().toPoint()
            self._press = None
            if self._on_separator(pos):
                sides = self._sides_at(pos)
                if sides:
                    self.start_drag(sides, pos)
                    return True
        elif kind == QEvent.MouseMove and self._press is not None:
            if not event.buttons() & Qt.LeftButton:
                self.end_drag()
                return False
            self._drag_to(event.position().toPoint())
            return True
        elif kind == QEvent.MouseButtonRelease and self._press is not None:
            self.end_drag()
            return True
        return False

    def start_drag(self, sides: list[Side], pos: QPoint):
        """A drag of a separator (or a collapsed panel's grip) from ``pos``
        (in the window)."""
        self._sides, self._press = sides, pos
        for side in sides:
            side.press_size = GRIP if side in self.collapsed else side.size

    def end_drag(self):
        self._press, self._sides = None, []
        if QWidget.mouseGrabber() is self.window:
            self.window.releaseMouse()

    def _drag_to(self, pos: QPoint):
        moved = pos - self._press
        sizes = []
        for side in self._sides:
            size = side.size_at(moved.x() if side.horizontal else moved.y())
            small, collapsed = size < side.minimum / 2, side in self.collapsed
            if small and not collapsed:
                side.size = side.press_size  # (to open at, later)
                self._collapse(side)
            elif collapsed and not small:
                self._open(side)
            sizes.append((side, size))
        # One side sized: the other side of the separator takes the rest.
        side, size = next(((s, n) for s, n in sizes if s in self.collapsed), sizes[0])
        self._resize(side, GRIP if side in self.collapsed else max(size, side.minimum))
