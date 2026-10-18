"""A debug session: a program run under the debugger (`vibefpmgui.dap`'s
client, with an adapter from `vibefpmgui.debug_adapters`), and what it's
doing: running, stopped (where, with its call stack and variables), or ended.

The order of things, as adapters expect it: ``initialize``, then ``launch``
(gdb answers it only after the configuration is done), and on the
``initialized`` event the breakpoints, then ``configurationDone``. At each
stop: the threads, the stopped thread's call stack, then the scopes (Locals,
Arguments, module variables…) of the frame shown; variables are asked for
as they're expanded.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from qtpy.QtCore import QObject, Signal

from .dap import DapClient
from .debug_adapters import Adapter

IDLE, STARTING, RUNNING, STOPPED = "idle", "starting", "running", "stopped"
MAX_ELEMENTS = 1000  # an array's elements asked for at once (a big one: the first ones, then "…")

# gdb's own messages, sent as the program's output: breakpoints set before the
# program is loaded (they're placed when it is: see breakpoint events). With its
# styling on (a terminal, a UTF-8 locale), gdb puts a symbol in front: "❌️ No
# source file…".
_GDB_NOISE = re.compile(r"^[^\w\s]*\s*(No source file named .*\.|Breakpoint \d+ \(.*\) pending\.)\s*$")


@dataclass
class Frame:
    id: int
    name: str
    path: str | None  # (None: no source: a library's)
    line: int


@dataclass
class Scope:
    name: str
    reference: int
    expensive: bool = False


@dataclass
class Variable:
    name: str
    value: str
    reference: int = 0  # (non-zero: it has parts: an array's elements, a type's components)
    count: int = 0  # (its elements or components, if the adapter says)
    type: str = ""


@dataclass
class BreakpointState:
    verified: bool
    message: str = ""


@dataclass
class _Breakpoints:
    """The breakpoints sent for each file, and what the debugger said of them."""
    lines: dict[str, list[int]] = field(default_factory=dict)
    ids: dict[int, tuple[str, int]] = field(default_factory=dict)  # the debugger's id → (file, line)
    pending: set[tuple[str, int]] = field(default_factory=set)  # "pending" (gdb's: not placed yet)


class DebugSession(QObject):
    state_changed = Signal(str)
    stopped_at = Signal(object, str)  # [Frame] (the call stack, innermost first), why ("breakpoint", "step"…)
    scopes_ready = Signal(int, object)  # frame index, [Scope]
    output = Signal(str, str)  # category ("stdout", "stderr", "console"), text
    breakpoints_checked = Signal(str, object)  # file, {line: BreakpointState}
    ended = Signal(str)  # why ("exited with code 0", "stopped", the debugger's error)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.client = DapClient(self)
        self.client.event.connect(self._event)
        self.client.stopped.connect(self._adapter_stopped)
        self.adapter: Adapter | None = None
        self.state = IDLE
        self.frames: list[Frame] = []
        self.frame_index = 0
        self.thread_id: int | None = None
        self.exit_code: int | None = None
        self.hit_breakpoints: list[tuple[str, int]] = []  # (file, line as set) the last stop was at
        self._breakpoints = _Breakpoints()
        self._launch: dict = {}
        self._ending = False

    # ---------------------------------------------------------- state
    def is_active(self) -> bool:
        return self.state != IDLE

    def _set_state(self, state: str):
        if state != self.state:
            self.state = state
            self.state_changed.emit(state)

    def _end(self, why: str):
        if self.state == IDLE:
            return
        self._ending = True
        self.client.stop()
        self._ending = False
        self.frames, self.thread_id = [], None
        self._set_state(IDLE)
        self.ended.emit(why)

    # ---------------------------------------------------------- starting
    def start(self, adapter: Adapter, program: str, args: list[str], cwd: str,
              breakpoints: dict[str, list[int]]) -> bool:
        """Run ``program`` under the debugger, stopping at ``breakpoints``
        (file → lines)."""
        if self.is_active():
            self.stop()
        self.adapter = adapter
        self.exit_code = None
        self.frames, self.thread_id = [], None
        self._breakpoints = _Breakpoints({path: sorted(set(lines)) for path, lines in breakpoints.items() if lines})
        self._launch = adapter.launch_arguments(program, args, cwd)
        self._set_state(STARTING)
        self.client.start(adapter.command, cwd)
        self.client.request("initialize", {
            "clientID": "vibefpmgui", "clientName": "VibeFPMGUI", "adapterID": adapter.kind,
            "linesStartAt1": True, "columnsStartAt1": True, "pathFormat": "path",
            "supportsVariableType": True, "supportsRunInTerminalRequest": False,
        }, self._initialized)
        return True

    def _initialized(self, success: bool, _body: dict, message: str):
        if not success:
            self._end(f"the debugger didn't start: {message}")
            return
        # (gdb answers launch only once the configuration is done: no timeout.)
        self.client.request("launch", self._launch, self._launched, timeout=0)

    def _launched(self, success: bool, _body: dict, message: str):
        if not success and self.state != IDLE:
            self._end(f"the program didn't start: {message or 'the debugger refused it'}")

    def _configure(self):
        """The adapter is ready for the breakpoints (the initialized event)."""
        for path in list(self._breakpoints.lines):
            self._send_breakpoints(path)
        self.client.request("configurationDone", {}, self._configured)

    def _configured(self, success: bool, _body: dict, message: str):
        if not success:
            self._end(f"the debugger couldn't start the program: {message}")
        elif self.state == STARTING:
            self._set_state(RUNNING)

    # ---------------------------------------------------------- breakpoints
    def set_breakpoints(self, path: str, lines: list[int]):
        """A file's breakpoints now (sent to the debugger, if one is running)."""
        self._breakpoints.lines[path] = sorted(set(lines))
        if self.state in (RUNNING, STOPPED):
            self._send_breakpoints(path)

    def _send_breakpoints(self, path: str):
        lines = self._breakpoints.lines.get(path, [])
        self.client.request("setBreakpoints", {"source": {"path": path, "name": Path(path).name},
                                               "breakpoints": [{"line": line} for line in lines]},
                            lambda success, body, message, p=path, ls=lines: self._breakpoints_set(
                                p, ls, success, body, message))

    def _breakpoints_set(self, path: str, lines: list[int], success: bool, body: dict, message: str):
        states: dict[int, BreakpointState] = {}
        found = body.get("breakpoints") or [] if success else []
        self._breakpoints.pending -= {(path, n) for n in self._breakpoints.lines.get(path, [])}
        for line, answer in zip(lines, found, strict=False):
            if isinstance(answer.get("id"), int):
                self._breakpoints.ids[answer["id"]] = (path, line)
            if not answer.get("verified") and answer.get("reason") == "pending":
                self._breakpoints.pending.add((path, line))
            states[line] = BreakpointState(_placed(answer), _why_not(answer))
        for line in lines[len(found):]:
            states[line] = BreakpointState(False, message or "the debugger didn't place it")
        self.breakpoints_checked.emit(path, states)

    def _breakpoint_event(self, body: dict):
        answer = body.get("breakpoint") or {}
        where = self._breakpoints.ids.get(answer.get("id"))
        if where is not None and body.get("reason") in ("changed", "new"):
            path, line = where
            if answer.get("verified"):
                self._breakpoints.pending.discard(where)
            self.breakpoints_checked.emit(path, {line: BreakpointState(_placed(answer), _why_not(answer))})

    # ---------------------------------------------------------- running
    def _request_running(self, command: str, arguments: dict | None = None):
        if self.state != STOPPED or self.thread_id is None:
            return
        self.frames = []
        self._set_state(RUNNING)
        self.client.request(command, {"threadId": self.thread_id, **(arguments or {})}, self._answered)

    def _answered(self, success: bool, _body: dict, message: str):
        if not success and message and self.state != IDLE:
            self.output.emit("console", f"{message}\n")

    def continue_(self):
        self._request_running("continue")

    def step_over(self):
        self._request_running("next")

    def step_in(self):
        self._request_running("stepIn")

    def step_out(self):
        self._request_running("stepOut")

    def pause(self):
        if self.state == RUNNING:
            self.client.request("pause", {"threadId": self.thread_id or 1}, self._answered)

    def stop(self):
        """End the session: the program and the debugger."""
        if not self.is_active():
            return
        self.client.request("disconnect", {"terminateDebuggee": True}, timeout=1000)
        self._end("stopped")

    # ---------------------------------------------------------- events
    def _event(self, name: str, body: dict):
        if name == "initialized":
            self._configure()
        elif name == "stopped":
            self.thread_id = body.get("threadId", self.thread_id)
            # (Which breakpoints, as they were set: the debugger may have moved one to
            # the next line with code, where the program stops.)
            ids = self._breakpoints.ids
            self.hit_breakpoints = [ids[n] for n in body.get("hitBreakpointIds") or [] if n in ids]
            self._set_state(STOPPED)
            self._unplaced()
            why = str(body.get("reason") or "")
            self.client.request("threads", {}, lambda ok, b, m: self._threads(ok, b, why))
        elif name == "continued":
            if self.state == STOPPED:
                self.frames = []
                self._set_state(RUNNING)
        elif name == "output":
            self._output(body)
        elif name == "breakpoint":
            self._breakpoint_event(body)
        elif name == "exited":
            self.exit_code = body.get("exitCode")
        elif name == "terminated":
            code = self.exit_code
            self._end("the program ended" if code is None else f"the program exited with code {code}")

    def _unplaced(self):
        """Stopped, so the program is loaded: a breakpoint still "pending"
        never will be placed (gdb leaves one on a line with no code so)."""
        pending, self._breakpoints.pending = self._breakpoints.pending, set()
        by_file: dict[str, dict[int, BreakpointState]] = {}
        for path, line in pending:
            by_file.setdefault(path, {})[line] = BreakpointState(False, "no code on that line: the debugger "
                                                                        "couldn't place it")
        for path, states in by_file.items():
            self.breakpoints_checked.emit(path, states)

    def _output(self, body: dict):
        category, text = str(body.get("category") or "console"), str(body.get("output") or "")
        if category == "telemetry" or not text:
            return
        if self.adapter is not None and self.adapter.kind == "gdb":
            text = "".join(line for line in text.splitlines(keepends=True) if not _GDB_NOISE.match(line))
        if text:
            self.output.emit(category, text)

    def _adapter_stopped(self, why: str):
        if not self._ending:
            self._end(why)

    # ---------------------------------------------------------- where it stopped
    def _threads(self, success: bool, body: dict, why: str):
        threads = body.get("threads") or [] if success else []
        if self.thread_id is None and threads:
            self.thread_id = threads[0].get("id")
        if self.thread_id is None:
            return
        self.client.request("stackTrace", {"threadId": self.thread_id, "levels": 100},
                            lambda ok, b, m, w=why: self._stack(ok, b, w))

    def _stack(self, success: bool, body: dict, why: str):
        if self.state != STOPPED:
            return
        self.frames = [Frame(int(f.get("id", 0)), str(f.get("name") or "?"),
                             (f.get("source") or {}).get("path"), int(f.get("line") or 0))
                       for f in (body.get("stackFrames") or [] if success else [])]
        # The first frame with source (not a library's): where the program is, for the user.
        self.frame_index = next((i for i, f in enumerate(self.frames) if f.path), 0)
        self.stopped_at.emit(self.frames, why)
        self.select_frame(self.frame_index)

    def select_frame(self, index: int):
        """Show a frame of the call stack: its scopes (scopes_ready)."""
        if self.state != STOPPED or not 0 <= index < len(self.frames):
            return
        self.frame_index = index
        self.client.request("scopes", {"frameId": self.frames[index].id},
                            lambda ok, b, m, i=index: self._scopes(i, ok, b))

    def _scopes(self, index: int, success: bool, body: dict):
        scopes = [Scope(str(s.get("name") or ""), int(s.get("variablesReference") or 0), bool(s.get("expensive")))
                  for s in (body.get("scopes") or [] if success else [])
                  # (A processor's registers: not what a Fortran programmer is after.)
                  if s.get("presentationHint") != "registers" and str(s.get("name")).lower() != "registers"]
        self.scopes_ready.emit(index, scopes)

    def variables(self, reference: int, callback: Callable[[list[Variable], bool], None]):
        """The variables of a scope, or the parts of one (an array's
        elements, a type's components): ``callback(variables, more)``, more:
        there were more than MAX_ELEMENTS."""
        if self.state != STOPPED:
            callback([], False)
            return
        self.client.request("variables", {"variablesReference": reference, "start": 0, "count": MAX_ELEMENTS},
                            lambda ok, b, m: callback(*_variables(ok, b)))

    def evaluate(self, expression: str, callback: Callable[[bool, str, int], None], context: str = "watch"):
        """An expression's value in the frame shown: ``callback(ok, text, reference)``.
        ("watch": an expression; gdb takes "repl" as one of its commands.)"""
        frame = self.frames[self.frame_index].id if self.frames and self.state == STOPPED else None
        arguments = {"expression": expression, "context": context}
        if frame is not None:
            arguments["frameId"] = frame
        self.client.request("evaluate", arguments, lambda ok, b, m: callback(
            ok, str(b.get("result", "")) if ok else (m or "can't evaluate that"), int(b.get("variablesReference") or 0)))


def _placed(answer: dict) -> bool:
    """Placed, or as good as: "pending" (gdb's, until the program is loaded,
    when it says where it went), not shown as a problem meanwhile."""
    return bool(answer.get("verified")) or answer.get("reason") == "pending"


def _why_not(answer: dict) -> str:
    if _placed(answer):
        return ""
    return str(answer.get("message") or "the debugger couldn't place it (no code on that line?)")


def _variables(success: bool, body: dict) -> tuple[list[Variable], bool]:
    found = body.get("variables") or [] if success else []
    variables = [Variable(str(v.get("name") or ""), str(v.get("value") or ""), int(v.get("variablesReference") or 0),
                          int(v.get("indexedVariables") or v.get("namedVariables") or 0), str(v.get("type") or ""))
                 for v in found]
    return variables, len(found) >= MAX_ELEMENTS
