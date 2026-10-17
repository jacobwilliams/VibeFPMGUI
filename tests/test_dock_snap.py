"""Dragging a panel's edge past its smallest size collapses it, and dragging
the edge back out opens it (as in VS Code)."""

from __future__ import annotations

import pytest
from qtpy.QtCore import QPoint, Qt
from qtpy.QtTest import QTest


def drag(qtbot, window, start: QPoint, *points: QPoint):
    QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, start)
    for point in points:
        QTest.mouseMove(window, point)
        qtbot.wait(10)
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, points[-1])
    qtbot.wait(30)


@pytest.fixture
def mac_style():
    """The macOS style, as the app has it there: its separators are 1 pixel,
    with an invisible widget over them to catch the mouse (Fusion's are 6,
    with none). Slow to lay out, so only for the tests of finding them."""
    from qtpy.QtWidgets import QApplication, QStyleFactory

    from vibefpmgui import themes

    if "macOS" not in QStyleFactory.keys():  # noqa: SIM118 (a list, not a dict)
        pytest.skip("no macOS style here")
    app = QApplication.instance()
    before, native = app.style(), themes._native_style
    restore = themes.StillStyle(themes.style_name(before)) if isinstance(before, themes.StillStyle) \
        else themes.style_name(before)
    app.setStyle(themes.StillStyle("macOS"))
    yield
    app.setStyle(restore)
    themes._native_style = native  # (the app's first look at the style: not this one)


@pytest.fixture(params=["Fusion", "macOS"])
def any_style(request):
    if request.param == "macOS":
        request.getfixturevalue("mac_style")
    return request.param


def ready(qtbot, window):
    window.resize(1400, 900)
    window.show()
    qtbot.wait(100)


def collapsed(window, name: str) -> bool:
    return window.snap_closed.is_collapsed(window.docks[name])


def editor_left(window) -> int:
    return window.centralWidget().geometry().left()


def test_collapsing_the_left_side_and_opening_it_again(any_style, qtbot, window):
    ready(qtbot, window)
    files = window.docks["files"]
    y = files.geometry().center().y()
    edge = files.geometry().right() + 3
    drag(qtbot, window, QPoint(edge, y), QPoint(edge - 50, y))  # (a little: just narrower)
    assert not collapsed(window, "files")
    edge = files.geometry().right() + 3
    heights = (window.docks["options"].height(), files.height())
    drag(qtbot, window, QPoint(edge, y), QPoint(edge - 100, y), QPoint(5, y))
    assert collapsed(window, "files") and collapsed(window, "options")  # (the whole left side)
    assert editor_left(window) < 15  # (the editor takes the room at once: no empty space left)
    assert not window._content(window.docks["options"]).isVisible()
    # The separator stays at the window's edge: drag it out again.
    edge = editor_left(window) - 3
    y = window.centralWidget().geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge + 40, y), QPoint(edge + 250, y))
    assert not collapsed(window, "files") and not collapsed(window, "options")
    assert window._content(window.docks["options"]).isVisible()
    assert abs(files.width() - 250) < 20  # (following the mouse)
    assert abs(window.docks["options"].height() - heights[0]) < 10 and abs(files.height() - heights[1]) < 10


def test_moving_back_before_letting_go_keeps_it_open(qtbot, window):
    ready(qtbot, window)
    files = window.docks["files"]
    edge = files.geometry().right() + 3
    y = files.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(5, y), QPoint(edge, y))
    assert not collapsed(window, "files") and window._content(files).isVisible()
    assert not window.snap_closed.collapsed


def test_the_bottom_panels_collapse_with_their_tabs(qtbot, window):
    ready(qtbot, window)
    build = window.docks["build"]
    top = window.docks["output"].geometry().top() - 3  # (above the tab bar of Build's stack)
    x = build.geometry().center().x()
    bottom = window.centralWidget().geometry().bottom()
    drag(qtbot, window, QPoint(x, top), QPoint(x, top + 100), QPoint(x, window.height() - 5))
    for name in ("build", "tests", "terminal", "output"):
        assert collapsed(window, name)
    assert window.centralWidget().geometry().bottom() > bottom + 100  # (the editor got the room)
    assert not collapsed(window, "files")  # (not the other sides)
    window.show_panel(window.test_monitor)  # (a test run shows Tests: the bottom opens again)
    qtbot.wait(30)
    for name in ("build", "tests", "terminal", "output"):
        assert not collapsed(window, name)
    assert window.panel_in_front(window.test_monitor)


