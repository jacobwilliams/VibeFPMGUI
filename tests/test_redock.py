"""Double-clicking a floating panel's title docks it back once the mouse
button is up (on macOS, docking it at the double-click itself, during the
window move macOS started, left it over the main window)."""

from __future__ import annotations

from qtpy.QtCore import QEvent, QPoint, QPointF, Qt
from qtpy.QtGui import QMouseEvent
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication


def double_click_title(dock):
    """A double-click on the panel's title bar: the system's (as a floating
    panel has on macOS), which Qt sees outside the panel's area."""
    pos = QPointF(dock.width() / 2, -10)
    event = QMouseEvent(QEvent.NonClientAreaMouseButtonDblClick, pos, dock.mapToGlobal(pos),
                        Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(dock, event)


def test_double_clicking_a_floating_panels_title_docks_it(qtbot, window, monkeypatch):
    window.show()
    dock = window.docks["search"]
    dock.raise_()
    qtbot.wait(100)
    place = dock.geometry()
    dock.setFloating(True)
    qtbot.wait(200)
    held = [Qt.LeftButton]  # (the button, as macOS reports it: still down at first)
    monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: held[0]))
    double_click_title(dock)
    qtbot.wait(150)
    assert dock.isFloating()  # (not while the button is down: macOS may be moving the window)
    held[0] = Qt.NoButton
    qtbot.waitUntil(lambda: not dock.isFloating(), timeout=2_000)
    assert window.dockWidgetArea(dock) == Qt.BottomDockWidgetArea
    assert dock in window.tabifiedDockWidgets(window.docks["build"])  # (back in its stack)
    assert dock.geometry() == place


def test_double_clicking_inside_a_floating_panel_doesnt_dock_it(qtbot, window):
    window.show()
    dock = window.docks["search"]
    dock.setFloating(True)
    qtbot.wait(200)
    inside = dock.widget().geometry().center()
    QTest.mouseDClick(dock, Qt.LeftButton, Qt.NoModifier, inside)
    qtbot.wait(200)
    assert dock.isFloating()
    dock.setFloating(False)


def test_the_layout_still_follows_the_window_after_docking_back(qtbot, window, monkeypatch):
    """(On macOS, Qt's drag of the floating panel could be left unended by
    the double-click: the window's layout stayed frozen, panels didn't
    follow a resize, and collapsing one left empty space.)"""
    from test_dock_snap import drag

    window.resize(1400, 900)
    window.show()
    dock = window.docks["coverage"]
    window.show_panel(window.coverage_panel)
    qtbot.wait(100)
    dock.setFloating(True)
    qtbot.wait(100)
    monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: Qt.NoButton))
    double_click_title(dock)
    qtbot.waitUntil(lambda: not dock.isFloating(), timeout=2_000)
    qtbot.wait(50)
    assert window.panel_in_front(window.coverage_panel)
    files = window.docks["files"]
    height = files.height()
    window.resize(1400, 700)
    qtbot.wait(50)
    assert files.height() < height - 100  # (the panels follow the window)
    y = files.geometry().center().y()
    edge = files.geometry().right() + 3
    drag(qtbot, window, QPoint(edge, y), QPoint(edge - 100, y), QPoint(5, y))
    assert window.centralWidget().geometry().left() < 20  # (collapsed: the editor takes the room)


def test_docking_back_keeps_every_stacks_tab(qtbot, window, monkeypatch):
    """(Docking Coverage back on the right switched the left stack from
    Project files to Build options; resetting the layout, the bottom stack's.)"""
    from test_dock_snap import current_tab

    window.resize(1400, 900)
    window.show()
    d = window.docks
    window.tabifyDockWidget(d["options"], d["files"])
    window.removeDockWidget(d["coverage"])
    window.addDockWidget(Qt.RightDockWidgetArea, d["coverage"])
    d["coverage"].show()
    qtbot.wait(50)
    d["files"].raise_()
    d["terminal"].raise_()
    qtbot.wait(50)
    before = (current_tab(window, "Project files"), current_tab(window, "Build"))
    assert before == ("Project files", "Terminal")
    d["coverage"].setFloating(True)
    qtbot.wait(100)
    monkeypatch.setattr(QApplication, "mouseButtons", staticmethod(lambda: Qt.NoButton))
    double_click_title(d["coverage"])
    qtbot.waitUntil(lambda: not d["coverage"].isFloating(), timeout=2_000)
    qtbot.wait(50)
    assert (current_tab(window, "Project files"), current_tab(window, "Build")) == before
    assert window.panel_in_front(window.coverage_panel)
