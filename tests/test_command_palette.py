"""View → Command Palette…: the menus' commands, found by typing."""

from __future__ import annotations

import gc

from qtpy.QtCore import Qt
from qtpy.QtGui import QAction, QKeySequence
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QMainWindow, QMenu

from vibefpmgui import command_palette as cp


def a_window(qtbot) -> tuple[QMainWindow, list[str]]:
    """A window with menus like the app's: shortcuts, a submenu, a separator, a
    hidden action, a checked one, a disabled one, and a menu filled when it opens."""
    window = QMainWindow()
    qtbot.addWidget(window)
    ran: list[str] = []
    edit = window.menuBar().addMenu("&Edit")
    undo = edit.addAction("&Undo")
    undo.setShortcut(QKeySequence("Ctrl+Z"))
    undo.triggered.connect(lambda: ran.append("undo"))
    edit.addSeparator()
    lines = edit.addMenu("Lines")
    lines.addAction("Move Line Up").triggered.connect(lambda: ran.append("up"))
    hidden = edit.addAction("Hidden")
    hidden.setVisible(False)
    view = window.menuBar().addMenu("&View")
    wrap = view.addAction("Word Wrap")
    wrap.setCheckable(True)
    wrap.setChecked(True)
    off = view.addAction("Format")
    off.setEnabled(False)
    off.triggered.connect(lambda: ran.append("format"))
    styles = view.addMenu("Syntax Colors")

    def fill():  # (as View → Syntax Colors: made when it opens, with a submenu)
        styles.clear()
        dark = styles.addMenu("Dark Styles")
        dark.addAction("monokai").triggered.connect(lambda: ran.append("monokai"))
        window.kept = dark  # (as the app keeps its menus)

    styles.aboutToShow.connect(fill)
    return window, ran


def test_the_commands_are_the_menus(qtbot):
    window, _ran = a_window(qtbot)
    titles = [c.title for c in cp.collect(window.menuBar())]
    assert titles == ["Edit › Undo", "Edit › Lines › Move Line Up", "View › Word Wrap", "View › Format",
                      "View › Syntax Colors › Dark Styles › monokai"]  # (no separator, submenu or hidden one)
    added = QAction("Added Later", window)
    next(m for m in window.findChildren(QMenu) if m.title() == "&View").addAction(added)
    assert "View › Added Later" in [c.title for c in cp.collect(window.menuBar())]  # (nothing to list it)


def test_collecting_leaves_the_menus_working(qtbot):
    """(QAction.menu() on a menu the menu bar made leaves it, and its actions, deleted as
    far as PySide knows: the app's own menus broke.)"""
    window, ran = a_window(qtbot)
    commands = cp.collect(window.menuBar())
    window.show()
    gc.collect()
    qtbot.wait(10)
    for command in commands:
        assert command.action.text()  # (still there)
    next(c for c in commands if c.name == "Undo").action.trigger()
    assert ran == ["undo"]


def test_matching_and_ranking():
    def command(*path, enabled=True):
        action = QAction(path[-1])
        action.setEnabled(enabled)
        return cp.Command(path, action)

    commands = [command("View", "Panels", "Build"), command("Commands", "Build"), command("Edit", "Lines", "Move Line Up"),
                command("View", "Syntax Colors", "Light Styles", "algol"), command("Edit", "Format", enabled=False)]
    assert [c.name for c in cp.ranked("mvup", commands, [])] == ["Move Line Up"]  # (letters in order)
    assert cp.ranked("close", commands, []) == []  # (letters scattered over "Colors … Light Styles": no)
    assert [c.title for c in cp.ranked("build", commands, [])][:2] == ["Commands › Build", "View › Panels › Build"]
    assert next(c.title for c in cp.ranked("build", commands, ["View › Panels › Build"])) == "View › Panels › Build"
    assert [c.name for c in cp.ranked("format", commands, [])] == ["Format"]  # (disabled: still listed)
    assert next(c.name for c in cp.ranked("", commands, ["Edit › Lines › Move Line Up"])) == "Move Line Up"
    assert cp.clean("&Find && Replace…\tCtrl+F") == "Find & Replace…"


def test_the_palette(qtbot):
    window, ran = a_window(qtbot)
    window.resize(800, 500)
    window.show()
    qtbot.waitExposed(window)
    recent: list[str] = []
    saved = []
    palette = cp.CommandPalette(window, recent)
    palette.recent_changed = lambda titles: saved.append(list(titles))
    palette.open()
    assert palette.isVisible() and len(palette.current_titles()) == 5
    QTest.keyClicks(palette.field, "mvup")
    assert palette.current_titles() == ["Edit › Lines › Move Line Up"]
    QTest.keyClick(palette.field, Qt.Key_Return)
    qtbot.waitUntil(lambda: ran == ["up"], timeout=2_000)
    assert not palette.isVisible() and saved[-1] == ["Edit › Lines › Move Line Up"]
    palette.open()
    assert palette.current_titles()[0] == "Edit › Lines › Move Line Up"  # (the last used first)
    QTest.keyClicks(palette.field, "format")
    QTest.keyClick(palette.field, Qt.Key_Return)  # (not available: nothing runs)
    qtbot.wait(20)
    assert ran == ["up"] and palette.isVisible()
    palette.field.clear()
    QTest.keyClick(palette.field, Qt.Key_Down)
    assert palette.list.currentRow() == 1
    QTest.keyClick(palette.field, Qt.Key_Escape)
    assert not palette.isVisible()


def test_the_app_s_palette(qtbot, window):
    assert window.palette_action.shortcut() == QKeySequence("Ctrl+Shift+P")
    commands = cp.collect(window.menuBar())
    titles = {c.title for c in commands}
    assert {"View › Command Palette…", "Edit › Toggle Line Comment", "Edit › Lines › Move Line Up",
            "View › Theme › Nord", "Commands › Build"} <= titles
    assert any(t.startswith("View › Syntax Colors › ") for t in titles)  # (filled when it opens: there)
    gc.collect()
    window._update_editor_actions()  # (the app's menus: still working)
    assert window.save_action.text() and all(c.action.text() for c in commands)
    window.show_command_palette()
    palette = window._command_palette
    palette.field.setText("theme nord")
    QTest.keyClick(palette.field, Qt.Key_Return)
    from vibefpmgui import themes
    qtbot.waitUntil(lambda: themes.current == "nord", timeout=2_000)
    assert window.settings.value("palette_recent", type=list)[0] == "View › Theme › Nord"
    themes.apply(themes.SYSTEM)


def test_its_icon_in_the_menu_and_the_palette(qtbot, window):
    """View → Command Palette…: its icon on the menu item, and in front of the palette's field (the theme's
    colors: redrawn when they change)."""
    from qtpy.QtWidgets import QApplication

    from vibefpmgui.icons import make_icon
    from vibefpmgui.window.running import QPalette

    action = window.palette_action
    color = QApplication.palette().color(QPalette.WindowText)
    assert action.icon().pixmap(18, 18).toImage() == make_icon("palette", color).pixmap(18, 18).toImage()
    window.show_command_palette()
    palette = window._command_palette
    qtbot.waitUntil(palette.isVisible, timeout=3000)
    assert palette.icon_action.icon().cacheKey() == action.icon().cacheKey()  # (at the field's start)
    palette.close()
    key = action.icon().cacheKey()
    window._update_command_icons()  # (a theme change)
    assert action.icon().cacheKey() != key
