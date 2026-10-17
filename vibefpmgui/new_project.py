"""File → New fpm Project…: ``fpm new`` with its options chosen in a dialog.

`new_command` makes the command, `name_problem` checks a name as fpm does;
`NewProjectDialog` asks for them, showing the command it will run. fpm
makes one of three kinds of project (its options for them can't be mixed):
*full* (``--full``, the default here: an annotated ``fpm.toml`` and every
folder), the *folders chosen* (``--lib``, ``--app``…, with a minimal
``fpm.toml``), or *bare* (``--bare``: ``fpm.toml`` and ``README.md`` only).
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from qtpy.QtCore import Qt
from qtpy.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

NO_NAME = "Give the project a name."
NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,62}$")  # (fpm's rule: up to 63, starting with a letter)
# The parts a project can be made with: (flag, what it is, its folder).
FULL, CHOSEN, BARE = "full", "chosen", "bare"  # the kinds of project
PARTS = (("--lib", "Library", "src/"), ("--app", "Application", "app/"), ("--test", "Tests", "test/"),
         ("--example", "Example", "example/"))


def name_problem(name: str, folder: Path, backfill: bool = False) -> str:
    """Why ``name`` can't be a new project in ``folder`` ("" if it can)."""
    if not name:
        return NO_NAME
    if not NAME_RE.match(name):
        return ("A name is up to 63 letters, digits, underscores or hyphens, starting with a letter "
                "(no spaces).")
    if not folder.is_dir():
        return f"There's no folder {folder}."
    target = folder / name
    if target.exists() and not backfill:
        return f"{target} already exists (Backfill adds only what's missing to it)."
    if target.exists() and not target.is_dir():
        return f"{target} is a file."
    return ""


def new_command(fpm: str, name: str, kind: str = FULL, parts: set[str] = frozenset(),
                backfill: bool = False) -> list[str]:
    """``fpm new NAME`` for a kind of project: --full, the ``parts`` chosen
    (their flags, in fpm's order), or --bare; and --backfill."""
    command = [fpm, "new", name]
    if kind == FULL:
        command.append("--full")
    elif kind == BARE:
        command.append("--bare")
    else:
        command += [flag for flag, _what, _folder in PARTS if flag in parts]
    if backfill:
        command.append("--backfill")
    return command


class NewProjectDialog(QDialog):
    """The new project's name, folder and parts; Create runs nothing itself
    (see ``command`` and ``project_folder``: the window runs fpm)."""

    def __init__(self, fpm: str, folder: Path, parent=None):
        super().__init__(parent)
        self.fpm = fpm
        self.setWindowTitle("New fpm Project")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("my_project")
        self.folder_edit = QLineEdit(str(folder))
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        where = QHBoxLayout()
        where.addWidget(self.folder_edit, 1)
        where.addWidget(browse)
        # The kind of project (fpm's options for them can't be mixed).
        self.kind_buttons: dict[str, QRadioButton] = {}
        self.kinds = QButtonGroup(self)
        kinds = QVBoxLayout()
        kinds.setSpacing(2)
        for kind, text, tip in (
                (FULL, "Full: an annotated fpm.toml, and src/, app/, test/ and example/",
                 ("--full: a fpm.toml with every setting explained (and links to the documentation), and every "
                 "folder with a placeholder Fortran file")),
                (CHOSEN, "Only the folders chosen, with a minimal fpm.toml:",
                 "--lib, --app, --test, --example: the folders ticked below"),
                (BARE, "Bare: fpm.toml and README.md only", "--bare: no folders or Fortran files")):
            button = QRadioButton(text)
            button.setToolTip(tip)
            button.toggled.connect(self._update)
            self.kinds.addButton(button)
            self.kind_buttons[kind] = button
            kinds.addWidget(button)
            if kind == CHOSEN:
                self.part_checks: dict[str, QCheckBox] = {}
                for flag, what, folder_name in PARTS:
                    check = QCheckBox(f"{what} ({folder_name})")
                    check.setChecked(flag != "--example")  # (fpm's own choice without --full)
                    check.setToolTip(f"{flag}: create {folder_name} with a placeholder Fortran file")
                    check.toggled.connect(self._update)
                    row = QHBoxLayout()
                    row.setContentsMargins(22, 0, 0, 0)
                    row.addWidget(check)
                    kinds.addLayout(row)
                    self.part_checks[flag] = check
        self.kind_buttons[FULL].setChecked(True)
        parts_box = QWidget()
        parts_box.setLayout(kinds)
        self.backfill_check = QCheckBox("Backfill an existing folder (add only what's missing)")
        self.backfill_check.setToolTip("--backfill: the folder may exist already; only the files and folders "
                                       "it doesn't have are created")
        self.backfill_check.toggled.connect(self._update)
        self.command_label = QLabel()
        self.command_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.command_label.setWordWrap(True)
        self.problem_label = QLabel()
        self.problem_label.setWordWrap(True)
        self.problem_label.setStyleSheet("color: #e03131;")
        form = QFormLayout()
        form.addRow("Name:", self.name_edit)
        form.addRow("Create in:", where)
        form.addRow("Make:", parts_box)
        form.addRow("", self.backfill_check)
        note = QLabel("<span style='color:gray'>fpm also makes it a git repository; it opens here once it's "
                      "made.</span>")
        note.setWordWrap(True)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.create_button = self.buttons.addButton("Create", QDialogButtonBox.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(self.command_label)
        layout.addWidget(self.problem_label)
        layout.addWidget(self.buttons)
        self.name_edit.textChanged.connect(self._update)
        self.folder_edit.textChanged.connect(self._update)
        self.setMinimumWidth(520)
        self._update()

    def _browse(self):
        folder = QFileDialog.getExistingDirectory(self, "Create the project in", self.folder_edit.text())
        if folder:
            self.folder_edit.setText(folder)

    def kind(self) -> str:
        return next(kind for kind, button in self.kind_buttons.items() if button.isChecked())

    def parts(self) -> set[str]:
        return {flag for flag, check in self.part_checks.items() if check.isChecked()}

    def project_folder(self) -> Path:
        return Path(self.folder_edit.text().strip()).expanduser() / self.name_edit.text().strip()

    def command(self) -> list[str]:
        return new_command(self.fpm, self.name_edit.text().strip(), self.kind(), self.parts(),
                           self.backfill_check.isChecked())

    def problem(self) -> str:
        """What's wrong (the name's first: it's the first field), or ""."""
        problem = name_problem(self.name_edit.text().strip(), Path(self.folder_edit.text().strip()).expanduser(),
                               self.backfill_check.isChecked())
        if not problem and self.kind() == CHOSEN and not self.parts():
            problem = "Choose at least one folder."
        return problem

    def _update(self, *_):
        if not hasattr(self, "create_button"):
            return  # (being built)
        for check in self.part_checks.values():
            check.setEnabled(self.kind() == CHOSEN)
        problem = self.problem()
        self.problem_label.setText("" if problem == NO_NAME else problem)  # (not before anything's typed)
        self.create_button.setEnabled(not problem)
        shown = " ".join(["fpm", *self.command()[1:]])
        self.command_label.setText(f"<span style='color:gray'>Runs, in {html.escape(self.folder_edit.text().strip())}"
                                   f":</span> <code>{html.escape(shown)}</code>")
