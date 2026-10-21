"""View → Show Git Blame: beside the editor (left of the minimap), for each
line of the file, the commit that last changed it — its short hash, author
and date — scrolling with the text. Each run of lines from one commit is a
card spanning them, labelled at its top, tinted and with a bar in the
color of the commit's age (see AGE_BANDS: this week, this month, this
year, older — the same in every file). A card lights up under the mouse and
sinks when pressed, as a button: click it to open the commit on GitHub or
GitLab (the repository's ``origin``), or, with no web page for it, to copy
its hash; hover it for the commit's message.

The blame is of the text in the editor (``git blame --contents``), so its
lines stay matched while you edit: lines changed since the last commit say
"Not committed yet". It's read off the window's thread, again once typing
pauses and when the file is saved.
"""

from __future__ import annotations

import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from qtpy.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QUrl
from qtpy.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QPainter,
    QPalette,
    QPen,
)
from qtpy.QtWidgets import QApplication, QToolTip, QWidget

from . import git_status as gs

NOT_COMMITTED = "0" * 40  # (git's hash for lines changed since the last commit)
HEADER_RE = re.compile(r"^([0-9a-f]{40}) (\d+) (\d+)(?: (\d+))?$")
CHARACTERS = 34  # the column's width, in characters ("a1b2c3d  Jacob Williams  2026-10-12")
DAY = 86_400  # seconds
# A commit's age: (younger than this many days, its name, its color, how strongly shown). The same in every file.
AGE_BANDS = ((7, "this week", "#f08c00", 1.0), (31, "this month", "#e3b341", 0.9),
             (365, "this year", "#7b9cc4", 0.8), (None, "older", "#9a9a9a", 0.55))


def age_band(commit_time: int, now: float | None = None) -> tuple[str, str, float]:
    """A commit's age band: (its name, its color, how strongly it's shown)."""
    days = ((now if now is not None else time.time()) - commit_time) / DAY
    for limit, name, color, strength in AGE_BANDS:
        if limit is None or days < limit:
            return name, color, strength
    raise AssertionError("the last band has no limit")


