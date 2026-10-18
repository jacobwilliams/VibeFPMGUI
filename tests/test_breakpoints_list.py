"""The Debug panel's list of breakpoints: every one in the project, its state
(the gutter's dot), place and code; kept up to date with the gutter and the
Debug menu, and the one the program stopped at selected."""

from __future__ import annotations

import pytest
from conftest import open_editor
from qtpy.QtCore import Qt
from qtpy.QtGui import QTextCursor
from qtpy.QtTest import QTest
from test_debugger import fake_adapter

from vibefpmgui.breakpoints_list import DISABLED, ENABLED, UNPLACED, breakpoint_icon
from vibefpmgui.debug_session import IDLE, STOPPED, BreakpointState, Frame


def rows(window) -> list[tuple[str, str, str]]:
    """(place, state, code) of each row."""
    listed = window.debug_panel.breakpoints
    states = {(e.path, e.line): e.state for e in listed.entries}
    out = []
    for i in range(listed.topLevelItemCount()):
        item = listed.topLevelItem(i)
        out.append((item.text(0), states[tuple(item.data(0, Qt.UserRole))], item.text(1)))
    return out


def selected(window) -> str | None:
    key = window.debug_panel.breakpoints.current_key()
    return None if key is None else next(e.shown for e in window.debug_panel.breakpoints.entries
                                         if (e.path, e.line) == key)


def test_every_breakpoint_with_its_state_and_code(window, project):
    window.set_session_breakpoints([  # (closed files': from the project's session)
        {"path": str(project / "test/check.f90"), "lines": [3]},
        {"path": str(project / "app/main.f90"), "lines": [2, 3], "disabled": [2]},
    ])
    code = open_editor(window, project / "src/foo.f90").editor
    code.toggle_breakpoint(6)
    assert rows(window) == [("app/main.f90:2", DISABLED, "use foo"), ("app/main.f90:3", ENABLED, "call bump()"),
                            ("src/foo.f90:6", ENABLED, "count = count + 1 ! count"),
                            ("test/check.f90:3", ENABLED, "print *, count")]
    window._breakpoints_checked(str(project / "src/foo.f90"), {6: BreakpointState(False, "no code here")})
    assert rows(window)[2][1] == UNPLACED  # (grey, as in the gutter)
    item = window.debug_panel.breakpoints.topLevelItem(2)
    assert "Not placed: no code here" in item.toolTip(0)


def test_the_gutter_and_the_debug_menu_update_it(window, project):
    code = open_editor(window, project / "src/foo.f90").editor
    code.toggle_breakpoint(2)
    code.toggle_breakpoint(6)
    assert [r[:2] for r in rows(window)] == [("src/foo.f90:2", ENABLED), ("src/foo.f90:6", ENABLED)]
    code.toggle_breakpoint_enabled(6)  # (⌘/Ctrl-click, Ctrl+F9)
    assert rows(window)[1][1] == DISABLED
    window.disable_all_breakpoints_action.trigger()
    assert {r[1] for r in rows(window)} == {DISABLED}
    window.disable_all_breakpoints_action.trigger()  # (Enable All, now)
    assert {r[1] for r in rows(window)} == {ENABLED}
    code.toggle_breakpoint(2)  # (removed in the gutter)
    assert [r[0] for r in rows(window)] == ["src/foo.f90:6"]
    window.clear_breakpoints_action.trigger()
    assert rows(window) == []


def test_its_menu_disables_and_removes_one(window, project):
    window.set_session_breakpoints([{"path": str(project / "app/main.f90"), "lines": [2, 3]}])  # (a closed file)
    code = open_editor(window, project / "src/foo.f90").editor
    code.toggle_breakpoint(6)
    listed = window.debug_panel.breakpoints

    def menu(row: int) -> dict:
        return {a.text(): a for a in listed.menu_for(listed.topLevelItem(row)).actions() if a.text()}

    assert list(menu(0)) == ["Go to Line", "Disable Breakpoint", "Remove Breakpoint",
                             "Disable All Breakpoints", "Remove All Breakpoints"]
    menu(2)["Disable Breakpoint"].trigger()  # (src/foo.f90:6, open: hollow in the gutter too)
    assert code.disabled_breakpoints() == [6] and rows(window)[2][1] == DISABLED
    assert "Enable Breakpoint" in menu(2)
    menu(0)["Disable Breakpoint"].trigger()  # (app/main.f90:2, closed)
    assert window.disabled_breakpoints[str(project / "app/main.f90")] == [2]
    menu(0)["Enable Breakpoint"].trigger()
    assert str(project / "app/main.f90") not in window.disabled_breakpoints
    menu(1)["Remove Breakpoint"].trigger()  # (app/main.f90:3, closed)
    assert window.breakpoints[str(project / "app/main.f90")] == [2]
    listed.setCurrentItem(listed.topLevelItem(1))  # (src/foo.f90:6, open)
    QTest.keyClick(listed, Qt.Key_Delete)
    assert code.breakpoint_lines() == [] and [r[0] for r in rows(window)] == ["app/main.f90:2"]


