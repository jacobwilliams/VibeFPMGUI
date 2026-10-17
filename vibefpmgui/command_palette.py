"""View → Command Palette… (⌘⇧P / Ctrl+Shift+P): every command in the menus,
found by typing part of its name, as in VS Code.

The commands aren't listed here: they're the menu bar's actions, read when
the palette opens (`collect`), so a command added to a menu is in the
palette too. A menu that fills itself when it opens (Syntax Colors, Window
Style, Recent Projects…) is asked to, as it is when it's opened. Running a
command triggers its action, as choosing it in its menu does.
"""

from __future__ import annotations

import re
from typing import NamedTuple

from qtpy.QtCore import QEvent, QRect, QSize, Qt, QTimer
from qtpy.QtGui import QAction, QColor, QFontMetrics, QKeySequence, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QFrame,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMenuBar,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

SEPARATOR = " › "
MAX_RECENT = 12
COMMAND_ROLE = Qt.UserRole + 1  # an item's Command


class Command(NamedTuple):
    path: tuple[str, ...]  # its menus' titles, then its own ("View", "Theme", "Nord")
    action: QAction

    @property
    def name(self) -> str:
        return self.path[-1]

    @property
    def title(self) -> str:
        return SEPARATOR.join(self.path)


def clean(text: str) -> str:
    """A menu text as shown: no "&" for its mnemonic ("&&" is an "&"), no
    tab and what's after it (a shortcut some menus put in their text)."""
    text = text.split("\t", 1)[0]
    return re.sub(r"&(.)", r"\1", text).strip()


def collect(menu_bar: QMenuBar) -> list[Command]:
    """Every command in the menu bar's menus, in menu order: not separators,
    submenus themselves or hidden actions.

    A submenu is found from its menu (``QMenu.menuAction()``), never with
    ``QAction.menu()``: in PySide that leaves a menu the menu bar made (and
    its actions) deleted as far as Python knows, which broke the app's own
    menus."""
    commands: list[Command] = []
    seen: set[int] = set()
    submenus = {m.menuAction(): m for m in menu_bar.window().findChildren(QMenu)}

    def walk(menu: QMenu, path: tuple[str, ...]):
        menu.aboutToShow.emit()  # (a menu filled when it opens: filled now, as then)
        submenus.update({m.menuAction(): m for m in menu.findChildren(QMenu)})  # (made by filling it)
        for action in menu.actions():
            if action.isSeparator() or not action.isVisible() or not clean(action.text()):
                continue
            here = (*path, clean(action.text()))
            submenu = submenus.get(action)
            if submenu is not None:
                walk(submenu, here)
            elif id(action) not in seen:
                seen.add(id(action))
                commands.append(Command(here, action))

    for action in menu_bar.actions():
        submenu = submenus.get(action)
        if submenu is not None and action.isVisible():
            walk(submenu, (clean(action.text()),))
    return commands


def _word_starts(text: str) -> set[int]:
    return {0} | {i for i in range(1, len(text)) if not text[i - 1].isalnum() and text[i].isalnum()}


def _subsequence(word: str, text: str, starts: set[int], prefer: bool = True) -> int | None:
    """How well ``word``'s letters appear, in order, in ``text`` (None: they
    don't): each letter counts, more at a word's start or right after the
    one before. Each letter goes at the next word's start that has it, if
    any (``prefer``); if that leaves a later letter unmatched, at the next
    place it is."""
    score, at, last, first = 0, 0, -2, None
    for ch in word:
        found = text.find(ch, at)
        if found < 0:
            return None if prefer is False else _subsequence(word, text, starts, prefer=False)
        if prefer and found != last + 1 and found not in starts:
            later = found
            while later >= 0 and later not in starts:
                later = text.find(ch, later + 1)
            if later >= 0:
                found = later
        gap = found - last - 1 if last >= 0 else 0
        score += 1 + (3 if found in starts else 0) + (2 if found == last + 1 else 0) - min(gap, 4)
        first = found if first is None else first
        last, at = found, found + 1
    if first is not None and last - first + 1 > max(3 * len(word), len(word) + 8):
        return None  # (its letters scattered far apart: not what was meant)
    return score


def score(query: str, command: Command) -> int | None:
    """How well a command matches what's typed (None: it doesn't). Each word
    typed must match, letters in order (so "mvup" finds Move Line Up); in
    the command's own name counts more than in its menus' names."""
    words = query.lower().split()
    if not words:
        return 0
    name, title = command.name.lower(), command.title.lower()
    name_starts, title_starts = _word_starts(name), _word_starts(title)
    total = 0
    for word in words:
        in_name = _subsequence(word, name, name_starts)
        in_title = _subsequence(word, title, title_starts)
        if in_name is None and in_title is None:
            return None
        total += max(in_name * 2 if in_name is not None else 0, in_title or 0)
        if name.startswith(word):
            total += 5
    if name.rstrip("…") == " ".join(words):
        total += 10  # (just its name: it before others that start the same)
    return total


def ranked(query: str, commands: list[Command], recent: list[str]) -> list[Command]:
    """The commands matching ``query``, best first: the best match, then the
    most recently used, then those available now (the others are listed,
    greyed), then the fewest menus deep, then the shortest name, then menu
    order. Nothing typed: the
    recently used first, then the menus' order."""
    order = {title: n for n, title in enumerate(recent)}
    matched = []
    for index, command in enumerate(commands):
        points = score(query, command)
        if points is not None:
            matched.append((-points, order.get(command.title, len(recent)), not command.action.isEnabled(),
                            len(command.path), len(command.name), index, command))
    matched.sort(key=lambda m: m[:6] if query.strip() else (m[1], m[2], m[5]))
    return [m[-1] for m in matched]


