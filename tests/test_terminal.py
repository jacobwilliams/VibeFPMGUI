"""The Terminal tab: a real shell in a pseudo-terminal (macOS and Linux)."""

from __future__ import annotations

import os
import sys

import pyte
import pytest
from qtpy.QtCore import QPointF, Qt
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
    # (In a session of its own: run from a terminal, an interactive shell would
    # otherwise try to take that terminal over, and be stopped.)
    result = subprocess.run(launch.argv, input="env -0\nexit\n", env=launch.env, cwd=tmp_path,
                            capture_output=True, text=True, timeout=30, check=False,
                            start_new_session=True)
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


def test_word_wrap_off_makes_a_wide_terminal(qtbot, view):
    sizes = []
    view.size_changed.connect(lambda rows, cols: sizes.append(cols))
    visible = view.screen.columns
    long_line = "x" * 200
    view.set_word_wrap(False)
    assert view.screen.columns == terminal.NO_WRAP_COLUMNS and sizes[-1] == terminal.NO_WRAP_COLUMNS
    view.feed(long_line.encode())
    qtbot.waitUntil(lambda: view._hscroll > 0, timeout=2_000)  # (follows the cursor sideways)
    assert view.text() == long_line  # (one line: not wrapped)
    cell = view._cell_at(QPointF(terminal.PADDING + 1, terminal.PADDING + 1))
    assert cell[1] == int(view._hscroll // view.cell_width)  # (the mouse sees the scrolled columns)
    view.feed(b"\r\n$ ")
    qtbot.waitUntil(lambda: view._hscroll == 0, timeout=2_000)  # (back to the left for the prompt)
    view.set_word_wrap(True)
    assert view.screen.columns == visible and sizes[-1] == visible
    view.feed(b"\r\n" + long_line.encode())
    assert long_line not in view.text().splitlines()  # (wrapped now)


def test_word_wrap_setting_reaches_the_terminal(window):
    assert window.terminal_panel.view.word_wrap is window.word_wrap_action.isChecked()
    window.word_wrap_action.trigger()
    assert window.terminal_panel.view.word_wrap is window.word_wrap_action.isChecked()
    assert window.terminal_panel.hscroll_bar.isVisibleTo(window.terminal_panel) is not window.word_wrap_action.isChecked()


def test_text_lines_up_with_the_cells(qtbot):
    # Inside a widget with a smaller font (as the panel), with a font whose size isn't set.
    from qtpy.QtGui import QColor, QFont
    from qtpy.QtWidgets import QWidget

    parent = QWidget()
    parent.setFont(QFont(parent.font().family(), 7))
    qtbot.addWidget(parent)
    view = terminal.TerminalView(parent)
    view.set_terminal_font(QFont("Menlo"))
    view.resize(500, 80)
    parent.show()
    qtbot.waitExposed(parent)
    view.feed(b"0123456789" * 3)
    view._after_output()
    image = view.grab().toImage()
    background = QColor(view.background).rgb()
    rows = range(terminal.PADDING + 1, int(terminal.PADDING + view.cell_height) - 1)
    ink = [x for x in range(image.width()) if any(image.pixel(x, y) != background for y in rows)]
    text_end = max(x for x in ink if x < terminal.PADDING + 30 * view.cell_width - 1)
    assert text_end > terminal.PADDING + 29 * view.cell_width  # (the 30th character is in the 30th cell)


# ------------------------------------------------------------------ reflow
def screen_of(columns: int, lines: int) -> terminal.TerminalScreen:
    screen = terminal.TerminalScreen(columns, lines)
    screen.stream = pyte.ByteStream(terminal.SafeScreen(screen))
    return screen


def all_rows(screen) -> list[str]:
    rows = ["".join(c.data for c in row).rstrip() for row in screen.history]
    rows += ["".join(screen.buffer[y][x].data for x in range(screen.columns)).rstrip()
             for y in range(screen.lines)]
    while rows and not rows[-1]:
        rows.pop()
    return rows


def logical_lines(screen) -> list[str]:
    """The whole lines: wrapped rows joined (as the wrap marks say)."""
    rows = [("".join(c.data for c in row), row.wrapped) for row in screen.history]
    rows += [("".join(screen.buffer[y][x].data for x in range(screen.columns)),
              terminal._wrapped(screen.buffer[y])) for y in range(screen.lines)]
    lines, current = [], ""
    for text, wrapped in rows:
        current += text if wrapped else text.rstrip()
        if not wrapped:
            lines.append(current)
            current = ""
    while lines and not lines[-1]:
        lines.pop()
    return lines


def test_narrower_rewraps_instead_of_cutting_off():
    screen = screen_of(40, 5)
    long_line = "".join(f"word{i} " for i in range(12)).strip()  # (82 characters)
    screen.stream.feed(f"{long_line}\r\nshort\r\n$ ".encode())
    screen.resize(5, 20)
    assert all(len(r) <= 20 for r in all_rows(screen))
    assert logical_lines(screen) == [long_line, "short", "$"]  # (all of it, wrapped at 20)
    assert (screen.cursor.y, screen.cursor.x) == (screen.lines - 1, 2)  # (still after "$ ")


def test_wider_joins_wrapped_lines():
    screen = screen_of(20, 6)
    long_line = "x" * 50
    screen.stream.feed(f"{long_line}\r\nnext\r\n".encode())
    assert all_rows(screen)[:3] == ["x" * 20, "x" * 20, "x" * 10]
    screen.resize(6, 80)
    assert all_rows(screen) == [long_line, "next"]  # (one line again; "next" stays its own)


def test_reflow_keeps_the_cursor_on_its_character():
    screen = screen_of(30, 4)
    screen.stream.feed(("$ " + "a" * 40).encode())  # (a long command being typed: the cursor at its end)
    screen.stream.feed(b"\x1b[5D")  # (5 characters back)
    screen.resize(4, 15)
    line = "".join(all_rows(screen))
    row = len(screen.history) + screen.cursor.y
    offset = sum(len("".join(c.data for c in r)) for r in screen.history) if row >= len(screen.history) else 0
    assert line == "$ " + "a" * 40
    assert screen.cursor.y * 15 + screen.cursor.x + offset == 2 + 40 - 5


def test_scrollback_reflows_and_stays_above():
    screen = screen_of(30, 3)
    screen.stream.feed("".join(f"line {i} " + "-" * 30 + "\r\n" for i in range(6)).encode())
    screen.resize(3, 60)
    rows = all_rows(screen)
    assert rows[:6] == [f"line {i} " + "-" * 30 for i in range(6)]


def test_alternate_screen_is_not_reflowed_but_main_is_after():
    screen = screen_of(20, 5)
    screen.stream.feed(("y" * 30 + "\r\n$ ").encode())
    screen.stream.feed(b"\x1b[?1049h" + b"z" * 30)  # (a full-screen program)
    screen.resize(5, 40)
    assert screen.alternate
    screen.stream.feed(b"\x1b[?1049l")
    assert all_rows(screen) == ["y" * 30, "$"]  # (joined at the new width on the way back)


def test_sideways_scroll_bar_only_when_needed(qtbot, panel):
    view, bar = panel.view, panel.hscroll_bar
    view.set_word_wrap(False)
    view.feed(b"\x1b[2J\x1b[3J\x1b[Hshort\r\n")
    view._after_output()
    assert not bar.isVisibleTo(panel)  # (nothing wider than the window)
    height = view.height()
    view.feed(b"y" * 200 + b"\r\n")
    view._after_output()
    assert bar.isVisibleTo(panel) and bar.maximum() > 0
    assert bar.maximum() < terminal.NO_WRAP_COLUMNS * view.cell_width - view.width()  # (to the text's end)
    qtbot.wait(50)
    assert view.height() == height  # (its room was kept: the terminal didn't change size)
    view.feed(b"\x1b[2J\x1b[3J\x1b[H")  # (clear: no wide text any more)
    view._after_output()
    assert not bar.isVisibleTo(panel)
    view.set_word_wrap(True)
    assert not bar.isVisibleTo(panel)


def test_tab_stops_follow_the_width():
    screen = screen_of(80, 3)
    screen.resize(3, 200)
    screen.stream.feed(b"a" + b"\t" * 12 + b"b")  # (ls aligns its columns with tabs)
    assert screen.cursor.x == 97 and screen.buffer[0][96].data == "b"
    screen.resize(3, 40)
    assert max(screen.tabstops) < 40


def test_another_project_restarts_the_shell_there(qtbot, panel, project, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    pid = panel.shell.pid
    panel.set_directory(project)  # (the same project, reloaded: the shell stays)
    assert panel.shell.pid == pid
    panel.set_directory(other)
    qtbot.waitUntil(lambda: panel.shell.is_running() and panel.shell.pid != pid, timeout=5_000)
    run(qtbot, panel, "pwd -P", str(other.resolve()))
    assert "other" in panel.title.text()


def test_opening_another_project_moves_the_terminal(qtbot, window, conflict_project, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    panel = window.terminal_panel
    window.terminal_action.trigger()
    qtbot.waitUntil(lambda: panel.shell is not None and panel.shell.is_running(), timeout=5_000)
    pid = panel.shell.pid
    window.reload()  # (same project: same shell)
    assert panel.shell.pid == pid
    window.open_manifest(conflict_project / "fpm.toml")
    qtbot.waitUntil(lambda: panel.shell.is_running() and panel.shell.pid != pid, timeout=5_000)
    assert panel.directory == conflict_project


def test_choosing_the_tab_focuses_the_terminal(qtbot, window, monkeypatch):
    monkeypatch.setenv("SHELL", "/bin/sh")
    window.activateWindow()
    tabs = window.monitor_tabs
    tabs.setCurrentWidget(window.build_monitor)
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, Qt.NoModifier,
                     tabs.tabBar().tabRect(tabs.indexOf(window.terminal_panel)).center())
    assert tabs.currentWidget() is window.terminal_panel
    qtbot.waitUntil(lambda: QApplication.focusWidget() is window.terminal_panel.view, timeout=2_000)