def test_between_two_panels_only_the_squeezed_one_collapses(qtbot, window):
    ready(qtbot, window)
    options, files = window.docks["options"], window.docks["files"]
    edge = options.geometry().bottom() + 3
    x = options.geometry().center().x()
    drag(qtbot, window, QPoint(x, edge), QPoint(x, edge + 50), QPoint(x, files.geometry().bottom() - 2))
    assert not collapsed(window, "options") and collapsed(window, "files")


def test_the_right_side_collapses(qtbot, window):
    ready(qtbot, window)
    graph = window.docks["graph"]
    edge = graph.geometry().left() - 3
    y = graph.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge + 100, y), QPoint(window.width() - 5, y))
    assert collapsed(window, "graph") and collapsed(window, "outputs")
    assert not collapsed(window, "files") and not collapsed(window, "build")


def test_the_right_side_collapses_with_thin_separators(mac_style, qtbot, window):
    """(With the macOS style's thin separators the bottom panels' tab bar touched the
    right side's, which was then taken to reach across the window: it didn't collapse.)"""
    ready(qtbot, window)
    graph = window.docks["graph"]
    edge = graph.geometry().left() - 3
    y = graph.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge + 100, y), QPoint(window.width() - 5, y))
    assert collapsed(window, "graph") and collapsed(window, "outputs")


def test_a_panel_takes_only_its_own_stack_s_tab_bar(qtbot):
    """Which tab bars count as a panel's (they sit between it and the separator):
    worked out from where they are, on any platform and style. (Another stack's
    bar that only touched it, as the bottom panels' did the right side's with
    thin separators, made the right side reach across the window.)"""
    from qtpy.QtCore import QRect
    from qtpy.QtWidgets import QDockWidget, QMainWindow, QWidget

    from vibefpmgui.dock_snap import SnapClosed

    window = QMainWindow()
    qtbot.addWidget(window)
    snap = SnapClosed(window, [])
    right = QDockWidget(window)
    right.setGeometry(QRect(967, 110, 433, 479))
    own, beside, short = QWidget(window), QWidget(window), QWidget(window)
    own.setGeometry(QRect(967, 86, 433, 24))      # (its stack's tabs, above it)
    beside.setGeometry(QRect(311, 590, 1004, 24))  # (the bottom stack's tabs, touching its bottom)
    short.setGeometry(QRect(967, 86, 200, 24))     # (tabs only as wide as they need: still its own)
    assert snap._rect(right, [own, beside]) == QRect(967, 86, 433, 503)
    assert snap._rect(right, [short]) == QRect(967, 86, 433, 503)
    left = QDockWidget(window)
    left.setGeometry(QRect(0, 110, 310, 400))
    side = QWidget(window)
    side.setGeometry(QRect(310, 110, 24, 180))     # (tabs down its side)
    assert snap._rect(left, [side, beside]) == QRect(0, 110, 334, 400)
    # A panel beside a stack, level with its tabs (the bottom right one): not its tabs.
    alone = QDockWidget(window)
    alone.setGeometry(QRect(924, 404, 376, 419))
    stack_tabs = QWidget(window)
    stack_tabs.setGeometry(QRect(293, 404, 630, 24))  # (ending 1 pixel before it: a thin separator)
    assert snap._rect(alone, [stack_tabs]) == QRect(924, 404, 376, 419)


def test_reset_layout_opens_collapsed_panels(qtbot, window):
    ready(qtbot, window)
    files = window.docks["files"]
    edge = files.geometry().right() + 3
    y = files.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(5, y))
    window.reset_layout()
    assert not window.snap_closed.collapsed and window._content(files).isVisible()


def test_dragging_the_bottom_back_up(any_style, qtbot, window):
    ready(qtbot, window)
    build = window.docks["build"]
    top = window.docks["output"].geometry().top() - 3
    x = build.geometry().center().x()
    drag(qtbot, window, QPoint(x, top), QPoint(x, window.height() - 5))
    assert collapsed(window, "build")
    edge = window.centralWidget().geometry().bottom() + 3
    drag(qtbot, window, QPoint(x, edge), QPoint(x, edge - 40), QPoint(x, edge - 300))
    assert not collapsed(window, "build") and not collapsed(window, "output")
    assert window.panel_in_front(window.build_monitor)
    assert abs(window.docks["output"].height() - 300) < 40  # (following the mouse)


