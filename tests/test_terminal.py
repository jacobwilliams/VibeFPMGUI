"""The Terminal tab: a real shell in a pseudo-terminal (macOS and Linux)."""

from __future__ import annotations

import os
import sys

import pytest
from qtpy.QtCore import Qt
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QApplication

from vibefpmgui import terminal

pytestmark = [
    pytest.mark.skipif(sys.platform == "win32", reason="the pty shell is for macOS and Linux"),
]

CONTROL = Qt.MetaModifier if sys.platform == "darwin" else Qt.ControlModifier


@pytest.fixture
def panel(qtbot, monkeypatch, project):
    """A Terminal tab running /bin/sh (not your own shell and its settings)."""
    monkeypatch.setenv("SHELL", "/bin/sh")
    monkeypatch.setenv("PS1", "$ ")
    monkeypatch.setenv("ENV", "")  # (sh -i reads no startup file)
    panel = terminal.TerminalPanel()
    qtbot.addWidget(panel)
    panel.set_directory(project)
    panel.resize(800, 300)
    panel.show()
    qtbot.waitExposed(panel)
    panel.start()
    qtbot.waitUntil(lambda: "$" in screen(panel), timeout=5_000)
    yield panel
    panel.shutdown()


def screen(panel) -> str:
    return panel.view.text()


def run(qtbot, panel, command: str, expect: str):
    """Type a command; wait for ``expect`` in its output (long lines wrap)."""
    QTest.keyClicks(panel.view, command)
    QTest.keyClick(panel.view, Qt.Key_Return)
    qtbot.waitUntil(lambda: expect in screen(panel).split(command, 1)[-1].replace("\n", ""),
                    timeout=5_000)


def test_runs_commands_in_the_project_folder(qtbot, panel, project):
    assert "sh" in panel.title.text()
    run(qtbot, panel, "echo hello-$((6*7))", "hello-42")
    run(qtbot, panel, "pwd -P", str(project.resolve()))


def test_control_c_interrupts(qtbot, panel):
    QTest.keyClicks(panel.view, "sleep 30")
    QTest.keyClick(panel.view, Qt.Key_Return)
    qtbot.wait(300)
    QTest.keyClick(panel.view, Qt.Key_C, CONTROL)
    run(qtbot, panel, "echo after", "after")


def test_paste(qtbot, panel):
    QApplication.clipboard().setText("echo pasted-text")
    panel.view.paste()
    QTest.keyClick(panel.view, Qt.Key_Return)
    qtbot.waitUntil(lambda: screen(panel).count("pasted-text") >= 2, timeout=5_000)


def test_keys_the_shell_needs(panel):
    sent = []
    panel.view.write = sent.append
    for key in (Qt.Key_Delete, Qt.Key_Backspace, Qt.Key_Home, Qt.Key_End):
        QTest.keyClick(panel.view, key)
    assert sent == [b"\x1b[3~", b"\x7f", b"\x1b[H", b"\x1b[F"]
    assert panel.view.focusNextPrevChild(True) is False  # (Tab stays in the terminal)


def test_exit_and_restart(qtbot, panel):
    pid = panel.shell.pid
    run(qtbot, panel, "exit", "exited")
    assert "exited" in panel.title.text() and not panel.shell.is_running()
    panel.restart()
    qtbot.waitUntil(lambda: panel.shell.is_running() and panel.shell.pid != pid, timeout=5_000)
    run(qtbot, panel, "echo again", "again")


def test_shutdown_ends_the_shell(qtbot, panel):
    pid = panel.shell.pid
    panel.shutdown()

    def gone():
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        try:  # (a zombie until reaped)
            return os.waitpid(pid, os.WNOHANG)[0] == pid
        except ChildProcessError:
            return True
        return False

    qtbot.waitUntil(gone, timeout=5_000)


