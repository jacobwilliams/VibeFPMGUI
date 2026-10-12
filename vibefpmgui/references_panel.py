"""The "References" tab: every use of a name, from Find References.

Results are grouped by file (project files first, then dependencies), shown
like the *Search* tab's (`vibefpmgui.result_tree`): each file with its icon,
each use with its line in the editor's syntax colors and the name
highlighted. Click one to open the file there.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import Signal
from qtpy.QtWidgets import QLabel, QVBoxLayout, QWidget

from .result_tree import ResultTree, Root, file_labels, make_match, root_for


def short_path(path: Path, project_dir: Path | None) -> str:
    if project_dir is not None:
        try:
            return str(path.relative_to(project_dir.resolve()))
        except ValueError:
            pass
    return str(path)


class ReferencesPanel(QWidget):
    open_requested = Signal(str, int, int, int, int)  # file, line, column, end line, end column
    HINT = "Use Find References (⇧F12) on a name in the editor to list its uses here."

    def __init__(self, parent=None):
        super().__init__(parent)
        self.title = QLabel(self.HINT)
        self.title.setWordWrap(True)
        self.title.setContentsMargins(8, 6, 8, 2)
        self.tree = ResultTree()
        self.tree.open_requested.connect(self.open_requested)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(self.title)
        layout.addWidget(self.tree, 1)

    def reset(self):
        """Another project was opened: no results."""
        self.tree.clear_results()
        self.title.setText(self.HINT)

    def show_results(self, name: str, results: list[tuple[Path, int, int, int]], roots: list[Root],
                     project_dir: Path | None):
        """``results``: (file, 1-based line, start column, end column);
        ``roots``: the packages, to label the dependencies' files."""
        self.tree.clear_results()
        files: dict[Path, list[tuple[int, int, int]]] = {}
        for path, line, start, end in results:
            files.setdefault(Path(path).resolve(), []).append((line, start, end))
        if not results:
            self.title.setText(f"No references to <b>{name}</b> found.")
            return
        self.title.setText(f"<b>{name}</b>: {len(results)} reference{'s' * (len(results) != 1)} "
                           f"in {len(files)} file{'s' * (len(files) != 1)}")
        rows = []
        for path, places in files.items():
            root = root_for(path, roots)
            folder, package = file_labels(path, root, project_dir)
            lines = self.tree.file_lines(path)
            matches = [make_match(lines[line - 1] if 0 < line <= len(lines) else "", line, start, line, end)
                       for line, start, end in sorted(places)]
            outside = root is None or root.dependency  # (dependencies after the project's own files)
            rows.append(((outside, package, folder, path.name), path, matches, folder, package))
        for _key, path, matches, folder, package in sorted(rows, key=lambda r: r[0]):
            self.tree.set_file(path, matches, folder, package)
