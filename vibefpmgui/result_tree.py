"""A list of places in files, grouped by file: the *Search*, *References*
and *Problems* tabs.

Each file has a row with its icon, name, folder, package (for dependencies)
and number of results; under it, a row per result with the line number and
the line, in the colors the editor would give it, with the result's part
highlighted (or, while replacing, struck through, with the replacement
after it); for a problem, its line number, a colored badge (the rule or
kind) and the message. Click a row to open the file there.
"""

from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from pygments import lex
from pygments.styles import get_style_by_name
from qtpy.QtCore import QRect, QSize, Qt, Signal
from qtpy.QtGui import QColor, QFont, QFontMetrics, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTreeWidget,
    QTreeWidgetItem,
)

from . import highlighter as hl

MAX_FILE_SIZE = 8 * 1024 * 1024
PREVIEW_BEFORE = 50   # characters of the line shown before a result
PREVIEW_AFTER = 200
CONTEXT_LINES = 40    # lexed before a result's line, for its syntax colors

PARTS_ROLE = Qt.UserRole + 1    # how a row is drawn (see _ResultDelegate)
RESULT_ROLE = Qt.UserRole + 2   # (path, Match or None)

MATCH_COLOR = QColor(242, 201, 76, 110)
REMOVED_COLOR = QColor(255, 107, 107, 90)
ADDED_COLOR = QColor(64, 192, 87, 100)


class Root(NamedTuple):
    """A package folder (the project's, or a dependency's)."""
    name: str
    directory: Path
    dependency: bool


class Match(NamedTuple):
    """A place in a file, with the text shown for it."""
    line: int        # 1-based
    column: int      # 0-based
    end_line: int
    end_column: int
    before: str      # the line's text around the match, for the results list
    text: str        # (the matched part of the first line)
    after: str
    start: int = 0   # the column ``before`` starts at (after any "…")


class Problem(NamedTuple):
    """An issue in a file (a compiler message, fortitude, fortls, spelling,
    AI review), for the Problems tab."""
    line: int        # 1-based
    column: int      # 0-based
    end_line: int
    end_column: int
    severity: str    # e.g. "error", "lint", "spelling" (see editor.MARKER_COLORS)
    badge: str       # shown in the colored badge: the rule code or the kind
    message: str
    color: str       # the badge's color
    source: str      # which tool: "build", "fortitude", "fortls", "spelling", "review"


def make_match(line_text: str, line: int, column: int, end_line: int, end_column: int) -> Match:
    """A Match for a place on a line (the preview is cut from the line)."""
    end = end_column if end_line == line else len(line_text)
    before = line_text[:column].lstrip()
    first = column - len(before)
    if len(before) > PREVIEW_BEFORE:
        before, first = "…" + before[-PREVIEW_BEFORE:], column - PREVIEW_BEFORE
    return Match(line, column, end_line, end_column, before, line_text[column:end][:PREVIEW_AFTER],
                 line_text[end:][:PREVIEW_AFTER], first)


def read_text(path: Path) -> str | None:
    """A file's text ("\\n" line ends), or None for binary or huge files."""
    try:
        if path.stat().st_size > MAX_FILE_SIZE:
            return None
        raw = path.read_bytes()
    except OSError:
        return None
    if b"\0" in raw[:8192]:
        return None
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    return text.replace("\r\n", "\n")


def root_for(path: Path, roots: list[Root]) -> Root | None:
    """The package a file is in (the innermost, for a package inside another)."""
    best, best_len = None, -1
    for root in roots:
        top = root.directory.resolve()
        if top in path.parents and len(str(top)) > best_len:
            best, best_len = root, len(str(top))
    return best


