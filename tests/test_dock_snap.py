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
    """(Collapsed, a panel showed no grip dots and no resize cursor.)"""
    from vibefpmgui.dock_snap import Grip

    ready(qtbot, window)
    collapse_left(qtbot, window)
    grip = window.docks["files"].widget()
    assert isinstance(grip, Grip) and grip.isVisible()
    assert grip.cursor().shape() == Qt.SplitHCursor
    assert grip.height() > 300  # (all along the panel: not squeezed into a corner)
    assert grip.width() < 15 and editor_left(window) < 20
    image = grip.grab().toImage()
    ratio = image.devicePixelRatio()
    background = image.pixel(0, 0)
    marked = [y / ratio for y in range(image.height())
              if any(image.pixel(x, y) != background for x in range(image.width()))]
    assert marked and abs((min(marked) + max(marked)) / 2 - grip.height() / 2) < 3  # (the dots, in the middle)
    assert max(marked) - min(marked) > 8  # (three of them, along the strip)


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
