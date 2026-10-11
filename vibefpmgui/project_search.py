"""*Find in Files* (⌘/Ctrl+Shift+F): search, and replace, across the project
and its dependencies.

The search runs on a worker thread over every text file of the project and
of the dependencies on disk. Files open in the editor are searched as they
are in the editor, unsaved changes included. Like the *Project files* tree it
leaves out fpm's ``build`` folder, ``.git`` and compiled files; files ignored
by git and hidden files only with *Ignored files* checked.

Results are grouped by file in the *Search* tab, each line in the colors
the editor would give it; click one to open the file there. Replacing changes open files in the editor (not saved: Undo reverts
it) and saves the others, like *Rename Symbol*.
"""

from __future__ import annotations

import fnmatch
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from pygments import lex
from pygments.styles import get_style_by_name
from qtpy.QtCore import QRect, QSize, Qt, QThread, QTimer, Signal
from qtpy.QtGui import QColor, QFont, QFontMetrics, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QPushButton,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import highlighter as hl
from . import manifest as m
from .lsp import TextEdit
from .project_tree import SKIP_DIRS, SKIP_SUFFIXES, GitIgnore

MAX_RESULTS = 10_000          # matches shown (the search stops there)
MAX_FILE_SIZE = 8 * 1024 * 1024
PREVIEW_BEFORE = 50           # characters of the line shown before a match
PREVIEW_AFTER = 200
CONTEXT_LINES = 40            # lexed before a result's line, for its syntax colors


# ------------------------------------------------------------------ search
@dataclass(frozen=True)
class Query:
    """What to search for, and where."""
    text: str
    regex: bool = False
    case: bool = False        # match case
    word: bool = False        # whole words only
    include: str = ""         # comma-separated globs, e.g. "*.f90, src/"
    exclude: str = ""
    ignored: bool = False     # also files ignored by git, and hidden files
    dependencies: bool = True


class Root(NamedTuple):
    """A package folder to search."""
    name: str
    directory: Path
    dependency: bool


class Match(NamedTuple):
    line: int        # 1-based
    column: int      # 0-based
    end_line: int
    end_column: int
    before: str      # the line's text around the match, for the results list
    text: str        # (the matched part of the first line)
    after: str
    start: int = 0   # the column ``before`` starts at (after any "…")


@dataclass
class FileResult:
    path: Path
    root: Root
    matches: list[Match] = field(default_factory=list)

    @property
    def relative(self) -> str:
        try:
            return self.path.relative_to(self.root.directory.resolve()).as_posix()
        except ValueError:
            return str(self.path)


def compile_query(query: Query) -> re.Pattern | None:
    """The query as a regular expression (None if there's nothing to find).
    Raises `re.error` for a bad regular expression."""
    if not query.text:
        return None
    pattern = query.text if query.regex else re.escape(query.text)
    if query.word:
        pattern = rf"(?<!\w)(?:{pattern})(?!\w)"
    return re.compile(pattern, re.MULTILINE | (0 if query.case else re.IGNORECASE))


def search_roots(graph: m.DependencyGraph | None, project_dir: Path,
                 dependencies: bool = True) -> list[Root]:
    """The project folder, then each dependency on disk."""
    name = project_dir.name
    if graph is not None and graph.root in graph.packages:
        name = graph.packages[graph.root].name
    roots, seen = [Root(name, project_dir, False)], {project_dir.resolve()}
    if graph is not None and dependencies:
        for pkg in graph.packages.values():
            if pkg.name == graph.root or pkg.kind in (m.META, m.MISSING):
                continue
            if pkg.directory and pkg.directory.is_dir() and pkg.directory.resolve() not in seen:
                seen.add(pkg.directory.resolve())
                roots.append(Root(pkg.name, pkg.directory, True))
    return roots


def _globs(text: str) -> list[str]:
    return [g.strip() for g in text.replace(";", ",").split(",") if g.strip()]


def glob_matches(relative: str, globs: list[str]) -> bool:
    """Whether a file (its path in its package, "/"-separated) matches one
    of the globs: "*.f90" (a name anywhere), "src/" or "src" (a folder and
    everything in it), "src/*.f90" (a path)."""
    relative = relative.lower()  # (*.f90 matches .F90 files too)
    name = relative.rsplit("/", 1)[-1]
    for glob in globs:
        glob = glob.lower().removeprefix("./")
        folder = glob.rstrip("/")
        if fnmatch.fnmatch(name, glob) or fnmatch.fnmatch(relative, glob) \
                or fnmatch.fnmatch(relative, f"*/{glob}") \
                or fnmatch.fnmatch(relative, f"{folder}/*") or fnmatch.fnmatch(relative, f"*/{folder}/*"):
            return True
    return False


