"""Double-clicking a floating panel's title docks it back once the mouse
button is up (on macOS, docking it at the double-click itself, during the
window move macOS started, left it over the main window)."""

from __future__ import annotations

from qtpy.QtCore import QEvent, QPointF, Qt
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
