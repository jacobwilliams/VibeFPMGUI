"""The *Terminal* tab: your shell, in the project's folder, with exactly the
environment VibeFPMGUI was started with.

The shell gets the app's environment variables as they are (only ``TERM``
is set, for the terminal, and a prompt: ``PS1``) and is started without its startup files
(``zsh -f``, ``bash --noprofile --norc``, ``fish --no-config``), which
would change them: conda's "base" activation, Homebrew's or your own PATH
changes. Started from an activated pixi or conda environment, the terminal
is in that environment, with everything its activation set (PATH,
CONDA_PREFIX, the compilers' variables...): ``fpm`` and the compilers are
the ones the app uses. (So your prompt, aliases and other shell settings
don't load; the prompt is a plain one, starting with the environment's
name, e.g. ``(VibeFPMGUI) src %``, as activation would show it.)

On macOS and Linux the shell runs in a pseudo-terminal read with a
`QSocketNotifier`, so everything happens in the GUI thread; on Windows it
runs through pywinpty (ConPTY), read in a thread.

The terminal is [pyte](https://github.com/selectel/pyte) (the screen: escape
codes, colors, cursor) drawn by `TerminalView`, with scrollback, the
alternate screen (full-screen programs: vim, less, htop) and reflow (long
lines wrapped again when the terminal is resized) added in
`TerminalScreen`. `TerminalView` handles the keys as xterm sends them
(application cursor keys, function keys, modifiers), ⌘C/⌘V (macOS) or
Ctrl+Shift+C/V (elsewhere) to copy and paste while Ctrl keys go to the
shell, bracketed paste, selection, scrolling, and colors that follow
light/dark mode.
"""

import os
import shutil
import signal
import struct
import sys
import threading
import warnings
from collections import deque
from pathlib import Path
from typing import NamedTuple

import pyte
from pyte.graphics import FG_BG_256
from qtpy.QtCore import (
    QEvent,
    QObject,
    QPointF,
    QRectF,
    QSocketNotifier,
    Qt,
    QTimer,
    Signal,
)
from qtpy.QtGui import QClipboard, QColor, QFont, QFontMetricsF, QPainter, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .ansi import DARK, LIGHT

MAC = sys.platform == "darwin"
WINDOWS = sys.platform == "win32"


def user_shell() -> str:
    if WINDOWS:
        return os.environ.get("COMSPEC", "cmd.exe")
    return os.environ.get("SHELL") or shutil.which("zsh") or shutil.which("bash") or "/bin/sh"


class Launch(NamedTuple):
    argv: list[str]
    env: dict[str, str]


# Options that keep each shell from reading its startup files (they would
# change the environment: conda's "base", Homebrew, PATH edits...).
NO_STARTUP_FILES = {
    "zsh": ["-f"],                         # no .zshenv, .zprofile, .zshrc, .zlogin
    "bash": ["--noprofile", "--norc"],
    "fish": ["--no-config"],
    "sh": [], "dash": [],                  # (read startup files only with $ENV / as login shells)
}


# A plain prompt (the startup files that would set one aren't read): the
# folder's name, after the environment's name when one is active.
PROMPTS = {"zsh": "%1~ %# ", "bash": "\\W \\$ ", "sh": "$ ", "dash": "$ "}


def shell_launch() -> Launch:
    """How to start the user's shell with exactly the app's environment:
    the same variables (only TERM is set, for the terminal, and PS1, the
    prompt), and no startup files to change them."""
    shell = user_shell()
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    if WINDOWS:
        return Launch([shell], env)
    name = Path(shell).name
    if name in PROMPTS and "PS1" not in env:  # (fish has its own prompt function)
        active = environment_name()
        env["PS1"] = (f"({active}) " if active else "") + PROMPTS[name]
    return Launch([shell, *NO_STARTUP_FILES.get(name, []), "-i"], env)


def environment_name() -> str:
    """The activated environment the app (and the terminal) runs in, if any:
    a pixi workspace's name (plus its environment, when not "default"), or a
    conda environment's."""
    pixi = os.environ.get("PIXI_PROJECT_NAME", "")
    if pixi:
        which = os.environ.get("PIXI_ENVIRONMENT_NAME", "default")
        return pixi if which in ("", "default") else f"{pixi}:{which}"
    return os.environ.get("CONDA_DEFAULT_ENV", "")


