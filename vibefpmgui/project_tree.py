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

from qtpy.QtCore import QFileInfo, QSortFilterProxyModel, Qt, QUrl, Signal
from qtpy.QtGui import QColor, QDesktopServices, QFont, QStandardItem, QStandardItemModel
from qtpy.QtWidgets import (
    QAbstractItemView, QApplication, QFileIconProvider, QLabel, QLineEdit, QMenu, QTreeView,
    QVBoxLayout, QWidget,
)

from . import manifest as m
from .build_monitor import SOURCE_EXTS
from .icons import badged_icon

PATH_ROLE = Qt.UserRole + 1
KIND_ROLE = Qt.UserRole + 2    # "package", "group", "dir", "file", "placeholder"
LOADED_ROLE = Qt.UserRole + 3  # folder contents have been read

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


def reveal_in_file_manager(path: str):
    """Show a file (selected) or folder in Finder / Explorer / the file manager."""
    if sys.platform == "darwin":
        subprocess.run(["open", "-R", path], check=False)
    elif sys.platform == "win32":
        subprocess.run(["explorer", "/select,", os.path.normpath(path)], check=False)
    else:
        folder = path if Path(path).is_dir() else str(Path(path).parent)
        QDesktopServices.openUrl(QUrl.fromLocalFile(folder))


class _Filter(QSortFilterProxyModel):
    """Case-insensitive name filter that keeps the parents of matches."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRecursiveFilteringEnabled(True)
        self.setFilterCaseSensitivity(Qt.CaseInsensitive)

    def filterAcceptsRow(self, row, parent):
        pattern = self.filterRegularExpression().pattern()
        if not pattern:
            return True
        index = self.sourceModel().index(row, 0, parent)
        if self.sourceModel().data(index, KIND_ROLE) != "file":
            return False  # folders are shown when something inside matches
        return super().filterAcceptsRow(row, parent)


class ProjectTree(QWidget):
    """The file tree. Emits :attr:`open_requested` with a file path."""

    open_requested = Signal(str)
    compile_command_requested = Signal(str)

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
        self._role_icons: dict = {}
        self._all_loaded = False

        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("Filter files…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)

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
        self.view.expanded.connect(self._expanded)     # folders load when opened

        self.empty = QLabel("Open an fpm.toml to see its files.")
        self.empty.setWordWrap(True)
        self.empty.setStyleSheet("color: gray;")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(self.filter_edit)
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
        self._all_loaded = False
        if self.project_dir is None:
            self._update_empty()
            return
        packages = self._packages()
        package_dirs = {p.resolve() for _, p, _ in packages}
        # A folder that is another package (e.g. a path dependency inside the
        # project) is listed under that package, not inside this one.
        self._skip_dirs = package_dirs
        self._roles = {}
        for _, directory, _ in packages:
            manifest = directory / "fpm.toml"
            if manifest.is_file():
                self._roles.update(fpm_folders(manifest))
        root_item = None
        deps_group = None
        for name, directory, is_root in packages:
            item = self._package_item(name, directory, is_root)
            self._fill(item, directory, package_dirs - {directory.resolve()})
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

    def _fill(self, parent: QStandardItem, directory: Path, other_packages: set[Path]):
        """List one folder: subfolders first, then files. Subfolders are
        loaded when opened (see :meth:`_load`), so big trees stay fast."""
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
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue
            path = Path(entry.path)
            if entry.is_dir(follow_symlinks=False):
                if name in SKIP_DIRS or path.resolve() in self._skip_dirs:
                    continue
                child = QStandardItem(name)
                child.setData(str(path), PATH_ROLE)
                child.setData("dir", KIND_ROLE)
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
                if path.suffix in SKIP_SUFFIXES or path.suffix.lower() in SKIP_SUFFIXES:
                    continue
                child = QStandardItem(name)
                child.setData(str(path), PATH_ROLE)
                child.setData("file", KIND_ROLE)
                child.setIcon(self._manifest_icon() if name == "fpm.toml" else self._file_icon(path))
                item.appendRow(child)
                self._items[str(path)] = child
                self._mark_item(str(path), child)

    def _load_all(self):
        """Load every folder (for filtering, which must see all files)."""
        if self._all_loaded:
            return
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            stack = [self.model.item(r) for r in range(self.model.rowCount())]
            while stack:
                item = stack.pop()
                if item is None:
                    continue
                if item.data(KIND_ROLE) in ("package", "dir"):
                    self._load(item)
                for r in range(item.rowCount()):
                    stack.append(item.child(r))
            self._all_loaded = True
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

    def _file_icon(self, path: Path):
        """Icons by file type (asking the OS once per type, not per file)."""
        key = path.suffix.lower() or path.name
        icon = self._icon_cache.get(key)
        if icon is None:
            icon = self._icon_cache[key] = self._icons.icon(QFileInfo(str(path)))
        return icon

    # -------------------------------------------------- build results marks
    def set_marks(self, marks: dict[str, str]):
        """Colour files by the last build: {path: "error" | "warning"}."""
        self._marks = {str(Path(p).resolve()): v for p, v in marks.items()}
        self._apply_marks()

    def _apply_marks(self):
        for path, item in self._items.items():
            if item.data(KIND_ROLE) == "file":
                self._mark_item(path, item)

    def _mark_item(self, path: str, item: QStandardItem):
        mark = self._marks.get(str(Path(path).resolve())) if self._marks else None
        if mark:
            item.setForeground(QColor(FILE_COLORS[mark]))
        else:
            item.setData(None, Qt.ForegroundRole)  # the normal text colour
        tip = path + {"error": "\n(errors in the last build)",
                      "warning": "\n(warnings in the last build)"}.get(mark, "")
        item.setToolTip(tip)

    # -------------------------------------------------------------- filter
    def _apply_filter(self, text: str):
        if text.strip():
            self._load_all()
        self.proxy.setFilterFixedString(text.strip())
        if text.strip():
            self.view.expandAll()
        self._update_empty()

    def _update_empty(self):
        has_rows = self.proxy.rowCount() > 0
        if self.project_dir is None:
            self.empty.setText("Open an fpm.toml to see its files.")
        elif not has_rows:
            self.empty.setText("No files match the filter." if self.filter_edit.text().strip()
                               else "No files found.")
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
    def _activated(self, index):
        if index.data(KIND_ROLE) == "file":
            self.open_requested.emit(index.data(PATH_ROLE))

    def _context_menu(self, pos):
        index = self.view.indexAt(pos)
        path = index.data(PATH_ROLE) if index.isValid() else None
        menu = QMenu(self)
        if path and index.data(KIND_ROLE) == "file":
            menu.addAction("Open", lambda: self.open_requested.emit(path))
            menu.addAction("Open in Default App",
                           lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(path)))
            if Path(path).suffix.lower() in SOURCE_EXTS:
                menu.addAction("Show Compile Command…",
                               lambda: self.compile_command_requested.emit(path))
        if path:
            label = {"darwin": "Show in Finder", "win32": "Show in Explorer"}.get(
                sys.platform, "Open Containing Folder")
            menu.addAction(label, lambda: reveal_in_file_manager(path))
            menu.addAction("Copy Path", lambda: QApplication.clipboard().setText(path))
            menu.addSeparator()
        menu.addAction("Refresh", self.refresh)
        menu.addAction("Collapse All", self.view.collapseAll)
        menu.exec(self.view.viewport().mapToGlobal(pos))
