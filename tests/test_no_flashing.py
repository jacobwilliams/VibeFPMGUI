"""Moving through the project's files: no window flashing up, and each
file checked by fortitude once."""

from __future__ import annotations

from conftest import open_editor
from qtpy.QtCore import QEvent, QObject, QProcess, Qt
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication

from vibefpmgui import lint


class WindowsShown(QObject):
    """The top-level windows shown while it's installed."""

    def __init__(self):
        super().__init__()
        self.shown = []

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Show and obj.isWidgetType() and obj.isWindow():
            self.shown.append(type(obj).__name__)
        return False


def test_choosing_files_in_the_tree_shows_no_other_window(qtbot, window, project):
    """(A new tab's minimap was a window of its own for a moment, before the
    layout took it: on Windows, a window flashing up at every file chosen.)"""
    tree = window.project_tree
    tree.reveal(str(project / "app/main.f90"))
    tree.view.setFocus()
    watch = WindowsShown()
    QApplication.instance().installEventFilter(watch)
    try:
        for _ in range(3):
            QTest.keyClick(tree.view, Qt.Key_Down)
            qtbot.wait(20)
        window.open_file(str(project / "src/foo.f90"), force_editor=True)
        qtbot.wait(20)
    finally:
        QApplication.instance().removeEventFilter(watch)
    assert window.current_editor() is not None and watch.shown == []


def test_a_file_is_linted_once_when_opened(qtbot, window, project, monkeypatch):
    if lint.executable() is None:
        return
    started = []
    real = QProcess.start

    def start(process, program, args=(), *rest):
        if "check" in list(args):
            started.append(list(args))
        return real(process, program, args, *rest)

    monkeypatch.setattr(QProcess, "start", start)
    window.set_lint_enabled(True)
    editor = open_editor(window, project / "src/foo.f90")
    qtbot.waitUntil(lambda: editor._lint_process is None and bool(started), timeout=10_000)
    qtbot.wait(800)  # (past the typing pause: the text loaded, the syntax colored, count as changes too)
    assert len(started) == 1
    editor.editor.appendPlainText("! changed")
    qtbot.waitUntil(lambda: len(started) == 2, timeout=10_000)  # (a real change: checked again)
