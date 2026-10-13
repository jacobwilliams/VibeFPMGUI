"""The "Review" tab: progress and findings of an AI code review, the
model's explanation of one problem (*Explain with AI*), or a chat with the
model (the box at the bottom)."""

from __future__ import annotations

import html
import re
import time
from typing import ClassVar

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from qtpy.QtGui import (
    QColor,
    QFont,
    QFontDatabase,
    QFontMetricsF,
    QPainter,
    QPen,
    QTextCharFormat,
    QTextCursor,
    QTextFormat,
)
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from .ai_review import CATEGORIES, Finding
from .build_monitor import (
    PALETTES,
    ROW_HEIGHT,
    ProgressLine,
    SortableTable,
    TitleIcon,
    _tint,
    app_is_light,
    chip_stylesheet,
    panel_stylesheet,
    scaled,
)
from .editor import monospace_font

FINDING_LINE_RE = re.compile(r"\s*\**\s*LINES?\s*\d", re.IGNORECASE)  # (a finding in a chat answer)

CATEGORY_COLORS = {"bug": "#e03131", "warning": "#f08c00", "performance": "#0c8599",
                   "style": "#7048e8", "suggestion": "#1c7ed6"}
CATEGORY_ICONS = {"bug": "✕", "warning": "⚠", "performance": "⚡", "style": "✎", "suggestion": "💡"}


