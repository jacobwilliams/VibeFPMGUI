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
from .background import call_in_background

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
    root: Path | None = None       # the repository's top folder (for a commit's details)


@dataclass
class FileChange:
    status: str                    # A, M, D, R, C, T (git's letters)
    path: str
    old_path: str = ""             # a renamed or copied file's name before
    added: int | None = None       # lines (None: a binary file)
    deleted: int | None = None


@dataclass
class CommitDetails:
    """What the tooltip shows of a commit, besides what the blame says."""
    email: str = ""
    message: str = ""              # the whole of it
    committer: str = ""            # (when not the author: a rebase, a patch applied by someone else)
    commit_time: int = 0
    files: list[FileChange] | None = None


MAX_FILES = 30  # files listed in the tooltip (more: "and N more")


def commit_details(root: Path, sha: str) -> CommitDetails:
    """A commit's message, its author's email, its committer, and the files it
    changed (each with its lines added and deleted; a merge's: what it
    brought into its first parent, not git's "combined" list, which leaves
    out what came from one side only). Raises GitError."""
    header = gs._check(root, "show", "-s", "--no-color", "--format=%ae%x00%cn%x00%ct%x00%an%x00%B", sha)
    email, committer, commit_time, author, message = (header.decode("utf-8", "replace").split("\0", 4) + [""] * 5)[:5]
    statuses = gs._check(root, "-c", "core.quotepath=false", "show", "--no-color", "--format=", "--name-status",
                         "-M", "-m", "--first-parent", sha).decode("utf-8", "replace").splitlines()
    counts = gs._check(root, "-c", "core.quotepath=false", "show", "--no-color", "--format=", "--numstat", "-M",
                       "-m", "--first-parent", sha).decode("utf-8", "replace").splitlines()
    files = []
    for status_line, count_line in zip(statuses, counts, strict=False):
        parts = status_line.split("\t")
        letter = parts[0][:1]
        path, old = (parts[2], parts[1]) if letter in "RC" and len(parts) > 2 else (parts[-1], "")
        added, deleted, *_ = count_line.split("\t") + ["", ""]
        files.append(FileChange(letter, path, old, int(added) if added.isdigit() else None,
                                int(deleted) if deleted.isdigit() else None))
    return CommitDetails(email.strip(), message.strip(), committer.strip() if committer.strip() != author.strip()
                         else "", int(commit_time) if commit_time.strip().isdigit() else 0, files)


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


def blame(path: Path, contents: str | bytes) -> Blame | None:
    """The blame of ``contents`` as the file ``path`` (None: it isn't in a git
    repository, or git doesn't know it). ``contents``: the bytes the file would
    have, saved (its line endings and encoding: the editor's text has "\n",
    and a file committed with "\r\n" would otherwise differ on every line).
    Raises GitError if git fails."""
    root = gs.find_repository(path.parent)  # (a folder: the file's)
    if root is None:
        return None
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    data = contents.encode("utf-8") if isinstance(contents, str) else contents
    with tempfile.NamedTemporaryFile("wb", suffix=path.suffix, delete=False) as file:
        file.write(data)
    try:
        code, output, message = gs._git(root, "blame", "--porcelain", "--contents", file.name, "--", relative)
    finally:
        os.unlink(file.name)
    if code != 0:
        if "no such path" in message or "not tracked" in message or "fatal: no such ref" in message:
            return None  # (a file git doesn't know yet)
        raise gs.GitError(message.strip().splitlines()[-1] if message.strip() else "git blame failed")
    lines, commits = parse_porcelain((output or b"").decode("utf-8", "replace"))
    _code, remote, _message = gs._git(root, "config", "--get", "remote.origin.url")
    return Blame(lines, commits, (remote or b"").decode().strip(), root)


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
        self._details: dict[str, CommitDetails | str] = {}  # a commit's details (or why git couldn't), once read
        self._loading: set[str] = set()
        self._card_at = QPoint()  # (where the mouse rested: the card is shown there)
        self.setMouseTracking(True)
        self.setAttribute(Qt.WA_OpaquePaintEvent)
        editor.updateRequest.connect(lambda *_: self.update())  # (scrolled, edited: drawn again with it)
        self.refresh_font()

    # ------------------------------------------------------------ contents
    def set_blame(self, blame: Blame | None, message: str = ""):
        self.blame, self.message = blame, message
        if self._hover is not None:  # (its lines may have moved: tinted again as the mouse moves)
            self._hover = None
            self.editor.show_commit_lines(None)
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
        if event.type() == QEvent.ToolTip:  # (the mouse rested on a card: its card, as fortitude's)
            commit = self.commit_at(event.pos().y())
            popup = self.editor.hover_popup
            if commit is None:
                if (popup.anchor or ("",))[0] == "blame":
                    popup.schedule_hide()
            else:
                self._card_at = event.globalPos()
                self.show_card(commit)
                if commit.committed and commit.sha not in self._details:
                    self._load_details(commit)
            return True
        return super().event(event)

    def show_card(self, commit: Commit):
        """A commit's card in the editor's hover popup (it stays while the mouse
        goes into it, so its links can be clicked)."""
        palette = self.editor.palette()
        html = commit_card_html(commit, self._details.get(commit.sha), self.blame.remote if self.blame else "",
                                palette.color(QPalette.Text))
        self.editor.hover_popup.show_at(self._card_at, html, ("blame", commit.sha))

    def _load_details(self, commit: Commit):
        """Read a commit's message and files (off the window's thread, once):
        its card then shows them, if it's still open."""
        if commit.sha in self._loading or self.blame is None or self.blame.root is None:
            return
        self._loading.add(commit.sha)
        root, sha = self.blame.root, commit.sha

        def work():
            try:
                return commit_details(root, sha)
            except gs.GitError as error:
                return str(error)

        def done(details):
            self._loading.discard(sha)
            self._details[sha] = details
            popup = self.editor.hover_popup
            if popup.isVisible() and popup.anchor == ("blame", sha):
                self.show_card(commit)

        call_in_background(work, done, self)

    def _show_commit_lines(self, commit: Commit | None):
        """Every line of the file from ``commit`` tinted in its card's color
        (None: none), while the mouse is on its card."""
        if commit is None or self.blame is None:
            self.editor.show_commit_lines(None)
            return
        tint = self._age_color(commit, self.editor.palette().color(QPalette.Base))
        tint.setAlphaF(0.22 if self.editor.palette().color(QPalette.Base).lightness() < 128 else 0.28)
        lines = {n for n, sha in enumerate(self.blame.lines) if sha == commit.sha}
        self.editor.show_commit_lines(lines, tint)

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
            self._show_commit_lines(run[1] if hover is not None else None)
        self.setCursor(Qt.PointingHandCursor if hover is not None else Qt.ArrowCursor)

    def leaveEvent(self, event):
        self._hover = self._pressed = None
        self.update()
        self._show_commit_lines(None)
        if (self.editor.hover_popup.anchor or ("",))[0] == "blame":
            self.editor.hover_popup.schedule_hide()  # (unless the mouse went into it)
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


