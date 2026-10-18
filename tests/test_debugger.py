"""Debugging (vibefpmgui.dap, debug_session, debug_panel, window.debugging),
with a fake debug adapter (tests/fake_dap.py): no debugger needed. The real
one, gdb, is tested in test_debug_gdb.py."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from conftest import open_editor
from qtpy.QtCore import QEvent, QPoint, Qt
from qtpy.QtGui import QMouseEvent, QTextCursor
from qtpy.QtWidgets import QMessageBox

from vibefpmgui import debug_adapters
from vibefpmgui.dap import DapClient
from vibefpmgui.debug_adapters import Adapter
from vibefpmgui.debug_session import IDLE, RUNNING, STOPPED, DebugSession
from vibefpmgui.wire import FrameReader, frame

FAKE = str(Path(__file__).with_name("fake_dap.py"))


def fake_adapter(kind: str = "fake") -> Adapter:
    return Adapter(kind, [sys.executable, "-I", FAKE], "1.0")


# ----------------------------------------------------------------- the framing
def test_messages_framed_and_read_back_in_pieces():
    a, b = frame({"seq": 1, "type": "event"}), frame({"seq": 2, "body": {"text": "é ✓"}})
    reader = FrameReader()
    stream = b"garbage\r\n\r\n" + a + b
    found = []
    for i in range(0, len(stream), 7):  # (arriving a few bytes at a time)
        found += reader.feed(stream[i:i + 7])
    assert found == [{"seq": 1, "type": "event"}, {"seq": 2, "body": {"text": "é ✓"}}]
    assert reader.feed(b"Content-Length: 3\r\n\r\nnot") == [] and reader.buffer == b""  # (not JSON: dropped)


# ----------------------------------------------------------------- the client
@pytest.fixture
def client(qtbot):
    client = DapClient()
    yield client
    client.stop()


def answer(qtbot, client, command, arguments=None, timeout=10_000):
    got = []
    client.request(command, arguments or {}, lambda ok, body, message: got.append((ok, body, message)),
                   timeout=timeout)
    qtbot.waitUntil(lambda: bool(got), timeout=10_000)
    return got[0]


def test_requests_get_their_answers(qtbot, client, tmp_path):
    client.start([sys.executable, "-I", FAKE], str(tmp_path))
    ok, body, _ = answer(qtbot, client, "initialize")
    assert ok and client.capabilities == body == {"supportsConfigurationDoneRequest": True}
    ok, _, message = answer(qtbot, client, "frobnicate")
    assert not ok and "not supported" in message


def test_a_request_not_answered_in_time(qtbot, client, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DAP_SILENT", "threads")
    client.start([sys.executable, "-I", FAKE], str(tmp_path))
    ok, _, message = answer(qtbot, client, "threads", timeout=300)
    assert not ok and "didn't answer threads in time" in message


def test_an_adapter_that_ends_fails_what_was_asked(qtbot, client, tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_DAP_SILENT", "threads")
    stopped = []
    client.stopped.connect(stopped.append)
    client.start([sys.executable, "-I", FAKE], str(tmp_path))
    got = []
    client.request("threads", {}, lambda ok, body, message: got.append((ok, message)), timeout=0)
    client.request("disconnect", {})  # (the fake exits)
    qtbot.waitUntil(lambda: bool(stopped and got), timeout=10_000)
    assert got[0][0] is False and "stopped" in got[0][1] and not client.is_running()
    assert client.request("threads") is False


def test_an_adapter_that_isnt_there(qtbot, client, tmp_path):
    stopped = []
    client.stopped.connect(stopped.append)
    client.start([str(tmp_path / "no-such-debugger")], str(tmp_path))
    qtbot.waitUntil(lambda: bool(stopped), timeout=10_000)
    assert "could not be started" in stopped[0]


# ----------------------------------------------------------------- the session
@pytest.fixture
def source(tmp_path) -> Path:
    path = tmp_path / "prog.f90"
    path.write_text("".join(f"line {n}\n" for n in range(1, 21)))
    return path


@pytest.fixture
def session(qtbot):
    session = DebugSession()
    yield session
    session.stop()


def test_a_session_from_start_to_end(qtbot, session, source):
    states, stops, outputs, ended = [], [], [], []
    session.state_changed.connect(states.append)
    session.stopped_at.connect(lambda frames, why: stops.append((frames, why)))
    session.output.connect(lambda category, text: outputs.append(text))
    session.ended.connect(ended.append)
    session.start(fake_adapter(), "/bin/prog", [], str(source.parent), {str(source): [5, 9]})
    qtbot.waitUntil(lambda: bool(stops), timeout=10_000)
    frames, why = stops[0]
    assert why == "breakpoint" and session.state == STOPPED
    assert (frames[0].name, frames[0].path, frames[0].line) == ("demo", str(source), 5)
    assert frames[1].path is None and session.frame_index == 0
    session.step_over()
    qtbot.waitUntil(lambda: bool(len(stops) == 2), timeout=10_000)
    assert stops[1][0][0].line == 6 and stops[1][1] == "step"
    session.continue_()
    qtbot.waitUntil(lambda: bool(len(stops) == 3), timeout=10_000)
    assert stops[2][0][0].line == 9
    session.continue_()
    qtbot.waitUntil(lambda: bool(ended), timeout=10_000)
    assert ended == ["the program exited with code 0"] and session.state == IDLE
    assert states[:3] == ["starting", RUNNING, STOPPED] and states[-1] == IDLE
    assert "program started\n" in outputs and "done\n" in outputs


def test_gdbs_own_messages_arent_the_programs_output(qtbot, session, source):
    """gdb sends its notes on breakpoints set before the program is loaded as
    the program's output: left out (another debugger's output is kept as it is)."""
    outputs = []
    session.output.connect(lambda category, text: outputs.append(text))
    session.start(fake_adapter("gdb"), "/bin/prog", [], str(source.parent), {str(source): [5]})
    qtbot.waitUntil(lambda: session.state == STOPPED, timeout=10_000)
    assert "program started\n" in outputs
    assert not any("No source file named" in text for text in outputs)


def test_scopes_and_variables(qtbot, session, source):
    scopes = []
    session.scopes_ready.connect(lambda index, found: scopes.append(found))
    session.start(fake_adapter(), "/bin/prog", [], str(source.parent), {str(source): [4]})
    qtbot.waitUntil(lambda: bool(scopes), timeout=10_000)
    assert [s.name for s in scopes[0]] == ["Locals"]  # (not the registers)
    got = []
    session.variables(scopes[0][0].reference, lambda variables, more: got.append(variables))
    qtbot.waitUntil(lambda: bool(got), timeout=10_000)
    assert [(v.name, v.value, bool(v.reference), v.count) for v in got[0]] == [
        ("n", "4", False, 0), ("a", "", True, 3), ("p", "", True, 2)]
    evaluated = []
    session.evaluate("n * 2", lambda ok, text, ref: evaluated.append((ok, text)))
    session.evaluate("nope", lambda ok, text, ref: evaluated.append((ok, text)))
    qtbot.waitUntil(lambda: bool(len(evaluated) == 2), timeout=10_000)
    assert evaluated[0] == (True, "8") and evaluated[1][0] is False and "No symbol" in evaluated[1][1]


def test_breakpoints_the_debugger_couldnt_place(qtbot, session, source, monkeypatch):
    monkeypatch.setenv("FAKE_DAP_NO_CODE", "2")
    checked = []
    session.breakpoints_checked.connect(lambda path, states: checked.append(dict(states)))
    session.start(fake_adapter(), "/bin/prog", [], str(source.parent), {str(source): [2, 7]})
    qtbot.waitUntil(lambda: bool(session.state == STOPPED), timeout=10_000)
    first = checked[0]
    assert first[7].verified  # (pending: as good as placed)
    assert not first[2].verified and "No code" in first[2].message


def test_a_breakpoint_left_pending_isnt_placed(qtbot, session, source, monkeypatch):
    """gdb leaves a breakpoint on a line with no code "pending" for ever: once
    the program has stopped (it's loaded), one still pending isn't placed."""
    monkeypatch.setenv("FAKE_DAP_NEVER_PLACED", "2")
    checked = []
    session.breakpoints_checked.connect(lambda path, states: checked.append(dict(states)))
    session.start(fake_adapter(), "/bin/prog", [], str(source.parent), {str(source): [2, 7]})
    qtbot.waitUntil(lambda: session.state == STOPPED, timeout=10_000)
    assert checked[0][2].verified  # (pending, at first)
    assert not checked[-1][2].verified and "no code" in checked[-1][2].message and 7 not in checked[-1]


def test_stop_ends_the_program(qtbot, session, source):
    ended = []
    session.ended.connect(ended.append)
    session.start(fake_adapter(), "/bin/prog", [], str(source.parent), {str(source): [3]})
    qtbot.waitUntil(lambda: bool(session.state == STOPPED), timeout=10_000)
    session.stop()
    assert ended == ["stopped"] and session.state == IDLE and not session.client.is_running()


# ----------------------------------------------------------------- the editor's gutter
def click_gutter(code, line: int, x: int):
    y = code.cursorRect(QTextCursor(code.document().findBlockByNumber(line - 1))).center().y()
    point = QPoint(x, y)
    code.gutter.mousePressEvent(QMouseEvent(QEvent.MouseButtonPress, point, code.gutter.mapToGlobal(point),
                                            Qt.LeftButton, Qt.LeftButton, Qt.NoModifier))


def test_breakpoints_in_the_gutter(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    assert code.debuggable and code._breakpoint_column() > 0
    click_gutter(code, 3, 4)
    assert code.breakpoint_lines() == [3] and window.breakpoints == {str(editor.path): [3]}
    click_gutter(code, 3, 4)  # (again: cleared)
    assert code.breakpoint_lines() == [] and window.breakpoints == {}
    click_gutter(code, 2, code._breakpoint_column() + 20)  # (on the number: selects the line)
    assert code.breakpoint_lines() == [] and code.textCursor().hasSelection()
    code.setTextCursor(QTextCursor(code.document().findBlockByNumber(1)))
    window.toggle_breakpoint_action.trigger()  # (F9)
    assert code.breakpoint_lines() == [2]
    assert "Breakpoint" in code.breakpoint_tip(2) and "F9" in code.breakpoint_tip(5)


def test_no_breakpoints_where_the_debugger_cant_stop(window, project):
    toml = open_editor(window, project / "fpm.toml").editor
    assert not toml.debuggable and toml._breakpoint_column() == 0
    toml.toggle_breakpoint(1)
    assert toml.breakpoint_lines() == []


def test_breakpoints_move_with_edits_and_are_kept(make_window, project):
    window = make_window(project / "fpm.toml")
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.toggle_breakpoint(4)
    cursor = QTextCursor(editor.editor.document())
    cursor.insertText("! a new first line\n")
    assert editor.editor.breakpoint_lines() == [5]  # (moved down with its line)
    editor.save()
    window.close()
    again = make_window(project / "fpm.toml")
    reopened = again._open_editor_for(project / "src/foo.f90")
    assert reopened is not None and reopened.editor.breakpoint_lines() == [5]
    again._close_tab(reopened)
    assert again.all_breakpoints() == {str((project / "src/foo.f90").resolve()): [5]}  # (closed: still kept)


# ----------------------------------------------------------------- debugging in the window
@pytest.fixture
def debugging(window, project, qtbot):
    """The window debugging src/foo.f90's "program" (the fake's), stopped at line 3."""
    path = project / "src/foo.f90"
    open_editor(window, path).editor.toggle_breakpoint(3)
    window._debug_adapter = fake_adapter()
    window._debug_candidates = [("foo", ["/bin/foo"])]
    window._after_debug_build(0, False)
    qtbot.waitUntil(lambda: bool(window.debug_session.state == STOPPED), timeout=10_000)
    qtbot.waitUntil(lambda: bool(window.debug_panel.variable_rows()), timeout=10_000)
    yield window
    window.debug_session.stop()


def test_stopped_at_a_breakpoint(debugging, project, qtbot):
    window = debugging
    editor = window.current_editor()
    assert editor.path == (project / "src/foo.f90").resolve() and editor.editor.execution_line == 3
    assert editor.editor.textCursor().blockNumber() == 2  # (the editor at the line)
    panel = window.debug_panel
    assert panel.stack.count() == 2 and panel.stack.item(0).text().startswith("demo")
    assert "Paused" in panel.title.text() and "fake 1.0" in panel.title.text()
    assert ("n", "3") in panel.variable_rows()
    assert window.debug_start_action.text() == "Continue" and window.step_over_action.isEnabled()
    # An array's elements: asked for when it's expanded.
    locals_item = panel.variables.topLevelItem(0)
    array = next(locals_item.child(i) for i in range(locals_item.childCount()) if locals_item.child(i).text(0) == "a")
    array.setExpanded(True)
    qtbot.waitUntil(lambda: bool(("2", "2.5") in panel.variable_rows()), timeout=10_000)


def test_stepping_moves_the_arrow(debugging, qtbot):
    window = debugging
    code = window.current_editor().editor
    window.step_over_action.trigger()
    qtbot.waitUntil(lambda: bool(code.execution_line == 4), timeout=10_000)
    qtbot.waitUntil(lambda: ("n", "4") in window.debug_panel.variable_rows(), timeout=10_000)  # (the new values)
    window.debug_start_action.trigger()  # (Continue: to the end)
    qtbot.waitUntil(lambda: bool(window.debug_session.state == IDLE), timeout=10_000)
    assert code.execution_line is None and not window.step_over_action.isEnabled()
    assert "exited with code 0" in window.debug_panel.console.toPlainText()
    assert window.debug_start_action.text() == "Start Debugging"


def test_evaluate_in_the_console(debugging, qtbot):
    panel = debugging.debug_panel
    panel.evaluate_edit.setText("n * 2")
    panel.evaluate_edit.returnPressed.emit()
    qtbot.waitUntil(lambda: bool("\n6\n" in panel.console.toPlainText()), timeout=10_000)


def test_stop_button(debugging, qtbot):
    window = debugging
    window.debug_stop_action.trigger()
    assert window.debug_session.state == IDLE and window.current_editor().editor.execution_line is None
    assert "[stopped]" in window.debug_panel.console.toPlainText()


def test_breakpoint_set_while_debugging_is_sent(debugging, qtbot):
    window = debugging
    window.current_editor().editor.toggle_breakpoint(8)
    window.debug_start_action.trigger()  # (Continue: stops at the new one)
    qtbot.waitUntil(lambda: bool(window.current_editor().editor.execution_line == 8), timeout=10_000)


# ----------------------------------------------------------------- starting
def test_start_debugging_builds_with_the_collecting_runner(window, monkeypatch):
    monkeypatch.setattr(debug_adapters, "_found", {"adapter": fake_adapter()})
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, sub, *a, **k: ran.append((sub, k)))
    window.runner_args_edit.setText("--size 3")
    window.start_debugging()
    (sub, kwargs), = ran
    args = kwargs["args"]
    assert sub == ["run"] and kwargs["phase"] == "debug" and args[0] == "run"
    runner = args.index("--runner")
    assert "--collect" in args[runner + 1] and args.index("--") > runner  # (before the program's arguments)
    # No profile chosen: the debug profile's (fpm passes no -g otherwise: no debug information).
    assert args[1:3] == ["--profile", "debug"]


@pytest.mark.parametrize("profile, features", [("release", []), ("(default)", ["mpi"])])
def test_the_profile_or_features_chosen_are_kept(window, monkeypatch, profile, features):
    monkeypatch.setattr(debug_adapters, "_found", {"adapter": fake_adapter()})
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)  # (release: debug anyway)
    window.project_features = {"mpi": []}
    window.selected_features = features
    window.profile_combo.setCurrentText(profile)
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, sub, *a, **k: ran.append(k["args"]))
    window.start_debugging()
    assert ran and ran[0].count("--profile") == (1 if profile == "release" else 0)


def test_no_debugger(window, monkeypatch):
    monkeypatch.setattr(debug_adapters, "_found", {"adapter": None})
    told = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: told.append(a[2]))
    window.start_debugging()
    assert "No debugger was found" in told[0]


def test_release_profile_asks(window, monkeypatch):
    monkeypatch.setattr(debug_adapters, "_found", {"adapter": fake_adapter()})
    window.profile_combo.setCurrentText("release")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, *a, **k: ran.append(a))
    window.start_debugging()
    assert "without debug information" in asked[0] and not ran


def test_which_program_when_fpm_ran_several(window, project, monkeypatch, qtbot):
    from qtpy.QtWidgets import QInputDialog

    monkeypatch.setattr(QInputDialog, "getItem", lambda *a, **k: ("second", True))
    started = []
    monkeypatch.setattr(window.debug_session, "start", lambda adapter, program, args, cwd, bps:
                        started.append((program, args)))
    window._debug_adapter = fake_adapter()
    window._debug_candidates = [("first", ["/bin/first"]), ("second", ["/bin/second", "x"])]
    window._after_debug_build(0, False)
    assert started == [("/bin/second", ["x"])]


def test_a_failed_build_doesnt_debug(window, monkeypatch):
    started = []
    monkeypatch.setattr(window.debug_session, "start", lambda *a: started.append(a))
    window._debug_candidates = []
    window._after_debug_build(1, False)
    assert not started and "build failed" in window.debug_panel.console.toPlainText()


def test_the_collected_line_is_read(window):
    window._test_building, window._monitoring = False, False
    window._debug_candidates = []
    marker = "@@vibefpm:exe:demo@@" + json.dumps(["/x/demo", "--size", "3"])
    assert window._debug_line(marker) == ""
    assert window._debug_candidates == [("demo", ["/x/demo", "--size", "3"])]
    assert window._debug_line("[ 50%] compiling") == "[ 50%] compiling\n"


# ----------------------------------------------------------------- tests and examples
@pytest.fixture
def fpm_runs(window, monkeypatch):
    """What start_debugging asked fpm to do (nothing runs)."""
    monkeypatch.setattr(debug_adapters, "_found", {"adapter": fake_adapter()})
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, sub, *a, **k: ran.append((sub, k["args"])))
    return ran


@pytest.mark.parametrize("kind, sub, first", [("program", ["run"], ["run"]), ("test", ["test"], ["test"]),
                                              ("example", ["run"], ["run", "--example"])])
def test_programs_tests_and_examples(window, fpm_runs, kind, sub, first):
    window.start_debugging(kind)
    (ran_sub, args), = fpm_runs
    assert ran_sub == sub and [a for a in args if a not in ("--profile", "debug")][:len(first)] == first
    assert "--runner" in args


def test_a_test_picked_in_the_tests_panel(window, fpm_runs):
    window.target_edit.setText("other_program")
    window.test_monitor.start(["check_a", "check_b"], {})
    menu = window.test_monitor.row_menu(1)
    (action,) = menu.actions()
    assert action.text() == "Debug “check_b”"
    action.trigger()
    (sub, args), = fpm_runs
    assert sub == ["test"] and "check_b" in args and "other_program" not in args  # (that one, not the Target's)
    assert window.test_monitor.row_menu(5) is None


def test_a_program_picked_in_the_run_panel(window, fpm_runs):
    window.run_monitor.start(["demo"], {})
    window.run_monitor.row_menu(0).actions()[0].trigger()
    (sub, args), = fpm_runs
    assert sub == ["run"] and "demo" in args and "--example" not in args


def test_f5_debugs_what_was_debugged_last(window, fpm_runs):
    window.start_debugging("example")
    window.debug_start_action.trigger()
    assert [args[:4] for _sub, args in fpm_runs] == [["run", "--profile", "debug", "--example"]] * 2


def test_no_test_to_debug(window, fpm_runs):
    window.start_debugging("test")
    window._after_debug_build(0, False)
    assert "fpm ran no test" in window.debug_panel.console.toPlainText()


def test_debug_from_build_outputs(window, fpm_runs):
    """A Debug button there: that program (test, example), built again with the
    Build options, as Start Debugging does (its build there may have no debug
    information: built without a profile)."""
    window.outputs_view.debug_requested.emit("example", "demo_example")
    (sub, args), = fpm_runs
    assert sub == ["run"] and "--example" in args and "demo_example" in args and "--profile" in args
