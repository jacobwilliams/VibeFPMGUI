"""Running tests in parallel (Build options → Parallel tests).

fpm runs tests one after another, so for a parallel run fpm only builds
them: ``fpm test --runner "test_runner.py --collect"`` prints each test's
command line instead of running it (see `vibefpmgui.test_runner`). Then
`ParallelTests` runs those commands itself, a few at a time, each in its own
process with its own output, so the outputs never mix.
"""

import codecs
import time
from collections.abc import Callable

from qtpy.QtCore import QObject, QProcess, QProcessEnvironment, Signal


class ParallelTests(QObject):
    """Run ``(name, argv)`` commands, at most ``workers`` at a time. Signals
    say when each starts, what it prints and how it ended; `done` comes once
    all have ended, or once the ones running when `stop` was called have."""

    started = Signal(str)                    # name
    output = Signal(str, str)                # name, text
    finished = Signal(str, int, float, bool)  # name, exit code, seconds, stopped
    done = Signal(bool)                      # stopped

    def __init__(self, commands: list[tuple[str, list[str]]], workers: int, cwd: str,
                 env: QProcessEnvironment, inner: list[str], kill: Callable[[QProcess], None],
                 parent=None):
        """``inner``: a runner put in front of each command (fpm's --runner,
        e.g. ``mpirun -np 2``); ``kill`` stops a process and what it started."""
        super().__init__(parent)
        self.queue = list(commands)
        self.workers = max(1, workers)
        self.cwd, self.env, self.inner, self.kill = cwd, env, inner, kill
        self.running: dict[str, QProcess] = {}
        self.stopping = False
        self._started_at: dict[str, float] = {}
        self._decoders: dict[str, codecs.IncrementalDecoder] = {}

    def start(self):
        self._fill()

    def stop(self):
        """Run no more, and kill the ones running (`done` follows)."""
        self.stopping = True
        self.queue.clear()
        for process in list(self.running.values()):
            self.kill(process)
        if not self.running:
            self.done.emit(True)

    def wait(self, msecs: int = 3000):
        for process in list(self.running.values()):
            process.waitForFinished(msecs)

    def command(self, argv: list[str]) -> list[str]:
        return self.inner + argv

    def _fill(self):
        while self.queue and len(self.running) < self.workers and not self.stopping:
            name, argv = self.queue.pop(0)
            self._launch(name, self.command(argv))
        if not self.running and not self.queue:
            self.done.emit(self.stopping)

    def _launch(self, name: str, command: list[str]):
        process = QProcess(self)
        process.setWorkingDirectory(self.cwd)
        process.setProcessEnvironment(self.env)
        process.setProcessChannelMode(QProcess.MergedChannels)
        # (Lambdas are fine here: a QProcess's signals come in the GUI thread.)
        process.readyReadStandardOutput.connect(lambda: self._read(name, process))
        process.finished.connect(lambda code, status: self._ended(name, process, code, status))
        process.errorOccurred.connect(lambda error: self._error(name, process, error, command[0]))
        self.running[name] = process
        self._started_at[name] = time.monotonic()
        self._decoders[name] = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.started.emit(name)
        process.start(command[0], command[1:])

    def _read(self, name: str, process: QProcess, final: bool = False):
        text = self._decoders[name].decode(bytes(process.readAllStandardOutput()), final=final)
        if text:
            text = text.replace("\r\n", "\n")
            # (Progress lines redrawn with \r: only their last state.)
            text = "\n".join(line.rsplit("\r", 1)[-1] for line in text.split("\n"))
            self.output.emit(name, text)

    def _ended(self, name: str, process: QProcess, code: int, status):
        if name not in self.running:
            return
        self._read(name, process, final=True)
        stopped = self.stopping
        if status == QProcess.CrashExit and not stopped:
            code = code or -1  # (a crash: the exit code isn't the program's)
        self._finish(name, process, code, stopped)

    def _error(self, name: str, process: QProcess, error, program: str):
        if error == QProcess.FailedToStart and name in self.running:
            self.output.emit(name, f"could not run {program}: {process.errorString()}\n")
            self._finish(name, process, 127, False)

    def _finish(self, name: str, process: QProcess, code: int, stopped: bool):
        del self.running[name]
        seconds = time.monotonic() - self._started_at.pop(name, time.monotonic())
        self._decoders.pop(name, None)
        process.deleteLater()
        self.finished.emit(name, code, seconds, stopped)
        self._fill()
