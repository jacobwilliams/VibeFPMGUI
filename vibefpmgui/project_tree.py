"""Sidebar tree of the project's files and those of its dependencies.

The project folder is listed first; each dependency (local path or a git
download in ``build/dependencies``) gets its own node under *Dependencies*.
Build output, hidden folders and compiled files are left out. A filter box
narrows the tree to matching file names. Double-click (or Enter) opens a file.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from qtpy.QtCore import (
    QEvent,
    QFileInfo,
    QSortFilterProxyModel,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from qtpy.QtGui import (
    QColor,
    QDesktopServices,
    QFont,
    QPalette,
    QStandardItem,
    QStandardItemModel,
)
from qtpy.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QFileIconProvider,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from . import manifest as m
from .build_monitor import SOURCE_EXTS
from .compat import file_manager_name, reveal_label
from .icons import (
    FORTRAN_INCLUDE_SUFFIXES,
    FORTRAN_SUFFIXES,
    badged_icon,
    fortran_file_icon,
)

PATH_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2    # "package", "group", "dir", "file", "placeholder"
LOADED_ROLE = Qt.UserRole + 3  # folder contents have been read
HIDDEN_ROLE = Qt.UserRole + 4  # why an entry is greyed out (empty: it isn't)
IGNORED_ROLE = Qt.UserRole + 6 # the .gitignore that ignores this entry (if any)
LINK_ROLE = Qt.UserRole + 5    # a folder that is listed as a package elsewhere

SKIP_DIRS = {"build", "__pycache__", "node_modules"}
SKIP_SUFFIXES = {".o", ".obj", ".mod", ".smod", ".a", ".so", ".dylib", ".dll", ".lib",
                 ".exe", ".pyc", ".gcda", ".gcno", ".DS_Store"}

FILE_COLORS = {"error": "#e03131", "warning": "#e8590c"}

# The folders fpm knows about: role -> (badge, tooltip).
ROLES = {
    "library": ("code", "Library sources"),
    "include": ("include", "Include files"),
    "app": ("run", "Programs (executables)"),
    "test": ("test", "Tests"),
    "example": ("example", "Example programs"),
}


def fpm_folders(manifest_path: Path) -> dict[str, str]:
    """Absolute folder -> role for the standard fpm folders of one package,
    following the manifest's source-dir / include-dir settings (defaults:
    src, include, app, test, example)."""
    root = manifest_path.parent
    try:
        data = m.load_toml(manifest_path)
    except Exception:  # noqa: BLE001 - an unreadable manifest just gets defaults
        data = {}
    roles: dict[str, str] = {}

    def add(folder, role):
        for f in folder if isinstance(folder, list) else [folder]:
            if isinstance(f, str) and f.strip():
                path = (root / f).resolve()
                if path.is_dir() and path != root.resolve():
                    roles.setdefault(str(path), role)

    library = data.get("library")
    library = library if isinstance(library, dict) else {}
    add(library.get("source-dir", "src"), "library")
    add(library.get("include-dir", "include"), "include")
    for section, role, default in (("executable", "app", "app"), ("test", "test", "test"),
                                   ("example", "example", "example")):
        targets = data.get(section)
        targets = targets if isinstance(targets, list) else []
        dirs = {t.get("source-dir", default) for t in targets if isinstance(t, dict)} or {default}
        for folder in dirs:
            add(folder, role)
    return roles


class GitIgnore:
    """git's ignore rules for one package: the .gitignore files from the root
    of its git repository (or the package folder, if it isn't in one) down to
    each folder, combined the way git does (deeper files win, "!" re-includes,
    and everything inside an ignored folder is ignored)."""

    def __init__(self, package_dir: Path):
        self.package_dir = package_dir.resolve()
        self.root = self._repo_root(self.package_dir)
        self._specs: dict[Path, object] = {}

    @staticmethod
    def _repo_root(start: Path) -> Path:
        home = Path.home().resolve()
        for folder in (start, *start.parents):
            if (folder / ".git").exists():
                return folder
            if folder == home or folder.parent == folder:
                break
        return start

    def _spec(self, folder: Path):
        """(spec, line number of each pattern) for folder/.gitignore, or None."""
        if folder not in self._specs:
            entry = None
            ignore_file = folder / ".gitignore"
            if ignore_file.is_file():
                try:
                    import pathspec
                    lines = ignore_file.read_text(errors="replace").splitlines()
                    spec = pathspec.GitIgnoreSpec.from_lines(lines)
                    # pathspec skips blank lines; map each pattern back to its line.
                    line_of, k = [], 0
                    for number, line in enumerate(lines, 1):
                        if k < len(spec.patterns) and spec.patterns[k].pattern == line:
                            line_of.append(number)
                            k += 1
                    entry = (spec, line_of)
                except (ImportError, OSError, ValueError):
                    entry = None
            self._specs[folder] = entry
        return self._specs[folder]

    def check(self, path: Path, is_dir: bool) -> str | None:
        """Where ``path`` is ignored ("…/.gitignore:LINE"), or None if it isn't."""
        path = path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError:
            return None
        # Folders from the repository root down to the entry's parent; the
        # deepest .gitignore with a matching rule decides.
        chain = [path.parent, *path.parent.parents]
        chain = [f for f in chain if f == self.root or self.root in f.parents]
        for folder in chain:  # deepest first
            entry = self._spec(folder)
            if entry is None:
                continue
            spec, line_of = entry
            rel = path.relative_to(folder).as_posix() + ("/" if is_dir else "")
            result = spec.check_file(rel)
            if result.include is not None:
                if not result.include:
                    return None  # re-included by a "!" rule
                line = line_of[result.index] if result.index is not None and result.index < len(line_of) else None
                return f"{folder / '.gitignore'}:{line}" if line else str(folder / ".gitignore")
        return None


def reveal_in_file_manager(path: str):
    """Show a file (selected) or folder in Finder / Explorer / the file manager."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
    else:
        folder = path if Path(path).is_dir() else str(Path(path).parent)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


# Keys that move the tree's selection: a file reached with them is previewed
# once the selection has rested this long.
NAVIGATION_KEYS = {Qt.Key_Up, Qt.Key_Down, Qt.Key_PageUp, Qt.Key_PageDown, Qt.Key_Home, Qt.Key_End}
KEY_PREVIEW_MS = 150


class _Filter(QSortFilterProxyModel):
    """Case-insensitive name filter that keeps the parents of matches.
    Files ignored by git and greyed-out entries only match when
    ``include_ignored`` is set (they are most of a big project's files)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.needle = ""
        self.include_ignored = False

    def set_filter(self, needle: str, include_ignored: bool):
        self.beginFilterChange()
        self.needle, self.include_ignored = needle.lower(), include_ignored
        self.endFilterChange(QSortFilterProxyModel.Direction.Rows)

    def filterAcceptsRow(self, row, parent):
        if not self.needle:
            return True
        item = self.sourceModel().itemFromIndex(self.sourceModel().index(row, 0, parent))
        if item is None or item.data(KIND_ROLE) != "file":
            return False  # folders are shown when something inside matches
        if not self.include_ignored and (item.data(IGNORED_ROLE) or item.data(HIDDEN_ROLE)):
            return False
        return self.needle in item.text().lower()


class ProjectTree(QWidget):
    """The file tree. Emits `open_requested` with a file path."""

    open_requested = Signal(str)
    compile_command_requested = Signal(str)
    preview_requested = Signal(str)  # a file clicked once: shown in a preview tab
    rename_requested = Signal(str)  # a file to rename (the window asks for the new name)
    delete_requested = Signal(str)  # a file to move to the trash (the window asks first)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project_dir: Path | None = None
        self.graph: m.DependencyGraph | None = None
        self._items: dict[str, QStandardItem] = {}  # path -> file/dir item
        self._marks: dict[str, str] = {}            # path -> "error" / "warning"
        self._icons = QFileIconProvider()
        self._folder_icon = self._icons.icon(QFileIconProvider.Folder)
        self._icon_cache: dict = {}
        self._skip_dirs: set[Path] = set()
        self._roles: dict[str, str] = {}  # folder -> fpm role
        self._ignores: dict[str, GitIgnore] = {}  # package folder -> its rules
        self._role_icons: dict = {}
        self._all_loaded: bool | None = None  # None, or whether ignored folders were loaded too

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter files…")
        self.filter_edit.setClearButtonEnabled(True)
        # Filter once typing pauses, not on every keystroke.
        self._filter_timer = QTimer(self)
        self._filter_timer.setSingleShot(True)
        self._filter_timer.setInterval(200)
        self._filter_timer.timeout.connect(lambda: self._apply_filter(self.filter_edit.text()))
        self.filter_edit.textChanged.connect(self._filter_timer.start)
        self.include_ignored = QCheckBox("Ignored")
        self.include_ignored.setToolTip("Also match files ignored by git and greyed-out files "
                                        "(hidden, compiled, build/) — slower on big projects")
        self.include_ignored.toggled.connect(lambda: self._apply_filter(self.filter_edit.text()))

        self.model = QStandardItemModel(self)
        self.proxy = _Filter(self)
        self.proxy.setSourceModel(self.model)
        self.view = QTreeView()
        self.view.setModel(self.proxy)
        self.view.setHeaderHidden(True)
        self.view.setUniformRowHeights(True)
        self.view.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.view.setContextMenuPolicy(Qt.CustomContextMenu)
        self.view.customContextMenuRequested.connect(self._context_menu)
        self.view.activated.connect(self._activated)  # double-click / Enter
        self.view.clicked.connect(self._clicked)      # one click: a preview
        # Moving through the files with the arrow keys previews them too, once
        # the selection rests (holding ↓ doesn't open every file on the way).
        # Only keys count: the app moves the selection itself too (revealing
        # the current tab's file, a refresh), and that mustn't open anything.
        self._key_moving = False
        self._key_preview_timer = QTimer(self)
        self._key_preview_timer.setSingleShot(True)
        self._key_preview_timer.setInterval(KEY_PREVIEW_MS)
        self._key_preview_timer.timeout.connect(lambda: self._clicked(self.view.currentIndex()))
        self.view.installEventFilter(self)
        self.view.selectionModel().currentChanged.connect(self._current_changed)
        self.view.expanded.connect(self._expanded)     # folders load when opened

        self.empty = QLabel("Open an fpm.toml to see its files.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)
        filter_row.addWidget(self.filter_edit, 1)
        filter_row.addWidget(self.include_ignored)
        layout.addLayout(filter_row)
        layout.addWidget(self.view, 1)
        layout.addWidget(self.empty)
        self._update_empty()

    # ------------------------------------------------------------- content
    def set_project(self, graph: m.DependencyGraph | None, project_dir: Path | None):
        new_project = project_dir != self.project_dir
        self.graph, self.project_dir = graph, project_dir
        if new_project:
            self._marks.clear()
        self.refresh(keep_state=not new_project)

    def refresh(self, keep_state: bool = True):
        """Rescan the folders (keeping open folders and the selection)."""
        expanded = self._expanded_paths() if keep_state else None
        selected = self._selected_path() if keep_state else None
        self.model.clear()
        self._items.clear()
        self._all_loaded = None
        if self.project_dir is None:
            self._update_empty()
            return
        packages = self._packages()
        package_dirs = {p.resolve() for _, p, _ in packages}
        # A folder that is another package (e.g. a path dependency inside the
        # project) is listed under that package, not inside this one.
        self._skip_dirs = package_dirs
        self._ignores = {str(d.resolve()): GitIgnore(d) for _, d, _ in packages}
        self._roles = {}
        for _, directory, _ in packages:
            manifest = directory / "fpm.toml"
            if manifest.is_file():
                self._roles.update(fpm_folders(manifest))
        root_item = None
        deps_group = None
        for name, directory, is_root in packages:
            item = self._package_item(name, directory, is_root)
            self._fill(item, directory)
            if is_root:
                self.model.appendRow(item)
                root_item = item
            else:
                if deps_group is None:
                    deps_group = QStandardItem("Dependencies")
                    deps_group.setData("group", KIND_ROLE)
                    deps_group.setIcon(self._icons.icon(QFileIconProvider.Folder))
                    font = QFont(deps_group.font())
                    font.setBold(True)
                    deps_group.setFont(font)
                    self.model.appendRow(deps_group)
                deps_group.appendRow(item)
        self._apply_marks()
        self._update_empty()
        if expanded is None:
            if root_item is not None:
                self.view.expand(self.proxy.mapFromSource(root_item.index()))
        else:
            self._restore_expanded(expanded)
        if selected:
            self.reveal(selected, expand=False)
        self._apply_filter(self.filter_edit.text())

    def _packages(self) -> list[tuple[str, Path, bool]]:
        """(name, folder, is_root) for the project and dependencies on disk."""
        out = []
        if self.graph is not None:
            root = self.graph.packages.get(self.graph.root)
            name = root.name if root else self.project_dir.name
        else:
            name = self.project_dir.name
        out.append((name, self.project_dir, True))
        if self.graph is not None:
            for pkg in self.graph.packages.values():
                if pkg.name == self.graph.root or pkg.kind in (m.META, m.MISSING):
                    continue
                if pkg.directory and pkg.directory.is_dir():
                    out.append((pkg.name, pkg.directory, False))
        return out

    def _package_item(self, name: str, directory: Path, is_root: bool) -> QStandardItem:
        item = QStandardItem(name)
        item.setData(str(directory), PATH_ROLE)
        item.setData("package", KIND_ROLE)
        item.setIcon(self._icons.icon(QFileIconProvider.Folder))
        item.setToolTip(str(directory))
        font = QFont(item.font())
        font.setBold(True)
        item.setFont(font)
        if not is_root:
            kind = self.graph.packages[name].kind if self.graph and name in self.graph.packages else ""
            item.setText(f"{name}  ({'local path' if kind == m.PATH else 'git'})" if kind else name)
        return item

    def _fill(self, parent: QStandardItem, directory: Path):
        """List one folder: subfolders first, then files. Subfolders are
        loaded when opened (see `_load`), so big trees stay fast."""
        parent.setData(str(directory), PATH_ROLE)
        self._load(parent)

    def _load(self, item: QStandardItem):
        """Fill a folder item with its contents (once)."""
        if item.data(LOADED_ROLE):
            return
        item.setData(True, LOADED_ROLE)
        item.removeRows(0, item.rowCount())  # the "loading" placeholder
        folder = Path(item.data(PATH_ROLE))
        try:
            entries = sorted(os.scandir(folder), key=lambda e: (not e.is_dir(), e.name.lower()))
        except OSError:
            return
        rules = self._rules_for(folder)
        parent_ignored = item.data(IGNORED_ROLE)  # inside an ignored folder
        for entry in entries:
            name = entry.name
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if path.resolve() in self._skip_dirs:
                    self._add_hidden(item, path, "dir",
                                     "A package of its own — listed under Dependencies "
                                     "(double-click to go there)", link=True)
                    continue
                if name.startswith(".") or name in SKIP_DIRS:
                    why = ("fpm's build output — see the Build outputs tab" if name == "build"
                           else "Hidden folder" if name.startswith(".") else "Generated folder")
                    self._add_hidden(item, path, "dir", why,
                                     ignored=parent_ignored or (rules.check(path, True) if rules else None))
                    continue
                child = QStandardItem(name)
                child.setData(str(path), PATH_ROLE)
                child.setData("dir", KIND_ROLE)
                self._set_ignored(child, path, True, rules, parent_ignored)
                role = self._roles.get(str(path.resolve()))
                if role:
                    child.setIcon(self._role_icon(role))
                    child.setToolTip(f"{ROLES[role][1]} — {path}")
                else:
                    child.setIcon(self._folder_icon)
                placeholder = QStandardItem("…")
                placeholder.setData("placeholder", KIND_ROLE)
                placeholder.setEnabled(False)
                child.appendRow(placeholder)  # gives the folder an expand arrow
                item.appendRow(child)
                self._items[str(path)] = child
            elif entry.is_file():
                hidden = ("Hidden file" if name.startswith(".") else
                          "Compiled / generated file"
                          if path.suffix in SKIP_SUFFIXES or path.suffix.lower() in SKIP_SUFFIXES else "")
                if hidden:
                    self._add_hidden(item, path, "file", hidden,
                                     ignored=parent_ignored or (rules.check(path, False) if rules else None))
                    continue
                child = QStandardItem(name)
                child.setData(str(path), PATH_ROLE)
                child.setData("file", KIND_ROLE)
                self._set_ignored(child, path, False, rules, parent_ignored)
                child.setIcon(self._manifest_icon() if name == "fpm.toml" else self._file_icon(path))
                item.appendRow(child)
                self._items[str(path)] = child
                self._mark_item(str(path), child)

    def _rules_for(self, folder: Path) -> GitIgnore | None:
        """The ignore rules of the package a folder belongs to."""
        resolved = folder.resolve()
        best = None
        for pkg, rules in self._ignores.items():
            pkg_path = Path(pkg)
            if (resolved == pkg_path or pkg_path in resolved.parents) and \
                    (best is None or len(pkg) > len(str(best.package_dir))):
                best = rules
        return best

    def _set_ignored(self, item: QStandardItem, path: Path, is_dir: bool, rules, parent_ignored):
        source = parent_ignored or (rules.check(path, is_dir) if rules else None)
        if source:
            item.setData(str(source), IGNORED_ROLE)
            item.setForeground(self._ignored_grey())
            item.setToolTip(f"Ignored by git: {source}\n{path}")

    def _ignored_grey(self) -> QColor:
        """Between normal text and the hidden-entry grey. The alpha is mixed
        too: macOS's dark palette uses white at different opacities for
        normal (~85%) and disabled (~25%) text, so mixing only RGB gave white."""
        pal = QApplication.palette()
        a, b = pal.color(QPalette.Text), pal.color(QPalette.Disabled, QPalette.Text)
        return QColor((a.red() + b.red()) // 2, (a.green() + b.green()) // 2,
                      (a.blue() + b.blue()) // 2, (a.alpha() + b.alpha()) // 2)

    def _add_hidden(self, parent: QStandardItem, path: Path, kind: str, why: str, link: bool = False,
                    ignored: str | None = None):
        """A greyed-out entry: listed, but folders can't be opened in the tree
        (double-click shows them in the file manager; files still open)."""
        child = QStandardItem(path.name)
        child.setData(str(path), PATH_ROLE)
        child.setData(kind, KIND_ROLE)
        child.setData(why, HIDDEN_ROLE)
        if link:
            child.setData(str(path.resolve()), LINK_ROLE)
        also = f"\nAlso ignored by git: {ignored}" if ignored else ""
        if ignored:
            child.setData(ignored, IGNORED_ROLE)
        if kind == "dir":
            child.setData(True, LOADED_ROLE)  # never filled, so no expand arrow
            child.setIcon(self._folder_icon)
            action = ("double-click to go there" if link
                      else f"double-click to show it in {file_manager_name()}")
            child.setToolTip(f"{why}{also}\n{path}" if link else f"{why} ({action}){also}\n{path}")
        else:
            child.setIcon(self._file_icon(path))
            child.setToolTip(f"{why}{also}\n{path}")
        child.setForeground(self._grey())
        parent.appendRow(child)
        self._items[str(path)] = child

    def _grey(self) -> QColor:
        return QApplication.palette().color(QPalette.Disabled, QPalette.Text)

    def _load_all(self, include_ignored: bool = False):
        """Load every folder (for filtering, which must see all files);
        folders ignored by git only if ``include_ignored``."""
        if self._all_loaded or self._all_loaded == include_ignored:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            stack = [self.model.item(r) for r in range(self.model.rowCount())]
            while stack:
                item = stack.pop()
                if item is None:
                    continue
                if item.data(KIND_ROLE) in ("package", "dir"):
                    if item.data(IGNORED_ROLE) and not include_ignored:
                        continue
                    self._load(item)
                for r in range(item.rowCount()):
                    stack.append(item.child(r))
            self._all_loaded = include_ignored
        finally:
            QApplication.restoreOverrideCursor()

    def _expanded(self, proxy_index):
        item = self.model.itemFromIndex(self.proxy.mapToSource(proxy_index))
        if item is not None and item.data(KIND_ROLE) in ("package", "dir"):
            self._load(item)

    def _role_icon(self, role: str):
        if role not in self._role_icons:
            self._role_icons[role] = badged_icon(self._folder_icon, ROLES[role][0])
        return self._role_icons[role]

    def _manifest_icon(self):
        if "manifest" not in self._role_icons:
            base = self._icons.icon(QFileIconProvider.File)
            self._role_icons["manifest"] = badged_icon(base, "manifest")
        return self._role_icons["manifest"]

    def icon_for(self, path: Path):
        """A file's icon, as in the tree (e.g. for the Search tab)."""
        return self._manifest_icon() if path.name == "fpm.toml" else self._file_icon(path)

    def _file_icon(self, path: Path):
        """Icons by file type (asking the OS once per type, not per file)."""
        key = path.suffix.lower() or path.name
        icon = self._icon_cache.get(key)
        if icon is None:
            icon = self._icon_cache[key] = (
                fortran_file_icon() if key in FORTRAN_SUFFIXES
                else fortran_file_icon(include=True) if key in FORTRAN_INCLUDE_SUFFIXES
                else self._icons.icon(QFileInfo(str(path))))
        return icon

    # -------------------------------------------------- build results marks
    def set_marks(self, marks: dict[str, str]):
        """Color files by the last build: {path: "error" | "warning"}."""
        self._marks = {str(Path(p).resolve()): v for p, v in marks.items()}
        self._apply_marks()

    def _apply_marks(self):
        for path, item in self._items.items():
            if item.data(KIND_ROLE) == "file" or item.data(HIDDEN_ROLE) or item.data(IGNORED_ROLE):
                self._mark_item(path, item)

    def _mark_item(self, path: str, item: QStandardItem):
        if item.data(HIDDEN_ROLE):
            item.setForeground(self._grey())  # greyed entries stay grey
            return
        mark = self._marks.get(str(Path(path).resolve())) if self._marks else None
        ignored = item.data(IGNORED_ROLE)
        if mark:
            item.setForeground(QColor(FILE_COLORS[mark]))
        elif ignored:
            item.setForeground(self._ignored_grey())
        else:
            item.setData(None, Qt.ForegroundRole)  # the normal text color
        tip = (f"Ignored by git: {ignored}\n" if ignored else "") + path + {
            "error": "\n(errors in the last build)",
            "warning": "\n(warnings in the last build)"}.get(mark, "")
        item.setToolTip(tip)

    # -------------------------------------------------------------- filter
    def _apply_filter(self, text: str):
        self._filter_timer.stop()
        needle = text.strip()
        include = self.include_ignored.isChecked()
        if needle:
            self._load_all(include)
            # Re-filtering a fully expanded tree of thousands of rows is slow
            # in QTreeView (seconds); everything is expanded again below.
            self.view.collapseAll()
        self.proxy.set_filter(needle, include)
        if needle:
            self.view.expandAll()
        self._update_empty()

    def _update_empty(self):
        has_rows = self.proxy.rowCount() > 0
        if self.project_dir is None:
            self.empty.setText("Open an fpm.toml to see its files.")
        elif not has_rows:
            if not self.filter_edit.text().strip():
                self.empty.setText("No files found.")
            elif self.include_ignored.isChecked():
                self.empty.setText("No files match the filter.")
            else:
                self.empty.setText("No files match the filter (tick Ignored to also "
                                   "search files ignored by git and greyed-out files).")
        self.empty.setVisible(not has_rows)
        self.view.setVisible(has_rows)

    # ---------------------------------------------------------- selection
    def reveal(self, path: str, expand: bool = True):
        """Select a file's row (e.g. the file in the current editor tab)."""
        item = self._items.get(str(path)) or self._items.get(str(Path(path).resolve()))
        if item is None:
            item = self._load_down_to(Path(path))
        if item is None:
            return
        index = self.proxy.mapFromSource(item.index())
        if not index.isValid():
            return
        if expand:
            parent = index.parent()
            while parent.isValid():
                self.view.expand(parent)
                parent = parent.parent()
        self.view.setCurrentIndex(index)
        self.view.scrollTo(index)

    def reveal_package(self, directory: Path) -> bool:
        """Select a package's row (the project, or one under Dependencies),
        open it, and scroll to it. False if it isn't in the tree."""
        target = str(Path(directory).resolve())
        stack = [self.model.item(r) for r in range(self.model.rowCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if item.data(KIND_ROLE) == "package" and str(Path(item.data(PATH_ROLE)).resolve()) == target:
                index = self.proxy.mapFromSource(item.index())
                if not index.isValid():
                    return False
                parent = index.parent()
                while parent.isValid():
                    self.view.expand(parent)
                    parent = parent.parent()
                self.view.expand(index)
                self.view.setCurrentIndex(index)
                self.view.scrollTo(index, QAbstractItemView.PositionAtTop)
                return True
            if item.data(KIND_ROLE) == "group":
                stack.extend(item.child(r) for r in range(item.rowCount()))
        return False

    def _load_down_to(self, path: Path) -> QStandardItem | None:
        """Load the folders between a package and ``path``; return its item."""
        target = path.resolve()
        packages = []
        stack = [self.model.item(r) for r in range(self.model.rowCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if item.data(KIND_ROLE) == "package":
                packages.append(item)
            elif item.data(KIND_ROLE) == "group":
                stack.extend(item.child(r) for r in range(item.rowCount()))
        for package in packages:
            root = Path(package.data(PATH_ROLE)).resolve()
            try:
                parts = target.relative_to(root).parts
            except ValueError:
                continue
            item = package
            for depth in range(len(parts)):
                self._load(item)
                wanted = str(root.joinpath(*parts[:depth + 1]))
                item = self._items.get(wanted)
                if item is None:
                    break
            else:
                return item
        return None

    def view_state(self) -> dict | None:
        """The open folders and the selected entry (to restore next time),
        or None while the tree is filtered (everything is expanded then)."""
        if self.filter_edit.text().strip():
            return None
        return {"expanded": sorted(self._expanded_paths()), "selected": self._selected_path()}

    def restore_view_state(self, state: dict):
        self._restore_expanded(set(state.get("expanded") or []))
        selected = state.get("selected")
        if selected and Path(selected).exists():
            self.reveal(selected, expand=False)

    def _selected_path(self) -> str | None:
        index = self.view.currentIndex()
        return index.data(PATH_ROLE) if index.isValid() else None

    def _expanded_paths(self) -> set[str]:
        paths = set()

        def visit(parent):
            for row in range(self.proxy.rowCount(parent)):
                index = self.proxy.index(row, 0, parent)
                if self.view.isExpanded(index):
                    paths.add(index.data(PATH_ROLE) or "group:" + str(index.data()))
                    visit(index)

        visit(self.view.rootIndex())
        return paths

    def _restore_expanded(self, paths: set[str]):
        def visit(parent):
            for row in range(self.proxy.rowCount(parent)):
                index = self.proxy.index(row, 0, parent)
                key = index.data(PATH_ROLE) or "group:" + str(index.data())
                if key in paths:
                    self.view.expand(index)
                    visit(index)

        visit(self.view.rootIndex())

    # ------------------------------------------------------------- actions
    def eventFilter(self, obj, event):
        if obj is self.view and event.type() == QEvent.KeyPress and event.key() in NAVIGATION_KEYS:
            self._key_moving = True  # (the view moves the selection now: see _current_changed)
            QTimer.singleShot(0, self, lambda: setattr(self, "_key_moving", False))
        return super().eventFilter(obj, event)

    def _current_changed(self, current, _previous):
        if self._key_moving:
            self._key_preview_timer.start()

    def _clicked(self, index):
        if index.data(KIND_ROLE) == "file" and not index.data(HIDDEN_ROLE) and index.data(PATH_ROLE):
            self.preview_requested.emit(index.data(PATH_ROLE))

    def _activated(self, index):
        path = index.data(PATH_ROLE)
        if index.data(KIND_ROLE) == "file":
            self.open_requested.emit(path)
        elif index.data(LINK_ROLE):
            self._go_to_package(index.data(LINK_ROLE))
        elif index.data(HIDDEN_ROLE) and path:
            reveal_in_file_manager(path)

    def _go_to_package(self, directory: str):
        """Select the Dependencies entry for a package folder."""
        stack = [self.model.item(r) for r in range(self.model.rowCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if item.data(KIND_ROLE) == "package" and \
                    str(Path(item.data(PATH_ROLE)).resolve()) == directory:
                index = self.proxy.mapFromSource(item.index())
                parent = index.parent()
                while parent.isValid():
                    self.view.expand(parent)
                    parent = parent.parent()
                self.view.setCurrentIndex(index)
                self.view.scrollTo(index)
                return
            stack.extend(item.child(r) for r in range(item.rowCount()))

    def _context_menu(self, pos):
        menu = self.context_menu(pos)
        menu.exec(self.view.viewport().mapToGlobal(pos))
        menu.deleteLater()

    def context_menu(self, pos) -> QMenu:
        """The right-click menu for the row at ``pos`` (viewport coordinates)."""
        index = self.view.indexAt(pos)
        path = index.data(PATH_ROLE) if index.isValid() else None
        is_file = bool(path) and index.data(KIND_ROLE) == "file"
        menu = QMenu(self)
        if is_file:
            menu.addAction("Open", lambda: self.open_requested.emit(path))
            menu.addAction("Open in Default App",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            if Path(path).suffix.lower() in SOURCE_EXTS:
                menu.addAction("Show Compile Command…",
                               lambda: self.compile_command_requested.emit(path))
        if path:
            menu.addAction(reveal_label(), lambda: reveal_in_file_manager(path))
            menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(path))
            menu.addSeparator()
        if is_file:
            menu.addAction("Rename…", lambda: self.rename_requested.emit(path))
            menu.addAction("Move to Trash…", lambda: self.delete_requested.emit(path))
            menu.addSeparator()
        menu.addAction("Refresh", self.refresh)
        menu.addAction("Collapse All", self.view.collapseAll)
        return menu
