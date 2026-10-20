"""Preview of a Rename Symbol: which files and lines will change.

Each file has a checkbox. Files outside the project (dependencies) start
unticked: fpm re-downloads git dependencies, so edits there would be lost,
and a dependency is usually not yours to change. Each changed line is shown
as it will read, in the editor's syntax colors (as in the Search panel), the
new name highlighted.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from pygments.styles import get_style_by_name
from qtpy.QtCore import QRect, Qt
from qtpy.QtGui import QColor, QFont, QFontMetrics, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from . import highlighter as hl
from . import lsp as lsp_mod
from .references_panel import short_path
from .result_tree import MATCH_COLOR, paint_code, syntax_formats

PATH_ROLE = Qt.UserRole + 1
LINE_ROLE = Qt.UserRole + 2  # a changed line's row: (file, line (0-based), the new name's (start, end) in it)


def renamed_spans(edits: list[lsp_mod.TextEdit], line: int) -> list[tuple[int, int]]:
    """Where a line's edits put their new text, in the line as it will read
    (each edit shifts those after it on the line)."""
    spans, shift = [], 0
    for edit in sorted((e for e in edits if e.line == line == e.end_line), key=lambda e: e.column):
        start = edit.column + shift
        spans.append((start, start + len(edit.new_text)))
        shift += len(edit.new_text) - (edit.end_column - edit.column)
    return spans


class _LineDelegate(QStyledItemDelegate):
    """A changed line: its number, then the line in the editor's font and
    syntax colors, the new name highlighted (plain on the selected row)."""

    def __init__(self, dialog: RenamePreviewDialog):
        super().__init__(dialog.tree)
        self.dialog = dialog

    def paint(self, painter, option, index):
        where = index.data(LINE_ROLE)
        if where is None:
            super().paint(painter, option, index)
            return
        path, line, spans = where
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(opt.state & QStyle.State_Selected)
        group = QPalette.Active if opt.state & QStyle.State_Active else QPalette.Inactive
        plain = opt.palette.color(group, QPalette.HighlightedText if selected else QPalette.Text)
        dim = QColor(plain)
        dim.setAlpha(140)
        mono = self.dialog.mono
        full = self.dialog.lines(path)[line] if line < len(self.dialog.lines(path)) else ""
        code = full.lstrip()
        column = len(full) - len(code)
        painter.save()
        painter.setClipRect(opt.rect)
        number = f"{line + 1:>5}  "
        painter.setFont(mono)
        painter.setPen(dim)
        width = QFontMetrics(mono).horizontalAdvance(number)
        painter.drawText(QRect(opt.rect.left() + 4, opt.rect.top(), width, opt.rect.height()),
                         Qt.AlignVCenter | Qt.AlignLeft, number)
        left = opt.rect.left() + 4 + width
        metrics = QFontMetrics(mono)
        for start, end in spans:  # (the new name: highlighted, as the Search panel's matches)
            a, b = max(start - column, 0), max(end - column, 0)
            x = left + metrics.horizontalAdvance(code[:a])
            painter.fillRect(QRect(x, opt.rect.top() + 2, metrics.horizontalAdvance(code[a:b]),
                                   opt.rect.height() - 4), MATCH_COLOR)
        formats = None if selected else self.dialog.line_formats(path, line)
        paint_code(painter, QRect(left, opt.rect.top(), opt.rect.right() - left, opt.rect.height()), code,
                   formats, column, mono, plain)
        painter.restore()


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
        self.mono = mono or QFont()
        self._lines: dict[str, list[str]] = {}  # file -> its lines as they will read
        self._style = get_style_by_name(hl.style_name(QApplication.palette()))
        self._formats: dict = {}  # token type -> format
        self._line_formats: dict[tuple[str, int], list | None] = {}
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setItemDelegate(_LineDelegate(self))
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
            self._lines[str(path)] = new_lines
            for line in sorted({e.line for e in file_edits}):
                after = new_lines[line].strip() if line < len(new_lines) else ""
                before = old_lines[line].strip() if line < len(old_lines) else ""
                child = QTreeWidgetItem([f"{line + 1}:  {after}"])
                child.setToolTip(0, f"Before: {before}\nAfter:  {after}")
                child.setData(0, LINE_ROLE, (str(path), line, renamed_spans(file_edits, line)))
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

    def lines(self, path: str) -> list[str]:
        return self._lines.get(path, [])

    def line_formats(self, path: str, line: int) -> list | None:
        """The syntax format of each column of a changed line, as it will read
        (lexed with the lines before it, as the Search panel's)."""
        key = (path, line)
        if key not in self._line_formats:
            self._line_formats[key] = syntax_formats(self.lines(path), line + 1, Path(path), self._style,
                                                     self._formats)
        return self._line_formats[key]

    def selected(self) -> list[Path]:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            node = self.tree.topLevelItem(i)
            if node.checkState(0) == Qt.Checked:
                out.append(Path(node.data(0, PATH_ROLE)))
        return out
