"""The edits the AI proposes, one card each, in the Review panel: a diff
of the change (made from the file, so it's right whatever the model wrote)
with Accept and Reject, and Accept All / Reject All below them.

The cards only show `vibefpmgui.ai_edits.Edit` objects and say what the
user chose; `vibefpmgui.main_window` changes the editor's text.
"""

from __future__ import annotations

import difflib
import html

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from . import ai_edits as ed

CONTEXT_LINES = 2  # unchanged lines shown around a change
DIFF_COLORS = {  # (background, text) of removed and added lines
    "light": {"-": ("#ffebe9", "#82071e"), "+": ("#dafbe1", "#116329")},
    "dark": {"-": ("#3d1d20", "#ffa198"), "+": ("#12261e", "#7ee787")},
}


def diff_rows(file_text: str, edit: ed.Edit) -> list[tuple[str, str]]:
    """The change as (" " | "-" | "+", line) rows, with a few unchanged lines
    around it; for an edit that wasn't found, the model's own text."""
    if edit.diff is not None:  # (made or turned down: as it was then)
        return edit.diff
    if not edit.located:
        return ([("-", line) for line in edit.search.split("\n")]
                + [("+", line) for line in edit.replace.split("\n")])
    old = file_text.split("\n")
    new = ed.apply(file_text, edit).split("\n")
    first = max(0, edit.line - 1 - CONTEXT_LINES)
    grown = edit.new_text.count("\n") - file_text.count("\n", edit.start, edit.end)
    last_old = min(len(old), edit.end_line + CONTEXT_LINES)
    last_new = min(len(new), last_old + grown)
    rows = []
    for line in difflib.ndiff(old[first:last_old], new[first:last_new]):
        if line[:1] in " -+":
            rows.append((line[0], line[2:]))
    return rows


class EditCard(QFrame):
    """One proposed edit."""

    accept = Signal()
    reject = Signal()
    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("EditCard")
        self.setCursor(Qt.PointingHandCursor)
        self.title = QLabel()
        self.state = QLabel()
        self.accept_button = QPushButton("Accept")
        self.accept_button.clicked.connect(self.accept)
        self.reject_button = QPushButton("Reject")
        self.reject_button.clicked.connect(self.reject)
        self.show_button = QPushButton("Show")
        self.show_button.setCheckable(True)
        self.show_button.setToolTip("The block as the model wrote it")
        self.show_button.toggled.connect(lambda on: self.diff.setVisible(on))
        header = QHBoxLayout()
        header.setSpacing(8)
        header.addWidget(self.title)
        header.addStretch(1)
        header.addWidget(self.state)
        header.addWidget(self.show_button)
        header.addWidget(self.reject_button)
        header.addWidget(self.accept_button)
        self.diff = QLabel()
        self.diff.setTextFormat(Qt.RichText)
        self.diff.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(6)
        layout.addLayout(header)
        layout.addWidget(self.diff)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)

    def show_edit(self, number: int, edit: ed.Edit, file_text: str, name: str, colors: dict,
                  muted: str, font_family: str):
        where = (f"line {edit.line}" if edit.line == edit.end_line else f"lines {edit.line}–{edit.end_line}") \
            if edit.located else "not applied"
        note = {"spacing": "spacing ignored", "indentation": "indentation adjusted",
                "line numbers": "line numbers taken out"}.get(edit.how, "")
        extra = f" <span style='color:{muted}'>· {note}</span>" if note else ""
        self.title.setText(f"<b>{number}.</b> {html.escape(name)} · {where}{extra}")
        pending = edit.status == ed.PENDING and edit.located
        self.accept_button.setVisible(pending)
        self.reject_button.setVisible(pending)
        self.show_button.setVisible(edit.status == ed.FAILED)
        state = {ed.APPLIED: "✓ applied", ed.REJECTED: "rejected",
                 ed.STALE: "⚠ the code changed since: not applied",
                 ed.FAILED: f"⚠ {edit.problem}"}.get(edit.status, "")
        self.state.setText(f"<span style='color:{muted}'>{html.escape(state)}</span>" if state else "")
        rows = []
        for kind, line in diff_rows(file_text, edit):
            text = html.escape(line) or " "
            if kind == " ":
                rows.append(f"<div style='color:{muted}'>&nbsp; {text}</div>")
            else:
                background, color = colors[kind]
                rows.append(f"<div style='background:{background}; color:{color}'>{kind} {text}</div>")
        self.diff.setText(f"<div style='font-family:\"{font_family}\"; white-space:pre'>{''.join(rows)}</div>")
        self.diff.setVisible(edit.status != ed.FAILED or self.show_button.isChecked())
        self.setEnabled(True)
        dim = edit.status in (ed.REJECTED, ed.STALE)
        self.diff.setStyleSheet("color: gray;" if dim else "")


