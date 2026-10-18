"""The Debug panel: the session's state and controls (the Debug menu's
actions), the call stack, the variables (expanded as asked for: an array's
elements, a type's components) and the console: the program's output and the
debugger's messages, with a field to evaluate an expression in the frame
shown. It shows what `vibefpmgui.debug_session.DebugSession` says; the window
(`vibefpmgui.window.debugging`) connects them.
"""

from __future__ import annotations

from qtpy.QtCore import QSize, Qt, Signal
from qtpy.QtGui import QAction, QTextCursor
from qtpy.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QSplitter,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .breakpoints_list import BreakpointsList
from .build_monitor import PALETTES, TitleIcon, app_is_light
from .debug_session import (
    IDLE,
    RUNNING,
    STARTING,
    STOPPED,
    DebugSession,
    Frame,
    Scope,
    Variable,
)
from .widgets import HistoryLineEdit

REFERENCE_ROLE = Qt.UserRole + 1  # a variable's item: its variablesReference (0: no parts)
LOADED_ROLE = Qt.UserRole + 2  # its parts asked for already
PLACEHOLDER = "…"  # (an item's child until its parts are asked for)

STATE_TEXT = {IDLE: "Not debugging", STARTING: "Starting…", RUNNING: "Running", STOPPED: "Paused"}


