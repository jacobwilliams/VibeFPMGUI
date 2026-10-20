"""View → Toolbar (and the toolbar's right-click menu): small icons, and
labels under them or not; the app's settings, for every project."""

from __future__ import annotations

from qtpy.QtCore import QPoint, Qt


def test_small_icons_and_no_labels(window):
    toolbar = window.command_toolbar
    assert toolbar.iconSize().width() == 22 and toolbar.toolButtonStyle() == Qt.ToolButtonTextUnderIcon
    window.small_icons_action.setChecked(True)
    assert toolbar.iconSize().width() == 16
    window.toolbar_labels_action.setChecked(False)
    assert toolbar.toolButtonStyle() == Qt.ToolButtonIconOnly
    build = next(a for a in window.command_actions if a.text() == "Build")
    assert build.toolTip().startswith("Build")  # (icons only: the name in the tooltip)
    assert window.settings.value("toolbar_small_icons", type=bool)
    assert not window.settings.value("toolbar_labels", type=bool)


def test_the_text_size_still_counts(window):
    window.small_icons_action.setChecked(True)
    window.set_font_delta(4)
    small = window.command_toolbar.iconSize().width()
    assert small > 16 and small == round(16 * window._ui_factor)
    window.small_icons_action.setChecked(False)
    assert window.command_toolbar.iconSize().width() == round(22 * window._ui_factor)
    window.set_font_delta(0)


def test_remembered_for_the_next_window(make_window, project, settings_file):
    first = make_window(project / "fpm.toml")
    first.small_icons_action.setChecked(True)
    first.toolbar_labels_action.setChecked(False)
    second = make_window(project / "fpm.toml")
    assert second.command_toolbar.iconSize().width() == 16
    assert second.command_toolbar.toolButtonStyle() == Qt.ToolButtonIconOnly


def test_the_right_click_menu(window, qtbot):
    window.command_toolbar.customContextMenuRequested.emit(QPoint(10, 10))
    qtbot.waitUntil(window.toolbar_menu.isVisible, timeout=3000)
    assert [a.text() for a in window.toolbar_menu.actions()] == ["Small Icons", "Show Labels"]
    window.toolbar_menu.hide()