class EditList(QWidget):
    """The cards, and Accept All / Reject All."""

    accept_requested = Signal(int)
    reject_requested = Signal(int)
    accept_all_requested = Signal()
    reject_all_requested = Signal()
    edit_clicked = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.cards: list[EditCard] = []
        self.is_light = True
        self.muted = "#8b949e"
        self.font_family = "Menlo"
        self.container = QWidget()
        self.container.setObjectName("EditCards")
        self.cards_layout = QVBoxLayout(self.container)
        self.cards_layout.setContentsMargins(0, 0, 0, 0)
        self.cards_layout.setSpacing(6)
        self.cards_layout.addStretch(1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setWidget(self.container)
        self.summary = QLabel()
        self.accept_all = QPushButton("Accept All")
        self.accept_all.setToolTip("Make every pending edit (one step to undo)")
        self.accept_all.clicked.connect(self.accept_all_requested)
        self.reject_all = QPushButton("Reject All")
        self.reject_all.clicked.connect(self.reject_all_requested)
        footer = QHBoxLayout()
        footer.addWidget(self.summary, 1)
        footer.addWidget(self.reject_all)
        footer.addWidget(self.accept_all)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addWidget(self.scroll, 1)
        layout.addLayout(footer)

    def set_palette(self, pal: dict, is_light: bool):
        self.is_light = is_light
        self.muted = pal["muted"]
        self.setStyleSheet(f"#EditCard {{ border: 1px solid {pal['line']}; border-radius: 6px; "
                           f"background: {pal['bg']}; }}"
                           f"QScrollArea, #EditCards {{ background: {pal['bg']}; border: none; }}")

    def show_edits(self, edits: list[ed.Edit], file_text: str, name: str, font_family: str):
        """Show ``edits`` (located in ``file_text``, the editor's text)."""
        self.font_family = font_family
        while len(self.cards) < len(edits):
            card = EditCard()
            i = len(self.cards)
            card.accept.connect(lambda i=i: self.accept_requested.emit(i))
            card.reject.connect(lambda i=i: self.reject_requested.emit(i))
            card.clicked.connect(lambda i=i: self.edit_clicked.emit(i))
            self.cards_layout.insertWidget(self.cards_layout.count() - 1, card)
            self.cards.append(card)
        while len(self.cards) > len(edits):
            self.cards.pop().deleteLater()
        colors = DIFF_COLORS["light" if self.is_light else "dark"]
        for i, (card, edit) in enumerate(zip(self.cards, edits, strict=True)):
            card.show_edit(i + 1, edit, file_text, name, colors, self.muted, font_family)
        pending = sum(e.status == ed.PENDING and e.located for e in edits)
        counts = {s: sum(e.status == s for e in edits) for s in (ed.APPLIED, ed.REJECTED, ed.FAILED, ed.STALE)}
        parts = [f"{len(edits)} edit{'s' * (len(edits) != 1)} proposed"]
        parts += [f"{n} {word}" for word, n in (("applied", counts[ed.APPLIED]), ("rejected", counts[ed.REJECTED]),
                                                ("not found", counts[ed.FAILED]), ("out of date", counts[ed.STALE]))
                  if n]
        self.summary.setText(f"<span style='color:{self.muted}'>{' · '.join(parts)}</span>")
        self.accept_all.setEnabled(pending > 0)
        self.accept_all.setText(f"Accept All ({pending})" if pending else "Accept All")
        self.reject_all.setEnabled(pending > 0)
        self.setVisible(bool(edits))
