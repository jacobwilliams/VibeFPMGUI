"""The About window: icon, version, links, and the versions of the tools in use;
under the icon, Fortran being typed (a quiet animation, only while it's open)."""

from __future__ import annotations

import html
import platform
import re
import shutil
import subprocess
import sys
import time

from qtpy.QtCore import QRectF, Qt, QTimer, qVersion
from qtpy.QtGui import (
    QColor,
    QFontDatabase,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPalette,
)
from qtpy.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .console import NO_WINDOW
from .icons import draw_app_icon, recolored_logo

FPM_DOCS_URL = "https://fpm.fortran-lang.org"
PROJECT_URL = "https://github.com/jacobwilliams/VibeFPMGUI"


def tool_version(command: list[str], timeout: float = 5) -> str:
    """First useful line of ``<tool> --version`` ("" if it can't be run)."""
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (out.stdout or out.stderr or "").strip()
    return text.splitlines()[0].strip() if text else ""


def fpm_version(fpm: str) -> str:
    """e.g. "0.13.0, alpha" from fpm's "Version: 0.13.0, alpha" line."""
    line = tool_version([fpm, "--version"])
    match = re.match(r"^\s*Version:\s*(.+)$", line)
    return match.group(1).strip() if match else line


class AnimatedLogo(QWidget):
    """The app icon, its colors slowly going around the color wheel while
    it's shown."""

    PERIOD = 20.0  # seconds for once around
    FRAME_MS = 50

    def __init__(self, size: int = 128, parent=None):
        from qtpy.QtSvg import QSvgRenderer

        super().__init__(parent)
        self.setFixedSize(size, size)
        self.renderer = QSvgRenderer(self)
        self.turn = 0.0
        self._start = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(self.FRAME_MS)
        self.timer.timeout.connect(self._tick)
        self._recolor()

    def _recolor(self):
        self.renderer.load(recolored_logo(self.turn))
        self.update()

    def _tick(self):
        self.turn = ((time.monotonic() - self._start) / self.PERIOD) % 1.0
        self._recolor()

    def showEvent(self, event):
        super().showEvent(event)
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def paintEvent(self, event):
        p = QPainter(self)
        draw_app_icon(p, self.width(), self.renderer)
        p.end()


# Typed under the icon, over and over (short lines: the column is the icon's width).
TYPED_CODE = """program orbit
  use iso_fortran_env
  implicit none
  integer, parameter :: &
    wp = real64
  real(wp) :: r(3), v(3)
  real(wp) :: dt = 0.01_wp
  integer :: i

  r = [1.0_wp, 0.0_wp, 0.0_wp]
  v = [0.0_wp, 1.0_wp, 0.0_wp]
  do i = 1, 1000
    call step(r, v, dt)
  end do
  print '(3f9.4)', r

contains

  pure subroutine step(r, v, h)
    real(wp), intent(inout) :: r(3)
    real(wp), intent(inout) :: v(3)
    real(wp), intent(in) :: h
    real(wp) :: a(3)
    ! gravity, mu = 1
    a = -r / norm2(r)**3
    v = v + h * a
    r = r + h * v
  end subroutine step

end program orbit
"""


