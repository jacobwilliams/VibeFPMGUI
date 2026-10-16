"""The panels: docks, their layout and the editor in the middle.

Every panel but the editor is a dock (moved, stacked, floated, closed
from View → Panels); `RedockOnDoubleClick` docks a floating one back.
"""

from __future__ import annotations

from qtpy.QtCore import QEvent, QObject, QPointF, Qt, QTimer
from qtpy.QtGui import QMouseEvent
from qtpy.QtWidgets import (
    QApplication,
    QDockWidget,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QTabWidget,
    QWidget,
)

from ..dock_snap import SnapClosed
from ..editor import EditorTab
from ..terminal import TerminalPanel


class RedockOnDoubleClick(QObject):
    """Double-clicking a floating panel's title docks it back, once the
    mouse button is up.

    On macOS a floating panel has the system's title bar, and pressing on it
    starts a window move that macOS carries out; the second press of a
    double-click starts one too.
    Qt docked the panel at the double-click itself, in the middle of that
    move: the panel was left over the main window instead of in its place
    (and macOS logged "Window move completed without beginning"). Here the
    double-click is taken over, and the panel is docked (as setFloating(False)
    does, which works) when the move is done."""

    POLL_MS = 30
    MAX_POLLS = 100  # (3 s: if the button is never seen up, dock anyway)

    def __init__(self, parent: QObject):
        super().__init__(parent)
        self._dock: QDockWidget | None = None
        self._polls = 0
        self._timer = QTimer(self)
        self._timer.setInterval(self.POLL_MS)
        self._timer.timeout.connect(self._check)

    def eventFilter(self, dock, event) -> bool:
        if not (isinstance(dock, QDockWidget) and dock.isFloating()):
            return False
        # The title: the window's own title bar (macOS: a floating panel has
        # the system's), or Qt's drawn above the panel's content.
        on_title = event.type() == QEvent.NonClientAreaMouseButtonDblClick or (
            event.type() == QEvent.MouseButtonDblClick and dock.widget() is not None
            and event.position().y() < dock.widget().geometry().top())
        if on_title and event.button() == Qt.LeftButton:
            self._dock, self._polls = dock, 0
            self._timer.start()
            return True  # (not Qt's own docking, mid-move)
        return False

    def _check(self):
        self._polls += 1
        if QApplication.mouseButtons() != Qt.NoButton and self._polls < self.MAX_POLLS:
            return  # (still held: macOS may still be moving the window)
        self._timer.stop()
        dock, self._dock = self._dock, None
        if dock is None:
            return
        # The button-up macOS kept to itself (moving the window): Qt's drag of
        # the panel, if it started one, ends. (Left unended, the main window
        # kept its layout frozen: panels stayed put when the window was
        # resized, and a collapsed one left empty space.)
        pos = QPointF(dock.width() / 2, -10)
        QApplication.sendEvent(dock, QMouseEvent(QEvent.NonClientAreaMouseButtonRelease, pos, dock.mapToGlobal(pos),
                                                 Qt.LeftButton, Qt.NoButton, Qt.NoModifier))
        if dock.isFloating():
            dock.setFloating(False)
        window = dock.parentWidget()
        front = [dock]
        if isinstance(window, QMainWindow):
            # Anything else of the drag's goes too: the layout restored as it is. (Each
            # other stack's tab in front is put back after: a restore doesn't keep them.)
            stack = window.tabifiedDockWidgets(dock)
            front += [d for d in window.findChildren(QDockWidget) if d is not dock and d not in stack
                      and d.isVisible() and not d.isFloating() and window.rect().intersects(d.geometry())]
            window.restoreState(window.saveState())
        for panel in front:
            panel.raise_()
            QTimer.singleShot(0, panel, panel.raise_)