# ------------------------------------------------------------ the shell
class PtyShell(QObject):
    """A shell in a pseudo-terminal (macOS, Linux), read in the GUI thread."""

    output = Signal(bytes)
    exited = Signal()

    def __init__(self, launch: Launch, cwd: Path, rows: int, cols: int, parent=None):
        super().__init__(parent)
        self.launch, self.cwd, self.rows, self.cols = launch, cwd, rows, cols
        self.pid = -1
        self.fd = -1
        self._notifier: QSocketNotifier | None = None
        # Input the shell hasn't taken yet (it isn't reading: a long command,
        # a big paste); sent when the pty can take more (see _flush).
        self._pending = bytearray()
        self._write_notifier: QSocketNotifier | None = None

    def start(self):
        import pty

        argv, env, cwd = self.launch.argv, self.launch.env, str(self.cwd)
        program = shutil.which(argv[0]) or argv[0]
        with warnings.catch_warnings():  # (the child only execs: fork's thread caveat doesn't apply)
            warnings.simplefilter("ignore", DeprecationWarning)
            pid, fd = pty.fork()
        if pid == 0:  # the child: become the shell
            try:
                os.chdir(cwd)
            except OSError:
                pass
            try:
                os.execve(program, argv, env)
            finally:
                os._exit(127)
        self.pid, self.fd = pid, fd
        os.set_blocking(fd, False)
        self.resize(self.rows, self.cols)
        self._notifier = QSocketNotifier(fd, QSocketNotifier.Read, self)
        self._notifier.activated.connect(self._read)

    def _read(self, *_):
        try:
            data = os.read(self.fd, 65536)
        except BlockingIOError:
            return
        except OSError:  # (Linux: EIO once the shell has exited)
            data = b""
        if data:
            self.output.emit(data)
        else:
            self._finish()

    def write(self, data: bytes):
        """Send input to the shell, now or (what doesn't fit yet) once the
        pty can take more: never waiting for it in the GUI thread."""
        if self.fd < 0:
            return
        self._pending += data
        self._flush()

    def _flush(self, *_):
        while self._pending and self.fd >= 0:
            try:
                written = os.write(self.fd, self._pending)
            except BlockingIOError:
                break  # (full: the rest when it's writable again)
            except OSError:
                self._pending.clear()
                break
            del self._pending[:written]
        waiting = bool(self._pending) and self.fd >= 0
        if waiting and self._write_notifier is None:
            self._write_notifier = QSocketNotifier(self.fd, QSocketNotifier.Write, self)
            self._write_notifier.activated.connect(self._flush)
        if self._write_notifier is not None:
            self._write_notifier.setEnabled(waiting)

    def resize(self, rows: int, cols: int):
        self.rows, self.cols = rows, cols
        if self.fd < 0 or rows <= 0 or cols <= 0:
            return
        import fcntl
        import termios

        try:  # (the shell gets SIGWINCH from the kernel)
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _finish(self):
        if self._notifier is not None:
            self._notifier.setEnabled(False)
            self._notifier = None
        if self._write_notifier is not None:
            self._write_notifier.setEnabled(False)
            self._write_notifier = None
        self._pending.clear()
        if self.fd >= 0:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = -1
        if self.pid > 0:
            try:
                os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                pass
            self.pid = -1
            self.exited.emit()

    def stop(self):
        """End the shell (and what it runs), e.g. when the window closes."""
        pid = self.pid
        if pid > 0:
            for sig in (signal.SIGHUP, signal.SIGCONT):
                try:
                    os.kill(pid, sig)
                except ProcessLookupError:
                    break
        self._finish()
        if pid > 0:
            def reap():  # (no zombie: force it if SIGHUP was ignored)
                try:
                    if os.waitpid(pid, os.WNOHANG) == (0, 0):
                        os.kill(pid, signal.SIGKILL)
                        os.waitpid(pid, 0)
                except (ChildProcessError, ProcessLookupError):
                    pass
            QTimer.singleShot(1000, reap)

    def is_running(self) -> bool:
        return self.fd >= 0


class WinptyShell(QObject):
    """A shell through pywinpty (Windows ConPTY). Its reads block, so they
    run in a thread; the output comes back to the GUI thread as a signal."""

    output = Signal(bytes)
    exited = Signal()

    def __init__(self, launch: Launch, cwd: Path, rows: int, cols: int, parent=None):
        super().__init__(parent)
        self.launch, self.cwd, self.rows, self.cols = launch, cwd, rows, cols
        self._process = None

    def start(self):
        from winpty import PtyProcess

        self._process = PtyProcess.spawn(self.launch.argv, cwd=str(self.cwd), env=self.launch.env,
                                         dimensions=(self.rows, self.cols))
        threading.Thread(target=self._read_loop, args=(self._process,), daemon=True,
                         name="Terminal output").start()

    def _read_loop(self, process):
        try:
            while True:
                data = process.read(65536)
                if not data and not process.isalive():
                    break
                if data:
                    self.output.emit(data.encode("utf-8") if isinstance(data, str) else data)
        except (EOFError, OSError):
            pass
        self.exited.emit()

    def write(self, data: bytes):
        if self._process is not None:
            try:
                self._process.write(data.decode("utf-8", errors="replace"))
            except (EOFError, OSError):
                pass

    def resize(self, rows: int, cols: int):
        self.rows, self.cols = rows, cols
        if self._process is not None and rows > 0 and cols > 0:
            try:
                self._process.setwinsize(rows, cols)
            except (EOFError, OSError):
                pass

    def stop(self):
        if self._process is not None:
            try:
                self._process.terminate(force=True)
            except (EOFError, OSError):
                pass
            self._process = None

    def is_running(self) -> bool:
        return self._process is not None and self._process.isalive()


# --------------------------------------------------------------- screen
SCROLLBACK = 10_000  # lines kept above the screen
NO_WRAP_COLUMNS = 300  # the terminal's width without word wrap (wider than the window: scroll sideways)

# Private modes (pyte keeps them shifted, see pyte.modes).
DECCKM = 1 << 5                 # application cursor keys
ALT_SCREEN = {47 << 5, 1047 << 5, 1049 << 5}
BRACKETED_PASTE = 2004 << 5


class Row(list):
    """A line of the scrollback: its Chars (trailing blanks dropped, unless
    it was wrapped), and whether it was wrapped (the next row continues it)."""
    wrapped = False


def _wrapped(line) -> bool:
    return getattr(line, "wrapped", False)


