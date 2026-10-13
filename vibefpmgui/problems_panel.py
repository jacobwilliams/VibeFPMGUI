"""The *Problems* tab: everything flagged in the file in the editor.

The last build's compiler messages, fortitude's and fortls's issues,
misspelled words, gremlins (non-ASCII and invisible characters) and the AI
review's findings, in line order, drawn like
the Search tab's results (`vibefpmgui.result_tree`). A button per tool
(with its count) shows or hides its problems. Click a problem to go there.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import Qt, Signal
from qtpy.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .result_tree import RESULT_ROLE, Problem, ResultTree

# (source, button label), in the order the buttons are shown.
SOURCES = [("build", "Build"), ("fortitude", "fortitude"), ("fortls", "fortls"),
           ("spelling", "Spelling"), ("gremlins", "Gremlins"), ("review", "AI review")]
HINT = "Open a file in the editor to see its problems here."
EXPLAINABLE = ("build", "fortitude", "fortls")  # (sources Explain with AI is offered for)


class ProblemsPanel(QWidget):
    open_requested = Signal(str, int, int, int, int)  # file, line, column, end line, end column
    explain_requested = Signal(object)  # a Problem (right-click → Explain with AI)
    fix_requested = Signal(object, object)  # its file's path and a Problem (right-click → Fix with AI…)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.path: Path | None = None
        self.problems: list[Problem] = []
        self.labels: tuple[str, str] = ("", "")  # (folder, package) shown after the file's name
        self.title = QLabel(HINT)
        self.title.setContentsMargins(8, 4, 8, 2)
        self.buttons: dict[str, QToolButton] = {}
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 6, 0)
        header.setSpacing(4)
        header.addWidget(self.title, 1)
        for source, label in SOURCES:
            button = QToolButton()
            button.setCheckable(True)
            button.setAutoRaise(True)
            shown = settings.value(f"problems/{source}", True, type=bool) if settings is not None else True
            button.setChecked(shown)
            button.setToolTip(f"Show or hide {label}'s problems")
            button.toggled.connect(self._filter_changed)
            button.setProperty("label", label)
            self.buttons[source] = button
            header.addWidget(button)
        self.tree = ResultTree()
        self.tree.set_wrapping(True)  # (long messages wrap: an AI review's can be a few sentences)
        self.tree.open_requested.connect(self.open_requested)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(header)
        layout.addWidget(self.tree, 1)
        self.show_problems(None, [])

    def _context_menu(self, pos):
        menu = self.context_menu(pos)
        if menu is not None:
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            menu.deleteLater()

    def context_menu(self, pos) -> QMenu | None:
        """A problem's right-click menu (None: not on a problem)."""
        item = self.tree.itemAt(pos)
        data = item.data(0, RESULT_ROLE) if item is not None else None
        path, problem = data if data else (None, None)
        if not isinstance(problem, Problem):
            return None
        menu = QMenu(self)
        explain = menu.addAction("Explain with AI", lambda: self.explain_requested.emit(problem))
        explain.setEnabled(problem.source in EXPLAINABLE)
        explain.setToolTip("Ask the local AI model what this means here, and how to fix it")
        fix = menu.addAction("Fix with AI…", lambda: self.fix_requested.emit(path, problem))
        fix.setEnabled(problem.source in (*EXPLAINABLE, "review"))
        fix.setToolTip("Ask the local AI model for an edit that addresses this (you accept or reject "
                       "each change)")
        menu.addAction("Copy Message", lambda: QApplication.clipboard().setText(problem.message))
        menu.setToolTipsVisible(True)
        return menu

    def _filter_changed(self):
        if self.settings is not None:
            for source, button in self.buttons.items():
                self.settings.setValue(f"problems/{source}", button.isChecked())
        self._refresh()

    def show_problems(self, path: Path | None, problems: list[Problem], folder: str = "", package: str = ""):
        """The file in the editor and its problems (``path`` None: no file)."""
        self.path, self.problems, self.labels = path, list(problems), (folder, package)
        self._refresh()

    def shown(self) -> list[Problem]:
        """The problems whose tool's button is on."""
        return [p for p in self.problems if self.buttons[p.source].isChecked()]

    def _refresh(self):
        counts = {source: 0 for source in self.buttons}
        for p in self.problems:
            counts[p.source] += 1
        for source, button in self.buttons.items():
            button.setText(f"{button.property('label')} {counts[source]}")
            button.setVisible(counts[source] > 0 or not button.isChecked())  # (a hidden kind stays findable)
        if self.path is None:
            self.tree.clear_results()
            self.title.setText(HINT)
            return
        shown = self.shown()
        self.tree.set_problems(self.path, shown, *self.labels)
        total = len(self.problems)
        if not total:
            self.title.setText(f"No problems in <b>{self.path.name}</b>")
        else:
            hidden = total - len(shown)
            self.title.setText(f"<b>{total}</b> problem{'s' * (total != 1)}"
                               + (f" ({hidden} hidden)" if hidden else ""))