def test_no_strip_left_by_a_stacks_tabs(qtbot, window):
    """(The tab bar of the right side's stack kept a strip of it open.)"""
    from qtpy.QtWidgets import QTabBar

    ready(qtbot, window)
    graph = window.docks["graph"]
    edge = graph.geometry().left() - 3
    y = graph.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(window.width() - 5, y))
    assert window.width() - window.centralWidget().geometry().right() < 15
    bars = [b for b in window.findChildren(QTabBar) if b.parent() is window and b.isVisible()]
    assert all(b.geometry().right() < window.width() - 15 for b in bars)
    window.docks["outputs"].toggleViewAction().trigger()  # (View → Panels → Build outputs)
    qtbot.wait(30)
    assert not collapsed(window, "graph") and window.panel_in_front(window.outputs_view)
    assert graph in window.tabifiedDockWidgets(window.docks["outputs"])  # (stacked as before)


def test_collapsed_panels_are_saved_open(qtbot, make_window, project):
    from conftest import close_window

    first = make_window(project / "fpm.toml")
    ready(qtbot, first)
    files = first.docks["files"]
    edge = files.geometry().right() + 3
    y = files.geometry().center().y()
    drag(qtbot, first, QPoint(edge, y), QPoint(5, y))
    close_window(first)
    second = make_window(project / "fpm.toml")
    ready(qtbot, second)
    assert second._content(second.docks["files"]).isVisible() and second.docks["files"].width() > 100


def collapse_left(qtbot, window):
    files = window.docks["files"]
    edge = files.geometry().right() + 3
    y = files.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(5, y))
    assert collapsed(window, "files")


def test_a_collapsed_panel_shows_a_grip(any_style, qtbot, window):
    """(Collapsed, a panel showed no resize cursor, and was hard to grab.)"""
    from vibefpmgui.dock_snap import Grip

    ready(qtbot, window)
    collapse_left(qtbot, window)
    grip = window.docks["files"].widget()
    assert isinstance(grip, Grip) and grip.isVisible()
    assert grip.cursor().shape() == Qt.SplitHCursor
    assert grip.height() > 300  # (all along the panel: not squeezed into a corner)
    assert grip.width() < 15 and editor_left(window) < 20


def test_dragging_the_grip_opens_the_panel(qtbot, window):
    from qtpy.QtWidgets import QWidget

    ready(qtbot, window)
    collapse_left(qtbot, window)
    files = window.docks["files"]
    grip = files.widget()
    QTest.mousePress(grip, Qt.LeftButton, Qt.NoModifier, grip.rect().center())
    assert QWidget.mouseGrabber() is window  # (the window follows the drag: the grip goes when it opens)
    start = grip.mapTo(window, grip.rect().center())
    for dx in (20, 60, 400):
        QTest.mouseMove(window, start + QPoint(dx, 0))
        qtbot.wait(10)
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, start + QPoint(400, 0))
    qtbot.wait(30)
    assert QWidget.mouseGrabber() is None
    assert not collapsed(window, "files") and window._content(files).isVisible()
    from vibefpmgui.dock_snap import Grip

    assert not isinstance(files.widget(), Grip) and not files.isFloating()  # (its content back)
    assert abs(files.width() - 405) < 25  # (following the mouse, past the smallest size)


def test_double_clicking_the_grip_opens_the_panel(qtbot, window):
    ready(qtbot, window)
    collapse_left(qtbot, window)
    files = window.docks["files"]
    QTest.mouseDClick(files.widget(), Qt.LeftButton)
    qtbot.wait(50)
    assert not collapsed(window, "files") and window._content(files).isVisible() and files.width() > 100
    assert not files.isFloating()


def test_the_tab_in_front_stays_in_front(qtbot, window):
    """(Collapsed and opened again, a stack showed another tab.)"""
    ready(qtbot, window)
    window.show_panel(window.terminal_panel)
    qtbot.wait(30)
    top = window.docks["output"].geometry().top() - 3
    x = window.docks["terminal"].geometry().center().x()
    drag(qtbot, window, QPoint(x, top), QPoint(x, window.height() - 5))
    assert collapsed(window, "terminal")
    edge = window.centralWidget().geometry().bottom() + 3
    drag(qtbot, window, QPoint(x, edge), QPoint(x, edge - 300))
    qtbot.wait(30)
    assert current_tab(window, "Build") == "Terminal"


def current_tab(window, title: str) -> str:
    """The tab in front in the stack with a tab called ``title``."""
    from qtpy.QtWidgets import QTabBar

    for bar in window.findChildren(QTabBar):
        if bar.parent() is window and bar.isVisible() and title in [bar.tabText(i) for i in range(bar.count())]:
            return bar.tabText(bar.currentIndex())
    return ""



