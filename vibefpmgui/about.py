"""The About window: icon, version, links, and the versions of the tools in use."""

from __future__ import annotations

import html
import platform
import re
import shutil
import subprocess
import time

from qtpy.QtCore import Qt, QTimer, qVersion
from qtpy.QtGui import QPainter
from qtpy.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from . import __version__
from .icons import draw_app_icon, recolored_logo

FPM_DOCS_URL = "https://fpm.fortran-lang.org"
PROJECT_URL = "https://github.com/jacobwilliams/VibeFPMGUI"


def tool_version(command: list[str], timeout: float = 5) -> str:
    """First useful line of ``<tool> --version`` ("" if it can't be run)."""
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)
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
        compiler_path = shutil.which(compiler) or ""
        rows = [
            ("fpm", (html.escape(fpm_ver) if fpm_ver else "<i>not found</i>")
             # (zero-width spaces after path separators let a long path wrap)
             + "<br><span style='color:gray'>"
             + re.sub(r"([/\\])", "\\1\u200b", html.escape(fpm_path)) + "</span>"),
            ("Compiler", (html.escape(compiler_ver) if compiler_ver
                          else f"<i>{html.escape(compiler)} not found</i>")
             + "<br><span style='color:gray'>"
             + re.sub(r"([/\\])", "\\1\u200b", html.escape(compiler_path or compiler))
             + (f" — {html.escape(compiler_source)}" if compiler_source else "") + "</span>"),
            ("Python", html.escape(platform.python_version())),
            ("Qt", html.escape(qVersion())),
            ("System", html.escape(f"{platform.system()} {platform.release()} ({platform.machine()})")),
        ]
        table = "".join(f"<tr><td style='padding-right:12px' valign='top'><b>{k}</b></td>"
                        f"<td>{v}</td></tr>" for k, v in rows)
        versions = QLabel(f"<table>{table}</table>")
        versions.setWordWrap(True)
        versions.setTextInteractionFlags(Qt.TextSelectableByMouse)

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
        text.addWidget(versions)
        text.addSpacing(4)
        text.addWidget(links)
        text.addWidget(legal)
        text.addStretch(1)

        top = QHBoxLayout()
        top.setSpacing(16)
        top.addWidget(self.logo, 0, Qt.AlignTop)
        top.addLayout(text, 1)

        copy = QPushButton("Copy Versions")
        copy.setToolTip("Copy the version information (handy for bug reports)")
        plain = "\n".join(f"{k}: {re.sub('<[^>]+>', ' ', v).strip()}" for k, v in rows).replace("\u200b", "")
        plain = f"VibeFPMGUI {__version__}\n" + re.sub(r"[ \t]+", " ", html.unescape(plain))
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