def iter_files(roots: list[Root], query: Query, cancelled: Callable[[], bool] = lambda: False):
    """(root, path) for every file to search, package by package."""
    others = {r.directory.resolve() for r in roots}
    include, exclude = _globs(query.include), _globs(query.exclude)
    for root in roots:
        top = root.directory.resolve()
        rules = GitIgnore(top)
        for folder, dirs, files in os.walk(top):
            if cancelled():
                return
            here = Path(folder)
            keep = []
            for name in sorted(dirs, key=str.lower):
                path = here / name
                if name == ".git" or name in SKIP_DIRS or path in others:
                    continue  # (other packages are searched as themselves)
                if not query.ignored and (name.startswith(".") or rules.check(path, True)):
                    continue
                keep.append(name)
            dirs[:] = keep
            for name in sorted(files, key=str.lower):
                path = here / name
                if path.suffix in SKIP_SUFFIXES or path.suffix.lower() in SKIP_SUFFIXES:
                    continue
                if not query.ignored and (name.startswith(".") or rules.check(path, False)):
                    continue
                relative = path.relative_to(top).as_posix()
                if include and not glob_matches(relative, include):
                    continue
                if exclude and glob_matches(relative, exclude):
                    continue
                yield root, path


def is_searched(root: Root, path: Path, query: Query, others: set[Path] = frozenset()) -> bool:
    """Whether `iter_files` would search a file (checked on its own)."""
    top = root.directory.resolve()
    try:
        parts = path.relative_to(top).parts
    except ValueError:
        return False
    rules = GitIgnore(top)
    folder = top
    for name in parts[:-1]:
        folder = folder / name
        if name == ".git" or name in SKIP_DIRS or folder in others:
            return False
        if not query.ignored and (name.startswith(".") or rules.check(folder, True)):
            return False
    name = parts[-1]
    if path.suffix in SKIP_SUFFIXES or path.suffix.lower() in SKIP_SUFFIXES:
        return False
    if not query.ignored and (name.startswith(".") or rules.check(path, False)):
        return False
    relative = "/".join(parts)
    include, exclude = _globs(query.include), _globs(query.exclude)
    return (not include or glob_matches(relative, include)) and not (exclude and glob_matches(relative, exclude))


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


def _position(text: str, offset: int) -> tuple[int, int]:
    """(line 0-based, column) of an offset."""
    line = text.count("\n", 0, offset)
    return line, offset - (text.rfind("\n", 0, offset) + 1)


def find_matches(text: str, pattern: re.Pattern, limit: int = MAX_RESULTS) -> list[Match]:
    """Every match in a text (empty matches are skipped)."""
    out: list[Match] = []
    line, last = 0, 0  # (counted incrementally: big files stay quick)
    for found in pattern.finditer(text):
        start, end = found.span()
        if start == end:
            continue
        line += text.count("\n", last, start)
        last = start
        line_start = text.rfind("\n", 0, start) + 1
        line_end = text.find("\n", start)
        line_end = len(text) if line_end < 0 else line_end
        end_line = line + text.count("\n", start, end)
        end_column = end - (text.rfind("\n", 0, end) + 1)
        column = start - line_start
        before = text[line_start:start].lstrip()
        first = column - len(before)
        if len(before) > PREVIEW_BEFORE:
            before, first = "…" + before[-PREVIEW_BEFORE:], column - PREVIEW_BEFORE
        out.append(Match(line + 1, column, end_line + 1, end_column, before,
                         text[start:min(end, line_end)][:PREVIEW_AFTER],
                         text[min(end, line_end):line_end][:PREVIEW_AFTER], first))
        if len(out) >= limit:
            break
    return out


def replacement(found: re.Match, query: Query, template: str) -> str:
    """What one match is replaced with: with *regex*, ``\\1`` and
    ``\\g<name>`` in the template are the match's groups."""
    return found.expand(template) if query.regex else template