def test_the_other_sides_divider_leaves_a_collapsed_side_alone(qtbot, window):
    """(With the left side collapsed, dragging the right side's divider
    opened the left side: its panels are beyond the left side too.)"""
    ready(qtbot, window)
    collapse_left(qtbot, window)
    graph = window.docks["graph"]
    width = graph.width()
    edge = graph.geometry().left() - 3
    y = graph.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge - 20, y), QPoint(edge - 60, y))
    assert collapsed(window, "files") and collapsed(window, "options")
    assert abs(graph.width() - (width + 60)) < 10  # (the right side moved instead)


def test_the_bottom_right_panel_collapses(any_style, qtbot, window):
    """Output (beside Build's stack at the bottom): dragging the divider
    between them to the window's right edge collapses Output, as dragging a
    side's edge does."""
    ready(qtbot, window)
    output = window.docks["output"]
    edge = output.geometry().left() - 3
    y = output.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge + 50, y), QPoint(window.width() - 5, y))
    assert collapsed(window, "output")
    assert not collapsed(window, "build")


def test_a_panel_alone_at_the_bottom_right_collapses(any_style, qtbot, window):
    """Terminal on its own beside the bottom stack (at its smallest width):
    dragging the divider between them to the window's right edge collapses
    it. (With the macOS style's thin separators the stack's tab bar, level
    with Terminal's top, was taken to be Terminal's: it reached across the
    window, and only the stack could collapse.)"""
    ready(qtbot, window)
    terminal = window.docks["terminal"]
    window.removeDockWidget(terminal)  # (out of the bottom stack: beside it)
    window.addDockWidget(Qt.BottomDockWidgetArea, terminal)
    window.splitDockWidget(window.docks["output"], terminal, Qt.Horizontal)  # (beside a stack: a tab of it)
    window.tabifyDockWidget(window.docks["build"], window.docks["output"])  # (Terminal: beside the stack)
    terminal.show()
    window.resizeDocks([terminal], [terminal.minimumSizeHint().width()], Qt.Horizontal)
    qtbot.wait(50)
    assert window.tabifiedDockWidgets(terminal) == [] and terminal.isVisible()
    edge = terminal.geometry().left() - 3
    y = terminal.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge + 50, y), QPoint(window.width() - 5, y))
    assert collapsed(window, "terminal")
    assert not collapsed(window, "build")


def test_collapsing_panels_with_one_already_collapsed(any_style, qtbot, window):
    """Two panels side by side at the bottom, one collapsed on its own, then
    the bottom collapsed: the second collapse took the first one's grip for
    its content, so opening the bottom again left that panel blank (its
    content gone) and still taken as collapsed."""
    from vibefpmgui.dock_snap import Grip

    ready(qtbot, window)
    terminal = window.docks["terminal"]
    window.removeDockWidget(terminal)
    window.addDockWidget(Qt.BottomDockWidgetArea, terminal)
    window.splitDockWidget(window.docks["output"], terminal, Qt.Horizontal)
    window.tabifyDockWidget(window.docks["build"], window.docks["output"])
    terminal.show()
    qtbot.wait(50)
    # Terminal collapsed on its own (to the right)...
    edge = terminal.geometry().left() - 3
    y = terminal.geometry().center().y()
    drag(qtbot, window, QPoint(edge, y), QPoint(edge + 50, y), QPoint(window.width() - 5, y))
    assert collapsed(window, "terminal") and not collapsed(window, "build")
    # ... then the whole bottom (its top edge dragged down) ...
    build = window.docks["build"]
    front = next(d for d in [build, *window.tabifiedDockWidgets(build)] if not d.visibleRegion().isEmpty())
    x, top = front.geometry().center().x(), front.geometry().top()
    snap = window.snap_closed
    edge = next(yy for yy in range(top - 60, top + 1)
                if snap._on_separator(QPoint(x, yy)) and snap._sides_at(QPoint(x, yy)))
    drag(qtbot, window, QPoint(x, edge), QPoint(x, edge + 50), QPoint(x, window.height() - 5))
    assert collapsed(window, "build") and collapsed(window, "terminal")
    # ... and opened again: every panel with its own content, and only grips where collapsed.
    window.show_panel(window.test_monitor)
    qtbot.wait(50)
    assert terminal.widget() is window.terminal_panel and not collapsed(window, "terminal")
    assert window.docks["build"].widget() is window.build_monitor
    for name, dock in window.docks.items():
        assert isinstance(dock.widget(), Grip) == collapsed(window, name), name