class DebugPanel(QWidget):
    frame_chosen = Signal(int)  # a call stack row clicked (its index)

    def __init__(self, session: DebugSession, actions: list[QAction], breakpoint_actions: list[QAction] = (),
                 parent=None):
        """``actions``: the buttons (the Debug menu's); ``breakpoint_actions``: in
        the breakpoints list's right-click menu (Disable All, Remove All)."""
        super().__init__(parent)
        self.session = session
        self.title_icon = TitleIcon("bug")  # (as the other panels': see refresh_palette)
        self.title = QLabel()
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        header = QHBoxLayout()
        header.setContentsMargins(8, 4, 6, 2)
        header.addWidget(self.title_icon)
        header.addSpacing(2)
        header.addWidget(self.title, 1)
        for action in actions:  # (the Debug menu's: Continue, Step Over…; their icons, tooltips saying which)
            button = QToolButton()
            button.setDefaultAction(action)
            button.setToolButtonStyle(Qt.ToolButtonIconOnly)
            button.setIconSize(QSize(18, 18))
            button.setAutoRaise(True)
            header.addWidget(button)

        self.stack = QListWidget()
        self.stack.currentRowChanged.connect(self._frame_clicked)
        self.variables = QTreeWidget()
        self.variables.setHeaderLabels(["Name", "Value"])
        self.variables.setColumnWidth(0, 160)
        self.variables.itemExpanded.connect(self._expanded)
        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setMaximumBlockCount(5000)
        self.evaluate_edit = HistoryLineEdit()  # (Up, Down: the expressions evaluated before)
        self.evaluate_edit.setPlaceholderText("Evaluate in the frame shown (e.g. a(2:3), p%x), Enter; ↑↓ earlier ones")
        self.evaluate_edit.submitted.connect(self._evaluate)

        self.breakpoints = BreakpointsList(breakpoint_actions)  # (filled by the window: see window.debugging)
        left = QSplitter(Qt.Vertical)
        left.addWidget(_titled("Call Stack", self.stack))
        left.addWidget(_titled("Breakpoints", self.breakpoints))
        middle = _titled("Variables", self.variables)
        console_box = QWidget()
        console_layout = QVBoxLayout(console_box)
        console_layout.setContentsMargins(0, 0, 0, 0)
        console_layout.setSpacing(2)
        console_layout.addWidget(_titled("Console", self.console), 1)
        console_layout.addWidget(self.evaluate_edit)
        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(middle)
        split.addWidget(console_box)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        split.setStretchFactor(2, 3)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(split, 1)

        session.state_changed.connect(self._state_changed)
        session.stopped_at.connect(self._stopped)
        session.scopes_ready.connect(self._scopes)
        session.output.connect(self.append)
        session.ended.connect(lambda why: self.append("console", f"[{why}]\n"))
        self._state_changed(session.state)
        self.refresh_palette()

    def refresh_palette(self):
        """The icon in the theme's text color (again when the theme changes)."""
        self.title_icon.draw(PALETTES["light" if app_is_light() else "dark"]["text"], self.title)

    # ---------------------------------------------------------- state
    def _state_changed(self, state: str):
        adapter = self.session.adapter
        debugger = f" · {adapter.title}" if adapter is not None and state != IDLE else ""
        self.title.setText(f"<b>{STATE_TEXT.get(state, state)}</b>{debugger}")
        self.evaluate_edit.setEnabled(state == STOPPED)
        if state != STOPPED:
            self.stack.clear()
            self.variables.clear()

    def begin(self, what: str):
        """A new session (``what``: the program): the console cleared."""
        self.console.clear()
        self.append("console", f"[debugging {what}]\n")

    def append(self, category: str, text: str):
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    # ---------------------------------------------------------- the stack
    def _stopped(self, frames: list[Frame], _why: str):
        self.stack.blockSignals(True)
        self.stack.clear()
        for frame in frames:
            where = f"{frame.path.rsplit('/', 1)[-1]}:{frame.line}" if frame.path else "(no source)"
            item = QListWidgetItem(f"{frame_name(frame.name)}  ·  {where}")
            item.setToolTip(f"{frame.path}:{frame.line}" if frame.path else frame.name)
            if not frame.path:
                item.setForeground(self.palette().placeholderText())
            self.stack.addItem(item)
        self.stack.setCurrentRow(self.session.frame_index)
        self.stack.blockSignals(False)

    def _frame_clicked(self, row: int):
        if row >= 0:
            self.frame_chosen.emit(row)

    # ---------------------------------------------------------- the variables
    def _scopes(self, _index: int, scopes: list[Scope]):
        self.variables.clear()
        for number, scope in enumerate(scopes):
            item = QTreeWidgetItem([scope.name, ""])
            item.setData(0, REFERENCE_ROLE, scope.reference)
            font = item.font(0)
            font.setBold(True)
            item.setFont(0, font)
            self.variables.addTopLevelItem(item)
            item.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
            if number == 0 or not scope.expensive and scope.name.lower() in ("locals", "arguments"):
                item.setExpanded(True)

    def _expanded(self, item: QTreeWidgetItem):
        if item.data(0, LOADED_ROLE):
            return
        item.setData(0, LOADED_ROLE, True)
        reference = int(item.data(0, REFERENCE_ROLE) or 0)
        self.session.variables(reference, lambda variables, more, it=item: self._fill(it, variables, more))

    def _fill(self, item: QTreeWidgetItem, variables: list[Variable], more: bool):
        try:
            item.takeChildren()
        except RuntimeError:
            return  # (cleared meanwhile: the program moved on)
        for variable in variables:
            child = QTreeWidgetItem([variable.name, variable.value])
            child.setToolTip(1, variable.value)
            if variable.type:
                child.setToolTip(0, variable.type)
            child.setData(0, REFERENCE_ROLE, variable.reference)
            if variable.reference:
                child.addChild(QTreeWidgetItem([PLACEHOLDER, ""]))
            item.addChild(child)
        if more:
            item.addChild(QTreeWidgetItem(["…", "more: evaluate a section, e.g. a(1001:1100)"]))
        if not variables:
            item.addChild(QTreeWidgetItem(["(none)", ""]))

    def variable_rows(self) -> list[tuple[str, str]]:
        """(name, value) of the variables shown, depth first (for the tests)."""
        rows = []

        def visit(item: QTreeWidgetItem):
            for i in range(item.childCount()):
                child = item.child(i)
                if child.text(0) != PLACEHOLDER:
                    rows.append((child.text(0), child.text(1)))
                    visit(child)

        for i in range(self.variables.topLevelItemCount()):
            visit(self.variables.topLevelItem(i))
        return rows

    # ---------------------------------------------------------- evaluate
    def _evaluate(self, expression: str):
        self.append("console", f"> {expression}\n")
        self.session.evaluate(expression, lambda ok, text, _ref: self.append("console", f"{text}\n"))


def frame_name(name: str) -> str:
    """A frame's procedure as a Fortran programmer knows it (gfortran calls
    the main program MAIN__)."""
    return "main program" if name == "MAIN__" else name


def _titled(title: str, widget: QWidget) -> QWidget:
    box = QWidget()
    layout = QVBoxLayout(box)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(0)
    label = QLabel(title)
    label.setContentsMargins(6, 2, 6, 2)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    layout.addWidget(label)
    layout.addWidget(widget, 1)
    return box