def age_words(commit_time: int, now: float | None = None) -> str:
    """ "this month (12 days ago)", "older (3 years ago)"…"""
    days = max(0, int(((now if now is not None else time.time()) - commit_time) // DAY))
    ago = ("today" if days == 0 else "yesterday" if days == 1 else f"{days} days ago" if days < 60
           else f"{days // 30} months ago" if days < 730 else f"{days // 365} years ago")
    return f"{age_band(commit_time, now)[0]} ({ago})"


@dataclass
class Commit:
    sha: str
    author: str = ""
    time: int = 0       # seconds since 1970 (its author's)
    summary: str = ""   # the first line of its message

    @property
    def committed(self) -> bool:
        return self.sha != NOT_COMMITTED

    def date(self) -> str:
        return time.strftime("%Y-%m-%d", time.localtime(self.time)) if self.time else ""


@dataclass
class Blame:
    lines: list[str]               # each line's commit (its hash), the file's lines in order
    commits: dict[str, Commit]
    remote: str = ""               # the repository's origin (for the commits' web pages)


def parse_porcelain(output: str) -> tuple[list[str], dict[str, Commit]]:
    """``git blame --porcelain`` read: each line's commit, and the commits."""
    lines: dict[int, str] = {}
    commits: dict[str, Commit] = {}
    current = None
    for row in output.split("\n"):
        if row.startswith("\t"):
            continue  # (the line's text)
        match = HEADER_RE.match(row)
        if match:
            current = commits.setdefault(match.group(1), Commit(match.group(1)))
            lines[int(match.group(3))] = current.sha
            continue
        if current is None:
            continue
        key, _, value = row.partition(" ")
        if key == "author":
            current.author = "You" if current.sha == NOT_COMMITTED else value
        elif key == "author-time" and value.isdigit():
            current.time = int(value)
        elif key == "summary":
            current.summary = value
    return [lines.get(n, NOT_COMMITTED) for n in range(1, max(lines, default=0) + 1)], commits


def blame(path: Path, text: str) -> Blame | None:
    """The blame of ``text`` as the file ``path`` (None: it isn't in a git
    repository, or git doesn't know it). Raises GitError if git fails."""
    root = gs.find_repository(path.parent)  # (a folder: the file's)
    if root is None:
        return None
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=path.suffix, delete=False) as contents:
        contents.write(text)
    try:
        code, output, message = gs._git(root, "blame", "--porcelain", "--contents", contents.name, "--",
                                        relative)
    finally:
        os.unlink(contents.name)
    if code != 0:
        if "no such path" in message or "not tracked" in message or "fatal: no such ref" in message:
            return None  # (a file git doesn't know yet)
        raise gs.GitError(message.strip().splitlines()[-1] if message.strip() else "git blame failed")
    lines, commits = parse_porcelain((output or b"").decode("utf-8", "replace"))
    _code, remote, _message = gs._git(root, "config", "--get", "remote.origin.url")
    return Blame(lines, commits, (remote or b"").decode().strip())


def short_name(author: str, room: int = 14) -> str:
    """An author's name, cut to fit: "Jacob Williams" or "Jacob W."."""
    if len(author) <= room:
        return author
    first, *rest = author.split()
    return f"{first} {rest[-1][0]}." if rest and len(first) + 3 <= room else author[:room - 1] + "…"


class BlameColumn(QWidget):
    """The column (see the module's description)."""

    def __init__(self, editor, parent=None):
        super().__init__(parent)
        self.editor = editor
        self.blame: Blame | None = None
        self.message = ""  # (why there's no blame: shown at the top)
        self._hover: int | None = None    # the card under the mouse (its first line)
        self._pressed: int | None = None  # the card being clicked (its first line)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        editor.updateRequest.connect(lambda *_: self.update())  # (scrolled, edited: drawn again with it)
        self.refresh_font()

    # ------------------------------------------------------------ contents
    def set_blame(self, blame: Blame | None, message: str = ""):
        self.blame, self.message = blame, message
        self.update()

    def refresh_font(self):
        font = QFont(QApplication.font())
        line = self.editor.fontMetrics().height()
        font.setPixelSize(max(8, int(line * 0.72)))
        self.setFont(font)
        self.setFixedWidth(QFontMetrics(font).horizontalAdvance("0" * CHARACTERS) + 12)

    def sizeHint(self) -> QSize:
        return QSize(self.width(), 100)

    # ------------------------------------------------------------ where
    def _visible_lines(self):
        """(line index, top y, height) of each line on view."""
        editor = self.editor
        block = editor.firstVisibleBlock()
        offset = editor.contentOffset()
        top = editor.blockBoundingGeometry(block).translated(offset).top()
        while block.isValid() and top <= self.height():
            height = editor.blockBoundingRect(block).height()
            if block.isVisible():
                yield block.blockNumber(), top, height
            top += height
            block = block.next()

    def runs(self):
        """The cards on view: (first line, commit, top y, bottom y) for each run
        of lines from one commit (a run starting above the view: from its top)."""
        if self.blame is None:
            return
        run = None
        for line, top, height in self._visible_lines():
            sha = self.blame.lines[line] if line < len(self.blame.lines) else None
            commit = self.blame.commits.get(sha) if sha else None
            if run is not None and commit is run[1] and line == run[4] + 1:
                run[3], run[4] = top + height, line
                continue
            if run is not None:
                yield tuple(run[:4])
            run = [line, commit, top, top + height, line] if commit is not None else None
        if run is not None:
            yield tuple(run[:4])

    def run_at(self, y: float):
        return next((r for r in self.runs() if r[2] <= y < r[3]), None)

    def commit_at(self, y: float) -> Commit | None:
        if self.blame is None:
            return None
        for line, top, height in self._visible_lines():
            if top <= y < top + height and line < len(self.blame.lines):
                return self.blame.commits.get(self.blame.lines[line])
        return None

    # ------------------------------------------------------------ drawing
    @staticmethod
    def _age_color(commit: Commit, base: QColor, now: float | None = None) -> QColor:
        """The color of a commit's age band (see AGE_BANDS); not committed: red."""
        if not commit.committed:
            return QColor("#e5484d")
        _name, name, strength = age_band(commit.time, now)
        color = QColor(name)
        color.setAlphaF(strength)
        return color if base.lightness() < 128 else color.darker(112)

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        palette = self.editor.palette()
        base = palette.color(QPalette.Base)
        light = base.lightness() >= 128
        background = base.darker(106) if light else base.lighter(110)
        painter.fillRect(self.rect(), background)
        text, dim = palette.color(QPalette.Text), QColor(palette.color(QPalette.Text))
        dim.setAlpha(150)
        if self.blame is None:
            if self.message:
                painter.setPen(dim)
                painter.drawText(QRect(8, 4, self.width() - 12, 40), Qt.AlignLeft | Qt.TextWordWrap, self.message)
            painter.end()
            return
        metrics = QFontMetrics(self.font())
        line_height = self.editor.fontMetrics().height()
        for first, commit, top, bottom in self.runs():
            accent = self._age_color(commit, base)
            hovered = commit.committed and first == self._hover
            pressed = hovered and first == self._pressed
            card = QRectF(4, top + 1.5, self.width() - 8, max(bottom - top - 3, 4))
            if pressed:
                card.translate(0, 1)  # (sunk: pressed in)
            elif commit.committed:  # (a soft shadow under it: raised)
                shadow = QColor(0, 0, 0, 40 if light else 90)
                painter.setPen(Qt.NoPen)
                painter.setBrush(shadow)
                painter.drawRoundedRect(card.translated(0, 1.2), 5, 5)
            fill = QColor(base.lighter(103) if light else base.lighter(135))
            tint = QColor(accent)
            tint.setAlphaF(0.10 + (0.12 if hovered else 0) + (0.10 if pressed else 0))
            painter.setBrush(fill)
            if commit.committed:
                painter.setPen(QPen(accent, 1.2) if hovered else QPen(QColor(0, 0, 0, 30 if light else 70), 1))
            else:  # (not committed: dashed, red, not clickable)
                painter.setPen(QPen(QColor("#e5484d"), 1, Qt.DashLine))
            painter.drawRoundedRect(card, 5, 5)
            painter.setPen(Qt.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(card, 5, 5)
            bar = QRectF(card.left() + 3, card.top() + 3, 3, card.height() - 6)  # (the age bar, in the card)
            painter.setBrush(accent)
            painter.drawRoundedRect(bar, 1.5, 1.5)
            if card.height() < line_height * 0.6:
                continue  # (too little of it on view for its label)
            label_top = card.top() + max(0.0, (min(card.height(), line_height) - metrics.height()) / 2)
            x, room = card.left() + 11, card.width() - 15
            font = QFont(self.font())
            if not commit.committed:
                font.setItalic(True)
                painter.setFont(font)
                painter.setPen(dim)
                painter.drawText(QRectF(x, label_top, room, metrics.height()), Qt.AlignLeft | Qt.AlignVCenter,
                                 "Not committed yet")
                continue
            parts = [(commit.sha[:7], True, text), (short_name(commit.author), False, text), (commit.date(), False, dim)]
            for part, bold, color in parts:
                font.setBold(bold)
                painter.setFont(font)
                width = QFontMetrics(font).horizontalAdvance(part)
                if width > room:
                    part = QFontMetrics(font).elidedText(part, Qt.ElideRight, int(room))
                    width = room
                painter.setPen(color)  # (the hash bold, in the text's color: the age is the bar's and tint's)
                painter.drawText(QRectF(x, label_top, width + 2, metrics.height()), Qt.AlignLeft | Qt.AlignVCenter,
                                 part)
                x += width + metrics.horizontalAdvance("  ")
                room = card.right() - 4 - x
                if room <= 8:
                    break
        painter.end()

    # ------------------------------------------------------------ the mouse
    def event(self, event):
        if event.type() == QEvent.ToolTip:
            commit = self.commit_at(event.pos().y())
            if commit is None:
                QToolTip.hideText()
            elif not commit.committed:
                QToolTip.showText(event.globalPos(), "Not committed yet: changed since the last commit", self)
            else:
                when = time.strftime("%Y-%m-%d %H:%M", time.localtime(commit.time)) if commit.time else ""
                link = "Click to open it on the web" if self._commit_url(commit) else "Click to copy its hash"
                QToolTip.showText(event.globalPos(), f"<b>{commit.sha[:10]}</b> · {_escape(commit.author)} · "
                                                     f"{when} · {age_words(commit.time)}<br>"
                                                     f"{_escape(commit.summary)}<br><i>{link}</i>", self)
            return True
        return super().event(event)

    def wheelEvent(self, event):
        # Scroll the editor, as if the wheel were over the text (as the minimap does): the
        # column, and the minimap, follow it.
        self.editor.wheelEvent(event)

    def mouseMoveEvent(self, event):
        run = self.run_at(event.position().y())
        hover = run[0] if run is not None and run[1].committed else None
        if hover != self._hover:
            self._hover = hover
            self.update()
        self.setCursor(Qt.PointingHandCursor if hover is not None else Qt.ArrowCursor)

    def leaveEvent(self, event):
        self._hover = self._pressed = None
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            run = self.run_at(event.position().y())
            self._pressed = run[0] if run is not None and run[1].committed else None
            self.update()

    def mouseReleaseEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        pressed, self._pressed = self._pressed, None
        self.update()
        run = self.run_at(event.position().y())
        if run is None or run[0] != pressed:  # (released off the card pressed: no click, as a button)
            return
        commit = run[1]
        url = self._commit_url(commit)
        if url:
            open_web_page(url)
        else:
            QApplication.clipboard().setText(commit.sha)
            QToolTip.showText(self.mapToGlobal(event.position().toPoint() + QPoint(8, 8)),
                              f"No web page for this repository's commits (its origin isn't on GitHub or "
                              f"GitLab): the hash {commit.sha[:10]} is copied.", self)

    def _commit_url(self, commit: Commit) -> str | None:
        if self.blame is None or not self.blame.remote:
            return None
        from .window.packages import (
            commit_web_url,  # (here: the window's module imports the editor's)
        )

        return commit_web_url(self.blame.remote, commit.sha)


def open_web_page(url: str):
    """Open a page in the browser."""
    QDesktopServices.openUrl(QUrl(url))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