def test_window_shortcut_opens_the_terminal(qtbot, window, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    window.terminal_action.trigger()
    assert window.monitor_tabs.currentWidget() is window.terminal_panel
    qtbot.waitUntil(lambda: window.terminal_panel.shell is not None
                    and window.terminal_panel.shell.is_running(), timeout=5_000)


def test_light_and_dark_colors(panel):
    from qtpy.QtGui import QColor

    from vibefpmgui.ansi import LIGHT

    view = panel.view
    view.set_colors(QColor("#ffffff"), QColor("#1f1f1f"), LIGHT)  # (the system went light)
    assert view._color("default", view.foreground) == QColor("#1f1f1f")
    assert view._color("red", view.foreground) == QColor(LIGHT[1])
    assert view._color("brightblue", view.foreground) == QColor(LIGHT[12])
    assert view._color("cd0000", view.foreground) == QColor(LIGHT[1])  # (38;5;1: the theme's red)
    assert view._color("ff8700", view.foreground) == QColor("#ff8700")


# ----------------------------------------------- the terminal (no shell needed)
@pytest.fixture
def view(qtbot):
    view = terminal.TerminalView()
    qtbot.addWidget(view)
    view.resize(400, 200)
    view.show()
    qtbot.waitExposed(view)
    return view


def test_scrollback(view):
    view.feed("".join(f"line {i}\r\n" for i in range(100)).encode())
    rows = view.screen.lines
    assert len(view.screen.history) == 100 - rows + 1
    assert view.text().splitlines()[0] == "line 0" and "line 99" in view.text()
    view.scroll_lines(10)
    assert view.scroll_bar is None or view._scroll == 10
    view.feed(b"\x1b[3J")  # ("clear" forgets the scrollback)
    assert len(view.screen.history) == 0


def test_alternate_screen(view):
    view.feed(b"$ less file\r\n")
    view.feed(b"\x1b[?1049h\x1b[2J\x1b[Hfull-screen program")
    assert view.screen.alternate and "full-screen program" in view.text()
    assert "less file" not in view.text()
    view.feed(b"\x1b[?1049l")
    assert not view.screen.alternate
    assert "less file" in view.text() and "full-screen program" not in view.text()


def test_resize_keeps_the_cursor_line(view):
    view.feed(b"first\r\nsecond\r\nprompt $ ")
    view.resize(400, 60)  # (fewer rows than the cursor's)
    assert view.screen.lines < 3
    assert view.text().splitlines()[-1].startswith("prompt $")
    assert "first" in view.text()  # (in the scrollback, not lost)


@pytest.mark.parametrize(("key", "mods", "expected"), [
    (Qt.Key_Up, Qt.NoModifier, b"\x1b[A"),
    (Qt.Key_Left, Qt.ShiftModifier, b"\x1b[1;2D"),
    (Qt.Key_Right, CONTROL, b"\x1b[1;5C"),
    (Qt.Key_Delete, Qt.NoModifier, b"\x1b[3~"),
    (Qt.Key_PageDown, Qt.NoModifier, b"\x1b[6~"),
    (Qt.Key_F1, Qt.NoModifier, b"\x1bOP"),
    (Qt.Key_F5, Qt.NoModifier, b"\x1b[15~"),
    (Qt.Key_Backspace, Qt.NoModifier, b"\x7f"),
    (Qt.Key_Return, Qt.NoModifier, b"\r"),
    (Qt.Key_C, CONTROL, b"\x03"),
    (Qt.Key_BracketLeft, CONTROL, b"\x1b"),
])
def test_key_bytes(view, key, mods, expected):
    assert view.key_bytes(key, mods, "") == expected


def test_application_cursor_keys(view):
    view.feed(b"\x1b[?1h")  # (vim, less: DECCKM)
    assert view.key_bytes(Qt.Key_Up, Qt.NoModifier, "") == b"\x1bOA"
    view.feed(b"\x1b[?1l")
    assert view.key_bytes(Qt.Key_Up, Qt.NoModifier, "") == b"\x1b[A"


def test_bracketed_paste(view):
    sent = []
    view.write = sent.append
    QApplication.clipboard().setText("one\ntwo")
    view.paste()
    view.feed(b"\x1b[?2004h")
    view.paste()
    assert sent == [b"one\rtwo", b"\x1b[200~one\rtwo\x1b[201~"]


def test_selection_and_copy(view):
    view.feed(b"hello world\r\nsecond line")
    history = len(view.screen.history)
    view._selection = ((history, 6), (history + 1, 5))
    view.copy()
    assert QApplication.clipboard().text() == "world\nsecond"
    sent = []
    view.write = sent.append
    QTest.keyClick(view, Qt.Key_A)  # (typing clears the selection)
    assert view._selection is None and sent == [b"a"]


def test_queries_are_answered(view):
    sent = []
    view.write = sent.append
    view.feed(b"ab\x1b[6n")  # (where is the cursor?)
    assert sent == [b"\x1b[1;3R"]


def test_draws_without_errors(view):
    view.feed("plain \x1b[1;31mbold red\x1b[0m \x1b[7mreverse\x1b[0m 宽字 \x1b[38;2;255;135;0mtrue\x1b[0m".encode())
    view.setFocus()
    image = view.grab()
    assert not image.isNull()


# ------------------------------------------- the app's environment in the shell
SHELL_OWNED = {"PWD", "OLDPWD", "SHLVL", "_", "TERM"}  # (set by the shell itself / the terminal)


@pytest.mark.parametrize("shell_name", ["zsh", "bash"])
def test_shell_has_exactly_the_app_environment(tmp_path, monkeypatch, shell_name):
    import shutil
    import subprocess

    shell = shutil.which(shell_name)
    if shell is None:
        pytest.skip(f"{shell_name} not installed")
    # Startup files that would change the environment (as conda's "base" or Homebrew do): not read.
    home = tmp_path / "home"
    home.mkdir()
    for name in (".zshenv", ".zprofile", ".zshrc", ".bashrc", ".bash_profile"):
        (home / name).write_text('export PATH="/opt/other/bin:$PATH"\nexport FROM_STARTUP=yes\n')
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setenv("FROM_ACTIVATION", "a value with spaces")  # (as an activate.d script sets)
    launch = terminal.shell_launch()
    result = subprocess.run(launch.argv, input="env -0\nexit\n", env=launch.env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30, check=False)
    shell_env = dict(item.split("=", 1) for item in result.stdout.split("\0") if "=" in item)
    expected = {k: v for k, v in os.environ.items() if k not in SHELL_OWNED}
    got = {k: v for k, v in shell_env.items() if k not in SHELL_OWNED}
    assert "FROM_STARTUP" not in got
    assert got.get("PATH") == os.environ["PATH"]
    assert got == expected
    assert shell_env.get("TERM") == "xterm-256color"


def test_odd_sequences_do_not_stop_the_terminal(view):
    # vim's modifyOtherKeys ("private" SGR) and a kitty keyboard query: pyte can't take them.
    view.feed(b"before \x1b[>4;2m\x1b[?u after")
    view.feed(b"\r\nstill working")
    assert "before  after" in view.text() and "still working" in view.text()
