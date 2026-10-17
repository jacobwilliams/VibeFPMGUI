"""Stop: a run stopped by the user is reported as stopped, however its
process ends (no fpm needed: a stand-in plays it)."""

from __future__ import annotations

import sys

from qtpy.QtCore import QProcess

from vibefpmgui.window import running


def run_stopped(qtbot, window, tmp_path, monkeypatch):
    """A run Stop ends, its process ending normally (code 9) when it's "killed"."""
    script = tmp_path / "fake_fpm.py"
    script.write_text("import sys\nsys.stdin.read()\nprint('<ERROR> stopping due to failed executions')\n"
                      "sys.exit(9)\n")
    monkeypatch.setattr(running, "kill_process_tree", lambda process: process.closeWriteChannel())
    window.fpm_edit.setText(sys.executable)
    window.run_fpm(["build"], False, False, args=[str(script)])
    qtbot.waitUntil(lambda: window.process is not None and window.process.state() == QProcess.ProcessState.Running,
                    timeout=10_000)
    window.stop()
    qtbot.waitUntil(lambda: window.process is None, timeout=10_000)


def test_a_run_stopped_but_ending_normally_is_stopped(qtbot, window, tmp_path, monkeypatch):
    """Stop kills the test program before fpm: fpm, seeing it die, can end
    normally ("returned exit code 9", exit code 9) before it's killed too,
    and the run was reported failed (seen on Linux CI)."""
    run_stopped(qtbot, window, tmp_path, monkeypatch)
    assert window.statusBar().currentMessage() == "Stopped"
    assert "[process was stopped]" in window.console.toPlainText()


def test_the_run_after_a_stopped_one_isnt_stopped(qtbot, window, tmp_path, monkeypatch):
    run_stopped(qtbot, window, tmp_path, monkeypatch)
    script = tmp_path / "fails.py"
    script.write_text("import sys\nsys.exit(3)\n")
    window.run_fpm(["build"], False, False, args=[str(script)])
    qtbot.waitUntil(lambda: window.process is None, timeout=10_000)
    assert window.statusBar().currentMessage() == "Failed (exit code 3)"