def replacement_edits(text: str, pattern: re.Pattern, query: Query, template: str,
                      keep: set[tuple[int, int]] | None = None) -> list[TextEdit]:
    """The edits that replace the matches in a text: all of them, or those
    starting at the (1-based line, column) positions in ``keep``.
    Raises `re.error` for a bad template."""
    edits = []
    for found in pattern.finditer(text):
        start, end = found.span()
        if start == end:
            continue
        line, column = _position(text, start)
        if keep is not None and (line + 1, column) not in keep:
            continue
        end_line, end_column = _position(text, end)
        edits.append(TextEdit(line, column, end_line, end_column, replacement(found, query, template)))
    return edits


def preview_replacement(match_text: str, pattern: re.Pattern, query: Query, template: str) -> str:
    """The replacement shown in the results list (the match on its own:
    good enough for a preview)."""
    if not query.regex:
        return template
    try:
        return pattern.sub(lambda f: f.expand(template), match_text, count=1)
    except (re.error, IndexError):
        return template


def search(roots: list[Root], query: Query, pattern: re.Pattern, open_texts: dict[Path, str],
           found: Callable[[FileResult], None], cancelled: Callable[[], bool] = lambda: False,
           limit: int = MAX_RESULTS) -> tuple[int, bool]:
    """Search the files; ``found`` gets each file with matches.
    Returns (files searched, whether it stopped at ``limit`` matches)."""
    searched, total = 0, 0
    for root, path in iter_files(roots, query, cancelled):
        if cancelled():
            break
        resolved = path.resolve()
        text = open_texts.get(resolved)
        if text is None:
            text = read_text(path)
        if text is None:
            continue
        searched += 1
        matches = find_matches(text, pattern, limit - total)
        if matches:
            total += len(matches)
            found(FileResult(resolved, root, matches))
            if total >= limit:
                return searched, True
    return searched, False


class SearchWorker(QThread):
    """Runs `search` on a thread; results arrive in batches."""
    found = Signal(object)          # list[FileResult]
    progress = Signal(int)          # files searched so far
    done = Signal(int, bool)        # files searched, stopped at the limit

    def __init__(self, roots, query, pattern, open_texts, parent=None):
        super().__init__(parent)
        self.roots, self.query, self.pattern, self.open_texts = roots, query, pattern, open_texts
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        batch: list[FileResult] = []
        last = time.monotonic()

        def found(result):
            nonlocal last
            batch.append(result)
            if time.monotonic() - last > 0.15:
                self.found.emit(batch[:])
                batch.clear()
                last = time.monotonic()

        searched, truncated = search(self.roots, self.query, self.pattern, self.open_texts, found,
                                     lambda: self._cancel)
        if not self._cancel:
            if batch:
                self.found.emit(batch)
            self.done.emit(searched, truncated)


# ------------------------------------------------------------------- panel
PARTS_ROLE = Qt.UserRole + 1    # how a row is drawn (see _ResultDelegate)
RESULT_ROLE = Qt.UserRole + 2   # (path, Match or None)

# Saved options: settings key -> SearchPanel attribute.
OPTIONS = {"case": "case_button", "word": "word_button", "regex": "regex_button",
           "dependencies": "deps_check", "ignored": "ignored_check"}

MATCH_COLOR = QColor(242, 201, 76, 110)
REMOVED_COLOR = QColor(255, 107, 107, 90)
ADDED_COLOR = QColor(64, 192, 87, 100)


