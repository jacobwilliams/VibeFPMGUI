"""View → Editor Tabs: the editor's tabs on one row that scrolls, or on several rows."""

from __future__ import annotations

from conftest import open_editor, write_files
from qtpy.QtCore import QPoint, Qt
from qtpy.QtTest import QTest

NAMES = [f"file_number_{i}.f90" for i in range(12)]


def open_many(window, project):
    write_files(project, {f"src/{name}": f"! {name}\n" for name in NAMES})
    for name in NAMES:
        open_editor(window, project / "src" / name)
    window._make_permanent(window.current_editor())


def test_switching_modes_is_remembered(qtbot, window, project, make_window):
    open_many(window, project)
    rows, bar = window.tab_rows, window.tabs.tabBar()
    assert not rows.isVisible() and bar.isVisible() and window.tab_rows_actions[False].isChecked()
    window.tab_rows_actions[True].trigger()
    assert rows.isVisible() and not bar.isVisible() and not window.tab_list_button.isVisible()
    assert window.tab_rows_list_button.isVisible()  # (the tab list at the rows' end)
    assert window.settings.value("tab_rows_wrap", type=bool) is True
    other = make_window(project / "fpm.toml")  # (the next start: several rows again)
    assert other.tab_rows_actions[True].isChecked() and other.tab_rows.isVisibleTo(other)
    window.tab_rows_actions[False].trigger()
    assert bar.isVisible() and not rows.isVisible() and window.tab_list_button.isVisible()
    assert not window.tab_rows_list_button.isVisible()


def test_tabs_wrap_onto_rows_as_wide_as_the_editor(qtbot, window, project):
    open_many(window, project)
    window.set_tab_rows(True)
    rows = window.tab_rows
    qtbot.wait(10)
    width = rows.width()  # (the editor's: the layout's)
    rects = [rows.tab_rect(i) for i in range(window.tabs.count())]
    assert len({r.top() for r in rects}) > 1  # (12 tabs don't fit on one row here: several)
    assert all(r.right() < width for r in rects) and rows.height() == max(r.bottom() for r in rects) + 1
    one_row = rects[0].height()
    assert rows.heightForWidth(50_000) == one_row  # (room for all of them: one row)
    assert rows.heightForWidth(width) == rows.height()
    assert [rows.tab_rect(i) for i in range(window.tabs.count())] == rects  # (only asked: they stay)


def test_choosing_closing_and_moving_tabs(qtbot, window, project):
    open_many(window, project)
    window.set_tab_rows(True)
    rows, tabs = window.tab_rows, window.tabs
    rows.resize(600, rows.height())
    QTest.mouseClick(rows, Qt.LeftButton, Qt.NoModifier, rows.tab_rect(3).center())
    assert tabs.currentIndex() == 3
    assert tabs.tabBar().tab_name(3) == NAMES[3]
    # Its close glyph: that tab closes.
    option = rows._option(5)
    QTest.mouseClick(rows, Qt.LeftButton, Qt.NoModifier, rows._close_rect(option).center())
    assert tabs.count() == len(NAMES) - 1 and NAMES[5] not in [tabs.tabBar().tab_name(i) for i in range(tabs.count())]
    # Dragged onto another tab: it takes that one's place.
    start, end = rows.tab_rect(0).center(), rows.tab_rect(2).center()
    QTest.mousePress(rows, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(rows, start + QPoint(20, 0))
    QTest.mouseMove(rows, end)
    QTest.mouseRelease(rows, Qt.LeftButton, Qt.NoModifier, end)
    assert tabs.tabBar().tab_name(2) == NAMES[0] and tabs.widget(2).path.name == NAMES[0]  # (its page moved too)


def test_a_preview_tab_and_double_click(qtbot, window, project):
    open_many(window, project)
    window.set_tab_rows(True)
    write_files(project, {"src/peek.f90": "! peek\n"})
    window.open_file(str(project / "src/peek.f90"), preview=True)
    tabs, rows = window.tabs, window.tab_rows
    index = tabs.currentIndex()
    assert tabs.tabBar().italic_name(index) == "peek.f90"  # (in italics, drawn as such on the rows)
    rows.grab()  # (drawn without trouble)
    QTest.mouseDClick(rows, Qt.LeftButton, Qt.NoModifier, rows.tab_rect(index).center())
    assert tabs.tabBar().italic_name(index) is None  # (made permanent)


def test_a_retina_icon_sits_where_the_tab_bar_puts_it(qtbot):
    """(A 16-point icon on a Retina screen is 32 pixels: it was placed by those, up in the corner.)"""
    from qtpy.QtGui import QColor, QPixmap
    from qtpy.QtWidgets import QLabel, QStyle, QTabBar, QTabWidget, QWidget

    from vibefpmgui.window.editor_tabs import ScrollingTabBar
    from vibefpmgui.wrapping_tabs import WrappingTabBar

    tabs = QTabWidget()
    qtbot.addWidget(tabs)
    tabs.setTabBar(ScrollingTabBar())
    tabs.addTab(QWidget(), "file.f90")
    pixmap = QPixmap(32, 32)
    pixmap.setDevicePixelRatio(2)  # (as icon_for(...).pixmap(16, 16) gives on a Retina screen)
    pixmap.fill(QColor("#ff00ff"))

    class RetinaLabel(QLabel):
        """(On a 1x screen, as the tests' offscreen one, a label hands back a 1x copy of its
        picture: the 2x one, as on a Retina screen, whatever the screen the tests run on.)"""

        def pixmap(self):
            return pixmap

    icon = RetinaLabel()
    icon.setFixedSize(16, 16)
    icon.setPixmap(pixmap)
    tabs.tabBar().setTabButton(0, QTabBar.LeftSide, icon)
    rows = WrappingTabBar(tabs)
    rows.resize(400, 40)
    rows.refresh()
    image = rows.grab().toImage()
    scale = image.devicePixelRatio()
    magenta = [(x, y) for y in range(image.height()) for x in range(image.width())
               if image.pixelColor(x, y) == QColor("#ff00ff")]
    xs, ys = [x / scale for x, _ in magenta], [y / scale for _, y in magenta]
    area = rows.style().subElementRect(QStyle.SE_TabBarTabLeftButton, rows._option(0), rows)
    assert magenta and round(max(xs) - min(xs) + 1 / scale) == 16  # (16 points wide, not 32)
    assert abs((min(xs) + max(xs)) / 2 - area.center().x()) <= 1 and abs((min(ys) + max(ys)) / 2 - area.center().y()) <= 1


def test_the_tab_list_and_close_all_on_rows(qtbot, window, project):
    """(With the tabs on rows, their list, and Close All Editor Tabs, were gone.)"""
    open_many(window, project)
    window.set_tab_rows(True)
    menu = window.tab_rows_list_button.menu()
    assert menu is window.tab_list_button.menu()  # (the same list)
    menu.aboutToShow.emit()
    names = [a.text() for a in menu.actions() if a.text()]
    assert names[:len(NAMES)] == NAMES and names[-1] == "Close All Editor Tabs"
    button = window.tab_rows_list_button
    assert button.geometry().right() >= window.tab_rows.geometry().right()  # (at the rows' end)
    close_all = next(a for a in menu.actions() if a.text() == "Close All Editor Tabs")
    close_all.trigger()
    assert window.tabs.count() == 0
