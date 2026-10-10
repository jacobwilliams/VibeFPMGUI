"""Preview of a Rename Symbol: which files and lines will change.

Each file has a checkbox. Files outside the project (dependencies) start
unticked: fpm re-downloads git dependencies, so edits there would be lost,
and a dependency is usually not yours to change.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy.QtCore import Qt
from qtpy.QtGui import QFont
from qtpy.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import lsp as lsp_mod
from .references_panel import short_path

PATH_ROLE = Qt.UserRole + 1


def is_dependency(path: Path, project_dir: Path | None) -> bool:
    if project_dir is None:
        return False
    root = project_dir.resolve()
    if root not in path.parents:
        return True
    return "dependencies" in path.relative_to(root).parts[:2]  # build/dependencies/...


class RenamePreviewDialog(QDialog):
    def __init__(self, old: str, new: str, edits: dict[Path, list[lsp_mod.TextEdit]],
                 file_text: Callable[[Path], str], open_files: set[Path], project_dir: Path | None,
                 mono: QFont | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Rename Symbol")
        self.resize(760, 480)
        count = sum(len(e) for e in edits.values())
        intro = QLabel(f"Rename <b>{old}</b> to <b>{new}</b>: {count} change{'s' * (count != 1)} in "
                       f"{len(edits)} file{'s' * (len(edits) != 1)}. Files open in the editor are changed "
                       "there (unsaved, Undo reverts); other files are saved straight away.")
        intro.setWordWrap(True)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        deps = False
        for path in sorted(edits, key=lambda p: (is_dependency(p, project_dir), short_path(p, project_dir))):
            file_edits = edits[path]
            dependency = is_dependency(path, project_dir)
            deps |= dependency
            notes = []
            if path in open_files:
                notes.append("open in the editor")
            if dependency:
                notes.append("dependency — not changed unless ticked")
            note = f"  — {', '.join(notes)}" if notes else ""
            node = QTreeWidgetItem([f"{short_path(path, project_dir)}  ({len(file_edits)}){note}"])
            node.setFlags(node.flags() | Qt.ItemIsUserCheckable)
            node.setCheckState(0, Qt.Unchecked if dependency else Qt.Checked)
            node.setData(0, PATH_ROLE, str(path))
            node.setToolTip(0, str(path))
            self.tree.addTopLevelItem(node)
            # The changed lines, as they will read afterwards.
            try:
                old_lines = file_text(path).split("\n")
            except OSError:
                old_lines = []
            new_lines = lsp_mod.apply_edits("\n".join(old_lines), file_edits).split("\n")
            for line in sorted({e.line for e in file_edits}):
                after = new_lines[line].strip() if line < len(new_lines) else ""
                before = old_lines[line].strip() if line < len(old_lines) else ""
                child = QTreeWidgetItem([f"{line + 1}:  {after}"])
                child.setToolTip(0, f"Before: {before}\nAfter:  {after}")
                if mono is not None:
                    child.setFont(0, mono)
                child.setFlags(child.flags() & ~Qt.ItemIsUserCheckable)
                node.addChild(child)
            node.setExpanded(len(edits) <= 6)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Rename")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addWidget(self.tree, 1)
        if deps:
            warn = QLabel("Files in dependencies are left unticked: fpm may download them again "
                          "(losing the change), and the rest of that package wouldn't be renamed.")
            warn.setWordWrap(True)
            warn.setStyleSheet("color: gray;")
            layout.addWidget(warn)
        layout.addWidget(buttons)

    def selected(self) -> list[Path]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            if node.checkState(0) == Qt.Checked:
                out.append(Path(node.data(0, PATH_ROLE)))
        return out