class PanelsMixin:
    """The panels and their layout (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_central(self):
        """The editor in the middle (a hint while no file is open)."""
        self.no_file_hint = QLabel("Open a file from <b>Project files</b>, or with "
                                   "<b>File → Open File in Editor…</b>")
        self.no_file_hint.setAlignment(Qt.AlignCenter)
        self.no_file_hint.setWordWrap(True)  # (narrow: the panels beside it can be wider)
        self.no_file_hint.setContentsMargins(12, 0, 12, 0)
        self.no_file_hint.setStyleSheet("color: gray;")
        self.central = QStackedWidget()
        self.central.addWidget(self.no_file_hint)
        self.central.addWidget(self.tabs)
        self.setCentralWidget(self.central)

    def _build_docks(self, options: QWidget, files_box: QWidget, graph_tab: QWidget, console_box: QWidget):
        """Every panel but the editor is a dock: moved to the left, right or
        bottom, stacked as tabs on another, floated as a window of its own, or
        closed (View → Panels, filled here)."""
        # (Not GroupedDragging: a stack of panels dragged out together goes in a
        # window of Qt's own, and once that's closed its panels come back stuck
        # over the editor, outside the layout. One panel at a time is solid.
        # Not AnimatedDocks: on macOS the panel layout could start animating
        # against itself, each finished animation laying out the panels again
        # and starting the next, at 100% CPU, the window frozen (seen with a
        # click in the editor). Panels snap into place instead of sliding; the
        # style has no animation duration either: see themes.StillStyle.)
        self.setDockOptions(QMainWindow.AllowNestedDocks | QMainWindow.AllowTabbedDocks)
        self.setTabPosition(Qt.AllDockWidgetAreas, QTabWidget.North)  # (stacked panels: tabs on top)
        self.setDocumentMode(True)
        self.setCorner(Qt.BottomLeftCorner, Qt.LeftDockWidgetArea)  # (the left panels: full height)
        self.setCorner(Qt.BottomRightCorner, Qt.BottomDockWidgetArea)
        self.docks: dict[str, QDockWidget] = {}
        self._dock_of: dict[QWidget, QDockWidget] = {}
        for name, title, widget in [
            ("options", "Build options", options), ("files", "Project files", files_box),
            ("graph", "Dependency graph", graph_tab), ("outputs", "Build outputs", self.outputs_view),
            ("build", "Build", self.build_monitor), ("tests", "Tests", self.test_monitor),
            ("run", "Run", self.run_monitor), ("coverage", "Coverage", self.coverage_panel),
            ("complexity", "Complexity", self.complexity_panel),
            ("problems", "Problems", self.problems_panel),
            ("review", "Review", self.review_monitor),
            ("references", "References", self.references_panel), ("search", "Search", self.search_panel),
            ("git", "Git", self.git_panel),
            ("terminal", "Terminal", self.terminal_panel), ("output", "Output", console_box),
        ]:
            self._make_dock(name, title, widget)
        self._docks_ready = False  # (until the window is shown: see _dock_shown)
        self.docks["outputs"].visibilityChanged.connect(lambda shown: shown and self.refresh_build_outputs())
        self.docks["terminal"].visibilityChanged.connect(self._terminal_dock_shown)
        self.docks["problems"].visibilityChanged.connect(self._problems_shown)
        self.docks["git"].visibilityChanged.connect(self._git_panel_shown)
        self.docks["complexity"].visibilityChanged.connect(self._complexity_panel_shown)
        self.docks["graph"].visibilityChanged.connect(self._update_editor_actions)  # (Find: the graph's search)
        self.default_layout()
        # Dragging a panel's edge past its smallest size collapses it (see dock_snap).
        self.snap_closed = SnapClosed(self, list(self.docks.values()))
        self._update_central()
        self.view_menu.addSeparator()
        panels = self.panels_menu = self.view_menu.addMenu("Panels")  # (kept: PySide would let it go)
        for dock in self.docks.values():
            panels.addAction(dock.toggleViewAction())  # (ticked while shown)
        panels.addSeparator()
        reset = panels.addAction("Reset Layout")
        reset.setStatusTip("Put every panel back where it starts: build options and project files on the "
                           "left, the graph and build outputs on the right, the rest at the bottom")
        reset.triggered.connect(self.reset_layout)

    PANEL_AREAS = Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea | Qt.BottomDockWidgetArea

    LAYOUT_VERSION = 2  # (saved layouts of another version aren't restored; 2: no grouped windows)

    def _make_dock(self, name: str, title: str, widget: QWidget) -> QDockWidget:
        dock = QDockWidget(title, self)
        dock.setObjectName(f"panel_{name}")  # (how a saved layout knows it)
        dock.setWidget(widget)
        dock.setAllowedAreas(self.PANEL_AREAS)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable
                         | QDockWidget.DockWidgetClosable)
        self.docks[name] = dock
        self._dock_of[widget] = dock
        if not hasattr(self, "_redock"):
            self._redock = RedockOnDoubleClick(self)
        dock.installEventFilter(self._redock)  # (see RedockOnDoubleClick)
        dock.toggleViewAction().toggled.connect(lambda shown, d=dock: shown and self._panel_shown(d))
        dock.topLevelChanged.connect(lambda floating, d=dock: floating or self._panel_docked(d))
        return dock

    def _panel_shown(self, dock: QDockWidget):
        """View → Panels → a panel: in front of its stack, and on a screen."""
        self._keep_on_screen(dock)
        QTimer.singleShot(0, dock, dock.raise_)

    def _panel_docked(self, dock: QDockWidget):
        """A floating panel docked again (double-clicking its title): in front
        of its stack. (Otherwise, on macOS, Qt leaves it shown but not given a
        place, outside the window, until the next start.)"""
        if self._docks_ready:
            QTimer.singleShot(0, dock, dock.raise_)

    def _keep_on_screen(self, dock: QDockWidget):
        """A floating panel shown again where no screen is (e.g. on a monitor
        that's gone): bring it over the window."""
        if not dock.isFloating():
            return
        frame = dock.frameGeometry()
        if any(screen.availableGeometry().intersects(frame) for screen in QApplication.screens()):
            return
        size = dock.size().boundedTo(self.size())
        dock.resize(size)
        dock.move(self.geometry().center() - dock.rect().center())

    def default_layout(self):
        """Build options above Project files on the left; the dependency graph
        and Build outputs (stacked) on the right; Build, Tests, Run, Problems,
        Review, References, Search and Terminal (stacked) at the bottom, Output
        beside them."""
        d = self.docks
        for dock in d.values():
            self.removeDockWidget(dock)
        self.addDockWidget(Qt.LeftDockWidgetArea, d["options"])
        self.splitDockWidget(d["options"], d["files"], Qt.Vertical)
        self.addDockWidget(Qt.RightDockWidgetArea, d["graph"])
        self.tabifyDockWidget(d["graph"], d["outputs"])
        self.addDockWidget(Qt.BottomDockWidgetArea, d["build"])
        # (Output beside Build first: split next to a stacked panel, it would be stacked too.)
        self.splitDockWidget(d["build"], d["output"], Qt.Horizontal)
        for name in ("tests", "run", "coverage", "complexity", "problems", "review", "references", "search", "git", "terminal"):
            self.tabifyDockWidget(d["build"], d[name])
        for dock in d.values():
            dock.setFloating(False)  # (after it's placed: a floating one stays floating otherwise)
            dock.show()
        d["graph"].raise_()
        d["build"].raise_()
        self._default_sizes()

    def _default_sizes(self):
        """The default panels' sizes, for the window's size (applied again once
        it's shown: before that it isn't its real size)."""
        d = self.docks
        self.resizeDocks([d["options"], d["graph"]], [310, max(420, self.width() // 3)], Qt.Horizontal)
        # Build options as tall as its fields need; Project files gets the rest.
        options = d["options"].widget().sizeHint().height() + 30
        self.resizeDocks([d["options"], d["files"]], [options, max(200, self.height() - options)], Qt.Vertical)
        self.resizeDocks([d["build"]], [max(220, self.height() // 3)], Qt.Vertical)
        self.resizeDocks([d["build"], d["output"]], [3, 2], Qt.Horizontal)

    def reset_layout(self):
        self.snap_closed.expand_all()
        self.default_layout()
        self.statusBar().showMessage("Panels back in their places", 3000)

    def show_panel(self, widget: QWidget):
        """Show a panel and bring it to the front of its stack."""
        dock = self._dock_of.get(widget)
        if dock is not None:
            dock.show()
            self.snap_closed.expand(dock)  # (collapsed by dragging its edge: open again)
            self._keep_on_screen(dock)
            dock.raise_()

    def _content(self, dock: QDockWidget) -> QWidget | None:
        """A panel's content (collapsed by dragging its edge, the panel shows a grip instead)."""
        snap = getattr(self, "snap_closed", None)
        return snap.content(dock) if snap is not None else dock.widget()

    def panel_in_front(self, widget: QWidget) -> bool:
        """Whether a panel is shown (and not behind another in its stack)."""
        dock = self._dock_of.get(widget)
        return dock is not None and not dock.isHidden() and widget.isVisible()

    def _update_central(self, *_):
        """The editor's tabs, or a hint while no file is open."""
        self.central.setCurrentWidget(self.tabs if self.tabs.count() else self.no_file_hint)

    def _terminal_dock_shown(self, shown: bool):
        # Choosing the Terminal tab: type there straight away (after the click
        # is done). Not while the window is being built and laid out.
        if shown and self._docks_ready:
            QTimer.singleShot(0, self.terminal_panel, self.terminal_panel.focus)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._docks_ready and not getattr(self, "_layout_restored", False):
            QTimer.singleShot(0, self, self._default_sizes)  # (now the window has its size)
        QTimer.singleShot(0, self, lambda: setattr(self, "_docks_ready", True))

    def _build_terminal(self):
        """The Terminal panel."""
        self.terminal_panel = TerminalPanel()
        self.terminal_panel.set_font(self._mono_font(editor=True))
        self.terminal_panel.set_word_wrap(EditorTab.word_wrap)

    def show_terminal(self):
        """Show the Terminal panel (starting the shell) and type there."""
        self.show_panel(self.terminal_panel)
        self.terminal_panel.start()
        self.terminal_panel.focus()