@pytest.mark.parametrize("first, edge", [("options", "bottom")])
def test_a_divider_between_two_panels_follows_the_mouse(any_style, qtbot, window, first, edge):
    """(Only the panels on one side were resized: Qt shared the change
    between both sides, and the divider moved half as far as the mouse.)"""
    ready(qtbot, window)
    snap = window.snap_closed
    stack = [window.docks[first], *window.tabifiedDockWidgets(window.docks[first])]
    dock = next(d for d in stack if not d.visibleRegion().isEmpty())  # (its stack's tab in front)
    g = dock.geometry()
    horizontal = edge == "right"
    # (The separator beside it: where it is depends on the style.)
    points = ([QPoint(x, g.center().y()) for x in range(g.right(), g.right() + 12)] if horizontal
              else [QPoint(g.center().x(), y) for y in range(g.bottom(), g.bottom() + 12)])
    pos = next(p for p in points if snap._on_separator(p)
               and any(dock in side.docks for side in snap._sides_at(p)) and len(snap._sides_at(p)) == 2)
    for delta in (30, -30):  # (the panel above at about its smallest: it grows first)
        before = dock.geometry().right() if horizontal else dock.geometry().bottom()
        target = pos + (QPoint(delta, 0) if horizontal else QPoint(0, delta))
        drag(qtbot, window, pos, pos + (target - pos) / 2, target)
        moved = (dock.geometry().right() if horizontal else dock.geometry().bottom()) - before
        assert abs(moved - delta) <= 2, (delta, moved)
        pos = target


def test_wobbling_at_the_threshold_doesnt_flicker(qtbot, window):
    """The mouse a pixel either side of where a panel collapses: it
    collapsed and opened again at every move (swapping its content for its
    grip, and its size between the grip's and its smallest)."""
    from qtpy.QtTest import QTest

    ready(qtbot, window)
    snap = window.snap_closed
    flips = []
    collapse, open_ = snap._collapse, snap._open
    snap._collapse = lambda side: (flips.append("collapse"), collapse(side))[1]
    snap._open = lambda side: (flips.append("open"), open_(side))[1]
    files = window.docks["files"]
    y, edge = files.geometry().center().y(), files.geometry().right() + 3
    side = snap._sides_at(QPoint(edge, y))[0]
    threshold = edge - (side.size - side.minimum // 2)
    QTest.mousePress(window, Qt.LeftButton, Qt.NoModifier, QPoint(edge, y))
    for x in [threshold + 20, threshold - 1, threshold + 1, threshold - 1, threshold + 1, threshold - 1]:
        QTest.mouseMove(window, QPoint(x, y))
        qtbot.wait(5)
    QTest.mouseRelease(window, Qt.LeftButton, Qt.NoModifier, QPoint(threshold - 1, y))
    assert flips == ["collapse"]
    assert collapsed(window, "files")



def test_the_bottom_collapses_whole_from_anywhere_on_its_edge(any_style, qtbot, window):
    """Two panels side by side at the bottom: pressed on the bottom's top
    edge where nothing above was near enough to be found, only the left one
    was taken: it collapsed but the bottom couldn't shrink (the right one
    kept its height), leaving a blank space. Any press on a side's edge
    takes the whole side."""
    ready(qtbot, window)
    terminal = window.docks["terminal"]
    window.removeDockWidget(terminal)
    window.addDockWidget(Qt.BottomDockWidgetArea, terminal)
    window.splitDockWidget(window.docks["output"], terminal, Qt.Horizontal)
    window.tabifyDockWidget(window.docks["build"], window.docks["output"])
    terminal.show()
    qtbot.wait(50)
    snap = window.snap_closed
    build = window.docks["build"]
    front = next(d for d in [build, *window.tabifiedDockWidgets(build)] if not d.visibleRegion().isEmpty())
    x, top = front.geometry().center().x(), min(front.geometry().top(), terminal.geometry().top())
    # (Every row near the edge with a side found, the separator or not: with the
    # macOS style, Qt's widget over the thin separator reaches a row or two past
    # the editor, where it wasn't found any more.)
    presses = [QPoint(x, yy) for yy in range(top - 40, top + 1) if snap._sides_at(QPoint(x, yy))]
    assert presses
    for press in presses:  # (every one: all the bottom's panels)
        (side,) = snap._sides_at(press)
        assert front in side.docks and terminal in side.docks, press.y()
    presses = [p for p in presses if snap._on_separator(p)]
    drag(qtbot, window, presses[0], presses[0] + QPoint(0, 50), QPoint(x, window.height() - 5))
    assert collapsed(window, "build") and collapsed(window, "terminal")
