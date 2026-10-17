"""*Find in Files* (⌘/Ctrl+Shift+F): search, and replace, across the project
and its dependencies.

The search runs on a worker thread over every text file of the project and
of the dependencies on disk. Files open in the editor are searched as they
are in the editor, unsaved changes included. Like the *Project files* tree it
leaves out fpm's ``build`` folder, ``.git`` and compiled files; files ignored
by git and hidden files only with *Ignored files* checked.

Results are grouped by file in the *Search* tab, each line in the colors
the editor would give it; click one to open the file there. Replacing
changes open files in the editor (not saved: Undo reverts it) and saves the
others, like *Rename Symbol*.
"""

import fnmatch
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from qtpy.QtCore import Qt, QThread, QTimer, Signal
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QPushButton,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from . import manifest as m
from .lsp import TextEdit
from .project_tree import SKIP_DIRS, SKIP_SUFFIXES, GitIgnore
from .result_tree import (
    RESULT_ROLE,
    Match,
    ResultTree,
    Root,
    file_labels,
    make_match,
    root_for,
)
from .source_text import search_text
from .widgets import StatusLabel

MAX_RESULTS = 10_000          # matches shown (the search stops there)


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


def search_roots(graph: m.DependencyGraph | None, project_dir: Path) -> list[Root]:
    """The project folder, then each dependency on disk."""
    name = project_dir.name
    if graph is not None and graph.root in graph.packages:
        name = graph.packages[graph.root].name
    roots, seen = [Root(name, project_dir, False)], {project_dir.resolve()}
    if graph is not None:
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
    """(root, path) for every file to search, package by package (the
    dependencies only with ``query.dependencies``; their folders are left out
    of the project's even when not searched, e.g. a local dependency in it)."""
    others = {r.directory.resolve() for r in roots}
    include, exclude = _globs(query.include), _globs(query.exclude)
    for root in roots:
        if root.dependency and not query.dependencies:
            continue
        top = root.directory.resolve()
        rules = GitIgnore(top)
        for folder, dirs, files in os.walk(top):
            if cancelled():
                return
            here = Path(folder)  # (resolved: the walk starts at a resolved folder, and doesn't follow links)
            keep = []
            for name in sorted(dirs, key=str.lower):
                path = here / name
                if name == ".git" or name in SKIP_DIRS or path in others:
                    continue  # (other packages are searched as themselves)
                if not query.ignored and (name.startswith(".") or rules.check_name(here, name, True)):
                    continue
                keep.append(name)
            dirs[:] = keep
            prefix = here.relative_to(top).as_posix()
            prefix = "" if prefix == "." else prefix + "/"
            for name in sorted(files, key=str.lower):
                suffix = os.path.splitext(name)[1]
                if suffix in SKIP_SUFFIXES or suffix.lower() in SKIP_SUFFIXES:
                    continue
                if not query.ignored and (name.startswith(".") or rules.check_name(here, name, False)):
                    continue
                path = here / name
                relative = prefix + name
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
        out.append(make_match(text[line_start:line_end], line + 1, start - line_start,
                              end_line + 1, end_column))
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
    line, line_start, last = 0, 0, 0  # (counted as it goes: recounting from the top was quadratic)
    for found in pattern.finditer(text):
        start, end = found.span()
        if start == end:
            continue
        newlines = text.count("\n", last, start)
        if newlines:
            line += newlines
            line_start = text.rfind("\n", 0, start) + 1
        last = start
        column = start - line_start
        if keep is not None and (line + 1, column) not in keep:
            continue
        end_line = line + text.count("\n", start, end)
        end_column = end - (text.rfind("\n", 0, end) + 1)
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
            text = search_text(path)
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
STOPPED = ("stopped",)  # (SearchPanel._ending: stopped, not finished)
# Saved options: settings key -> SearchPanel attribute.
OPTIONS = {"case": "case_button", "word": "word_button", "regex": "regex_button",
           "dependencies": "deps_check", "ignored": "ignored_check"}

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
    keep_requested = Signal(str)  # a file to keep open (a result double-clicked)
    replace_requested = Signal(object, bool)  # {Path: [TextEdit]}, ask first (Replace All)

    def __init__(self, settings=None, parent=None):
        super().__init__(parent)
        self.settings = settings
        # Set by the window: () -> (roots to search, {path: text of open editors}, project folder).
        self.provider: Callable[[], tuple[list[Root], dict[Path, str], Path | None]] | None = None
        self._results: dict[Path, FileResult] = {}
        self._query: Query | None = None      # what the results are for
        self._pattern: re.Pattern | None = None
        self._project_dir: Path | None = None
        self._worker: SearchWorker | None = None
        self._old_workers: set[SearchWorker] = set()
        self._changed: set[Path] = set()
        # Results not listed yet: added a slice at a time (a batch of a
        # thousand files at once held the window a tenth of a second).
        self._incoming: list[FileResult] = []
        self._ending: tuple | None = None  # (the search's end, shown once the last are listed)
        self._adding = QTimer(self)
        self._adding.setSingleShot(True)
        self._adding.setInterval(0)
        self._adding.timeout.connect(self._add_some)

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
        self.status = StatusLabel("Type to search the project and its dependencies.")
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

        self.tree = ResultTree()
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.open_requested.connect(self.open_requested)
        self.tree.keep_requested.connect(self.keep_requested)

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
        self.tree.replacement = self.preview if on else None
        self.tree.viewport().update()
        self._update_buttons()

    def preview(self, match_text: str) -> str:
        if self._pattern is None or self._query is None:
            return self.replace_edit.text()
        return preview_replacement(match_text, self._pattern, self._query, self.replace_edit.text())

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
            if self._incoming:
                self._ending = STOPPED  # (those found are still listed, then "stopped")
            elif self._results:
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

    ADD_SECONDS = 0.015  # listed at a time before the window gets its turn

    def _add_results(self, results: list[FileResult]):
        self._incoming += results
        self._add_some()

    def _add_some(self):
        started = time.monotonic()
        while self._incoming:  # (at least one each time: always getting on)
            result = self._incoming.pop(0)
            self._results[result.path] = result
            self._add_file_item(result)
            if time.monotonic() - started >= self.ADD_SECONDS:
                break
        if self._incoming:
            self._adding.start()
            self._show_counts(searching=True)
        elif self._ending == STOPPED:
            self._ending = None
            self._show_counts(stopped=True)
        elif self._ending is not None:
            searched, truncated = self._ending
            self._ending = None
            self._searched = searched
            self._show_counts(truncated=truncated)
        else:
            self._show_counts(searching=self._worker is not None)

    def _finished(self, searched: int, truncated: bool):
        self._worker = None
        self.stop_button.hide()
        self._ending = (searched, truncated)
        self._add_some()

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
        self.replace_all_button.setEnabled(has and self._worker is None and not self._incoming)  # (all listed)
        self.collapse_button.setEnabled(has)
        self.clear_button.setEnabled(has)

    def clear(self):
        self.stop()
        self._clear_results()
        self.status.setText("Type to search the project and its dependencies.")
        self._update_buttons()

    def reset(self):
        """Another project was opened: no search text or results (the options
        and file patterns are kept)."""
        self.find_edit.clear()
        self.replace_edit.clear()
        self._timer.stop()  # (no search for the cleared text)
        self.clear()

    def _clear_results(self):
        self._incoming.clear()
        self._ending = None
        self._adding.stop()
        self.collapse_button.setText("Collapse All")
        self.tree.clear_results()
        self._results.clear()
        self._query = self._pattern = None

    # ---------------------------------------------------------- results
    def _add_file_item(self, result: FileResult):
        self.tree.set_file(result.path, result.matches,
                           *file_labels(result.path, result.root, self._project_dir))

    def _remove_file(self, path: Path):
        self._results.pop(path, None)
        self.tree.remove_file(path)

    def _toggle_collapse(self):
        expanded = self.tree.toggle_collapse()
        self.collapse_button.setText("Collapse All" if expanded else "Expand All")

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
            self.tree.forget(path)
            result = self._results.get(path)
            if result is None:
                root = self._root_for(path, roots)
                if root is None:
                    continue
                result = FileResult(path, root)
            text = open_texts.get(path)
            if text is None:
                text = search_text(path) or ""
            result.matches = find_matches(text, self._pattern)
            if not result.matches:
                self._remove_file(path)
            else:
                self._results[path] = result
                self._add_file_item(result)
        self._show_counts()

    def _root_for(self, path: Path, roots: list[Root]) -> Root | None:
        """The package an open file belongs to, if it would be searched."""
        if self._query is None:
            return None
        best = root_for(path, roots)
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
                    text = search_text(path)
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
            self._add_file_item(result)
        self._show_counts()

    def _context_menu(self, pos):
        item = self.tree.itemAt(pos)
        if item is None:
            return
        path, found = item.data(0, RESULT_ROLE)
        path = Path(path)
        menu = QMenu(self)
        menu.addAction("Open", lambda: self.tree.open_item(item))
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
