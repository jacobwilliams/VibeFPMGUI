"""Blocking calls (a program run to completion, a slow server) on a pool
thread, so the window never waits for them; the result comes back in the
GUI thread."""

from __future__ import annotations

from collections.abc import Callable

from qtpy.QtCore import QObject, QRunnable, QThreadPool, Signal


class _Receiver(QObject):
    """Brings a background call's result to the GUI thread (it lives there,
    so its signal is delivered there) and hands it to ``then``."""

    done = Signal(object)

    def __init__(self, then, parent=None):
        super().__init__(parent)
        self._then = then
        self.done.connect(self._deliver)

    def _deliver(self, result):
        self.deleteLater()
        self._then(result)


class _Call(QRunnable):
    def __init__(self, fn, receiver: _Receiver):
        super().__init__()
        self.fn, self.receiver = fn, receiver

    def run(self):
        result = self.fn()
        try:
            self.receiver.done.emit(result)
        except RuntimeError:
            pass  # (the window that asked has closed)


def call_in_background(fn: Callable[[], object], then: Callable[[object], None], parent: QObject):
    """Run ``fn()`` on a pool thread, then ``then(result)`` in the GUI thread
    (not at all if ``parent`` is gone by then)."""
    QThreadPool.globalInstance().start(_Call(fn, _Receiver(then, parent)))