STATUS_CHIPS = {"A": ("added", "#2f9e44"), "M": ("modified", "#1c7ed6"), "D": ("deleted", "#e03131"),
                "R": ("renamed", "#7048e8"), "C": ("copied", "#7048e8"), "T": ("type changed", "#868e96")}


def web_base(remote: str) -> str | None:
    """The repository's web page (GitHub, GitLab, Codeberg…), None: none known."""
    if not remote:
        return None
    from .window.packages import (
        repo_web_url,  # (here: the window's module imports the editor's)
    )

    return repo_web_url(remote)


def issue_url(base: str, number: str, merge_request: bool = False) -> str | None:
    """An issue's ("#33") or merge request's ("!34", GitLab's) page."""
    if "gitlab." in base:
        return f"{base}/-/{'merge_requests' if merge_request else 'issues'}/{number}"
    if merge_request:
        return None  # (only GitLab writes them so)
    return f"{base}/issues/{number}"  # (GitHub sends a pull request's number on to it; Codeberg too)


def file_url(base: str, sha: str, path: str) -> str:
    """A file as it was in a commit."""
    if "gitlab." in base:
        return f"{base}/-/blob/{sha}/{path}"
    if "codeberg." in base:
        return f"{base}/src/commit/{sha}/{path}"
    if "bitbucket." in base:
        return f"{base}/src/{sha}/{path}"
    return f"{base}/blob/{sha}/{path}"


def linkify(text: str, base: str | None) -> str:
    """Text for the card: escaped, its web addresses links, and its issue
    ("#33") and merge request ("!34") numbers links to their pages."""
    out, last = [], 0
    pattern = re.compile(r"(?P<url>https?://[^\s<>\"')\]]+)|(?<![\w&/])(?P<kind>[#!])(?P<number>\d+)\b")
    for match in pattern.finditer(text):
        out.append(_escape(text[last:match.start()]))
        last = match.end()
        if match.group("url"):
            url = match.group("url").rstrip(".,;:")
            out.append(f"<a href='{_escape(url)}'>{_escape(url)}</a>{_escape(match.group('url')[len(url):])}")
            continue
        url = issue_url(base, match.group("number"), match.group("kind") == "!") if base else None
        out.append(f"<a href='{url}'>{_escape(match.group(0))}</a>" if url else _escape(match.group(0)))
    return "".join(out) + _escape(text[last:])