class _ItemDelegate(QStyledItemDelegate):
    """A command: its menus' names muted, then its own; a tick if it's
    checked; its shortcut on the right."""

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), QFontMetrics(option.font).height() + 10)

    def paint(self, painter, option, index):
        command: Command = index.data(COMMAND_ROLE)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)
        selected = bool(option.state & QStyle.State_Selected)
        pal = option.palette
        text = pal.color(QPalette.HighlightedText if selected else QPalette.Text)
        muted = QColor(text)
        muted.setAlpha(150)
        if not command.action.isEnabled():
            text.setAlpha(110)
            muted.setAlpha(80)
        fm = QFontMetrics(option.font)
        rect = option.rect.adjusted(10, 0, -10, 0)
        painter.save()
        shortcut = command.action.shortcut().toString(QKeySequence.NativeText)
        if shortcut:
            painter.setPen(muted)
            painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, shortcut)
            rect.setRight(rect.right() - fm.horizontalAdvance(shortcut) - 16)
        x = rect.left()
        tick = "✓ " if command.action.isCheckable() and command.action.isChecked() else ""
        menus = SEPARATOR.join(command.path[:-1]) + SEPARATOR if len(command.path) > 1 else ""
        for part, color in ((tick, text), (menus, muted), (command.name, text)):
            if not part:
                continue
            painter.setPen(color)
            width = rect.right() - x
            shown = fm.elidedText(part, Qt.ElideMiddle, max(0, width))
            painter.drawText(QRect(x, rect.top(), max(0, width), rect.height()), Qt.AlignLeft | Qt.AlignVCenter,
                             shown)
            x += fm.horizontalAdvance(shown)
        painter.restore()


class CommandPalette(QFrame):
    """The palette: a field to type in, the matching commands under it.
    ``recent`` (the titles of the commands last run, latest first) is kept
    up to date in place; ``recent_changed`` (if set) is called after."""

    WIDTH = 620
    ROWS = 14

    def __init__(self, window: QWidget, recent: list[str] | None = None):
        super().__init__(window, Qt.Popup)
        self.window_ = window
        self.recent = recent if recent is not None else []
        self.recent_changed = None
        self.commands: list[Command] = []
        self.setFrameShape(QFrame.StyledPanel)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Type a command (letters in order: \"mvup\" finds Move Line Up)")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(self._filter)
        self.field.installEventFilter(self)
        self.list = QListWidget()
        self.list.setItemDelegate(_ItemDelegate(self.list))
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.itemClicked.connect(lambda item: self.run(item.data(COMMAND_ROLE)))
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self.field)
        layout.addWidget(self.list)

    def open(self):
        """Read the menus, and show the palette at the top of the window."""
        menu_bar = self.window_.menuBar() if hasattr(self.window_, "menuBar") else None
        self.commands = collect(menu_bar) if menu_bar is not None else []
        self.field.clear()
        self._filter("")
        width = min(self.WIDTH, max(300, self.window_.width() - 40))
        row = self.list.sizeHintForRow(0) if self.list.count() else 24
        self.resize(width, self.field.sizeHint().height() + row * self.ROWS + 24)
        top = self.window_.mapToGlobal(self.window_.rect().topLeft())
        self.move(top.x() + (self.window_.width() - width) // 2, top.y() + 40)
        self.show()
        self.raise_()
        self.field.setFocus()

    def _filter(self, text: str):
        self.list.clear()
        for command in ranked(text, self.commands, self.recent):
            item = QListWidgetItem(command.title)
            item.setData(COMMAND_ROLE, command)
            if not command.action.isEnabled():
                item.setToolTip("Not available now")
            elif command.action.statusTip():
                item.setToolTip(command.action.statusTip())
            self.list.addItem(item)
        if self.list.count():
            self.list.setCurrentRow(0)

    def eventFilter(self, obj, event):
        if obj is self.field and event.type() == QEvent.KeyPress:
            key = event.key()
            steps = {Qt.Key_Up: -1, Qt.Key_Down: 1, Qt.Key_PageUp: -self.ROWS, Qt.Key_PageDown: self.ROWS}
            if key in steps and self.list.count():
                row = max(0, min(self.list.count() - 1, self.list.currentRow() + steps[key]))
                self.list.setCurrentRow(row)
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                item = self.list.currentItem()
                if item is not None:
                    self.run(item.data(COMMAND_ROLE))
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)

    def run(self, command: Command):
        """Run a command (if it's available now): the palette closes first, so
        the command acts where the focus was (e.g. the editor)."""
        if not command.action.isEnabled():
            return
        title = command.title
        if title in self.recent:
            self.recent.remove(title)
        self.recent.insert(0, title)
        del self.recent[MAX_RECENT:]
        if self.recent_changed is not None:
            self.recent_changed(self.recent)
        self.close()
        QTimer.singleShot(0, self.window_, command.action.trigger)

    def current_titles(self) -> list[str]:
        """(The titles listed, in order: for the tests.)"""
        return [self.list.item(i).data(COMMAND_ROLE).title for i in range(self.list.count())]