class TypingCode(QWidget):
    """Fortran being typed, a character at a time, scrolling up as it goes:
    small, faint, in the window's colors (keywords in its accent), fading
    out at the top and bottom. Runs only while shown."""

    FRAME_MS = 40
    CHARS_PER_SECOND = 18
    PAUSE = 2.5      # seconds at the end before typing it again
    OPACITY = 0.55   # (subtle: under the version information)

    def __init__(self, code: str = TYPED_CODE, parent=None):
        super().__init__(parent)
        self.font_ = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.font_.setPointSizeF(max(6.5, self.font_.pointSizeF() * 0.62))
        self.metrics = QFontMetricsF(self.font_)
        self.line_height = self.metrics.lineSpacing()
        self.lines = self._colored(code)
        self.typed = 0.0       # characters typed (of the code, again and again: see _text_at)
        self.offset = 0.0      # how far it's scrolled (pixels)
        self._last = time.monotonic()
        self.timer = QTimer(self)
        self.timer.setInterval(self.FRAME_MS)
        self.timer.timeout.connect(self._tick)
        self.setMinimumHeight(int(self.line_height * 4))
        self.setAttribute(Qt.WA_TransparentForMouseEvents)

    @staticmethod
    def _colored(code: str) -> list[list[tuple[str, str]]]:
        """The code's lines, each as (text, kind) pieces: "keyword", "comment",
        "string", "number" or "" (see paintEvent for their colors)."""
        from pygments.lexers import FortranLexer
        from pygments.token import Comment, Keyword, Name, Number, String

        lines: list[list[tuple[str, str]]] = [[]]
        for ttype, value in FortranLexer().get_tokens(code):
            kind = ("comment" if ttype in Comment else "string" if ttype in String else
                    "number" if ttype in Number else
                    "keyword" if ttype in Keyword or ttype in Name.Builtin else "")
            for n, part in enumerate(value.split("\n")):
                if n:
                    lines.append([])
                if part:
                    lines[-1].append((part, kind))
        while lines and not lines[-1]:
            lines.pop()
        return lines

    def _chars(self) -> int:
        """Characters in one go through the code (a line's end counts as one)."""
        return sum(len(text) for line in self.lines for text, _kind in line) + len(self.lines)

    def _tick(self):
        now = time.monotonic()
        elapsed, self._last = min(now - self._last, 0.2), now  # (not a jump after being hidden)
        self.typed += elapsed * self.CHARS_PER_SECOND
        rows = self._rows()
        target = max(0.0, rows * self.line_height - (self.height() - self.line_height * 1.5))
        self.offset += (target - self.offset) * min(1.0, elapsed * 6)  # (eased: a smooth scroll)
        self.update()

    def _position(self) -> tuple[int, int, int]:
        """(times through, line, characters typed of that line) at ``typed``."""
        total = self._chars()
        cycle = total + int(self.PAUSE * self.CHARS_PER_SECOND)
        rounds, left = divmod(int(self.typed), cycle)
        left = min(left, total - 1)
        for number, line in enumerate(self.lines):
            length = sum(len(text) for text, _kind in line) + 1
            if left < length:
                return rounds, number, left
            left -= length
        return rounds, len(self.lines) - 1, 0

    def _rows(self) -> int:
        """Rows typed so far (each time through follows the last, after a blank row)."""
        rounds, line, _chars = self._position()
        return rounds * (len(self.lines) + 1) + line + 1

    def showEvent(self, event):
        super().showEvent(event)
        self._last = time.monotonic()
        self.timer.start()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.timer.stop()

    def paintEvent(self, event):
        pal = self.palette()
        text = pal.color(QPalette.WindowText)
        colors = {"": text, "keyword": pal.color(QPalette.Highlight), "comment": QColor(text),
                  "string": QColor("#2f9e44") if pal.color(QPalette.Window).lightness() > 128 else QColor("#69db7c"),
                  "number": QColor("#e8590c") if pal.color(QPalette.Window).lightness() > 128 else QColor("#ffa94d")}
        colors["comment"].setAlpha(130)
        p = QPainter(self)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setOpacity(self.OPACITY)
        p.setFont(self.font_)
        _rounds, _line, typed = self._position()
        rows = self._rows()
        lh, ascent = self.line_height, self.metrics.ascent()
        first = max(0, int(self.offset // lh) - 1)
        for row in range(first, rows):
            y = row * lh - self.offset
            if y > self.height():
                break
            number = row % (len(self.lines) + 1)  # (the last of each time through: the blank row between)
            if number == len(self.lines):
                continue
            limit = typed if row == rows - 1 else None
            x = 0.0
            for piece, kind in self.lines[number]:
                if limit is not None:
                    if limit <= 0:
                        break
                    piece, limit = piece[:limit], limit - len(piece)
                p.setPen(colors[kind])
                p.drawText(QRectF(x, y, self.width() * 2, lh), Qt.AlignLeft | Qt.AlignVCenter, piece)
                x += self.metrics.horizontalAdvance(piece)
            if row == rows - 1 and int(time.monotonic() * 2) % 2 == 0:  # (the cursor, blinking)
                p.fillRect(QRectF(x + 1, y + (lh - ascent) / 2, 1.5, ascent), text)
        # Fade out at the top and bottom (into the window's color).
        p.setOpacity(1.0)
        background = pal.color(QPalette.Window)
        clear = QColor(background)
        clear.setAlpha(0)
        for top in (True, False):
            band = QRectF(0, 0 if top else self.height() - lh * 2, self.width(), lh * 2)
            gradient = QLinearGradient(band.topLeft(), band.bottomLeft())
            gradient.setColorAt(0.0, background if top else clear)
            gradient.setColorAt(1.0, clear if top else background)
            p.fillRect(band, gradient)
        p.end()


class AboutDialog(QDialog):
    def __init__(self, fpm: str, compiler: str, compiler_source: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle("About VibeFPMGUI")

        self.logo = AnimatedLogo(128)

        title = QLabel(f"<h2 style='margin:0'>VibeFPMGUI</h2>"
                       f"<p style='margin-top:2px; color:gray'>Version {html.escape(__version__)}</p>")
        blurb = QLabel("A graphical front end for the "
                       f"<a href='{FPM_DOCS_URL}'>Fortran Package Manager (fpm)</a>: build, run and "
                       "test fpm projects, explore their dependencies, edit sources and review "
                       "code with a local AI model.")
        blurb.setWordWrap(True)
        blurb.setOpenExternalLinks(True)

        fpm_path = shutil.which(fpm) or fpm
        fpm_ver = fpm_version(fpm)
        compiler_ver = tool_version([compiler, "--version"])
        compiler_path = shutil.which(compiler) or compiler
        compiler_tip = compiler_path + (f"\n(chosen by: {compiler_source})" if compiler_source else "")
        # (name, value, tooltip: where the program is, shown on hover rather than taking up the window)
        rows = [
            ("fpm", fpm_ver or "not found", fpm_path),
            ("Compiler", compiler_ver or f"{compiler} not found", compiler_tip),
            ("Python", platform.python_version(), sys.executable),
            ("Qt", qVersion(), ""),
            ("System", f"{platform.system()} {platform.release()} ({platform.machine()})", ""),
        ]
        versions = QGridLayout()
        versions.setHorizontalSpacing(12)
        versions.setVerticalSpacing(2)
        self.version_labels: dict[str, QLabel] = {}
        for row, (name, value, tip) in enumerate(rows):
            key = QLabel(f"<b>{html.escape(name)}</b>")
            missing = value.endswith("not found")
            shown = QLabel(f"<i>{html.escape(value)}</i>" if missing else html.escape(value))
            shown.setWordWrap(True)
            shown.setTextInteractionFlags(Qt.TextSelectableByMouse)
            for label in (key, shown):
                label.setToolTip(tip)
            versions.addWidget(key, row, 0, Qt.AlignTop)
            versions.addWidget(shown, row, 1)
            self.version_labels[name] = shown
        versions.setColumnStretch(1, 1)

        links = QLabel(f"<a href='{FPM_DOCS_URL}'>fpm documentation</a> &nbsp;·&nbsp; "
                       f"<a href='{PROJECT_URL}'>VibeFPMGUI on GitHub</a>")
        links.setOpenExternalLinks(True)
        legal = QLabel("<span style='color:gray'>© 2026 Jacob Williams · MIT License<br>"
                       "fpm logo: © 2020 fpm contributors · MIT License</span>")

        text = QVBoxLayout()
        text.setSpacing(8)
        text.addWidget(title)
        text.addWidget(blurb)
        text.addSpacing(4)
        text.addLayout(versions)
        text.addSpacing(4)
        text.addWidget(links)
        text.addWidget(legal)
        text.addStretch(1)

        self.typing = TypingCode()
        self.typing.setFixedWidth(self.logo.width())
        side = QVBoxLayout()
        side.setSpacing(12)
        side.addWidget(self.logo, 0, Qt.AlignTop)
        side.addWidget(self.typing, 1)  # (under the icon, down to the window's bottom)

        top = QHBoxLayout()
        top.setSpacing(16)
        top.addLayout(side)
        top.addLayout(text, 1)

        copy = QPushButton("Copy Versions")
        copy.setToolTip("Copy the version information (handy for bug reports)")
        # (Copied with where the programs are: handy in a bug report.)
        plain = "\n".join(f"{name}: {value}" + (f" ({' '.join(tip.split())})" if tip else "")
                          for name, value, tip in rows)
        plain = f"VibeFPMGUI {__version__}\n{plain}"
        copy.clicked.connect(lambda: (self._copy(plain), copy.setText("Copied")))
        packages = QPushButton("Packages…")
        packages.setToolTip("The packages installed in the environment the app runs in "
                            "(pixi list or conda list)")
        packages.clicked.connect(self.show_packages)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(copy)
        buttons.addWidget(packages)
        buttons.addStretch(1)
        buttons.addWidget(close)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 14)
        layout.addLayout(top)
        layout.addLayout(buttons)
        self.setMinimumWidth(560)

    def show_packages(self):
        from .packages import PackagesDialog

        dialog = PackagesDialog(self)
        try:
            dialog.exec()
        finally:
            dialog.deleteLater()

    @staticmethod
    def _copy(text: str):
        from qtpy.QtWidgets import QApplication
        QApplication.clipboard().setText(text)

