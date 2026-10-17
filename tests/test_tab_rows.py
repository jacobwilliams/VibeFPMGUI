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


def test_tabs_leave_little_room_around_the_name(qtbot):
    """(macOS's style leaves the tab's buttons at its edges and centers the name in much
    more room than it needs; Fusion leaves none. A style made generous, on any platform.)"""
    from qtpy.QtGui import QFontMetrics
    from qtpy.QtWidgets import (
        QProxyStyle,
        QStyle,
        QStyleFactory,
        QStyleOptionTab,
        QTabWidget,
        QWidget,
    )

    from vibefpmgui.window.editor_tabs import ScrollingTabBar

    class Generous(QProxyStyle):
        def sizeFromContents(self, kind, option, size, widget=None):
            size = super().sizeFromContents(kind, option, size, widget)
            if kind == QStyle.CT_TabBarTab:
                size.setWidth(size.width() + 40)  # (the extra room ends up around the name)
            return size

    kept = []  # (the tab widgets and styles: alive for the test, as Qt doesn't own a style set on a widget)

    def tab_bar(style):
        tabs = QTabWidget()
        qtbot.addWidget(tabs)
        bar = ScrollingTabBar()
        bar.setStyle(style)
        tabs.setTabBar(bar)
        tabs.addTab(QWidget(), "mathutils.f90")
        kept.extend((tabs, style))
        return bar

    def room(bar) -> int:
        option = QStyleOptionTab()
        bar.initStyleOption(option, 0)
        option.rect.setSize(bar.tabSizeHint(0))
        return bar.style().subElementRect(QStyle.SE_TabBarTabText, option, bar).width()

    plain = tab_bar(QStyleFactory.create("Fusion"))
    generous = tab_bar(Generous(QStyleFactory.create("Fusion")))
    name = QFontMetrics(generous.font()).horizontalAdvance("mathutils.f90")
    assert room(generous) <= name + 2 * ScrollingTabBar.NAME_GAP  # (the 40 spare pixels: gone)
    assert generous.tabSizeHint(0).width() < plain.tabSizeHint(0).width() + 40
    assert room(plain) >= name  # (no spare room: unchanged, the name still fits)


def test_tabs_on_later_rows_have_their_icon_and_close(qtbot):
    """(The macOS style puts a tab's buttons from the top of the bar, whatever the tab's own
    top: on the rows after the first, they were drawn up in the first, out of their tab.
    A style that does the same, on any platform.)"""
    from qtpy.QtCore import QRect
    from qtpy.QtGui import QColor, QPixmap
    from qtpy.QtWidgets import (
        QLabel,
        QProxyStyle,
        QStyle,
        QStyleFactory,
        QTabBar,
        QTabWidget,
        QWidget,
    )

    from vibefpmgui.window.editor_tabs import ScrollingTabBar
    from vibefpmgui.wrapping_tabs import WrappingTabBar

    class FromTheTop(QProxyStyle):
        def subElementRect(self, element, option, widget=None):
            rect = super().subElementRect(element, option, widget)
            if element in (QStyle.SE_TabBarTabLeftButton, QStyle.SE_TabBarTabRightButton):
                rect.moveTop(rect.top() - option.rect.top())  # (as if the tab were on the first row)
            return rect

    tabs = QTabWidget()
    qtbot.addWidget(tabs)
    tabs.setTabBar(ScrollingTabBar())
    for i in range(6):
        tabs.addTab(QWidget(), f"file_{i}.f90")
        pixmap = QPixmap(16, 16)
        pixmap.fill(QColor("#ff00ff"))
        icon = QLabel()
        icon.setFixedSize(16, 16)
        icon.setPixmap(pixmap)
        tabs.tabBar().setTabButton(i, QTabBar.LeftSide, icon)
    style = FromTheTop(QStyleFactory.create("Fusion"))
    rows = WrappingTabBar(tabs)
    rows.setStyle(style)
    width = tabs.tabBar().tabSizeHint(0).width() * 2 + 10  # (two to a row)
    rows.resize(width, rows.heightForWidth(width))
    rows.refresh()
    later = rows.tab_rect(4)
    assert later.top() > 0  # (on a later row)
    image = rows.grab().toImage()
    scale = image.devicePixelRatio()
    inside = QRect(later.x(), later.y(), later.width(), later.height())
    magenta = [(x / scale, y / scale) for y in range(image.height()) for x in range(image.width())
               if image.pixelColor(x, y) == QColor("#ff00ff")]
    assert any(inside.contains(int(x), int(y)) for x, y in magenta)  # (its icon: in it)
    option = rows._option(4)
    assert inside.contains(rows._close_rect(option))  # (its close glyph: in it)
