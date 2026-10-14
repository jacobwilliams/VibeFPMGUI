"""The *Review* tab: a timeline of everything the AI does (reviews,
*Explain with AI*, questions and edits from the box at the bottom), newest
at the bottom. Nothing replaces anything: each action adds an entry, older
ones fold to a one-line header (click to open), and everything in them
stays usable: findings jump to their line or get *Fix with AI…*, a review's
markers go back in the editor, edits can still be accepted. The timeline
lasts until the app quits (or *Clear*); *New Chat* draws a line: the model
forgets what's above it.

The panel only shows `Entry` objects and says what the user did;
`vibefpmgui.main_window` runs the model and changes files.
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pygments.lexers import get_lexer_by_name
from pygments.styles import get_style_by_name
from pygments.util import ClassNotFound
from qtpy.QtCore import QEvent, Qt, QTimer, Signal
from qtpy.QtGui import (
    QColor,
    QFont,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from qtpy.QtWidgets import (
    QApplication,
    QButtonGroup,
    QCheckBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import ai_edits as ed
from . import highlighter as hl
from .ai_edits import END_RE as EDIT_END_RE
from .ai_edits import START_RE as EDIT_START_RE
from .ai_review import Finding
from .build_monitor import (
    PALETTES,
    ProgressLine,
    TitleIcon,
    app_is_light,
    panel_stylesheet,
)
from .edit_cards import EditList
from .editor import monospace_font
from .widgets import StatusLabel

FINDING_LINE_RE = re.compile(r"\s*\**\s*LINES?\s*\d", re.IGNORECASE)  # (a finding in a chat answer)

CATEGORY_COLORS = {"bug": "#e03131", "warning": "#f08c00", "performance": "#0c8599",
                   "style": "#7048e8", "suggestion": "#1c7ed6"}
CATEGORY_ICONS = {"bug": "✕", "warning": "⚠", "performance": "⚡", "style": "✎", "suggestion": "💡"}
KINDS = {"review": ("Review", "#7048e8"), "explain": ("Explain", "#0c8599"), "ask": ("Ask", "#1c7ed6"),
         "edit": ("Edit", "#2f9e44")}
FILTERS = {"all": "All", "review": "Reviews", "explain": "Explanations", "chat": "Chat",
           "pending": "Pending edits"}
RUNNING, DONE, FAILED = "running", "done", "failed"


def _esc(text: str) -> str:
    """Text for a label (quotes as they are: it's not an attribute)."""
    return html.escape(text, quote=False)


@dataclass
class Entry:
    """One AI action: a review, an explanation, a question (Ask) or a
    change (Edit); or a divider (New Chat: the model forgets what's above)."""
    kind: str                     # "review", "explain", "ask", "edit", "divider"
    question: str = ""            # the question; the problem explained; a review's focus
    path: Path | None = None      # the file it's about
    where: str = ""               # shown in the header: "foo.f90", "foo.f90:12"
    model: str = ""
    answer: str = ""              # as the model wrote it (Markdown)
    reasoning: str = ""           # a thinking model's, before its answer (AI → Think Before Answering)
    findings: list[Finding] = field(default_factory=list)
    summary: str = ""             # a review's
    edits: list[ed.Edit] = field(default_factory=list)
    status: str = RUNNING
    note: str = ""                # "4.1 s · model", or what went wrong
    started: str = field(default_factory=lambda: time.strftime("%H:%M"))
    turn: Any = None              # what the model was asked (an ai_context.Turn), for later questions

    def pending_edits(self) -> int:
        return sum(e.status == ed.PENDING and e.located for e in self.edits)

    def markdown(self) -> str:
        """The entry as text (Copy)."""
        if self.kind == "review":
            lines = [f"**Review of {self.where}** ({self.question})", ""]
            lines += [f"- LINE {f.line}{'' if f.end_line == f.line else f'-{f.end_line}'}: {f.category}: "
                      f"{f.comment}" for f in self.findings]
            return "\n".join(lines + ([f"\n**Summary:** {self.summary}"] if self.summary else []))
        if self.kind == "explain":
            return f"**{self.where}:** {self.question}\n\n{self.answer}"
        return f"**You:** {self.question}\n\n{self.answer}"


def answer_display(answer: str) -> str:
    """A chat answer as shown: its "LINE n: …" lines each on their own
    (Markdown would run them together), as a list; its edit blocks as a
    pointer to their card."""
    shown: list[str] = []
    fenced = in_block = drop_fence = False
    n = 0
    for line in answer.split("\n"):
        fence = line.lstrip().startswith("```")
        if in_block:
            if EDIT_END_RE.match(line):
                in_block = False
                shown[-1] = f"*✎ Edit {n}: see its card below.*"
            continue
        if EDIT_START_RE.match(line):
            in_block, n = True, n + 1
            if fenced and shown and shown[-1].lstrip().startswith("```"):  # (a fence just around it)
                shown.pop()
                fenced, drop_fence = False, True
            shown.append(f"*✎ Writing edit {n}…*")
            continue
        if drop_fence and fence:  # (that fence's end)
            drop_fence = False
            continue
        if line.strip():
            drop_fence = False
        if fence:
            fenced = not fenced
        elif not fenced and FINDING_LINE_RE.match(line):
            line = "- " + line.strip()
        shown.append(line)
    return "\n".join(shown)


def style_code(document, family: str, style_name: str | None = None):
    """Code in a Markdown document in a fixed-width font (Qt's Markdown asks
    only for "monospace", which macOS turns into a serif font, or ignores),
    and code blocks labeled with a language (```fortran, ```toml…)
    highlighted as the editor would, in its colors (``style_name``: a
    Pygments style; by default the one the editor uses)."""
    fixed = QTextCharFormat()
    fixed.setFontFamilies([family])
    cursor = QTextCursor(document)
    block = document.begin()
    while block.isValid():
        if block.blockFormat().hasProperty(QTextFormat.BlockCodeFence):
            language = str(block.blockFormat().property(QTextFormat.BlockCodeLanguage) or "")
            lines = []
            while block.isValid() and block.blockFormat().hasProperty(QTextFormat.BlockCodeFence) \
                    and str(block.blockFormat().property(QTextFormat.BlockCodeLanguage) or "") == language:
                lines.append(block)
                block = block.next()
            cursor.setPosition(lines[0].position())
            cursor.setPosition(lines[-1].position() + lines[-1].length() - 1, QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(fixed)
            _highlight(cursor, lines, language, style_name)
            continue
        it = block.begin()
        while not it.atEnd():
            fragment = it.fragment()
            if fragment.charFormat().fontFixedPitch():
                cursor.setPosition(fragment.position())
                cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.KeepAnchor)
                cursor.mergeCharFormat(fixed)
            it += 1
        block = block.next()


def _highlight(cursor: QTextCursor, lines: list, language: str, style_name: str | None):
    """One code block's lines, colored by the lexer for ``language``."""
    if not language:
        return
    try:
        lexer = hl.keep_positions(get_lexer_by_name(language))
        style = get_style_by_name(style_name or hl.style_name(QApplication.palette()))
    except ClassNotFound:
        return  # (a language Pygments doesn't know: plain)
    background = QTextBlockFormat()
    background.setBackground(QColor(style.background_color))
    plain = QTextCharFormat()
    plain.setForeground(hl.style_text_color(style))
    first = lines[0].position()
    cursor.setPosition(first)
    cursor.setPosition(lines[-1].position() + lines[-1].length() - 1, QTextCursor.KeepAnchor)
    cursor.mergeBlockFormat(background)
    cursor.mergeCharFormat(plain)
    text = "\n".join(line.text() for line in lines)  # (a line break is one position, as between blocks)
    for index, ttype, value in lexer.get_tokens_unprocessed(text):
        if value and not value.isspace():
            cursor.setPosition(first + index)
            cursor.setPosition(first + index + len(value), QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(hl.token_format(style, ttype))


class MarkdownView(QTextBrowser):
    """Markdown as tall as its text (the timeline scrolls, not this)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setOpenExternalLinks(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.document().documentLayout().documentSizeChanged.connect(
            lambda size: self.setFixedHeight(int(size.height()) + 6))
        self._text = None

    def set_markdown(self, text: str):
        if text != self._text:
            self._text = text
            self.setMarkdown(text)
            style_code(self.document(), monospace_font(editor=True).family())

    def restyle(self):
        """Again, in the editor's colors as they are now."""
        text, self._text = self._text, None
        if text is not None:
            self.set_markdown(text)


class WrappedLabel(QLabel):
    """A word-wrapped label that remembers its size for its text: Qt asks a
    wrapped label its height again on every layout pass, laying out its text
    each time, and with the timeline's findings (hundreds of rows) and an
    answer streaming in, that kept the window busy."""

    def __init__(self, text: str = ""):
        super().__init__(text)
        self.setWordWrap(True)
        self._heights: dict[int, int] = {}
        self._hints: dict[str, object] = {}

    def setText(self, text: str):
        if text != self.text():
            self._forget()
        super().setText(text)

    def _forget(self):
        self._heights.clear()
        self._hints.clear()

    def changeEvent(self, event):
        if event.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._forget()
        super().changeEvent(event)

    def heightForWidth(self, width: int) -> int:
        height = self._heights.get(width)
        if height is None:
            height = self._heights[width] = super().heightForWidth(width)
        return height

    def sizeHint(self):
        if "size" not in self._hints:
            self._hints["size"] = super().sizeHint()
        return self._hints["size"]

    def minimumSizeHint(self):
        if "minimum" not in self._hints:
            self._hints["minimum"] = super().minimumSizeHint()
        return self._hints["minimum"]


class FindingRow(QWidget):
    """One finding: its line, category and comment; click to go to the
    line; Fix with AI…"""

    def __init__(self, view: FindingsView):
        super().__init__()
        self.finding: Finding | None = None
        self.setObjectName("FindingRow")
        self.setCursor(Qt.PointingHandCursor)
        self.label = WrappedLabel()
        self.label.setToolTip("Go to the line")
        fix = QToolButton()
        fix.setText("Fix with AI…")
        fix.setToolTip("Ask the AI for an edit that addresses this finding")
        fix.clicked.connect(lambda: self.finding is not None and view.fix.emit(self.finding))
        box = QHBoxLayout(self)
        box.setContentsMargins(6, 3, 6, 3)
        box.addWidget(self.label, 1)
        box.addWidget(fix, 0, Qt.AlignTop)
        self._view = view

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.finding is not None:
            self._view.clicked.emit(self.finding)
        super().mousePressEvent(event)


class FindingsView(QWidget):
    """Findings, one row each: click to go to the line; Fix with AI…

    The rows are kept and updated in place (only a row whose finding changed
    gets new text; new findings add rows at the end): rebuilding every row
    for each finding a review streamed in took ever longer, and with a few
    dozen findings the window crawled."""

    clicked = Signal(object)
    fix = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(2)
        self.rows: list[FindingRow] = []

    def show_findings(self, findings: list[Finding], pal: dict, is_light: bool):
        family = monospace_font(editor=True).family()
        # Each row stays with its finding (by line and category): a finding
        # coming in between others gets a new row there, rather than every
        # row below taking the next one's text (and being measured again).
        spare: dict[tuple, list[FindingRow]] = {}
        for row in self.rows:
            spare.setdefault(self._key(row.finding), []).append(row)
        rows = []
        for f in sorted(findings, key=lambda f: f.line):
            same = spare.get(self._key(f))
            row = same.pop(0) if same else FindingRow(self)
            row.finding = f
            color = CATEGORY_COLORS.get(f.category, pal["muted"])
            lines = str(f.line) if f.end_line == f.line else f"{f.line}–{f.end_line}"
            text = (f"<span style='font-family:\"{family}\"'>{lines}</span>"
                    f"&nbsp;&nbsp;<b style='color:{color}'>{CATEGORY_ICONS.get(f.category, '')} "
                    f"{_esc(f.category)}</b>&nbsp;&nbsp;{_esc(f.comment)}")
            if row.label.text() != text:  # (only what changed is measured again)
                row.label.setText(text)
            rows.append(row)
        for left in spare.values():
            for row in left:
                self.layout_.removeWidget(row)
                row.deleteLater()
        for index, row in enumerate(rows):  # (new rows into their places; the others keep theirs)
            if self.layout_.indexOf(row) != index:
                self.layout_.insertWidget(index, row)
        self.rows = rows
        self.setVisible(bool(findings))

    @staticmethod
    def _key(f: Finding | None) -> tuple:
        return (f.line, f.end_line, f.category) if f is not None else ()


class EntryWidget(QFrame):
    """One entry of the timeline: a header (click to fold or open it) and
    what it holds."""

    def __init__(self, entry: Entry, monitor: ReviewMonitor):
        super().__init__()
        self.entry, self.monitor = entry, monitor
        self.expanded = True
        self.setObjectName("Entry")
        self.header = QWidget()
        self.header.setCursor(Qt.PointingHandCursor)
        self.header.mousePressEvent = lambda event: self.set_expanded(not self.expanded) \
            if event.button() == Qt.LeftButton else None
        self.kind = QLabel()
        self.title = QLabel()
        self.title.setTextFormat(Qt.RichText)
        self.title.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.when = QLabel()
        self.show_button = QToolButton()
        self.show_button.setText("Show in Editor")
        self.show_button.setToolTip("Mark these findings in the editor again")
        self.show_button.clicked.connect(lambda: monitor.show_in_editor_requested.emit(entry))
        self.copy_button = QToolButton()
        self.copy_button.setText("Copy")
        self.copy_button.setToolTip("Copy this entry (Markdown)")
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(entry.markdown()))
        self.chevron = QLabel()
        top = QHBoxLayout(self.header)
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(8)
        top.addWidget(self.chevron)
        top.addWidget(self.kind)
        top.addWidget(self.title, 1)
        top.addWidget(self.show_button)
        top.addWidget(self.copy_button)
        top.addWidget(self.when)

        self.body = QWidget()
        self.question = QLabel()
        self.question.setWordWrap(True)
        self.question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reasoning_button = QToolButton()
        self.reasoning_button.setCheckable(True)
        self.reasoning_button.setText("Show reasoning")
        self.reasoning_button.toggled.connect(self._show_reasoning)
        self.reasoning_button.setToolTip("What the model thought before it answered")
        self.reasoning = QLabel()
        self.reasoning.setWordWrap(True)
        self.reasoning.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.reasoning.setTextFormat(Qt.PlainText)
        self.reasoning.hide()
        self.answer = MarkdownView()
        self.findings = FindingsView()
        self.findings.clicked.connect(lambda f: monitor.finding_selected.emit(entry, f))
        self.findings.fix.connect(lambda f: monitor.fix_requested.emit(entry, f))
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.edit_list = EditList(scrolling=False)
        self.edit_list.accept_requested.connect(lambda i: monitor.accept_requested.emit(entry, i))
        self.edit_list.reject_requested.connect(lambda i: monitor.reject_requested.emit(entry, i))
        self.edit_list.accept_all_requested.connect(lambda: monitor.accept_all_requested.emit(entry))
        self.edit_list.reject_all_requested.connect(lambda: monitor.reject_all_requested.emit(entry))
        self.edit_list.edit_clicked.connect(lambda i: monitor.edit_clicked.emit(entry, i))
        self.edit_list.hide()
        self.footer = QLabel()
        body = QVBoxLayout(self.body)
        body.setContentsMargins(22, 4, 0, 0)
        body.setSpacing(6)
        for widget in (self.question, self.reasoning_button, self.reasoning, self.answer, self.findings,
                       self.summary, self.edit_list, self.footer):
            body.addWidget(widget)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(4)
        layout.addWidget(self.header)
        layout.addWidget(self.body)
        self.refresh()

    def _show_reasoning(self, on: bool):
        self.reasoning_button.setText("Hide reasoning" if on else "Show reasoning")
        self.reasoning.setText(self.entry.reasoning.strip())
        self.reasoning.setVisible(on)

    def set_expanded(self, on: bool):
        self.expanded = on
        self.refresh()

    def refresh(self, file_text: str | None = None):
        """Show the entry as it is now (``file_text``: its file's text, for
        the edit cards' diffs)."""
        e, pal = self.entry, self.monitor.palette_colors
        muted = pal["muted"]
        label, color = KINDS.get(e.kind, (e.kind, muted))
        self.kind.setText(f"<b style='color:{color}'>{label}</b>")
        self.chevron.setText("▾" if self.expanded else "▸")
        self.title.setText(self._title(muted))
        self.when.setText(f"<span style='color:{muted}'>{e.started}</span>")
        self.show_button.setVisible(bool(e.findings) and e.path is not None)
        self.copy_button.setVisible(self.expanded and (bool(e.answer) or bool(e.findings)))
        self.body.setVisible(self.expanded)
        if not self.expanded:
            return
        if e.kind == "review":
            question = f"<span style='color:{muted}'>{_esc(e.question)}</span>"
            if e.status == RUNNING and not e.findings:
                question += f"<br><i style='color:{muted}'>Findings appear here as the model writes them.</i>"
            elif e.status == DONE and not e.findings:
                question += "<br><i>No specific findings.</i>"
        elif e.kind == "explain":
            question = f"<b>{_esc(e.question)}</b>"
        else:
            question = f"<b>You:</b> {_esc(e.question)}"
        self.question.setText(question)
        self.reasoning_button.setVisible(bool(e.reasoning))
        if e.reasoning and self.reasoning.isVisibleTo(self.body):
            self.reasoning.setText(e.reasoning.strip())
        self.reasoning.setStyleSheet(f"color: {muted};")
        if e.kind == "review":
            text = ""
        elif e.kind == "explain":
            text = e.answer
        else:
            text = answer_display(e.answer)
        if e.status == FAILED and not e.answer and e.kind != "review":
            text = f"*{e.note}*"
        self.answer.setVisible(bool(text))
        self.answer.set_markdown(text)
        self.findings.show_findings(e.findings, pal, self.monitor.is_light)
        self.summary.setText(f"<b>Summary:</b> {_esc(e.summary)}" if e.summary else "")
        self.summary.setVisible(bool(e.summary))
        if e.edits and file_text is not None:
            self.edit_list.set_palette(pal, self.monitor.is_light)
            self.edit_list.show_edits(e.edits, file_text, e.where, monospace_font(editor=True).family())
        self.edit_list.setVisible(bool(e.edits))
        note = e.note if e.status != RUNNING else ""
        if e.status == FAILED and e.kind != "review" and not e.answer:
            note = ""  # (said above)
        self.footer.setText(f"<span style='color:{'#e03131' if e.status == FAILED else muted}'>"
                            f"{_esc(note)}</span>" if note else "")
        self.footer.setVisible(bool(note))

    def _title(self, muted: str) -> str:
        e = self.entry
        parts = []
        if e.kind in ("ask", "edit", "explain"):
            question = e.question if len(e.question) <= 80 else e.question[:77] + "…"
            parts.append(f"“{_esc(question)}”")
        if e.where:
            parts.append(_esc(e.where))
        if e.kind == "review" and e.question:
            parts.append(_esc(e.question.lower()))
        status = ""
        if e.status == RUNNING:
            status = "running…"
        elif e.status == FAILED:
            status = "failed" if "stopped" not in e.note.lower() else "stopped"
        elif e.kind == "review" or e.findings:
            status = f"{len(e.findings)} finding{'s' * (len(e.findings) != 1)}"
        if e.edits:
            applied = sum(x.status == ed.APPLIED for x in e.edits)
            pending = e.pending_edits()
            status = ", ".join(s for s in (status if e.status != DONE else "",
                                           f"{applied} of {len(e.edits)} applied",
                                           f"{pending} pending" if pending else "") if s)
        text = " · ".join(parts)
        return text + (f" <span style='color:{muted}'>· {status}</span>" if status else "")


class Divider(QLabel):
    def __init__(self):
        super().__init__("— New chat: the AI forgets everything above —")
        self.setAlignment(Qt.AlignCenter)
        self.entry = Entry("divider", status=DONE)

    def refresh(self, _file_text=None):
        self.setStyleSheet(f"color: {PALETTES['light' if app_is_light() else 'dark']['muted']};")


class ReviewMonitor(QWidget):
    """The timeline, the progress of what's running (and Stop), and the box
    for questions (Ask) and changes (Edit). It emits what the user does."""

    stop_requested = Signal()
    chat_requested = Signal(str, bool, str)  # question, send the open file along, "ask" or "edit"
    new_chat_requested = Signal()
    clear_requested = Signal()
    selection_cleared = Signal()
    finding_selected = Signal(object, object)  # entry, finding
    fix_requested = Signal(object, object)     # entry, finding
    show_in_editor_requested = Signal(object)  # entry
    accept_requested = Signal(object, int)     # entry, edit's index
    reject_requested = Signal(object, int)
    accept_all_requested = Signal(object)
    reject_all_requested = Signal(object)
    edit_clicked = Signal(object, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.entries: list[Entry] = []
        self.widgets: list[EntryWidget | Divider] = []
        self.current: Entry | None = None  # the one running
        self.filter = "all"
        self.context_note = ""  # how full the model's context is ("context ~14k of 32k")
        self.is_light = True
        self.palette_colors = PALETTES["light"]
        self._started = 0.0
        self._tokens = 0
        self._status = ""
        self._follow = True  # (keep the newest in view, unless scrolled up)

        self.setObjectName("ReviewMonitor")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.title_icon = TitleIcon("robot")
        self.title = QLabel("AI")
        self.subtitle = StatusLabel()
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_icon)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle)
        header.addStretch(1)
        self.filter_buttons: dict[str, QPushButton] = {}
        group = QButtonGroup(self)
        for key, label in FILTERS.items():
            button = QPushButton(label)
            button.setCheckable(True)
            button.setObjectName("Filter")
            button.clicked.connect(lambda _=False, k=key: self.set_filter(k))
            group.addButton(button)
            header.addWidget(button)
            self.filter_buttons[key] = button
        self.filter_buttons["all"].setChecked(True)
        self.clear_button = QPushButton("Clear")
        self.clear_button.setToolTip("Empty the timeline (the AI forgets it too)")
        self.clear_button.clicked.connect(self.clear_requested)
        header.addSpacing(6)
        header.addWidget(self.clear_button)

        self.progress = ProgressLine(self)
        self.progress_label = StatusLabel()
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop the model")
        self.stop_button.clicked.connect(self.stop_requested)
        self.stop_button.hide()
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)
        progress_row.addWidget(self.stop_button)

        self.timeline = QWidget()
        self.timeline.setObjectName("Timeline")
        self.timeline_layout = QVBoxLayout(self.timeline)
        self.timeline_layout.setContentsMargins(0, 0, 4, 0)
        self.timeline_layout.setSpacing(6)
        self.empty = QLabel("Everything the AI does shows here: reviews (AI → Review Current File), "
                            "explanations (Explain with AI), and questions and edits from the box below.")
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.timeline_layout.addWidget(self.empty)
        self.timeline_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setWidget(self.timeline)
        bar = self.scroll.verticalScrollBar()
        bar.rangeChanged.connect(lambda _lo, hi: bar.setValue(hi) if self._follow else None)
        bar.valueChanged.connect(lambda value: setattr(self, "_follow", value >= bar.maximum() - 4))

        # The box for questions and changes.
        self.chat_kind = "ask"
        self.ask_button = QPushButton("Ask")
        self.edit_button = QPushButton("Edit")
        self.ask_button.setToolTip("Ask about Fortran, fpm, the open file, or anything above")
        self.edit_button.setToolTip("Ask for a change to the open file: you accept or reject each edit")
        kinds = QButtonGroup(self)
        for button, kind in ((self.ask_button, "ask"), (self.edit_button, "edit")):
            button.setCheckable(True)
            button.setObjectName("ChatKind")
            kinds.addButton(button)
            button.clicked.connect(lambda _=False, k=kind: self.set_chat_kind(k))
        self.ask_button.setChecked(True)
        self.selection_note = QPushButton()  # (Edit Selection with AI: which lines; click to drop)
        self.selection_note.setToolTip("Only these lines are to change (click to drop the selection)")
        self.selection_note.clicked.connect(lambda: (self.set_selection_note(""), self.selection_cleared.emit()))
        self.selection_note.hide()
        self.chat_edit = QLineEdit()
        self.chat_edit.setPlaceholderText("Ask the AI about Fortran, fpm or the open file… (Return to send)")
        self.chat_edit.setClearButtonEnabled(True)
        self.chat_edit.returnPressed.connect(self._send_chat)
        self.chat_edit.installEventFilter(self)  # (Up/Down: earlier questions)
        self.asked: list[str] = []  # questions asked, oldest first (this run of the app only)
        self._recall = 0            # where Up/Down are in them (len(asked): the text being typed)
        self._draft = ""            # what was being typed before Up
        self.chat_file_check = QCheckBox("Send the open file")
        self.chat_file_check.setChecked(True)
        self.chat_file_check.setToolTip("Send the file open in the editor along with the question, so the "
                                        "answer can be about it (lines it points to are listed and marked "
                                        "in the editor)")
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send_chat)
        self.new_chat_button = QPushButton("New Chat")
        self.new_chat_button.setToolTip("The AI forgets everything so far (it stays in the timeline)")
        self.new_chat_button.clicked.connect(self.new_chat_requested)
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        chat_row.addWidget(self.ask_button)
        chat_row.addWidget(self.edit_button)
        chat_row.addSpacing(4)
        chat_row.addWidget(self.selection_note)
        chat_row.addWidget(self.chat_edit, 1)
        chat_row.addWidget(self.chat_file_check)
        chat_row.addWidget(self.send_button)
        chat_row.addWidget(self.new_chat_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(chat_row)

        # While the model writes, the running entry is shown again at most
        # this often (each time is the whole answer, as Markdown, its code
        # highlighted: on every piece, a long answer froze the window).
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self._render_current)
        self._render_cost = 0.0  # seconds the last showing took
        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._update_label)
        self.refresh_fonts()

    # ------------------------------------------------------------- styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        accent = "#1f6feb" if self.is_light else "#388bfd"
        self.setStyleSheet(
            panel_stylesheet(self.objectName(), pal)
            + f"""
            #ReviewMonitor QScrollArea, #Timeline {{ background: {pal['bg']}; border: none; }}
            #Entry {{ border: 1px solid {pal['line']}; border-radius: 7px; background: {pal['bg']}; }}
            #FindingRow:hover {{ background: {pal['hover']}; border-radius: 4px; }}
            QPushButton#ChatKind:checked, QPushButton#Filter:checked {{ background: {accent}; color: white;
                border: 1px solid {accent}; border-radius: 5px; padding: 3px 10px; }}
            """)
        self.title_icon.draw(pal["text"], self.title)
        self.subtitle.setStyleSheet(f"color: {pal['muted']};")
        self.empty.setStyleSheet(f"color: {pal['muted']};")
        for widget in self.widgets:
            if isinstance(widget, EntryWidget):
                widget.answer.restyle()  # (code in the editor's colors, which may have changed)
            self._refresh(widget)
        self.progress.update()

    def refresh_fonts(self):
        title_font = QFont(QApplication.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.refresh_palette()

    # ------------------------------------------------------------ entries
    @property
    def running(self) -> bool:
        return self.current is not None

    def add_entry(self, entry: Entry) -> Entry:
        """A new action, at the bottom (the older ones fold); it's running."""
        for widget in self.widgets:
            if isinstance(widget, EntryWidget) and widget.expanded:
                widget.set_expanded(False)
        self._add_widget(EntryWidget(entry, self), entry)
        self.current = entry
        self.subtitle.setText(" · ".join(s for s in (entry.where, entry.model) if s))
        self._status = "Starting"
        self._started = time.monotonic()
        self._tokens = 0
        self.progress.set_busy(True)
        self.stop_button.setEnabled(False)  # (until there's a model to stop: see set_stoppable)
        self.stop_button.show()
        self._timer.start()
        self._update_label()
        return entry

    def add_divider(self):
        """New Chat: a line across the timeline."""
        divider = Divider()
        self._add_widget(divider, divider.entry)

    def _add_widget(self, widget, entry: Entry):
        self._follow = True
        self.entries.append(entry)
        self.widgets.append(widget)
        self.timeline_layout.insertWidget(self.timeline_layout.count() - 1, widget)
        self.empty.hide()
        self._apply_filter(widget)
        self._refresh(widget)

    def widget_for(self, entry: Entry) -> EntryWidget | None:
        return next((w for w in self.widgets if w.entry is entry), None)

    def refresh(self, entry: Entry, file_text: str | None = None):
        """Show an entry as it is now (``file_text``: its file's text, for
        its edit cards)."""
        widget = self.widget_for(entry)
        if widget is not None:
            self._refresh(widget, file_text)

    def _refresh(self, widget, file_text: str | None = None):
        widget.refresh(file_text)
        self._apply_filter(widget)

    def set_status(self, status: str, fraction: float = -1.0):
        """What the running entry is doing: a status text, or a download's
        progress (fraction 0..1)."""
        self._status = status
        if fraction >= 0:
            self.progress.set(fraction, "running")
        else:
            self.progress.set_busy(True)
        self._update_label()

    def add_answer(self, text: str):
        """More of the running entry's answer, as the model writes it."""
        entry = self.current
        if entry is None:
            return
        self._tokens += 1
        word = {"review": "Reviewing", "explain": "Explaining"}.get(entry.kind, "Answering")
        if self._status != word:
            self._status = word
            self._update_label()
        entry.answer += text
        self._render_soon()

    RENDER_INTERVAL = 0.15  # seconds, at least, between showings of the answer being written

    def refresh_soon(self, entry: Entry):
        """Show an entry again: the running one with the next showing (see
        _render_soon: what the model writes comes in many small pieces),
        any other one now."""
        if entry is self.current:
            self._render_soon()
        else:
            self.refresh(entry)

    def _render_soon(self):
        if not self._render_timer.isActive():
            # (Twice what the last showing took, if more: the window stays responsive.)
            self._render_timer.start(int(1000 * max(self.RENDER_INTERVAL, 2 * self._render_cost)))

    def _render_current(self):
        if self.current is not None:
            started = time.monotonic()
            self.refresh(self.current)
            # Laid out now, not later: the time it takes counts, so a costly
            # showing makes the next one wait longer and the window stays free.
            # Only this panel's timeline: laying out the whole app here (all
            # posted layout requests) cut into other windows' changes, a panel
            # docked again by double-clicking its title left half placed.
            widget = self.widget_for(self.current)
            if widget is not None:
                widget.layout().activate()
            self.timeline_layout.activate()
            self._render_cost = time.monotonic() - started

    def add_reasoning(self, text: str):
        """More of a thinking model's reasoning (before its answer)."""
        entry = self.current
        if entry is None:
            return
        self._tokens += 1
        if self._status != "Reasoning":
            self._status = "Reasoning"
            self._update_label()
        entry.reasoning += text
        self._render_soon()

    def finish(self, ok: bool, note: str = ""):
        """The running entry is done (or failed: ``note`` says why)."""
        entry = self.current
        self.current = None
        self._render_timer.stop()  # (shown in full just below)
        self._render_cost = 0.0
        self._timer.stop()
        self.stop_button.hide()
        self.progress.set(1.0, "ok" if ok else "failed")
        self._status = note
        if entry is not None:
            entry.status = DONE if ok else FAILED
            entry.note = note
            self.refresh(entry)
        self._update_label()

    def set_stoppable(self, on: bool):
        """The model is working (Stop can end it), or not."""
        self.stop_button.setEnabled(on)

    def clear(self):
        """An empty timeline."""
        for widget in self.widgets:
            widget.deleteLater()
        self.entries, self.widgets, self.current = [], [], None
        self._render_timer.stop()
        self._timer.stop()
        self.stop_button.hide()
        self.progress.set(0, "running")
        self._status = ""
        self.context_note = ""
        self.subtitle.clear()
        self.empty.show()
        self._update_label()

    def reset(self):
        """Back to the empty state (e.g. when another project is opened)."""
        self.clear()

    # ------------------------------------------------------------- filter
    def set_filter(self, key: str):
        self.filter = key
        self.filter_buttons[key].setChecked(True)
        for widget in self.widgets:
            self._apply_filter(widget)

    def _apply_filter(self, widget):
        e, key = widget.entry, self.filter
        shown = (key == "all" or (key == "chat" and e.kind in ("ask", "edit"))
                 or (key == "pending" and e.pending_edits() > 0) or e.kind == key)
        widget.setVisible(shown)

    # --------------------------------------------------------------- chat
    def set_sending(self, on: bool):
        """Questions can be sent (not while the model is busy: the question
        stays in the box until it's done)."""
        self.send_button.setEnabled(on)
        self.send_button.setToolTip("" if on else "The AI is busy: wait for its answer, or Stop it")

    def _send_chat(self):
        question = self.chat_edit.text().strip()
        if question and self.send_button.isEnabled():
            if not self.asked or self.asked[-1] != question:
                self.asked.append(question)
            self._recall, self._draft = len(self.asked), ""
            self.chat_requested.emit(question, self.chat_file_check.isChecked(), self.chat_kind)

    def set_chat_kind(self, kind: str):
        """"ask" or "edit" (which always sends the open file)."""
        self.chat_kind = kind
        editing = kind == "edit"
        (self.edit_button if editing else self.ask_button).setChecked(True)
        if editing:
            self._file_wanted = self.chat_file_check.isChecked()
            self.chat_file_check.setChecked(True)
        elif not self.chat_file_check.isEnabled():
            self.chat_file_check.setChecked(getattr(self, "_file_wanted", True))
        self.chat_file_check.setEnabled(not editing)
        self.chat_edit.setPlaceholderText(
            "Describe a change to the open file… (Return to send)" if editing
            else "Ask the AI about Fortran, fpm or the open file… (Return to send)")
        if not editing:
            self.set_selection_note("")

    def set_selection_note(self, text: str):
        self.selection_note.setText(f"{text}  ✕" if text else "")
        self.selection_note.setVisible(bool(text))

    def _recall_question(self, step: int) -> bool:
        """Up (-1) / Down (+1): the previous / next question asked, as in a
        shell; past the newest, what was being typed. False at either end."""
        position = self._recall + step
        if not 0 <= position <= len(self.asked):
            return False
        if self._recall == len(self.asked):
            self._draft = self.chat_edit.text()
        self._recall = position
        self.chat_edit.setText(self.asked[position] if position < len(self.asked) else self._draft)
        return True

    def eventFilter(self, obj, event):
        # (macOS marks arrow keys with KeypadModifier: only real modifiers count.)
        if obj is self.chat_edit and event.type() == QEvent.KeyPress \
                and not event.modifiers() & ~Qt.KeypadModifier and event.key() in (Qt.Key_Up, Qt.Key_Down):
            self._recall_question(-1 if event.key() == Qt.Key_Up else 1)
            return True
        return super().eventFilter(obj, event)

    # ------------------------------------------------------------- display
    def _update_label(self):
        pal = self.palette_colors
        muted = f"<span style='color:{pal['muted']}'>"
        context = f" · {self.context_note}" if self.context_note else ""
        if self.running:
            seconds = time.monotonic() - self._started
            extra = f" · {self._tokens} token{'s' * (self._tokens != 1)}" if self._tokens else ""
            text = f"<b>{self._status or 'Working'}…</b> {muted}{seconds:.0f} s{extra}{context}</span>"
        elif self._status:
            text = f"{muted}{_esc(self._status)}{context}</span>"
        else:
            text = ""
        self.progress_label.setText(text)
