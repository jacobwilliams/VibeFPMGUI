"""A Debug Adapter Protocol client: one debug adapter (gdb's ``--interpreter=dap``,
``lldb-dap``…) run as a process, spoken to over its stdin and stdout (see
`vibefpmgui.wire` for the framing; `vibefpmgui.debug_session` drives it).

Each request gets its answer through a callback, ``callback(success, body,
message)``, called once: with the adapter's answer, or, when it doesn't
answer in time or stops, with success False and why. Events come as the
`event` signal. A request from the adapter (``runInTerminal``…) is answered
"not supported".

As for fortls (`vibefpmgui.lsp`): no threads (the process is read in the GUI
thread), and methods, not lambdas, connected to the process.
"""

from __future__ import annotations

from collections.abc import Callable

from qtpy.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from .wire import FrameReader, frame

TIMEOUT = 10_000  # ms a request may take (a hung adapter can't leave a session stuck)

Callback = Callable[[bool, dict, str], None]


class DapClient(QObject):
    event = Signal(str, object)  # an event's name and body ({} if none)
    stopped = Signal(str)  # the adapter's process ended (why), not by stop()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process: QProcess | None = None
        self._reader = FrameReader()
        self._seq = 0
        self._pending: dict[int, tuple[str, Callback, QTimer]] = {}
        self.capabilities: dict = {}

    # ---------------------------------------------------------- lifecycle
    def is_running(self) -> bool:
        return self.process is not None

    def start(self, command: list[str], cwd: str, env: dict[str, str] | None = None) -> bool:
        """Start the adapter (``command``: its program and arguments)."""
        self.stop()
        process = QProcess(self)
        process.setWorkingDirectory(cwd)
        if env:
            environment = QProcessEnvironment.systemEnvironment()
            for key, value in env.items():
                environment.insert(key, value)
            process.setProcessEnvironment(environment)
        process.setStandardErrorFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(self._read)
        process.finished.connect(self._process_finished)
        process.errorOccurred.connect(self._process_error)
        self.process = process
        process.start(command[0], command[1:])
        return True

    def stop(self):
        """End the adapter (and with it the program it debugs), at once."""
        process, self.process = self.process, None
        self._fail_pending("the debugger was stopped")
        self._reader.clear()
        if process is not None:
            process.kill()
            process.waitForFinished(1000)
            process.deleteLater()

    def _process_finished(self, *_):
        if isinstance(self.sender(), QProcess):
            self._ended(self.sender())

    def _process_error(self, error):
        if error == QProcess.FailedToStart and isinstance(self.sender(), QProcess):
            self._ended(self.sender())

    def _ended(self, process: QProcess):
        if process is not self.process:
            return  # one we stopped
        self.process = None
        why = ("the debugger could not be started" if process.error() == QProcess.FailedToStart
               else f"the debugger stopped (exit code {process.exitCode()})")
        self._fail_pending(why)
        try:
            process.deleteLater()
        except RuntimeError:
            return  # the app is shutting down
        self.stopped.emit(why)

    # ---------------------------------------------------------- messages
    def request(self, command: str, arguments: dict | None = None, callback: Callback | None = None,
                timeout: int = TIMEOUT) -> bool:
        """Send a request; False if no adapter is running (``callback`` is
        then called at once, with success False)."""
        if self.process is None:
            if callback is not None:
                callback(False, {}, "no debugger is running")
            return False
        self._seq += 1
        seq = self._seq
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(timeout)
        timer.timeout.connect(self._timed_out)
        timer.setProperty("seq", seq)
        self._pending[seq] = (command, callback or _ignore, timer)
        if timeout > 0:
            timer.start()
        self.process.write(frame({"seq": seq, "type": "request", "command": command,
                                  "arguments": arguments or {}}))
        return True

    def _timed_out(self):
        seq = self.sender().property("seq") if self.sender() is not None else None
        entry = self._pending.pop(seq, None)
        if entry is not None:
            command, callback, timer = entry
            timer.deleteLater()
            _call(callback, False, {}, f"the debugger didn't answer {command} in time")

    def _fail_pending(self, why: str):
        pending, self._pending = self._pending, {}
        for _command, callback, timer in pending.values():
            timer.stop()
            timer.deleteLater()
            _call(callback, False, {}, why)

    def _read(self):
        if self.process is None:
            return
        for message in self._reader.feed(bytes(self.process.readAllStandardOutput())):
            self._dispatch(message)

    def _dispatch(self, message: dict):
        kind = message.get("type")
        if kind == "response":
            entry = self._pending.pop(message.get("request_seq"), None)
            if entry is None:
                return
            command, callback, timer = entry
            timer.stop()
            timer.deleteLater()
            if command == "initialize" and message.get("success"):
                self.capabilities = message.get("body") or {}
            _call(callback, bool(message.get("success")), message.get("body") or {},
                  str(message.get("message") or ""))
        elif kind == "event":
            self.event.emit(str(message.get("event") or ""), message.get("body") or {})
        elif kind == "request" and self.process is not None:  # (runInTerminal…: not supported yet)
            self._seq += 1
            self.process.write(frame({"seq": self._seq, "type": "response", "request_seq": message.get("seq"),
                                      "command": message.get("command"), "success": False,
                                      "message": "not supported"}))


def _ignore(*_):
    pass


def _call(callback: Callback, success: bool, body: dict, message: str):
    try:
        callback(success, body, message)
    except RuntimeError:
        pass  # what asked has been deleted (a closed panel)