def commit_card_html(commit: Commit, details: CommitDetails | str | None, remote: str, text: QColor) -> str:
    """A commit, as the editor's other hover cards (fortitude's): its hash
    (a link: its page, or "copy"), age band, author (and committer), date,
    the whole message (issue and merge request numbers, web addresses:
    links), and the files it changed (each a link to it as it was then),
    with their lines added and deleted. ``details``: None while they're
    read; a string: why git couldn't."""
    muted = QColor(text)
    muted.setAlpha(150)
    muted = muted.name(QColor.HexArgb)
    dark = text.lightness() > 128
    green, red = ("#57d163", "#ff6b6b") if dark else ("#1a7f37", "#cf222e")
    small = "font-size:small"
    base = web_base(remote)
    host = "GitHub" if base and "github." in base else "GitLab" if base and "gitlab." in base else "the web"
    page = f"{base}/{'-/commit' if base and 'gitlab.' in base else 'commit'}/{commit.sha}" if base else None
    if not commit.committed:
        return (f"<span style='color:{muted}; {small}'>GIT BLAME</span><br>"
                "<i>Not committed yet</i>: changed since the last commit.")
    band, color, _strength = age_band(commit.time)
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(commit.time)) if commit.time else ""
    chip = f"background-color:{color}; color:white; font-weight:700; {small}"
    hash_link = page or f"copy:{commit.sha}"
    out = [(f"<span style='color:{muted}; {small}'>COMMIT</span><br>"
            f"<a href='{hash_link}' style='text-decoration:none'><b><code>{commit.sha[:12]}</code></b></a>"
            f"&nbsp;&nbsp;<span style='{chip}'>&nbsp;{band}&nbsp;</span>")]
    email = f" &lt;{_escape(details.email)}&gt;" if isinstance(details, CommitDetails) and details.email else ""
    ago = age_words(commit.time).split(" (")[-1][:-1]
    out.append(f"<br><b>{_escape(commit.author)}</b><span style='color:{muted}'>{email}</span><br>"
               f"<span style='color:{muted}; {small}'>{when} · {ago}</span>")
    if isinstance(details, CommitDetails) and details.committer:
        out.append(f"<br><span style='color:{muted}; {small}'>committed by {_escape(details.committer)}</span>")
    message = details.message if isinstance(details, CommitDetails) and details.message else commit.summary
    subject, _, body = message.partition("\n")
    out.append(f"<p style='margin-top:8px; margin-bottom:0'><b>{linkify(subject, base)}</b></p>")
    if body.strip():
        lines = (linkify(line, base) if line.strip() else "" for line in body.strip("\n").split("\n"))
        out.append(f"<p style='margin-top:4px; margin-bottom:0'>{'<br>'.join(lines)}</p>")
    if details is None:
        out.append(f"<p style='margin-top:8px; color:{muted}'><i>Reading the commit…</i></p>")
    elif isinstance(details, str):
        out.append(f"<p style='margin-top:8px; color:{muted}'><i>git couldn't read it: {_escape(details)}</i></p>")
    elif details.files is not None:
        files = details.files
        added = sum(f.added or 0 for f in files)
        deleted = sum(f.deleted or 0 for f in files)
        rows = [(f"<p style='margin-top:10px; margin-bottom:2px'><span style='color:{muted}; {small}'>"
                 f"FILES CHANGED · {len(files)}</span>&nbsp; <span style='color:{green}'>+{added}</span> "
                 f"<span style='color:{red}'>−{deleted}</span></p><table cellspacing='0' cellpadding='1'>")]
        for change in files[:MAX_FILES]:
            word, status_color = STATUS_CHIPS.get(change.status, (change.status, "#868e96"))
            name = (f"{_escape(change.old_path)} → {_escape(change.path)}" if change.old_path
                    else _escape(change.path))
            if base and change.status != "D":
                name = f"<a href='{_escape(file_url(base, commit.sha, change.path))}'>{name}</a>"
            if change.added is None:
                counts, bar = f"<span style='color:{muted}'>binary</span>", ""
            elif change.added + change.deleted == 0:  # (renamed, or its mode changed: no lines)
                counts, bar = f"<span style='color:{muted}'>—</span>", ""
            else:
                counts = (f"<span style='color:{green}'>+{change.added}</span>&nbsp;"
                          f"<span style='color:{red}'>−{change.deleted}</span>")
                total = change.added + change.deleted
                plus = round(5 * change.added / total) if total else 0
                bar = (f"<span style='color:{green}'>{'■' * plus}</span>"
                       f"<span style='color:{red}'>{'■' * (5 - plus if total else 0)}</span>"
                       f"<span style='color:{muted}'>{'□' * (0 if total else 5)}</span>")
            rows.append(f"<tr><td style='padding-right:6px'><span style='background-color:{status_color}; "
                        f"color:white; font-weight:700; {small}' title='{word}'>&nbsp;{change.status}&nbsp;</span>"
                        f"</td><td style='padding-right:10px'>{name}</td><td align='right'>{counts}&nbsp;</td>"
                        f"<td>{bar}</td></tr>")
        rows.append("</table>")
        if len(files) > MAX_FILES:
            rows.append(f"<span style='color:{muted}; {small}'>and {len(files) - MAX_FILES} more</span>")
        out += rows
    actions = [f"<a href='{page}'>Open on {host}</a>"] if page else []
    actions.append(f"<a href='copy:{commit.sha}'>Copy hash</a>")
    out.append(f"<p style='margin-top:10px; margin-bottom:0; {small}'>{' &nbsp;·&nbsp; '.join(actions)}</p>")
    return "".join(out)


def open_web_page(url: str):
    """Open a page in the browser."""
    QDesktopServices.openUrl(QUrl(url))


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