def test_a_click_opens_the_line(window, project):
    window.set_session_breakpoints([{"path": str(project / "app/main.f90"), "lines": [3]}])
    listed = window.debug_panel.breakpoints
    listed.itemClicked.emit(listed.topLevelItem(0), 0)
    editor = window.current_editor()
    assert editor.path == (project / "app/main.f90").resolve() and editor.editor.textCursor().blockNumber() == 2


def test_edits_move_it(window, project, qtbot):
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.toggle_breakpoint(6)
    cursor = editor.editor.textCursor()
    cursor.movePosition(QTextCursor.Start)
    cursor.insertText("! a new first line\n")
    qtbot.waitUntil(lambda: rows(window)[0][0] == "src/foo.f90:7", timeout=3000)  # (once typing pauses)


# ----------------------------------------------------------------- while debugging
@pytest.fixture
def stopped(window, project, qtbot):
    """Debugging the fake's program, with breakpoints on src/foo.f90's lines 3
    and 5: stopped at 3."""
    code = open_editor(window, project / "src/foo.f90").editor
    code.toggle_breakpoint(3)
    code.toggle_breakpoint(5)
    window._debug_adapter = fake_adapter()
    window._debug_candidates = [("foo", ["/bin/foo"])]
    window._after_debug_build(0, False)
    qtbot.waitUntil(lambda: window.debug_session.state == STOPPED, timeout=10_000)
    qtbot.waitUntil(lambda: selected(window) is not None, timeout=10_000)
    yield window
    window.debug_session.stop()


def test_the_breakpoint_stopped_at_is_selected(stopped, qtbot):
    window = stopped
    assert selected(window) == "src/foo.f90:3"
    window.step_over_action.trigger()  # (to line 4: no breakpoint there)
    qtbot.waitUntil(lambda: window.debug_session.state == STOPPED
                    and window.current_editor().editor.execution_line == 4, timeout=10_000)
    assert selected(window) is None
    window.debug_start_action.trigger()  # (Continue: to the next one)
    qtbot.waitUntil(lambda: selected(window) == "src/foo.f90:5", timeout=10_000)
    window.debug_start_action.trigger()  # (to the end)
    qtbot.waitUntil(lambda: window.debug_session.state == IDLE, timeout=10_000)
    assert selected(window) is None


def arrows(window) -> list[str]:
    """The rows with the yellow arrow (the program stopped at them)."""
    listed = window.debug_panel.breakpoints
    out = []
    for i in range(listed.topLevelItemCount()):
        entry = listed.entry_at(i)
        if listed.topLevelItem(i).icon(0).cacheKey() == breakpoint_icon(entry.state, True).cacheKey():
            out.append(entry.shown)
    return out


def test_the_arrow_marks_the_breakpoint_stopped_at(stopped, qtbot):
    window = stopped
    assert arrows(window) == ["src/foo.f90:3"]
    window.step_over_action.trigger()  # (to line 4: no breakpoint: no arrow in the list)
    qtbot.waitUntil(lambda: window.current_editor().editor.execution_line == 4, timeout=10_000)
    assert arrows(window) == []
    window.debug_start_action.trigger()
    qtbot.waitUntil(lambda: arrows(window) == ["src/foo.f90:5"], timeout=10_000)
    window.debug_stop_action.trigger()
    assert arrows(window) == []


def test_the_code_is_syntax_colored(window, project):
    code = open_editor(window, project / "src/foo.f90").editor
    code.toggle_breakpoint(5)  # ("  subroutine bump()")
    listed = window.debug_panel.breakpoints
    formats = listed.line_formats(listed.entry_at(0))
    keyword = formats[len("  ")]  # ("subroutine")
    assert keyword is not None and keyword.foreground().color() != formats[len("  subroutine ")].foreground().color()
    assert listed.mono.family() == window._mono_font().family()  # (the editor's font, as the Search panel's)


def test_a_breakpoint_the_debugger_moved(window, project):
    """gdb moves a breakpoint on a line with no code to the next one with
    some; the program stops there. The one hit is the one set."""
    code = open_editor(window, project / "src/foo.f90").editor
    code.toggle_breakpoint(4)
    path = str((project / "src/foo.f90").resolve())
    window.debug_session.hit_breakpoints = [(path, 4)]  # (from the stop's hitBreakpointIds)
    assert window._stopped_at_breakpoint([Frame(1, "bump", path, 6)]) == (path, 4)
    window.debug_session.hit_breakpoints = []  # (none said: the line stopped at)
    assert window._stopped_at_breakpoint([Frame(1, "bump", path, 6)]) is None
    assert window._stopped_at_breakpoint([Frame(1, "bump", path, 4)]) == (path, 4)