class TerminalScreen(pyte.Screen):
    """pyte's screen, with what pyte lacks: scrollback (the lines that
    scroll off the top), the alternate screen full-screen programs draw on,
    and reflow: lines pyte wrapped at the right edge are marked, and joined
    and wrapped again at the new width when the terminal is resized (pyte
    would cut them off, or leave them wrapped short)."""

    def __init__(self, columns: int, lines: int):
        self.history: deque = deque(maxlen=SCROLLBACK)  # Rows above the screen
        self.alternate = False
        self._saved_main = None
        self._drawing = False
        self.reply = lambda data: None  # answers to the terminal's queries go to the shell
        super().__init__(columns, lines)

    # ------------------------------------------------------- wrapped lines
    def draw(self, data):
        self._drawing = True  # (a line feed now is pyte wrapping at the right edge)
        try:
            super().draw(data)
        finally:
            self._drawing = False

    def linefeed(self):
        # Mark the row being left: wrapped (it continues on the next) or not.
        self.buffer[self.cursor.y].wrapped = self._drawing
        super().linefeed()

    def _row(self, y: int) -> Row:
        line = self.buffer[y]
        row = Row(line[x] for x in range(self.columns))
        row.wrapped = _wrapped(line)
        if not row.wrapped:
            while row and row[-1] == self.default_char:
                row.pop()
        return row

    def index(self):
        top, bottom = self.margins or pyte.screens.Margins(0, self.lines - 1)
        if self.cursor.y == bottom and top == 0 and not self.alternate:
            self.history.append(self._row(0))
        super().index()

    def erase_in_display(self, how=0, *args, **kwargs):
        if how == 3:  # ("clear" also clears the scrollback)
            self.history.clear()
        rows = {0: range(self.cursor.y, self.lines), 1: range(self.cursor.y)}.get(how, range(self.lines))
        for y in rows:
            self.buffer[y].wrapped = False
        super().erase_in_display(how, *args, **kwargs)

    def erase_in_line(self, how=0, private=False):
        if how in (0, 2):  # (the end of the line is gone: it no longer continues)
            self.buffer[self.cursor.y].wrapped = False
        super().erase_in_line(how, private)

    # -------------------------------------------------------------- resize
    def resize(self, lines=None, columns=None):
        lines, columns = lines or self.lines, columns or self.columns
        if (lines, columns) == (self.lines, self.columns):
            return
        if columns != self.columns:
            # Tab stops every 8 columns across the new width (pyte sets them
            # once, for the first width: a tab past it went to the last column).
            self.tabstops = {x for x in self.tabstops if x < columns}
            first_new = (min(self.columns, columns) + 7) // 8 * 8  # (the old ones stopped before the old width)
            self.tabstops.update(range(max(8, first_new), columns, 8))
        if self.alternate:  # (full-screen programs redraw themselves at the new size)
            super().resize(lines, columns)
            self.cursor.y = min(self.cursor.y, self.lines - 1)
            self.cursor.x = min(self.cursor.x, self.columns - 1)
            return
        self._reflow(lines, columns)

    def _reflow(self, lines: int, columns: int):
        """Join the wrapped rows (scrollback and screen) into whole lines and
        wrap them again at the new width, keeping the cursor on its character
        and the bottom of the output at the bottom of the screen."""
        last = max([self.cursor.y] + [y for y in range(self.lines) if self._row(y)])
        rows = list(self.history) + [self._row(y) for y in range(last + 1)]
        cursor_row = len(self.history) + self.cursor.y
        # Whole lines (lists of Chars); where the cursor is in them.
        logical, cursor_at = [], (0, 0)
        current: list = []
        for n, row in enumerate(rows):
            if n == cursor_row:
                cursor_at = (len(logical), len(current) + self.cursor.x)
            current.extend(row)
            if not row.wrapped or n == len(rows) - 1:
                logical.append(current)
                current = []
        # Wrap them at the new width.
        new_rows: list[Row] = []
        cursor_pos = (0, 0)
        for index, cells in enumerate(logical):
            start_row = len(new_rows)
            pieces = [cells[i:i + columns] for i in range(0, len(cells), columns)] or [[]]
            for k, piece in enumerate(pieces):
                row = Row(piece)
                row.wrapped = k < len(pieces) - 1
                new_rows.append(row)
            if index == cursor_at[0]:
                offset = cursor_at[1]
                y, x = divmod(offset, columns)
                if y >= len(pieces) and x == 0 and offset:  # (just past a full row: stay at its end)
                    y, x = y - 1, columns
                cursor_pos = (start_row + y, x)
        while len(new_rows) <= cursor_pos[0]:
            new_rows.append(Row())
        top = max(0, len(new_rows) - lines, cursor_pos[0] - lines + 1)
        top = min(top, cursor_pos[0])
        self.history.clear()
        self.history.extend(new_rows[:top])
        self.buffer.clear()
        for y, row in enumerate(new_rows[top:top + lines]):
            line = self.buffer[y]
            for x, char in enumerate(row):
                line[x] = char
            line.wrapped = row.wrapped
        self.lines, self.columns = lines, columns
        self.set_margins()
        self.cursor.y = cursor_pos[0] - top
        self.cursor.x = min(cursor_pos[1], columns)
        self.dirty.update(range(lines))

    # ---------------------------------------------------- alternate screen
    def set_mode(self, *modes, **kwargs):
        if kwargs.get("private") and not self.alternate and {m << 5 for m in modes} & ALT_SCREEN:
            self._saved_main = (dict(self.buffer), self.cursor.x, self.cursor.y, self.cursor.attrs,
                                self.lines, self.columns)
            self.alternate = True
            self.buffer.clear()
            self.dirty.update(range(self.lines))
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes, **kwargs):
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private") and self.alternate and {m << 5 for m in modes} & ALT_SCREEN:
            buffer, x, y, attrs, lines, columns = self._saved_main
            self.alternate = False
            new_size = self.lines, self.columns
            # Back to the main screen as it was, then reflowed if the size changed meanwhile.
            self.buffer.clear()
            self.buffer.update(buffer)
            self.lines, self.columns = lines, columns
            self.cursor.x, self.cursor.y, self.cursor.attrs = x, y, attrs
            self.set_margins()
            self.resize(*new_size)
            self.dirty.update(range(self.lines))

    def write_process_input(self, data: str):
        self.reply(data.encode("utf-8"))


class SafeScreen:
    """The screen as pyte's parser sees it. A sequence pyte can't handle
    (e.g. vim's "\\e[>4;2m", which pyte passes on with private=True to a
    method that doesn't take it) is dropped: an error inside pyte's parser
    would end it for good, and the terminal would show nothing more."""

    def __init__(self, screen):
        self._screen = screen

    def __getattr__(self, name):
        value = getattr(self._screen, name)
        if not callable(value):
            return value

        def safe(*args, **kwargs):
            try:
                return value(*args, **kwargs)
            except Exception:  # noqa: BLE001 - one odd sequence must not stop the terminal
                return None

        return safe


# --------------------------------------------------------------- widget
def terminal_colors(palette: QPalette) -> tuple[QColor, QColor, list[str]]:
    """(background, text, the 16 ANSI colors) for the terminal: dark or
    light like the app (the ANSI colors are the Output panel's)."""
    if palette.color(QPalette.Base).lightness() < 128:
        return QColor("#1e1e1e"), QColor("#d4d4d4"), DARK
    return QColor("#ffffff"), QColor("#1f1f1f"), LIGHT


