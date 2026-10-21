"""Go to File (⌘P / Ctrl+P): any file of the project, or of its dependencies,
found by typing part of its name, opened in the editor.

The files are those the Project files tree lists (`ProjectTree.all_files`):
the ones ignored by git and the greyed-out ones only with its *Ignored*
ticked. What's typed matches the file's name first (its start, then
anywhere, then its letters in order: "dstat" finds demo_stats.f90), then
its path ("src/dem"); the project's own files before its dependencies',
then the shortest paths. Nothing typed: the files open in the editor first.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath

from qtpy.QtCore import QEvent, QRect, QSize, Qt
from qtpy.QtGui import QFont, QFontMetrics, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from .project_tree import ProjectFile

FILE_ROLE = Qt.UserRole + 1
MAX_SHOWN = 300  # files listed at once (more: type more of the name)


def _in_order(letters: str, text: str) -> bool:
    """Whether ``letters`` appear in ``text`` in that order (not together)."""
    found = 0
    for ch in text:
        if found < len(letters) and ch == letters[found]:
            found += 1
    return found == len(letters)


def score(query: str, item: ProjectFile) -> int | None:
    """How well a file matches what's typed (None: not at all)."""
    typed = "".join(query.lower().split())
    name, path = PurePosixPath(item.relative).name.lower(), item.relative.lower()
    if name.startswith(typed):
        return 1000
    if typed in name:
        return 800
    if _in_order(typed, name):
        return 600
    if typed in path:
        return 400
    if _in_order(typed, path):
        return 200
    return None


def ranked(query: str, files: list[ProjectFile], open_paths: list[str] | None = None) -> list[ProjectFile]:
    """The files matching ``query``, best first (see the module's description)."""
    open_order = {path: n for n, path in enumerate(open_paths or [])}
    if not query.strip():
        return sorted(files, key=lambda f: (open_order.get(f.path, len(open_order)), f.dependency, f.package,
                                            f.relative.lower()))
    matched = []
    for item in files:
        points = score(query, item)
        if points is not None:
            matched.append((-points, item.dependency, open_order.get(item.path, len(open_order)),
                            len(item.relative), item.relative.lower(), item))
    matched.sort(key=lambda m: m[:5])
    return [m[-1] for m in matched]


