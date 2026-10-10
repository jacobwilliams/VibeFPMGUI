"""The "References" tab: every use of a name, from Find References.

Results are grouped by file (project files first, then dependencies), each
with its line of code; click one to open the file there.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from qtpy.QtCore import Qt, Signal
from qtpy.QtGui import QFont
from qtpy.QtWidgets import QLabel, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

LOCATION_ROLE = Qt.UserRole + 1


def short_path(path: Path, project_dir: Path | None) -> str:
    if project_dir is not None:
        try:
            return str(path.relative_to(project_dir.resolve()))
        except ValueError:
            pass
    return str(path)


class ReferencesPanel(QWidget):
    open_requested = Signal(str, int, int)  # file, line (1-based), column (0-based)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel("Use Find References (⇧F12) on a name in the editor to list its uses here.")
        self.title.setWordWrap(True)
        self.title.setContentsMargins(8, 6, 8, 2)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setRootIsDecorated(True)
        self.tree.itemClicked.connect(self._open)
        self.tree.itemActivated.connect(self._open)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.tree, 1)

    def show_results(self, name: str, results: list[tuple[Path, int, int, int]],
                     line_text: Callable[[Path, int], str], project_dir: Path | None,
                     mono: QFont | None = None):
        """``results``: (file, 1-based line, start column, end column);
        ``line_text(file, line)`` gives a line of a file."""
        self.tree.clear()
        files: dict[Path, list] = {}
        for r in results:
            files.setdefault(r[0], []).append(r)
        if not results:
            self.title.setText(f"No references to <b>{name}</b> found.")
            return
        self.title.setText(f"<b>{name}</b>: {len(results)} reference{'s' * (len(results) != 1)} "
                           f"in {len(files)} file{'s' * (len(files) != 1)}")

        def outside(path: Path) -> bool:  # dependencies after the project's own files
            return project_dir is None or project_dir.resolve() not in path.parents or \
                "dependencies" in path.relative_to(project_dir.resolve()).parts[:2]

        for path in sorted(files, key=lambda p: (outside(p), short_path(p, project_dir))):
            refs = files[path]
            node = QTreeWidgetItem([f"{short_path(path, project_dir)}  ({len(refs)})"])
            node.setToolTip(0, str(path))
            font = QFont(node.font(0))
            font.setBold(True)
            node.setFont(0, font)
            node.setData(0, LOCATION_ROLE, (str(path), refs[0][1], refs[0][2]))
            self.tree.addTopLevelItem(node)
            for _path, line, column, _end in refs:
                text = line_text(path, line).strip()
                child = QTreeWidgetItem([f"{line}:  {text}"])
                if mono is not None:
                    child.setFont(0, mono)
                child.setToolTip(0, f"{path}:{line}")
                child.setData(0, LOCATION_ROLE, (str(path), line, column))
                node.addChild(child)
            node.setExpanded(True)

    def _open(self, item: QTreeWidgetItem, _column: int = 0):
        location = item.data(0, LOCATION_ROLE)
        if location:
            self.open_requested.emit(*location)