# pyte's color names -> index in the 16 ANSI colors.
ANSI_NAMES = {name: i for i, name in enumerate(
    ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"])}
ANSI_NAMES.update({"bright" + name: i + 8 for name, i in list(ANSI_NAMES.items())})
ANSI_NAMES.update({"yellow": 3, "brightyellow": 11})
ANSI_HEX = {FG_BG_256[i]: i for i in range(16)}  # (38;5;n with n < 16: the theme's colors too)

# Keys -> what xterm sends ("{m}": the modifier parameter, when there is one).
CURSOR_KEYS = {Qt.Key_Up: "A", Qt.Key_Down: "B", Qt.Key_Right: "C", Qt.Key_Left: "D",
               Qt.Key_Home: "H", Qt.Key_End: "F"}
TILDE_KEYS = {Qt.Key_Insert: 2, Qt.Key_Delete: 3, Qt.Key_PageUp: 5, Qt.Key_PageDown: 6,
              Qt.Key_F5: 15, Qt.Key_F6: 17, Qt.Key_F7: 18, Qt.Key_F8: 19, Qt.Key_F9: 20,
              Qt.Key_F10: 21, Qt.Key_F11: 23, Qt.Key_F12: 24}
SS3_KEYS = {Qt.Key_F1: "P", Qt.Key_F2: "Q", Qt.Key_F3: "R", Qt.Key_F4: "S"}
# On macOS ⌘ is Qt's ControlModifier and the Control key its MetaModifier.
COMMAND = Qt.ControlModifier if MAC else Qt.NoModifier
CONTROL = Qt.MetaModifier if MAC else Qt.ControlModifier
PADDING = 4


class TerminalView(QWidget):
    """Draws a `TerminalScreen` and sends keys to the shell (see the
    module's description)."""

    size_changed = Signal(int, int)  # rows, columns

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        self.setCursor(Qt.IBeamCursor)
        self.setMinimumSize(80, 40)
        self.write = lambda data: None  # bytes to the shell (set by the panel)
        self.screen = TerminalScreen(80, 24)
        self.screen.reply = lambda data: self.write(data)
        self.stream = pyte.ByteStream(SafeScreen(self.screen))
        self.scroll_bar: QScrollBar | None = None
        self.hscroll_bar: QScrollBar | None = None
        self.word_wrap = True       # View → Word Wrap (off: a wide terminal, scrolled sideways)
        self._hscroll = 0           # pixels scrolled to the right (without word wrap)
        self._scroll = 0            # lines scrolled up from the bottom (0: following the output)
        self._scroll_px = 0.0       # and pixels more, into the line above (trackpad: smooth, see scroll_pixels)
        self._selection = None      # ((line, column), (line, column)) in all lines (scrollback + screen)
        self._colors: dict[str, QColor] = {}
        self.set_colors(QColor("#1e1e1e"), QColor("#d4d4d4"), DARK)
        self.set_terminal_font(QFont("Menlo" if MAC else "Monospace"))
        self._redraw_all = True     # (else only the rows the output changed: see _after_output)
        self._wheel_rest = 0.0      # part of a line scrolled, not yet moved (see wheelEvent)
        self._cursor_drawn_at = 0   # the screen row the cursor was last drawn on
        self._repaint_timer = QTimer(self)  # (output in bursts: draw at most every few ms)
        self._repaint_timer.setSingleShot(True)
        self._repaint_timer.setInterval(10)
        self._repaint_timer.timeout.connect(self._after_output)

    # ------------------------------------------------------------ setup
    def set_colors(self, background: QColor, text: QColor, ansi: list[str]):
        self.background, self.foreground = QColor(background), QColor(text)
        self.ansi = [QColor(c) for c in ansi]
        self._colors.clear()
        self.update()

    def set_terminal_font(self, font: QFont):
        # Family and size set explicitly: a size left unset would be measured
        # at the application's default size but drawn at the widget's.
        size = font.pointSizeF() if font.pointSizeF() > 0 else QApplication.font().pointSizeF()
        font = QFont(font)
        font.setFamily(font.family())
        font.setPointSizeF(size)
        font.setStyleHint(QFont.Monospace)
        font.setFixedPitch(True)
        font.setKerning(False)
        self.font_normal = font
        self.font_bold = QFont(font)
        self.font_bold.setBold(True)
        metrics = QFontMetricsF(font, self)  # (as drawn on this widget: its screen's resolution)
        self.cell_width = metrics.horizontalAdvance("M")
        self.cell_height = metrics.lineSpacing()
        self.ascent = metrics.ascent()
        self._fit()
        self.update()

    def connect_scroll_bar(self, bar: QScrollBar):
        self.scroll_bar = bar
        bar.valueChanged.connect(self._scrolled)
        self._update_scroll_bar()

    def connect_hscroll_bar(self, bar: QScrollBar):
        self.hscroll_bar = bar
        bar.valueChanged.connect(self._hscrolled)
        self._update_hscroll_bar()

    def set_word_wrap(self, on: bool):
        """With word wrap the terminal is as wide as the window and long
        lines wrap; without, it's NO_WRAP_COLUMNS wide and the window shows
        part of it (the shell is told the real width either way, so line
        editing and full-screen programs stay right)."""
        self.word_wrap = on
        if on:
            self._hscroll = 0
        self._fit()
        self._update_hscroll_bar()
        self.update()

    # ----------------------------------------------------------- output
    def feed(self, data: bytes):
        """Output from the shell."""
        before = len(self.screen.history)
        self.stream.feed(data)
        added = len(self.screen.history) - before
        if added:  # (lines scrolled into the scrollback: everything in view moved)
            self._redraw_all = True
        if self._scroll:  # (reading the scrollback: keep the same lines in view)
            self._scroll = min(self._scroll + max(0, added), len(self.screen.history))
        if not self._repaint_timer.isActive():
            self._repaint_timer.start()

    def _after_output(self):
        hscroll = self._hscroll
        self._update_scroll_bar()
        self.ensure_cursor_visible()
        self._update_hscroll_bar()
        if self._redraw_all or self._scroll or self._scroll_px or self._hscroll != hscroll:
            self.update()  # (scrolled: every row in view is another)
        else:
            self._update_rows(set(self.screen.dirty))
        self._redraw_all = False
        self.screen.dirty.clear()

    def _update_rows(self, rows: set[int]):
        """Repaint only these screen rows (what the output changed), and the
        cursor's old and new rows."""
        cursor = self.screen.cursor.y
        rows |= {cursor, self._cursor_drawn_at}
        for row in rows:
            if 0 <= row < self.screen.lines:
                top = int(PADDING + row * self.cell_height)
                self.update(0, top, self.width(), int(self.cell_height) + 2)

    # ------------------------------------------------- sideways (no wrap)
    def _used_columns(self) -> int:
        """How far the text in view (and the cursor) reaches, in columns."""
        screen = self.screen
        first = len(screen.history) - self._scroll
        cursor_row = len(screen.history) + screen.cursor.y - first
        widest = screen.cursor.x + 1 if 0 <= cursor_row < screen.lines else 0
        blank = screen.default_char
        for n in range(first, first + screen.lines):
            line = self.line(n)
            if isinstance(line, list):
                end = len(line)
                while end > widest and line[end - 1] == blank:
                    end -= 1
            else:  # (a screen line: only the cells written are in it)
                end = max((x + 1 for x, cell in line.items() if cell != blank), default=0)
            widest = max(widest, min(end, screen.columns))
        return widest

    def _scroll_range(self) -> int:
        """How far the view can scroll sideways (pixels): to the end of the
        widest text in view, not the terminal's full width."""
        if self.word_wrap:
            return 0
        return max(0, int(self._used_columns() * self.cell_width + 2 * PADDING) - self.width())

    def _update_hscroll_bar(self):
        bar = self.hscroll_bar
        maximum = self._scroll_range()
        self._hscroll = min(self._hscroll, maximum)
        if bar is None:
            return
        bar.blockSignals(True)
        bar.setRange(0, maximum)
        bar.setPageStep(max(1, self.width()))
        bar.setSingleStep(max(1, int(self.cell_width * 4)))
        bar.setValue(self._hscroll)
        bar.blockSignals(False)
        # Shown only when there's something to scroll to; without word wrap
        # its room is kept while hidden, so the terminal's height (and the
        # shell's idea of it) doesn't change as it comes and goes.
        policy = bar.sizePolicy()
        if policy.retainSizeWhenHidden() == self.word_wrap:
            policy.setRetainSizeWhenHidden(not self.word_wrap)
            bar.setSizePolicy(policy)
            layout = bar.parentWidget().layout() if bar.parentWidget() else None
            if layout is not None:  # (a hidden widget's new policy doesn't re-lay-out by itself)
                layout.invalidate()
                layout.activate()
        bar.setVisible(maximum > 0)

    def _hscrolled(self, value: int):
        self._hscroll = value
        self.update()

    def scroll_sideways(self, pixels: int):
        if not self.word_wrap:
            self._hscroll = max(0, min(self._scroll_range(), self._hscroll + pixels))
            self._update_hscroll_bar()
            self.update()

    def ensure_cursor_visible(self):
        """Without word wrap: scroll sideways to the cursor (as you type, and
        back to the left at a new prompt)."""
        if self.word_wrap or self._scroll or self._scroll_px:
            return
        x = PADDING + self.screen.cursor.x * self.cell_width
        margin = 4 * self.cell_width
        if x < self._hscroll + margin:
            self._hscroll = int(max(0, x - margin)) if x > self.width() - margin else 0
        elif x + self.cell_width > self._hscroll + self.width() - margin:
            self._hscroll = int(x + self.cell_width + margin - self.width())
        self._update_hscroll_bar()

    def _update_scroll_bar(self):
        bar = self.scroll_bar
        if bar is None:
            return
        history = len(self.screen.history)
        bar.blockSignals(True)
        bar.setRange(0, history)
        bar.setPageStep(self.screen.lines)
        bar.setValue(history - self._scroll)
        bar.blockSignals(False)

    def _scrolled(self, value: int):
        self._scroll = max(0, len(self.screen.history) - value)
        self._scroll_px = 0.0
        self._update_hscroll_bar()  # (the lines now in view may be wider or narrower)
        self.update()

    def scroll_pixels(self, pixels: float):
        """Scroll the view up (positive) or down by pixels, through the
        scrollback: smoothly, part of a line at a time (a trackpad)."""
        history, ch = len(self.screen.history), self.cell_height
        total = max(0.0, min(history * ch, self._scroll * ch + self._scroll_px + pixels))
        self._scroll = min(history, int(total // ch))
        self._scroll_px = total - self._scroll * ch if self._scroll < history else 0.0
        self._update_scroll_bar()
        self._update_hscroll_bar()
        self.update()

    def scroll_lines(self, lines: int):
        """Scroll the view up (positive) or down through the scrollback."""
        self._scroll = max(0, min(len(self.screen.history), self._scroll + lines))
        self._scroll_px = 0.0
        self._update_scroll_bar()
        self._update_hscroll_bar()
        self.update()

    # ------------------------------------------------------------ text
    def total_lines(self) -> int:
        return len(self.screen.history) + self.screen.lines

    def line(self, n: int):
        """Line ``n`` of the scrollback and screen (a list or pyte line of Chars)."""
        history = self.screen.history
        return history[n] if n < len(history) else self.screen.buffer[n - len(history)]

    def _line_text(self, n: int, start: int = 0, end: int | None = None) -> str:
        line = self.line(n)
        width = len(line) if isinstance(line, list) else self.screen.columns
        end = width if end is None else min(end, width)
        return "".join(line[x].data for x in range(start, end)).rstrip()

    def text(self) -> str:
        """All the text: the scrollback, then the screen."""
        lines = [self._line_text(n) for n in range(self.total_lines())]
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    def selected_text(self) -> str:
        if not self._selection:
            return ""
        (l1, c1), (l2, c2) = sorted(self._selection)
        if l1 == l2:
            return self._line_text(l1, c1, c2 + 1)
        parts = [self._line_text(l1, c1)] + [self._line_text(n) for n in range(l1 + 1, l2)]
        return "\n".join(parts + [self._line_text(l2, 0, c2 + 1)])

    def _row_selection(self, n: int, width: int) -> tuple[int, int] | None:
        """The columns of line ``n`` that are selected, [start, end), or None."""
        if not self._selection:
            return None
        (l1, c1), (l2, c2) = sorted(self._selection)
        if not l1 <= n <= l2:
            return None
        return (c1 if n == l1 else 0), (c2 + 1 if n == l2 else width)

    # ---------------------------------------------------- Edit menu, clipboard
    def copy(self):
        text = self.selected_text()
        if text:
            QApplication.clipboard().setText(text)

    def paste(self):
        text = QApplication.clipboard().text()
        if not text:
            return
        data = text.replace("\r\n", "\n").replace("\n", "\r").encode("utf-8")
        if BRACKETED_PASTE in self.screen.mode:  # (so the shell doesn't run it line by line)
            data = b"\x1b[200~" + data + b"\x1b[201~"
        self._input(data)

    def cut(self):
        self.copy()

    def selectAll(self):
        last = self.total_lines() - 1
        self._selection = ((0, 0), (last, self.screen.columns - 1))
        self.update()

    # (These exist so the Edit menu doesn't reach past the terminal to the editor.)
    def undo(self):
        pass

    def redo(self):
        pass

    def clear_scrollback(self):
        """⌘K: forget the scrollback and redraw the prompt at the top."""
        self.screen.history.clear()
        self._scroll, self._scroll_px = 0, 0.0
        self._selection = None
        self._update_scroll_bar()
        self._input(b"\x0c")  # Ctrl+L

    # ------------------------------------------------------------ keys
    def _input(self, data: bytes):
        """Typing goes to the shell, and back to the bottom of the output."""
        if self._scroll or self._scroll_px or self._selection:
            self._scroll, self._scroll_px, self._selection = 0, 0.0, None
            self._update_scroll_bar()
            self.update()
        self.write(data)

    def focusNextPrevChild(self, _next):
        return False  # Tab goes to the shell (completion), not to the next widget

    def _wanted(self, event) -> bool:
        """Keys the terminal takes before the window's shortcuts: Control
        combinations (Ctrl+C must interrupt, not copy), Escape and Tab."""
        mods = event.modifiers() & ~Qt.KeypadModifier
        if mods & CONTROL and not mods & Qt.ShiftModifier and not (MAC and mods & COMMAND):
            return True
        return mods == Qt.NoModifier and event.key() in (Qt.Key_Escape, Qt.Key_Tab)

    def event(self, event):
        if event.type() == QEvent.ShortcutOverride and self._wanted(event):
            event.accept()
            return True
        return super().event(event)

    def key_bytes(self, key: int, mods, text: str) -> bytes | None:
        """What xterm sends for a key (None: nothing)."""
        shift, alt, ctrl = bool(mods & Qt.ShiftModifier), bool(mods & Qt.AltModifier), bool(mods & CONTROL)
        modifier = 1 + shift + 2 * alt + 4 * ctrl  # (xterm's modifier parameter)
        if MAC and alt and not ctrl and key in (Qt.Key_Left, Qt.Key_Right):
            return b"\x1bb" if key == Qt.Key_Left else b"\x1bf"  # Option+arrow: a word
        if key in CURSOR_KEYS:
            final = CURSOR_KEYS[key]
            if modifier > 1:
                return f"\x1b[1;{modifier}{final}".encode()
            return f"\x1b{'O' if DECCKM in self.screen.mode else '['}{final}".encode()
        if key in TILDE_KEYS:
            number = TILDE_KEYS[key]
            return (f"\x1b[{number};{modifier}~" if modifier > 1 else f"\x1b[{number}~").encode()
        if key in SS3_KEYS:
            return (f"\x1b[1;{modifier}{SS3_KEYS[key]}" if modifier > 1 else f"\x1bO{SS3_KEYS[key]}").encode()
        if key == Qt.Key_Backtab:
            return b"\x1b[Z"
        if key in (Qt.Key_Return, Qt.Key_Enter):
            return b"\r"
        if key == Qt.Key_Backspace:
            return b"\x08" if ctrl else b"\x7f"
        if key == Qt.Key_Escape:
            return b"\x1b"
        if key == Qt.Key_Tab:
            return b"\t"
        if ctrl:
            if Qt.Key_A <= key <= Qt.Key_Z:
                return bytes([key - Qt.Key_A + 1])
            control = {Qt.Key_Space: 0, Qt.Key_At: 0, Qt.Key_BracketLeft: 27, Qt.Key_Backslash: 28,
                       Qt.Key_BracketRight: 29, Qt.Key_AsciiCircum: 30, Qt.Key_Underscore: 31,
                       Qt.Key_Minus: 31, Qt.Key_Slash: 31}
            if key in control:
                return bytes([control[key]])
        if text:
            data = text.encode("utf-8")
            # Alt+key: ESC first (on macOS Option types characters instead).
            return b"\x1b" + data if alt and not MAC else data
        return None

    def keyPressEvent(self, event):
        key, mods, text = event.key(), event.modifiers() & ~Qt.KeypadModifier, event.text()
        if MAC and mods & COMMAND:  # ⌘: the app's, not the shell's
            actions = {Qt.Key_C: self.copy, Qt.Key_V: self.paste, Qt.Key_K: self.clear_scrollback,
                       Qt.Key_A: self.selectAll}
            if key in actions:
                actions[key]()
            else:
                event.ignore()
            return
        if not MAC and mods == (Qt.ControlModifier | Qt.ShiftModifier) and key in (Qt.Key_C, Qt.Key_V):
            self.copy() if key == Qt.Key_C else self.paste()
            return
        if mods == Qt.ShiftModifier and key in (Qt.Key_PageUp, Qt.Key_PageDown):  # read the scrollback
            self.scroll_lines(self.screen.lines - 1 if key == Qt.Key_PageUp else 1 - self.screen.lines)
            return
        data = self.key_bytes(key, mods, text)
        if data:
            self._input(data)
        else:
            event.ignore()

    # ------------------------------------------------------------ mouse
    def _cell_at(self, pos) -> tuple[int, int]:
        """(line in scrollback + screen, column) under a point."""
        row = int((pos.y() - PADDING - self._scroll_px) // self.cell_height)
        col = int((pos.x() + self._hscroll - PADDING) // self.cell_width)
        row = max(-1 if self._scroll_px else 0, min(self.screen.lines - 1, row))
        col = max(0, min(self.screen.columns - 1, col))
        return len(self.screen.history) - self._scroll + row, col

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            cell = self._cell_at(event.position())
            self._selection = (cell, cell)
            self._selecting = True
            self.update()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if getattr(self, "_selecting", False) and self._selection:
            self._selection = (self._selection[0], self._cell_at(event.position()))
            self.update()

    def mouseReleaseEvent(self, event):
        self._selecting = False
        if self._selection and self._selection[0] == self._selection[1]:
            self._selection = None  # (a click, not a selection)
            self.update()
        elif self._selection and not MAC and sys.platform.startswith("linux"):
            QApplication.clipboard().setText(self.selected_text(), QClipboard.Selection)

    def mouseDoubleClickEvent(self, event):
        """Select the word under the mouse."""
        n, x = self._cell_at(event.position())
        text = self._line_text(n).ljust(self.screen.columns)

        def in_word(c):
            return c.isalnum() or c in "_-./~:@%+"

        if x < len(text) and in_word(text[x]):
            start, end = x, x
            while start > 0 and in_word(text[start - 1]):
                start -= 1
            while end + 1 < len(text) and in_word(text[end + 1]):
                end += 1
            self._selection = ((n, start), (n, end))
            self.update()

    def wheelEvent(self, event):
        """Scroll through the scrollback (or, in a full-screen program, send it
        arrow keys, as xterm does). A trackpad's distance is in pixels and
        comes in many small steps: they add up, the part of a line left over
        kept for the next one, so a swipe up and back down the same distance
        ends where it started."""
        pixels, angle = event.pixelDelta(), event.angleDelta()
        dx, dy = pixels.x() or angle.x(), pixels.y() or angle.y()
        if event.modifiers() & Qt.ShiftModifier:
            dx, dy = dx or dy, 0  # (Shift+wheel: sideways)
        if abs(dx) > abs(dy):  # (mostly sideways: a vertical swipe's bit of sideways doesn't count)
            self.scroll_sideways(-dx)
            return
        # Up and down: a trackpad in its exact pixels; a mouse wheel 3 lines a notch.
        distance = pixels.y() if pixels.y() else angle.y() / 120 * 3 * self.cell_height
        if not distance:
            return
        if not self.screen.alternate:
            self.scroll_pixels(distance)
            return
        # Full-screen programs: the wheel is arrow keys (as in xterm), a line at a time.
        self._wheel_rest += distance / max(1.0, self.cell_height)
        whole = int(self._wheel_rest)  # (towards zero: the rest waits for the next step)
        self._wheel_rest -= whole
        if whole:
            key = Qt.Key_Up if whole > 0 else Qt.Key_Down
            self.write(self.key_bytes(key, Qt.NoModifier, "") * abs(whole))

    def contextMenuEvent(self, event):
        from qtpy.QtWidgets import QMenu

        menu = QMenu(self)
        copy = menu.addAction("Copy", self.copy)
        copy.setEnabled(bool(self._selection))
        menu.addAction("Paste", self.paste)
        menu.addAction("Select All", self.selectAll)
        menu.addSeparator()
        menu.addAction("Clear Scrollback", self.clear_scrollback)
        menu.exec(event.globalPos())

    # -------------------------------------------------------- drawing
    def _fit(self):
        """Columns and rows for the widget's size, or wider without word
        wrap (the shell is told)."""
        cols = max(2, int((self.width() - 2 * PADDING) // self.cell_width))
        if not self.word_wrap:
            cols = max(cols, NO_WRAP_COLUMNS)
        rows = max(1, int((self.height() - 2 * PADDING) // self.cell_height))
        if (rows, cols) != (self.screen.lines, self.screen.columns):
            self.screen.resize(rows, cols)
            self._scroll, self._scroll_px = min(self._scroll, len(self.screen.history)), 0.0
            self._update_scroll_bar()
            self.size_changed.emit(rows, cols)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()
        self._update_hscroll_bar()

    def focusInEvent(self, event):
        super().focusInEvent(event)
        self.update()

    def focusOutEvent(self, event):
        super().focusOutEvent(event)
        self.update()

    def _color(self, name: str, default: QColor) -> QColor:
        if name == "default":
            return default
        color = self._colors.get(name)
        if color is None:
            if name in ANSI_NAMES:
                color = self.ansi[ANSI_NAMES[name]]
            elif name in ANSI_HEX:
                color = self.ansi[ANSI_HEX[name]]
            else:
                color = QColor("#" + name) if len(name) == 6 else QColor(default)
            self._colors[name] = color
        return color

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.fillRect(self.rect(), self.background)
        offset = self._scroll_px  # (scrolled part of a line further: everything a bit lower)
        painter.translate(-self._hscroll, offset)  # (and sideways, without word wrap)
        screen = self.screen
        history = len(screen.history)
        first = history - self._scroll
        ch = self.cell_height
        palette = self.palette()
        select_bg = palette.color(QPalette.Highlight)
        select_fg = palette.color(QPalette.HighlightedText)
        top_row = max(-1 if offset else 0, int((event.rect().top() - PADDING - offset) // ch))
        bottom_row = min(screen.lines - 1, int((event.rect().bottom() - PADDING - offset) // ch))
        for row in range(top_row, bottom_row + 1):
            n = first + row
            if n < 0:
                continue
            line = self.line(n)
            width = min(len(line), screen.columns) if isinstance(line, list) else screen.columns
            span = self._row_selection(n, width)  # (once per row: not per cell)
            y = PADDING + row * ch
            x = 0
            while x < width:
                cell = line[x]
                selected = span is not None and span[0] <= x < span[1]
                style = (cell.fg, cell.bg, cell.bold, cell.italics, cell.underscore,
                         cell.strikethrough, cell.reverse, selected)
                start, chars = x, [cell.data]
                x += 1
                while x < width:  # (a run of cells drawn alike)
                    other = line[x]
                    if (other.fg, other.bg, other.bold, other.italics, other.underscore, other.strikethrough,
                            other.reverse, span is not None and span[0] <= x < span[1]) != style:
                        break
                    chars.append(other.data)
                    x += 1
                self._draw_run(painter, start, y, chars, cell, selected, select_bg, select_fg)
        self._draw_cursor(painter, first)

    def _draw_run(self, painter, column, y, chars, cell, selected, select_bg, select_fg):
        cw, ch = self.cell_width, self.cell_height
        fg = self._color(cell.fg, self.foreground)
        bg = self._color(cell.bg, self.background)
        if cell.reverse:
            fg, bg = bg, fg
        if selected:
            fg, bg = select_fg, select_bg
        x0 = PADDING + column * cw
        if bg != self.background:
            painter.fillRect(QRectF(x0, y, len(chars) * cw, ch), bg)
        text = "".join(chars)
        if not text.strip():
            return
        font = self.font_bold if cell.bold else self.font_normal
        if cell.italics or cell.underscore or cell.strikethrough:
            font = QFont(font)
            font.setItalic(cell.italics)
            font.setUnderline(cell.underscore)
            font.setStrikeOut(cell.strikethrough)
        painter.setFont(font)
        painter.setPen(fg)
        baseline = y + self.ascent
        if text.isascii():
            painter.drawText(QPointF(x0, baseline), text)
        else:  # (each character in its cell: wide ones take two, the next is empty)
            for i, c in enumerate(chars):
                if c:
                    painter.drawText(QPointF(x0 + i * cw, baseline), c)

    def _draw_cursor(self, painter, first):
        screen = self.screen
        self._cursor_drawn_at = screen.cursor.y
        row = len(screen.history) + screen.cursor.y - first
        if screen.cursor.hidden or not 0 <= row < screen.lines:
            return
        x = PADDING + screen.cursor.x * self.cell_width
        y = PADDING + row * self.cell_height
        rect = QRectF(x, y, self.cell_width, self.cell_height)
        if self.hasFocus():
            painter.fillRect(rect, self.foreground)
            cell = screen.buffer[screen.cursor.y][screen.cursor.x]
            if cell.data.strip():
                painter.setFont(self.font_bold if cell.bold else self.font_normal)
                painter.setPen(self.background)
                painter.drawText(QPointF(x, y + self.ascent), cell.data)
        else:
            painter.setPen(self.foreground)
            painter.setBrush(Qt.NoBrush)
            painter.drawRect(rect.adjusted(0.5, 0.5, -0.5, -0.5))


class TerminalPanel(QWidget):
    """The *Terminal* tab: a header (shell, folder, environment, Restart)
    over the terminal."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.directory: Path = Path.home()
        self.shell: PtyShell | WinptyShell | None = None
        self.title = QLabel()
        self.title.setContentsMargins(6, 2, 6, 2)
        self.restart_button = QToolButton()
        self.restart_button.setText("Restart")
        self.restart_button.setAutoRaise(True)
        self.restart_button.setToolTip("End this shell and start a new one in the project's folder")
        self.restart_button.clicked.connect(self.restart)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(self.title, 1)
        header.addWidget(self.restart_button)
        self.view = TerminalView(self)
        self.scroll_bar = QScrollBar(Qt.Vertical)
        self.view.connect_scroll_bar(self.scroll_bar)
        self.hscroll_bar = QScrollBar(Qt.Horizontal)  # (shown without word wrap)
        self.view.connect_hscroll_bar(self.hscroll_bar)
        body = QGridLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self.view, 0, 0)
        body.addWidget(self.scroll_bar, 0, 1)
        body.addWidget(self.hscroll_bar, 1, 0)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addLayout(body, 1)
        self.view.size_changed.connect(self._resized)
        self.refresh_colors()

    def set_directory(self, directory: Path | None):
        """The project's folder: where the shell starts. A different one (another
        project opened) restarts a running shell there; the same one (the
        project reloaded) leaves it alone."""
        directory = directory if directory and directory.is_dir() else Path.home()
        moved = directory.resolve() != self.directory.resolve()
        self.directory = directory
        if moved and self.shell is not None and self.shell.is_running():
            self.restart(focus=False)
        elif self.shell is None:
            self._update_title()

    def _update_title(self, note: str = ""):
        shell = Path(user_shell()).name
        folder = str(self.directory).replace(str(Path.home()), "~", 1)
        name = environment_name()
        env = f" · environment <b>{name}</b>" if name else ""
        self.title.setText(f"<b>{shell}</b> — {folder}{env}" + (f"  <i>({note})</i>" if note else ""))
        self.title.setToolTip("The shell has exactly the environment VibeFPMGUI was started with "
                              "(your shell's startup files aren't read, so nothing changes it).")

    def start(self):
        """Start the shell (if it isn't running)."""
        if self.shell is not None and self.shell.is_running():
            return
        view = self.view
        kind = WinptyShell if WINDOWS else PtyShell
        launch = shell_launch()
        self.shell = kind(launch, self.directory, view.screen.lines, view.screen.columns, self)
        self.shell.output.connect(view.feed)
        self.shell.exited.connect(self._exited)
        view.write = self.shell.write
        try:
            self.shell.start()
        except (OSError, ImportError) as exc:
            view.feed(f"Could not start {launch.argv[0]}: {exc}\r\n".encode())
            self.shell = None
            return
        self._update_title()

    def _resized(self, rows: int, cols: int):
        if self.shell is not None:
            self.shell.resize(rows, cols)

    def _exited(self):
        self._update_title("exited — Restart for a new shell")
        self.view.feed(b"\r\n[The shell has exited.]\r\n")

    def restart(self, focus: bool = True):
        """A new shell (in ``directory``), on a clean screen."""
        self.shutdown()
        self.view.feed(b"\x1bc")  # (reset: a clean screen)
        self.view.screen.history.clear()
        self.start()
        if focus:
            self.focus()

    def focus(self):
        self.view.setFocus()

    def type_command(self, command: str):
        """Type ``command`` at the shell's prompt and press Return (starting
        the shell first if needed)."""
        self.start()
        if self.shell is not None:
            self.shell.write((command + "\r").encode())

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, self.start)  # (the first time the tab is shown: after it's laid out)

    def refresh_colors(self):
        self.view.set_colors(*terminal_colors(QApplication.palette()))

    def set_font(self, font: QFont):
        self.view.set_terminal_font(font)

    def set_word_wrap(self, on: bool):
        """View → Word Wrap (see `TerminalView.set_word_wrap`)."""
        self.view.set_word_wrap(on)

    def shutdown(self):
        if self.shell is not None:
            self.shell.exited.disconnect()
            self.shell.stop()
            self.shell = None