class FindingDelegate(QStyledItemDelegate):
    def __init__(self, monitor: ReviewMonitor):
        super().__init__(monitor.table)
        self.monitor = monitor

    def sizeHint(self, option, index):
        return QSize(100, scaled(ROW_HEIGHT))

    def paint(self, p: QPainter, option, index):
        mon = self.monitor
        if index.row() >= len(mon.findings):
            return
        f = mon.findings[index.row()]
        pal = mon.palette_colors
        r = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if index.row() == mon.selected_row:
            p.fillRect(r, _tint(QColor(pal["accent1"]), 28))
            if index.column() == 0:
                p.fillRect(QRectF(r.left(), r.top() + 6, 3, r.height() - 12), QColor(pal["accent1"]))
        elif index.row() == mon.hover_row:
            p.fillRect(r, QColor(pal["hover"]))
        p.setPen(QPen(QColor(pal["line"]), 1))
        p.drawLine(r.bottomLeft(), r.bottomRight())
        small = QFont(option.font)
        small.setPointSizeF(max(option.font.pointSizeF() - 2, 7))
        color = QColor(CATEGORY_COLORS.get(f.category, pal["muted"]))
        if index.column() == 0:
            mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
            mono.setPointSizeF(option.font.pointSizeF())
            p.setFont(mono)
            p.setPen(QColor(pal["text"]))
            text = str(f.line) if f.end_line == f.line else f"{f.line}–{f.end_line}"
            p.drawText(r.adjusted(14, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        elif index.column() == 1:
            font = QFont(small)
            font.setWeight(QFont.DemiBold)
            label = f"{CATEGORY_ICONS.get(f.category, '')} {f.category}"
            pill = QRectF(r.left() + 6, r.center().y() - 11,
                          QFontMetricsF(font).horizontalAdvance(label) + 18, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(_tint(color, 40))
            p.drawRoundedRect(pill, 11, 11)
            p.setFont(font)
            p.setPen(color.darker(115) if mon.is_light else color)
            p.drawText(pill, Qt.AlignCenter, label)
        else:
            p.setFont(option.font)
            p.setPen(QColor(pal["text"]))
            text_r = r.adjusted(8, 4, -8, -4)
            # Two lines of the comment, elided; the tooltip has all of it.
            p.drawText(text_r, Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap,
                       self._fit(f.comment, option.font, text_r))
        p.restore()

    @staticmethod
    def _fit(text: str, font: QFont, rect: QRectF) -> str:
        fm = QFontMetricsF(font)
        lines = max(1, int(rect.height() // fm.lineSpacing()))
        if fm.horizontalAdvance(text) <= rect.width() * lines * 0.95:
            return text
        return fm.elidedText(text, Qt.ElideRight, rect.width() * lines * 0.9)


def _category_rank(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


class ReviewMonitor(SortableTable, QWidget):
    """Findings table with chips, a progress line and the model's summary.
    Clicking a finding emits `finding_selected` with its line; Stop emits
    `stop_requested`. In the "explain" mode the table gives way to the
    explanation of one problem; in the "chat" mode, to the conversation
    (with the findings above it, if an answer has any). The box at the
    bottom emits `chat_requested` with the question and whether to send the
    open file along; New Chat emits `new_chat_requested`."""

    COLUMNS: ClassVar[list[str]] = ["LINE", "CATEGORY", "COMMENT"]
    finding_selected = Signal(int)
    stop_requested = Signal()
    chat_requested = Signal(str, bool)  # question, send the open file along
    new_chat_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.findings: list[Finding] = []
        self.hover_row = -1
        self.selected_row = -1
        self.filter_category: str | None = None
        self.running = False
        self.mode = "review"  # or "explain", "chat"
        self.answer = ""     # the explanation (or the chat's latest answer) so far (Markdown)
        self.turns: list[tuple[str, str]] = []  # the chat so far: (question, answer)
        self._started = 0.0
        self.is_light = True
        self.palette_colors = PALETTES["light"]

        self.setObjectName("ReviewMonitor")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.title_icon = TitleIcon("robot")
        self.title = QLabel("AI review")
        self.subtitle = QLabel()
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_icon)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle)
        header.addStretch(1)
        self.chips: dict[str, QPushButton] = {}
        for category in CATEGORIES:
            chip = QPushButton()
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(f"Show only “{category}” findings (click again to show all)")
            chip.clicked.connect(lambda checked, c=category: self.set_filter(c if checked else None))
            chip.hide()
            header.addWidget(chip)
            self.chips[category] = chip

        self.progress = ProgressLine(self)
        self.progress_label = QLabel()
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Stop the model")
        self.stop_button.clicked.connect(self.stop_requested)
        self.stop_button.hide()
        progress_row.addWidget(self.stop_button)
        self.copy_button = QPushButton("Copy")
        self.copy_button.setToolTip("Copy the answer (Markdown)")
        self.copy_button.clicked.connect(lambda: QApplication.clipboard().setText(self.copy_text()))
        self.copy_button.hide()
        progress_row.addWidget(self.copy_button)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setItemDelegate(FindingDelegate(self))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(scaled(ROW_HEIGHT))
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.cellEntered.connect(self._set_hover)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.viewport().installEventFilter(self)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self._init_sorting({
            0: lambda f: (f.line, f.end_line),
            1: lambda f: (_category_rank(f.category), f.line),
            2: lambda f: f.comment.lower(),
        })

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)
        self.question = QLabel()  # (explain mode: the problem's message)
        self.question.setWordWrap(True)
        self.question.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.explanation = QTextBrowser()
        self.explanation.setOpenExternalLinks(True)
        self.explanation.setFrameShape(QTextBrowser.NoFrame)

        # The chat box.
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
                                        "answer can be about it (lines it points to are listed here and "
                                        "marked in the editor)")
        self.send_button = QPushButton("Send")
        self.send_button.clicked.connect(self._send_chat)
        self.new_chat_button = QPushButton("New Chat")
        self.new_chat_button.setToolTip("Start over: the model forgets this conversation")
        self.new_chat_button.clicked.connect(self.new_chat_requested)
        self.new_chat_button.hide()
        chat_row = QHBoxLayout()
        chat_row.setSpacing(8)
        chat_row.addWidget(self.chat_edit, 1)
        chat_row.addWidget(self.chat_file_check)
        chat_row.addWidget(self.send_button)
        chat_row.addWidget(self.new_chat_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.summary)
        layout.addWidget(self.question)
        layout.addWidget(self.explanation, 2)
        layout.addLayout(chat_row)
        self.question.hide()
        self.explanation.hide()

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._tokens = 0
        self._status = ""
        self.refresh_fonts()
        self.show_message("Open a file in the editor and choose AI → Review Current File, or ask the AI below.")

    # ------------------------------------------------------------- styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal))
        self.title_icon.draw(pal["text"], self.title)
        for category, chip in self.chips.items():
            chip.setStyleSheet(chip_stylesheet(pal, CATEGORY_COLORS[category]))
        self.empty.setStyleSheet(f"color: {pal['muted']};")
        self.subtitle.setStyleSheet(f"color: {pal['muted']};")
        self.table.viewport().update()
        self.progress.update()

    def refresh_fonts(self):
        title_font = QFont(QApplication.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.table.verticalHeader().setDefaultSectionSize(scaled(ROW_HEIGHT))
        self.table.setColumnWidth(0, scaled(70))
        self.table.setColumnWidth(1, scaled(140))
        self.refresh_palette()

    # ------------------------------------------------------------- state
    def _set_mode(self, mode: str):
        """"review" (findings), "explain" (one problem's explanation) or
        "chat" (the conversation, and findings if an answer has any)."""
        self.mode = mode
        review, explain, chat = mode == "review", mode == "explain", mode == "chat"
        self.title.setText({"explain": "Explain with AI", "chat": "AI chat"}.get(mode, "AI review"))
        for widget in (self.empty, self.summary):
            widget.setVisible(review)
        self.table.setVisible(review or (chat and bool(self.findings)))
        self.question.setVisible(explain)
        for widget in (self.explanation, self.copy_button):
            widget.setVisible(not review)
        self.new_chat_button.setVisible(chat)
        if explain:
            for chip in self.chips.values():
                chip.hide()

    # ------------------------------------------------------------- chat
    def _send_chat(self):
        question = self.chat_edit.text().strip()
        if question:
            if not self.asked or self.asked[-1] != question:
                self.asked.append(question)
            self._recall, self._draft = len(self.asked), ""
            self.chat_requested.emit(question, self.chat_file_check.isChecked())

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

    def begin_chat(self, description: str, question: str):
        """A question to the model (a new conversation, or the next turn of
        this one): it's shown, and its answer will follow."""
        if self.mode != "chat":
            self.turns = []
            self._clear_findings()
        self._set_mode("chat")
        self.chat_edit.clear()
        self.subtitle.setText(description)
        self.turns.append((question, ""))
        self.answer = ""
        self.copy_button.setEnabled(bool(self.turns[:-1]))
        self.running = True
        self._status = "Starting"
        self.progress.set_busy(True)
        self._render_chat()
        self._start()

    def clear_chat(self):
        """New Chat: an empty conversation."""
        self.turns = []
        self.answer = ""
        self._clear_findings()
        self._render_chat()
        self.copy_button.setEnabled(False)
        self._status = ""
        self.subtitle.clear()
        self.progress.set(0, "running")
        self._update_label()
        self.chat_edit.setFocus()

    def add_chat(self, text: str):
        """More of the latest answer, as the model writes it."""
        self._tokens += 1
        if self._status != "Answering":
            self._status = "Answering"
            self._update_label()
        self.answer += text
        self.turns[-1] = (self.turns[-1][0], self.answer)
        self.copy_button.setEnabled(True)
        self._render_chat()

    def chat_markdown(self) -> str:
        return "\n\n---\n\n".join(f"**You:** {question}\n\n{answer}" for question, answer in self.turns)

    def _chat_display(self) -> str:
        """The conversation as shown: the answers' "LINE n: …" lines each
        on their own (Markdown would run them together), as a list."""
        shown, fenced = [], False
        for line in self.chat_markdown().split("\n"):
            if line.lstrip().startswith("```"):
                fenced = not fenced
            elif not fenced and FINDING_LINE_RE.match(line):
                line = "- " + line.strip()
            shown.append(line)
        return "\n".join(shown)

    def _render_chat(self):
        self._show_markdown(self._chat_display() if self.turns else
                            "*Ask the model anything: about Fortran, fpm, or the file open in the editor.*")

    def copy_text(self) -> str:
        return self.chat_markdown() if self.mode == "chat" else self.answer

    def _clear_findings(self):
        self.findings = []
        self.table.setRowCount(0)
        self.selected_row = -1
        self.summary.clear()
        self.set_filter(None)
        self._update_chips()
        if self.mode == "chat":
            self.table.hide()

    def show_message(self, message: str | None):
        self.empty.setText(message or "")
        self.empty.setVisible(bool(message))
        self.table.setVisible(not message)

    def reset(self):
        """Back to the empty state (e.g. when another project is opened)."""
        self._timer.stop()
        self._set_mode("review")
        self.stop_button.hide()
        self.running = False
        self.findings = []
        self.table.setRowCount(0)
        self.selected_row = -1
        self.summary.clear()
        self.subtitle.clear()
        self.set_filter(None)
        self._update_chips()
        self._started = 0.0
        self._status = ""
        self.progress.set(0, "running")
        self._update_label()
        self.show_message("Open a file in the editor and choose AI → Review Current File, or ask the AI below.")

    def begin(self, description: str, status: str = "Starting…"):
        """A review (or model download) is starting."""
        self._set_mode("review")
        self.findings = []
        self.table.setRowCount(0)
        self.selected_row = -1
        self.summary.clear()
        self.set_filter(None)
        self.subtitle.setText(description)
        self._update_chips()
        self.running = True
        self._status = status
        self.progress.set_busy(True)  # how long the model takes is unknown
        self.show_message("The model is reading the file…")
        self._start()

    def begin_explain(self, description: str, message: str):
        """An explanation of one problem (its ``message``) is starting; the
        findings of the last review are put aside."""
        self.findings = []
        self.table.setRowCount(0)
        self.selected_row = -1
        self.summary.clear()
        self._set_mode("explain")
        self.subtitle.setText(description)
        self.question.setText(f"<b>{html.escape(message, quote=False)}</b>")
        self.answer = ""
        self.explanation.clear()
        self.copy_button.setEnabled(False)
        self.running = True
        self._status = "Starting"
        self.progress.set_busy(True)
        self._start()

    def _start(self):
        self._started = time.monotonic()
        self._tokens = 0
        self.stop_button.setEnabled(False)  # (until there's a model to stop: see set_stoppable)
        self.stop_button.show()
        self._timer.start()
        self._update_label()

    def set_stoppable(self, on: bool):
        """The model is working (Stop can end it), or not."""
        self.stop_button.setEnabled(on)

    def add_explanation(self, text: str):
        """More of the explanation, as the model writes it."""
        self._tokens += 1
        if self._status != "Explaining":
            self._status = "Explaining"
            self._update_label()
        self.answer += text
        self.copy_button.setEnabled(True)
        self._show_markdown(self.answer)

    def _show_markdown(self, text: str):
        bar = self.explanation.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4  # (following the answer, unless scrolled back)
        self.explanation.setMarkdown(text)
        monospace_code(self.explanation.document(), monospace_font(editor=True).family())
        if at_end:
            bar.setValue(bar.maximum())

    def set_status(self, status: str, fraction: float = -1.0):
        """Download progress (fraction 0..1) or a status text (fraction < 0)."""
        self._status = status
        if fraction >= 0:
            self.progress.set(fraction, "running")  # e.g. a download with a known size
        else:
            self.progress.set_busy(True)
        self._update_label()

    def add_text(self, text: str):
        self._tokens += 1  # streamed pieces are roughly tokens
        if self._tokens == 1 and not self.findings:
            self.show_message("Reviewing… (findings appear here as the model writes them)")
        self._status = "Reviewing"

    @staticmethod
    def _key(f: Finding) -> tuple:
        return (f.line, f.end_line, f.category)

    def set_findings(self, findings: list[Finding], summary: str = "", final: bool = True):
        """Show findings. While the model is still writing (``final=False``)
        this is called again with the answer so far: the selection stays on
        the same finding and no "no findings" message or summary is shown."""
        selected = self._selected_object()
        selected_key = self._key(selected) if selected is not None else None
        self.findings = sorted(findings, key=lambda f: (f.line, _category_rank(f.category)))
        for seq, f in enumerate(self.findings):
            f.seq = seq  # original order (by line), for "unsorted"
        self.table.setRowCount(len(self.findings))
        for row in range(len(self.findings)):
            for column in range(len(self.COLUMNS)):
                self.table.setItem(row, column, QTableWidgetItem())
            self._sync_row(row)
        self.selected_row = -1
        self.apply_sort()  # keep the user's chosen order
        if selected_key is not None:
            self.selected_row = next((i for i, f in enumerate(self.findings)
                                      if self._key(f) == selected_key), -1)
        self._update_chips()
        if self.mode == "chat":  # (above the conversation, when there are some)
            self.table.setVisible(bool(self.findings))
            return
        if not final:
            if self.findings:
                self.show_message(None)
            return
        self.summary.setText(f"<b>Summary:</b> {summary}" if summary else "")
        self.show_message(None if self.findings else
                          "No specific findings." + ("" if summary else
                                                     " (See the Output panel for the full answer.)"))

    def finish(self, ok: bool, message: str = ""):
        self.running = False
        self._timer.stop()
        self.stop_button.hide()
        self.progress.set(1.0, "ok" if ok else "failed")
        self._status = message
        if self.mode == "chat":
            if not ok and not self.answer and self.turns:
                self.turns[-1] = (self.turns[-1][0], f"*{message}*")
                self._render_chat()
        elif self.mode == "explain":
            if not ok and not self.answer:
                self.explanation.setPlainText(message)
        elif not ok:
            # Keep findings that already arrived; the error goes in the label.
            if self.findings:
                self.show_message(None)
            else:
                self.show_message(message)
        self._update_label()

    # ------------------------------------------------------------- display
    def _update_chips(self):
        counts = {c: 0 for c in CATEGORIES}
        for f in self.findings:
            counts[f.category] = counts.get(f.category, 0) + 1
        for category, chip in self.chips.items():
            chip.setText(f"{CATEGORY_ICONS[category]} {counts.get(category, 0)}")
            chip.setVisible(counts.get(category, 0) > 0)

    def set_filter(self, category: str | None):
        self.filter_category = category
        for c, chip in self.chips.items():
            chip.setChecked(c == category)
        for row, f in enumerate(self.findings):
            self.table.setRowHidden(row, category is not None and f.category != category)

    def _update_label(self):
        pal = self.palette_colors
        muted = f"<span style='color:{pal['muted']}'>"
        seconds = time.monotonic() - self._started if self._started else 0
        if self.running:
            extra = f" · {self._tokens} token{'s' * (self._tokens != 1)}" if self._tokens else ""
            text = f"<b>{self._status or 'Working'}…</b> {muted}{seconds:.0f} s{extra}</span>"
        elif self.mode == "explain":
            text = f"<b>Explanation</b> {muted}· {self._status}</span>" if self._status else ""
        elif self.mode == "chat":
            n = len(self.findings)
            found = f" · {n} finding{'s' * (n != 1)}" if n else ""
            text = f"<b>Answer</b> {muted}{found} · {self._status}</span>" if self._status else ""
        elif self.findings or self._status:
            n = len(self.findings)
            text = f"<b>{n} finding{'s' * (n != 1)}</b> {muted}· {self._status}</span>"
        else:
            text = ""
        self.progress_label.setText(text)

    def _tick(self):
        self._update_label()  # elapsed time and token count

    # SortableTable hooks
    def rows(self) -> list[Finding]:
        return self.findings

    def _sync_row(self, row: int):
        f = self.findings[row]
        for column in range(len(self.COLUMNS)):
            self.table.item(row, column).setToolTip(f"Line {f.line}: {f.category}\n{f.comment}")

    def _reapply_filter(self):
        self.set_filter(self.filter_category)

    def _row_clicked(self, row: int, _column: int):
        if row < len(self.findings):
            self.selected_row = row
            self.table.viewport().update()
            self.finding_selected.emit(self.findings[row].line)

    def _set_hover(self, row: int, _column: int):
        if row != self.hover_row:
            self.hover_row = row
            self.table.viewport().update()

    def eventFilter(self, obj, event):
        # (macOS marks arrow keys with KeypadModifier: only real modifiers count.)
        if obj is self.chat_edit and event.type() == QEvent.KeyPress \
                and not event.modifiers() & ~Qt.KeypadModifier and event.key() in (Qt.Key_Up, Qt.Key_Down):
            self._recall_question(-1 if event.key() == Qt.Key_Up else 1)
            return True
        if obj is self.table.viewport() and event.type() == QEvent.Leave:
            self._set_hover(-1, 0)
        return super().eventFilter(obj, event)


def monospace_code(document, family: str):
    """Code in a Markdown document in a fixed-width font: Qt's Markdown asks
    only for "monospace", which macOS turns into a serif font (inline) or
    ignores (fenced blocks)."""
    fixed = QTextCharFormat()
    fixed.setFontFamilies([family])
    cursor = QTextCursor(document)
    block = document.begin()
    while block.isValid():
        if block.blockFormat().hasProperty(QTextFormat.BlockCodeFence):
            cursor.setPosition(block.position())
            cursor.setPosition(block.position() + block.length() - 1, QTextCursor.KeepAnchor)
            cursor.mergeCharFormat(fixed)
        else:
            it = block.begin()
            while not it.atEnd():
                fragment = it.fragment()
                if fragment.charFormat().fontFixedPitch():
                    cursor.setPosition(fragment.position())
                    cursor.setPosition(fragment.position() + fragment.length(), QTextCursor.KeepAnchor)
                    cursor.mergeCharFormat(fixed)
                it += 1
        block = block.next()