def file_labels(path: Path, root: Root | None, project_dir: Path | None) -> tuple[str, str]:
    """(folder, package) shown after a file's name: the project's files by
    their folder in the project, a dependency's by its folder in the
    dependency, with "· name" for the package."""
    base = root.directory.resolve() if root is not None else None
    if root is not None and not root.dependency and project_dir is not None:
        base = project_dir.resolve()
    folder = str(path.parent)
    if base is not None:
        try:
            folder = path.parent.relative_to(base).as_posix()
        except ValueError:
            pass
    folder = "" if folder == "." else folder
    package = f"· {root.name}" if root is not None and root.dependency else ""
    return folder, package


def _badge_font(font: QFont) -> QFont:
    """The small bold font of a problem's badge."""
    small = QFont(font)
    small.setPointSizeF(max(6.0, small.pointSizeF() - 2))
    small.setBold(True)
    return small


class _ResultDelegate(QStyledItemDelegate):
    """Draws a file row (icon, name, folder, package, count) or a result
    row (line number and the line, with the result highlighted, or the
    replacement shown when replacing)."""

    def __init__(self, tree: "ResultTree"):
        super().__init__(tree)
        self.tree = tree

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        line_height = QFontMetrics(self.tree.mono).height() + 6
        parts = index.data(PARTS_ROLE)
        if parts is not None and parts[0] == "problem":  # (the message wraps: as tall as it needs)
            # Where the row starts: indented one step per level (plus one for the root's arrows).
            depth, parent = 0, index.parent()
            while parent.isValid():
                depth, parent = depth + 1, parent.parent()
            left = self.tree.indentation() * (depth + int(self.tree.rootIsDecorated()))
            width = max(80, self.tree.viewport().width() - left - 2)
            _badge, message = self._problem_layout(option.font, parts[2], QRect(0, 0, width, line_height))
            height = QFontMetrics(option.font).boundingRect(
                message, Qt.TextWordWrap | Qt.AlignLeft, parts[2].message).height()
            return QSize(0, max(line_height, height + 6))  # (no width of its own: it wraps to the view's)
        return QSize(size.width(), max(size.height(), line_height))

    def _problem_layout(self, font: QFont, problem, rect: QRect) -> tuple[QRect, QRect]:
        """Where a problem row's badge goes (beside its line number, on the
        first line) and the rectangle its message wraps in."""
        number = QFontMetrics(self.tree.mono).horizontalAdvance(f"{problem.line:>5}  ")
        width = QFontMetrics(_badge_font(font)).horizontalAdvance(problem.badge) + 10
        first = QFontMetrics(self.tree.mono).height() + 6  # (the first line's height)
        badge = QRect(rect.left() + 4 + number, rect.top() + 3, width, first - 6)
        message = QRect(badge.right() + 9, rect.top() + 3, max(20, rect.right() - badge.right() - 13),
                        max(first - 6, rect.height() - 6))
        return badge, message

    def paint(self, painter, option, index):
        parts = index.data(PARTS_ROLE)
        if parts is None:
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        selected = bool(opt.state & QStyle.State_Selected)
        group = QPalette.Active if opt.state & QStyle.State_Active else QPalette.Inactive
        color = opt.palette.color(group, QPalette.HighlightedText if selected else QPalette.Text)
        dim = QColor(color)
        dim.setAlpha(140)
        rect = opt.rect
        x = rect.left() + 4
        painter.save()
        painter.setClipRect(rect)

        def draw(text, font, pen, background=None, strike=False, formats=None, column=0):
            """Draw text from x on; ``formats``: the syntax colors of the
            line's columns (``text`` starts at ``column``)."""
            nonlocal x
            if not text:
                return
            runs = [(text, None)]
            if formats:  # split where the syntax color changes
                runs, start = [], 0
                fmt_at = [formats[column + i] if column + i < len(formats) else None
                          for i in range(len(text))]
                for i in range(1, len(text) + 1):
                    if i == len(text) or fmt_at[i] is not fmt_at[start]:
                        runs.append((text[start:i], fmt_at[start]))
                        start = i
            pieces = []
            for piece, fmt in runs:
                f = QFont(font)
                f.setStrikeOut(strike)
                if fmt is not None:
                    f.setBold(fmt.fontWeight() >= QFont.Bold)
                    f.setItalic(fmt.fontItalic())
                pieces.append((piece, f, fmt, QFontMetrics(f).horizontalAdvance(piece)))
            if background is not None:
                width = sum(p[3] for p in pieces)
                painter.fillRect(QRect(x, rect.top() + 2, width, rect.height() - 4), background)
            for piece, f, fmt, width in pieces:
                painter.setFont(f)
                brush = fmt.foreground() if fmt is not None else None
                painter.setPen(brush.color() if brush is not None and brush.style() != Qt.NoBrush
                               else pen)
                painter.drawText(QRect(x, rect.top(), width, rect.height()), Qt.AlignVCenter | Qt.AlignLeft,
                                 piece)
                x += width

        if parts[0] == "file":
            _kind, path, name, folder, package, count = parts
            icon = self.tree.icon_for(path) if self.tree.icon_for else None
            if icon is not None and not icon.isNull():
                size = QFontMetrics(opt.font).height()
                icon.paint(painter, QRect(x, rect.top() + (rect.height() - size) // 2, size, size))
                x += size + 5
            bold = QFont(opt.font)
            bold.setBold(True)
            draw(name, bold, color)
            draw(f"   {folder}" if folder else "", opt.font, dim)
            draw(f"   {package}" if package else "", opt.font, dim)
            # The count at the right, in a small pill.
            text = str(count)
            w = QFontMetrics(opt.font).horizontalAdvance(text) + 12
            pill = QRect(rect.right() - w - 6, rect.top() + 3, w, rect.height() - 6)
            fill = QColor(color)
            fill.setAlpha(40)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            painter.setPen(color)
            painter.setFont(opt.font)
            painter.drawText(pill, Qt.AlignCenter, text)
        elif parts[0] == "problem":
            _kind, path, problem = parts
            badge, message = self._problem_layout(opt.font, problem, rect)
            # The line number and badge on the first line; the message wraps beside them.
            painter.setFont(self.tree.mono)
            painter.setPen(dim)
            painter.drawText(QRect(x, badge.top(), badge.left() - x, badge.height()),
                             Qt.AlignVCenter | Qt.AlignLeft, f"{problem.line:>5}  ")
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(problem.color))
            painter.drawRoundedRect(badge, 3, 3)
            painter.setPen(QColor("white"))
            painter.setFont(_badge_font(opt.font))
            painter.drawText(badge, Qt.AlignCenter, problem.badge)
            painter.setPen(color)
            painter.setFont(opt.font)
            first = QFontMetrics(opt.font).height()
            # (one line: centred on the badge, as before; more: from the top)
            if QFontMetrics(opt.font).boundingRect(message, Qt.TextWordWrap, problem.message).height() <= first:
                message.setTop(badge.top())
                message.setHeight(badge.height())
                painter.drawText(message, Qt.AlignVCenter | Qt.AlignLeft, problem.message)
            else:
                painter.drawText(message, Qt.TextWordWrap | Qt.AlignTop | Qt.AlignLeft, problem.message)
        else:
            _kind, path, found = parts
            mono = self.tree.mono
            # Syntax colors (none on a selected row: its text color reads best there).
            formats = None if selected else self.tree.line_formats(path, found.line)
            draw(f"{found.line:>5}  ", mono, dim)
            before = found.before
            if before.startswith("…") and found.column - found.start < len(before):
                draw("…", mono, dim)
                before = before[1:]
            draw(before, mono, color, formats=formats, column=found.start)
            if self.tree.replacement is not None:
                draw(found.text, mono, color, REMOVED_COLOR, strike=True, formats=formats,
                     column=found.column)
                draw(self.tree.replacement(found.text), mono, color, ADDED_COLOR)
            else:
                draw(found.text, mono, color, MATCH_COLOR, formats=formats, column=found.column)
            draw(found.after, mono, color, formats=formats, column=found.column + len(found.text))
        painter.restore()


class ResultTree(QTreeWidget):
    """Results grouped by file (see the module's description)."""

    open_requested = Signal(str, int, int, int, int)  # file, line, column, end line, end column

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mono = QFont()  # for the lines (set by the window)
        self.icon_for: Callable[[Path], object] | None = None    # a file's QIcon (set by the window)
        self.text_for: Callable[[Path], str | None] | None = None  # an open file's text (set by the window)
        # While replacing: the replacement shown for a result's text.
        self.replacement: Callable[[str], str] | None = None
        self.items: dict[Path, QTreeWidgetItem] = {}
        self._style = get_style_by_name(hl.LIGHT_STYLE)  # (see refresh_style)
        self._formats: dict = {}                        # token type -> format
        self._file_lines: dict[Path, list[str]] = {}
        self._line_formats: dict[tuple[Path, int], list | None] = {}
        self.setHeaderHidden(True)
        self.setUniformRowHeights(True)
        self.setItemDelegate(_ResultDelegate(self))
        self.itemClicked.connect(self.open_item)
        self.itemActivated.connect(self.open_item)

    # ------------------------------------------------------------ rows
    def set_file(self, path: Path, matches: list[Match], folder: str = "", package: str = "",
                 expanded: bool | None = None) -> QTreeWidgetItem:
        """Add a file's row (at the end) with its results, or fill it again."""
        item = self.items.get(path)
        new = item is None
        if new:
            item = self.items[path] = QTreeWidgetItem()
            self.addTopLevelItem(item)
        was_expanded = item.isExpanded()
        item.takeChildren()
        item.setText(0, f"{folder}/{path.name}" if folder else path.name)
        item.setToolTip(0, str(path))
        item.setData(0, PARTS_ROLE, ("file", path, path.name, folder, package, len(matches)))
        item.setData(0, RESULT_ROLE, (str(path), None))
        for found in matches:
            child = QTreeWidgetItem([f"{found.line}: {found.before}{found.text}{found.after}"])
            child.setToolTip(0, f"{path}:{found.line}")
            child.setData(0, PARTS_ROLE, ("match", path, found))
            child.setData(0, RESULT_ROLE, (str(path), found))
            item.addChild(child)
        item.setExpanded(expanded if expanded is not None else (True if new else was_expanded))
        return item

    def set_problems(self, path: Path, problems: list[Problem], folder: str = "", package: str = ""):
        """Show a file's problems (instead of whatever was shown), keeping
        the scroll position when it's the same file again."""
        scroll = self.verticalScrollBar().value() if path in self.items else 0
        self.clear()
        self.items.clear()
        self.forget(path)
        item = self.items[path] = QTreeWidgetItem()
        self.addTopLevelItem(item)
        item.setText(0, path.name)
        item.setToolTip(0, str(path))
        item.setData(0, PARTS_ROLE, ("file", path, path.name, folder, package, len(problems)))
        item.setData(0, RESULT_ROLE, (str(path), None))
        for problem in problems:
            child = QTreeWidgetItem([f"{problem.line}: {problem.badge} {problem.message}"])
            lines = self.file_lines(path)
            code = lines[problem.line - 1].strip() if 0 < problem.line <= len(lines) else ""
            child.setToolTip(0, f"{problem.message}\n\nLine {problem.line}: {code[:200]}")
            child.setData(0, PARTS_ROLE, ("problem", path, problem))
            child.setData(0, RESULT_ROLE, (str(path), problem))
            item.addChild(child)
        item.setExpanded(True)
        self.verticalScrollBar().setValue(scroll)

    def set_wrapping(self, on: bool):
        """Rows as tall as their (wrapped) text: the Problems tab's messages
        can be long (an AI review's). Off: one line each, quicker for long lists."""
        self.setUniformRowHeights(not on)
        self.setWordWrap(on)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if not self.uniformRowHeights() and event.size().width() != event.oldSize().width():
            self.doItemsLayout()  # (wrapped rows: new heights for the new width)

    def remove_file(self, path: Path):
        item = self.items.pop(path, None)
        if item is not None:
            self.takeTopLevelItem(self.indexOfTopLevelItem(item))

    def clear_results(self):
        self.clear()
        self.items.clear()
        self._file_lines.clear()
        self._line_formats.clear()

    def toggle_collapse(self) -> bool:
        """Collapse every file, or expand them all if none is expanded.
        Returns whether they are now expanded."""
        items = [self.topLevelItem(i) for i in range(self.topLevelItemCount())]
        expand = not any(item.isExpanded() for item in items)
        for item in items:
            item.setExpanded(expand)
        return expand

    def open_item(self, item: QTreeWidgetItem, _column: int = 0):
        """Open a result (a file's row: its first result)."""
        data = item.data(0, RESULT_ROLE)
        if not data:
            return
        path, found = data
        if found is None:
            if not item.childCount():
                return
            found = item.child(0).data(0, RESULT_ROLE)[1]
        self.open_requested.emit(path, found.line, found.column, found.end_line, found.end_column)

    # ---------------------------------------------------------- text
    def file_lines(self, path: Path) -> list[str]:
        """A file's lines, as in the editor if it's open (kept for drawing)."""
        lines = self._file_lines.get(path)
        if lines is None:
            text = self.text_for(path) if self.text_for is not None else None
            if text is None:
                text = read_text(path) or ""
            lines = text.split("\n")
            if len(self._file_lines) > 100:  # (keep the most recent files)
                self._file_lines.pop(next(iter(self._file_lines)))
            self._file_lines[path] = lines
        return lines

    def forget(self, path: Path):
        """A file changed: read and lex it again when its lines are drawn."""
        self._file_lines.pop(path, None)
        for key in [k for k in self._line_formats if k[0] == path]:
            del self._line_formats[key]

    # -------------------------------------------------- syntax colors
    def refresh_style(self):
        """Use the editor's syntax colors (View → Syntax Colors), on its
        background."""
        self._style = get_style_by_name(hl.style_name(QApplication.palette()))
        self._formats.clear()
        self._line_formats.clear()
        palette = self.palette()
        palette.setColor(QPalette.Base, QColor(self._style.background_color))
        palette.setColor(QPalette.Text, hl.style_text_color(self._style))
        self.setPalette(palette)
        self.viewport().update()

    def line_formats(self, path: Path, line: int) -> list | None:
        """The syntax format of each column of a line (None: plain), lexed
        with the lines before it for context (so a line inside a
        multi-line string or comment is colored as one)."""
        key = (path, line)
        if key in self._line_formats:
            return self._line_formats[key]
        lines = self.file_lines(path)
        result = None
        if 0 < line <= len(lines):
            limit = 4000  # (long lines: only their start is shown anyway)
            context = [s[:limit] for s in lines[max(0, line - 1 - CONTEXT_LINES):line]]
            chunk = "\n".join(context)
            offset = len(chunk) - len(context[-1])
            lexer = hl.lexer_for_path(path)
            lexer.stripnl = False  # (keep the positions as they are)
            result = [None] * len(context[-1])
            pos = 0
            try:
                for ttype, value in lex(chunk, lexer):
                    end = pos + len(value)
                    if end > offset:
                        fmt = self._formats.get(ttype)
                        if fmt is None:
                            fmt = self._formats[ttype] = hl.token_format(self._style, ttype)
                        for i in range(max(pos, offset), min(end, len(chunk))):
                            result[i - offset] = fmt
                    pos = end
            except Exception:  # noqa: BLE001 - never let highlighting break the list
                result = None
        self._line_formats[key] = result
        return result