class _ResultDelegate(QStyledItemDelegate):
    """Draws a file row (name, folder, package, count) or a match row (line
    number and the line, with the match highlighted, or the replacement
    shown when replacing)."""

    def __init__(self, panel: SearchPanel):
        super().__init__(panel)
        self.panel = panel

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), QFontMetrics(self.panel.mono).height() + 6))

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
            _kind, name, folder, package, count = parts
            icon = self.panel.icon_for(Path(index.data(RESULT_ROLE)[0])) if self.panel.icon_for else None
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
            fm = QFontMetrics(opt.font)
            w = fm.horizontalAdvance(text) + 12
            pill = QRect(rect.right() - w - 6, rect.top() + 3, w, rect.height() - 6)
            fill = QColor(color)
            fill.setAlpha(40)
            painter.setPen(Qt.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(pill, pill.height() / 2, pill.height() / 2)
            painter.setPen(color)
            painter.setFont(opt.font)
            painter.drawText(pill, Qt.AlignCenter, text)
        else:
            _kind, path, found = parts
            mono = self.panel.mono
            # Syntax colors (none on a selected row: its text color reads best there).
            formats = None if selected else self.panel.line_formats(path, found.line)
            draw(f"{found.line:>5}  ", mono, dim)
            before = found.before
            if before.startswith("…") and found.column - found.start < len(before):
                draw("…", mono, dim)
                before = before[1:]
            draw(before, mono, color, formats=formats, column=found.start)
            if self.panel.replacing():
                draw(found.text, mono, color, REMOVED_COLOR, strike=True, formats=formats,
                     column=found.column)
                draw(self.panel.preview(found.text), mono, color, ADDED_COLOR)
            else:
                draw(found.text, mono, color, MATCH_COLOR, formats=formats, column=found.column)
            draw(found.after, mono, color, formats=formats, column=found.column + len(found.text))
        painter.restore()


def _toggle(text: str, tip: str) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setToolTip(tip)
    button.setCheckable(True)
    button.setAutoRaise(True)
    # (Shown highlighted when on: an auto-raise button's checked look is faint.)
    button.setStyleSheet("QToolButton { padding: 2px 5px; border-radius: 4px; }"
                         "QToolButton:checked { background: palette(highlight); "
                         "color: palette(highlighted-text); }")
    return button


class SearchPanel(QWidget):
    """The *Search* tab."""

    open_requested = Signal(str, int, int, int, int)  # file, line, column, end line, end column
    replace_requested = Signal(object, bool)  # {Path: [TextEdit]}, ask first (Replace All)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        # Set by the window: () -> (roots to search, {path: text of open editors}, project folder).
        self.provider: Callable[[], tuple[list[Root], dict[Path, str], Path | None]] | None = None
        self.mono = QFont()
        self.icon_for: Callable[[Path], object] | None = None  # set by the window: a file's QIcon
        self._results: dict[Path, FileResult] = {}
        self._items: dict[Path, QTreeWidgetItem] = {}
        self._query: Query | None = None      # what the results are for
        self._pattern: re.Pattern | None = None
        self._project_dir: Path | None = None
        self._worker: SearchWorker | None = None
        self._old_workers: set[SearchWorker] = set()
        self._changed: set[Path] = set()
        self._style = get_style_by_name(hl.LIGHT_STYLE)  # (see refresh_style)
        self._formats: dict = {}                        # token type -> format
        self._file_lines: dict[Path, list[str]] = {}    # for syntax colors
        self._line_formats: dict[tuple[Path, int], list | None] = {}

        self.replace_toggle = QToolButton()
        self.replace_toggle.setCheckable(True)
        self.replace_toggle.setAutoRaise(True)
        self.replace_toggle.setArrowType(Qt.RightArrow)
        self.replace_toggle.setToolTip("Show the Replace field")
        self.find_edit = QLineEdit()
        self.find_edit.setPlaceholderText("Find in files (⏎ to search)")
        self.find_edit.setClearButtonEnabled(True)
        self.case_button = _toggle("Aa", "Match case")
        self.word_button = _toggle("ab", "Whole words only")
        font = self.word_button.font()
        font.setUnderline(True)
        self.word_button.setFont(font)
        self.regex_button = _toggle(".*", "Regular expression (Python syntax)")
        self.replace_edit = QLineEdit()
        self.replace_edit.setPlaceholderText("Replace with")
        self.replace_edit.setClearButtonEnabled(True)
        self.replace_edit.setToolTip("With a regular expression, \\1 or \\g<name> is a group of the match")
        self.replace_all_button = QPushButton("Replace All")
        self.replace_all_button.setToolTip("Replace every match in the list (asks first)")
        self.include_edit = QLineEdit()
        self.include_edit.setPlaceholderText("Files to include (e.g. *.f90, src/)")
        self.include_edit.setClearButtonEnabled(True)
        self.exclude_edit = QLineEdit()
        self.exclude_edit.setPlaceholderText("Files to exclude (e.g. test/)")
        self.exclude_edit.setClearButtonEnabled(True)
        for edit in (self.include_edit, self.exclude_edit):
            edit.setToolTip("Comma-separated: a name pattern (*.f90), a folder (src/) or a path "
                            "(src/*.f90), in each package")
        self.deps_check = QCheckBox("Dependencies")
        self.deps_check.setToolTip("Also search the dependencies' files")
        self.ignored_check = QCheckBox("Ignored files")
        self.ignored_check.setToolTip("Also search files ignored by git (.gitignore) and hidden files")
        self.status = QLabel("Type to search the project and its dependencies.")
        self.status.setContentsMargins(4, 0, 4, 0)
        self.stop_button = QToolButton()
        self.stop_button.setText("Stop")
        self.stop_button.setAutoRaise(True)
        self.stop_button.hide()
        self.refresh_button = QToolButton()
        self.refresh_button.setText("Search Again")
        self.refresh_button.setToolTip("Search again")
        self.refresh_button.setAutoRaise(True)
        self.collapse_button = QToolButton()
        self.collapse_button.setText("Collapse All")
        self.collapse_button.setToolTip("Collapse or expand all files")
        self.collapse_button.setAutoRaise(True)
        self.clear_button = QToolButton()
        self.clear_button.setText("Clear")
        self.clear_button.setToolTip("Clear the results")
        self.clear_button.setAutoRaise(True)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setUniformRowHeights(True)
        self.tree.setItemDelegate(_ResultDelegate(self))
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.itemClicked.connect(self._open)
        self.tree.itemActivated.connect(self._open)

        find_row = QHBoxLayout()
        find_row.setSpacing(2)
        find_row.addWidget(self.replace_toggle)
        find_row.addWidget(self.find_edit, 1)
        for button in (self.case_button, self.word_button, self.regex_button):
            find_row.addWidget(button)
        self.replace_row = QWidget()
        replace_row = QHBoxLayout(self.replace_row)
        replace_row.setContentsMargins(0, 0, 0, 0)
        replace_row.setSpacing(4)
        replace_row.addSpacing(self.replace_toggle.sizeHint().width() + 2)
        replace_row.addWidget(self.replace_edit, 1)
        replace_row.addWidget(self.replace_all_button)
        self.replace_row.hide()
        files_row = QHBoxLayout()
        files_row.setSpacing(4)
        files_row.addWidget(self.include_edit, 1)
        files_row.addWidget(self.exclude_edit, 1)
        files_row.addSpacing(6)
        files_row.addWidget(self.deps_check)
        files_row.addSpacing(6)
        files_row.addWidget(self.ignored_check)
        status_row = QHBoxLayout()
        status_row.setSpacing(2)
        status_row.addWidget(self.status, 1)
        for button in (self.stop_button, self.refresh_button, self.collapse_button, self.clear_button):
            status_row.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 6, 0)
        layout.setSpacing(4)
        layout.addLayout(find_row)
        layout.addWidget(self.replace_row)
        layout.addLayout(files_row)
        layout.addLayout(status_row)
        layout.addWidget(self.tree, 1)

        self._restore_options()

        # Search as you type (after a short pause), or at once with Enter.
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.start_search)
        self._changed_timer = QTimer(self)  # editor changes -> those files again
        self._changed_timer.setSingleShot(True)
        self._changed_timer.setInterval(500)
        self._changed_timer.timeout.connect(self._refresh_changed)
        self.find_edit.textChanged.connect(lambda: self._timer.start())
        self.find_edit.returnPressed.connect(self.start_search)
        for edit in (self.include_edit, self.exclude_edit):
            edit.textChanged.connect(lambda: self._timer.start())
            edit.returnPressed.connect(self.start_search)
        for button in (self.case_button, self.word_button, self.regex_button, self.deps_check,
                       self.ignored_check):
            button.toggled.connect(self.start_search)
        self.replace_toggle.toggled.connect(self.set_replace_visible)
        self.replace_edit.textChanged.connect(self.tree.viewport().update)
        self.replace_edit.returnPressed.connect(self.replace_all)
        self.replace_all_button.clicked.connect(self.replace_all)
        self.stop_button.clicked.connect(self.stop)
        self.refresh_button.clicked.connect(self.start_search)
        self.collapse_button.clicked.connect(self._toggle_collapse)
        self.clear_button.clicked.connect(self.clear)
        self._update_buttons()

    # ---------------------------------------------------------- options
    def _restore_options(self):
        s = self.settings
        for key, name in OPTIONS.items():
            default = key == "dependencies"
            value = s.value(f"search/{key}", default) if s is not None else default
            getattr(self, name).setChecked(value in (True, "true", "1", 1))
        if s is not None:
            self.include_edit.setText(str(s.value("search/include", "") or ""))
            self.exclude_edit.setText(str(s.value("search/exclude", "") or ""))

    def _save_options(self):
        s = self.settings
        if s is None:
            return
        for key, name in OPTIONS.items():
            s.setValue(f"search/{key}", getattr(self, name).isChecked())
        s.setValue("search/include", self.include_edit.text())
        s.setValue("search/exclude", self.exclude_edit.text())

    def query(self) -> Query:
        return Query(self.find_edit.text(), self.regex_button.isChecked(), self.case_button.isChecked(),
                     self.word_button.isChecked(), self.include_edit.text(), self.exclude_edit.text(),
                     self.ignored_check.isChecked(), self.deps_check.isChecked())

    # ------------------------------------------------------------- open
    def open_find(self, text: str = "", replace: bool = False):
        """Show the search with ``text`` in the field (e.g. the editor's
        selection), ready to type."""
        if replace:
            self.replace_toggle.setChecked(True)
        if text:
            self.find_edit.setText(text)
        field = self.replace_edit if replace and self.find_edit.text() else self.find_edit
        field.setFocus()
        field.selectAll()
        if text:
            self.start_search()

    def set_replace_visible(self, on: bool):
        self.replace_toggle.setChecked(on)
        self.replace_toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        self.replace_toggle.setToolTip("Hide the Replace field" if on else "Show the Replace field")
        self.replace_row.setVisible(on)
        self.tree.viewport().update()
        self._update_buttons()

    def replacing(self) -> bool:
        return self.replace_row.isVisible() or (self.replace_toggle.isChecked() and not self.isVisible())

    def preview(self, match_text: str) -> str:
        if self._pattern is None or self._query is None:
            return self.replace_edit.text()
        return preview_replacement(match_text, self._pattern, self._query, self.replace_edit.text())

    # --------------------------------------------------- syntax colors
    def refresh_style(self):
        """Use the editor's syntax colors (View → Syntax Colors), on its
        background."""
        self._style = get_style_by_name(hl.style_name(QApplication.palette()))
        self._formats.clear()
        self._line_formats.clear()
        palette = self.tree.palette()
        palette.setColor(QPalette.Base, QColor(self._style.background_color))
        palette.setColor(QPalette.Text, hl.style_text_color(self._style))
        self.tree.setPalette(palette)
        self.tree.viewport().update()

    def _forget(self, path: Path):
        """A file changed: lex it again when its lines are drawn."""
        self._file_lines.pop(path, None)
        for key in [k for k in self._line_formats if k[0] == path]:
            del self._line_formats[key]

    def line_formats(self, path: Path, line: int) -> list | None:
        """The syntax format of each column of a line (None: plain), lexed
        with the lines before it for context (so a line inside a
        multi-line string or comment is colored as one)."""
        key = (path, line)
        if key in self._line_formats:
            return self._line_formats[key]
        lines = self._file_lines.get(path)
        if lines is None:
            text = self.provider()[1].get(path) if self.provider is not None else None
            if text is None:
                text = read_text(path) or ""
            lines = text.split("\n")
            if len(self._file_lines) > 100:  # (keep the most recent files)
                self._file_lines.pop(next(iter(self._file_lines)))
            self._file_lines[path] = lines
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

    # ----------------------------------------------------------- search
    def start_search(self):
        self._timer.stop()
        self._save_options()
        self.stop()
        query = self.query()
        try:
            pattern = compile_query(query)
        except re.error as exc:
            self._show_error(f"Bad regular expression: {exc}")
            return
        self._clear_results()
        if pattern is None or self.provider is None:
            self.status.setText("Type to search the project and its dependencies.")
            self._update_buttons()
            return
        roots, open_texts, self._project_dir = self.provider()
        if not roots:
            self.status.setText("Open a project to search its files.")
            return
        if not query.dependencies:
            roots = [r for r in roots if not r.dependency]
        self._query, self._pattern = query, pattern
        worker = SearchWorker(roots, query, pattern, open_texts, self)
        worker.found.connect(self._add_results)
        worker.done.connect(self._finished)
        worker.finished.connect(lambda w=worker: self._old_workers.discard(w))
        self._worker = worker
        self._old_workers.add(worker)
        self.status.setText("Searching…")
        self.stop_button.show()
        worker.start()

    def stop(self):
        if self._worker is not None:
            self._worker.cancel()
            self._worker.found.disconnect()
            self._worker.done.disconnect()
            self._worker = None
            self.stop_button.hide()
            if self._results:
                self._show_counts(stopped=True)
            else:
                self.status.setText("Stopped.")

    def shutdown(self):
        """Before the window closes: stop searching."""
        self.stop()
        for worker in list(self._old_workers):
            worker.cancel()
            worker.wait(3000)

    def _show_error(self, text: str):
        self.status.setText(f"<span style='color:#e03131'>{text}</span>")

    def _add_results(self, results: list[FileResult]):
        for result in results:
            self._results[result.path] = result
            self._add_file_item(result)
        self._show_counts(searching=True)

    def _finished(self, searched: int, truncated: bool):
        self._worker = None
        self.stop_button.hide()
        self._searched = searched
        self._show_counts(truncated=truncated)

    def _show_counts(self, searching=False, truncated=False, stopped=False):
        matches = sum(len(r.matches) for r in self._results.values())
        files = len(self._results)
        if not matches and not searching:
            text = f"No results ({getattr(self, '_searched', 0):,} files searched)."
        else:
            text = (f"{matches:,} result{'s' * (matches != 1)} in {files:,} file{'s' * (files != 1)}")
            if searching:
                text += " — searching…"
            elif truncated:
                text += f" (stopped at {MAX_RESULTS:,}: narrow the search)"
            elif stopped:
                text += " (stopped)"
        self.status.setText(text)
        self._update_buttons()

    def _update_buttons(self):
        has = bool(self._results)
        self.replace_all_button.setEnabled(has and self._worker is None)
        self.collapse_button.setEnabled(has)
        self.clear_button.setEnabled(has)

    def clear(self):
        self.stop()
        self._clear_results()
        self.status.setText("Type to search the project and its dependencies.")
        self._update_buttons()

    def _clear_results(self):
        self._file_lines.clear()
        self._line_formats.clear()
        self.collapse_button.setText("Collapse All")
        self.tree.clear()
        self._results.clear()
        self._items.clear()
        self._query = self._pattern = None

    # ---------------------------------------------------------- results
    def _file_parts(self, result: FileResult):
        relative = result.relative
        folder, _, name = relative.rpartition("/")
        if not result.root.dependency and self._project_dir is not None:
            try:  # (the project's own files: relative to the project)
                folder = str(result.path.parent.relative_to(self._project_dir.resolve()).as_posix())
                folder = "" if folder == "." else folder
            except ValueError:
                pass
        package = f"· {result.root.name}" if result.root.dependency else ""
        return ("file", name, folder, package, len(result.matches))

    def _fill_file_item(self, item: QTreeWidgetItem, result: FileResult):
        item.takeChildren()
        item.setText(0, result.relative)
        item.setToolTip(0, str(result.path))
        item.setData(0, PARTS_ROLE, self._file_parts(result))
        item.setData(0, RESULT_ROLE, (str(result.path), None))
        for found in result.matches:
            child = QTreeWidgetItem([f"{found.line}: {found.before}{found.text}{found.after}"])
            child.setToolTip(0, f"{result.path}:{found.line}")
            child.setData(0, PARTS_ROLE, ("match", result.path, found))
            child.setData(0, RESULT_ROLE, (str(result.path), found))
            item.addChild(child)

    def _add_file_item(self, result: FileResult, index: int | None = None):
        item = QTreeWidgetItem()
        self._fill_file_item(item, result)
        if index is None:
            self.tree.addTopLevelItem(item)
        else:
            self.tree.insertTopLevelItem(index, item)
        item.setExpanded(True)
        self._items[result.path] = item

    def _remove_file(self, path: Path):
        item = self._items.pop(path, None)
        self._results.pop(path, None)
        if item is not None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(item))

    def _toggle_collapse(self):
        items = [self.tree.topLevelItem(i) for i in range(self.tree.topLevelItemCount())]
        expand = not any(item.isExpanded() for item in items)
        for item in items:
            item.setExpanded(expand)
        self.collapse_button.setText("Collapse All" if expand else "Expand All")

    def _open(self, item: QTreeWidgetItem, _column: int = 0):
        data = item.data(0, RESULT_ROLE)
        if not data:
            return
        path, found = data
        if found is None:
            if item.childCount():
                found = item.child(0).data(0, RESULT_ROLE)[1]
            else:
                return
        self.open_requested.emit(path, found.line, found.column, found.end_line, found.end_column)

    # ---------------------------------------- keeping results up to date
    def file_changed(self, path: Path):
        """An open file was edited: search it again shortly (if it's listed,
        or could be)."""
        if self._pattern is not None:
            self._changed.add(Path(path).resolve())
            self._changed_timer.start()

    def _refresh_changed(self):
        if self._worker is not None:  # (still searching: try again later)
            self._changed_timer.start()
            return
        paths, self._changed = self._changed, set()
        self.refresh_files(paths)

    def refresh_files(self, paths):
        """Search these files again (after they changed)."""
        if self._pattern is None or self.provider is None:
            return
        roots, open_texts, _ = self.provider()
        for path in paths:
            path = Path(path).resolve()
            self._forget(path)
            result = self._results.get(path)
            if result is None:
                root = self._root_for(path, roots)
                if root is None:
                    continue
                result = FileResult(path, root)
            text = open_texts.get(path)
            if text is None:
                text = read_text(path) or ""
            result.matches = find_matches(text, self._pattern)
            item = self._items.get(path)
            if not result.matches:
                self._remove_file(path)
            elif item is not None:
                expanded = item.isExpanded()
                self._fill_file_item(item, result)
                item.setExpanded(expanded)
            else:
                self._results[path] = result
                self._add_file_item(result)
        self._show_counts()

    def _root_for(self, path: Path, roots: list[Root]) -> Root | None:
        """The package an open file belongs to, if it would be searched."""
        if self._query is None:
            return None
        best = None
        for root in roots:
            top = root.directory.resolve()
            if (top in path.parents) and (best is None or len(str(top)) > len(str(best.directory.resolve()))):
                best = root
        if best is None or (best.dependency and not self._query.dependencies):
            return None
        others = {r.directory.resolve() for r in roots if r is not best}
        return best if is_searched(best, path, self._query, others) else None

    # ---------------------------------------------------------- replace
    def _plan(self, selection: dict[Path, set[tuple[int, int]] | None]) -> dict[Path, list[TextEdit]] | None:
        """The edits for the chosen matches (None for all of a file's listed
        ones), made from the files' current text."""
        if self._pattern is None or self.provider is None:
            return None
        _roots, open_texts, _ = self.provider()
        plan = {}
        try:
            for path, keep in selection.items():
                result = self._results.get(path)
                if result is None:
                    continue
                if keep is None:
                    keep = {(found.line, found.column) for found in result.matches}
                text = open_texts.get(path)
                if text is None:
                    text = read_text(path)
                if text is None:
                    continue
                edits = replacement_edits(text, self._pattern, self._query, self.replace_edit.text(), keep)
                if edits:
                    plan[path] = edits
        except re.error as exc:
            self._show_error(f"Bad replacement: {exc}")
            return None
        return plan

    def replace_all(self):
        if not self._results or self._worker is not None:
            return
        plan = self._plan({path: None for path in self._results})
        if plan:
            self.replace_requested.emit(plan, True)

    def _replace(self, selection):
        plan = self._plan(selection)
        if plan:
            self.replace_requested.emit(plan, False)

    def _dismiss(self, path: Path, found: Match | None):
        result = self._results.get(path)
        if result is None:
            return
        if found is not None:
            result.matches = [f for f in result.matches if f != found]
        if found is None or not result.matches:
            self._remove_file(path)
        else:
            item = self._items[path]
            self._fill_file_item(item, result)
            item.setExpanded(True)
        self._show_counts()

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        path, found = item.data(0, RESULT_ROLE)
        path = Path(path)
        menu = QMenu(self)
        menu.addAction("Open", lambda: self._open(item))
        menu.addSeparator()
        enabled = self._worker is None
        if found is None:
            action = menu.addAction("Replace in This File", lambda: self._replace({path: None}))
            action.setEnabled(enabled)
            menu.addAction("Dismiss File", lambda: self._dismiss(path, None))
        else:
            action = menu.addAction("Replace", lambda: self._replace({path: {(found.line, found.column)}}))
            action.setEnabled(enabled)
            menu.addAction("Dismiss", lambda: self._dismiss(path, found))
        menu.addSeparator()
        menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(str(path)))
        if found is not None:
            menu.addAction("Copy Line", lambda: QApplication.clipboard().setText(
                found.before + found.text + found.after))
        menu.exec(self.tree.viewport().mapToGlobal(pos))