class _FileDelegate(QStyledItemDelegate):
    """A file: its icon, name (bold), folder (muted) and, for a dependency's,
    the package."""

    def __init__(self, palette: QuickOpen):
        super().__init__(palette.list)
        self.palette_ = palette

    def sizeHint(self, option, index):
        return QSize(option.rect.width(), QFontMetrics(option.font).height() + 10)

    def paint(self, painter, option, index):
        item: ProjectFile = index.data(FILE_ROLE)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)
        selected = bool(option.state & QStyle.State_Selected)
        pal = option.palette
        text = pal.color(QPalette.HighlightedText if selected else QPalette.Text)
        muted = pal.color(QPalette.HighlightedText if selected else QPalette.PlaceholderText)
        rect = option.rect.adjusted(8, 0, -8, 0)
        painter.save()
        icon = self.palette_.icon_for(Path(item.path)) if self.palette_.icon_for else None
        size = QFontMetrics(option.font).height()
        x = rect.left()
        if icon is not None and not icon.isNull():
            icon.paint(painter, QRect(x, rect.top() + (rect.height() - size) // 2, size, size))
            x += size + 6
        bold = QFont(option.font)
        bold.setBold(True)
        name = PurePosixPath(item.relative).name
        folder = str(PurePosixPath(item.relative).parent)
        parts = [(name, bold, text), (f"  {folder}" if folder != "." else "", option.font, muted)]
        if item.dependency:
            parts.append((f"  · {item.package}", option.font, muted))
        if item.hidden:
            parts.append(("  · ignored", option.font, muted))
        for part, font, color in parts:
            if not part or x >= rect.right():
                continue
            painter.setFont(font)
            painter.setPen(color)
            metrics = QFontMetrics(font)
            shown = metrics.elidedText(part, Qt.ElideMiddle, rect.right() - x)
            painter.drawText(QRect(x, rect.top(), rect.right() - x, rect.height()), Qt.AlignVCenter | Qt.AlignLeft,
                             shown)
            x += metrics.horizontalAdvance(shown)
        painter.restore()


class QuickOpen(QFrame):
    """The palette: a field to type in, the matching files under it (as the
    Command Palette). ``opened`` is called with the file chosen."""

    WIDTH = 660
    ROWS = 14

    def __init__(self, window: QWidget, opened, icon_for=None):
        super().__init__(window, Qt.Popup)
        self.window_ = window
        self.opened = opened
        self.icon_for = icon_for
        self.files: list[ProjectFile] = []
        self.open_paths: list[str] = []
        self.setFrameShape(QFrame.StyledPanel)
        self.field = QLineEdit()
        self.field.setPlaceholderText("Type part of a file's name (letters in order: \"dstat\" finds demo_stats.f90)")
        self.field.setClearButtonEnabled(True)
        self.field.textChanged.connect(self._filter)
        self.field.installEventFilter(self)
        self.list = QListWidget()
        self.list.setItemDelegate(_FileDelegate(self))
        self.list.setUniformItemSizes(True)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.itemClicked.connect(lambda item: self.choose(item.data(FILE_ROLE)))
        self.note = QLabel()  # (whether ignored files are in the list, and how many more there are)
        self.note.setStyleSheet("color: gray;")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(6)
        layout.addWidget(self.field)
        layout.addWidget(self.list)
        layout.addWidget(self.note)
        self.include_ignored = False

    def open(self, files: list[ProjectFile], open_paths: list[str], include_ignored: bool):
        """Show the palette at the top of the window, for these files."""
        self.files, self.open_paths, self.include_ignored = files, open_paths, include_ignored
        self.field.clear()
        self._filter("")
        width = min(self.WIDTH, max(320, self.window_.width() - 40))
        row = self.list.sizeHintForRow(0) if self.list.count() else 24
        self.resize(width, self.field.sizeHint().height() + row * self.ROWS + self.note.sizeHint().height() + 30)
        top = self.window_.mapToGlobal(self.window_.rect().topLeft())
        self.move(top.x() + (self.window_.width() - width) // 2, top.y() + 40)
        self.show()
        self.raise_()
        self.field.setFocus()

    def _filter(self, text: str):
        self.list.clear()
        found = ranked(text, self.files, self.open_paths)
        for item in found[:MAX_SHOWN]:
            row = QListWidgetItem(item.relative)
            row.setData(FILE_ROLE, item)
            row.setToolTip(item.path)
            self.list.addItem(row)
        if self.list.count():
            self.list.setCurrentRow(0)
        more = f"{len(found) - MAX_SHOWN} more: type more of the name. " if len(found) > MAX_SHOWN else ""
        which = ("Files ignored by git are included" if self.include_ignored
                 else "Files ignored by git are left out")
        self.note.setText(f"{more}{which} (Project files → Ignored). {len(self.files)} files.")

    def choose(self, item: ProjectFile):
        self.close()
        self.opened(item.path)

    def eventFilter(self, obj, event):
        if obj is self.field and event.type() == QEvent.KeyPress:
            key = event.key()
            steps = {Qt.Key_Up: -1, Qt.Key_Down: 1, Qt.Key_PageUp: -self.ROWS, Qt.Key_PageDown: self.ROWS}
            if key in steps and self.list.count():
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, self.list.currentRow() + steps[key])))
                return True
            if key in (Qt.Key_Return, Qt.Key_Enter):
                item = self.list.currentItem()
                if item is not None:
                    self.choose(item.data(FILE_ROLE))
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)
