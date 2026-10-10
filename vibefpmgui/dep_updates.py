"""Check git dependencies for newer versions (only when the user asks).

For each git dependency the remote is asked for its tags and branches with
``git ls-remote`` (nothing is downloaded), and compared with what the project
requests (tag / branch / rev / nothing) and the commit fpm checked out
(``build/cache.toml``). A dependency pinned to an older tag can be moved to
the newest tag: only that tag value in the declaring ``fpm.toml`` is changed.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from qtpy.QtCore import QObject, Qt, QThread, QUrl, Signal, Slot
from qtpy.QtGui import QColor, QDesktopServices
from qtpy.QtWidgets import (
    QAbstractItemView, QDialog, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import manifest as m

PRERELEASE_RE = re.compile(r"(alpha|beta|rc|dev|pre|preview|snapshot|nightly)", re.IGNORECASE)
LS_REMOTE_TIMEOUT = 30  # seconds per repository

# Status kinds (also used for colours)
UP_TO_DATE, UPDATE, NEW_COMMITS, INFO, ERROR = "up to date", "update", "new commits", "info", "error"
TAG_AVAILABLE = "tag available"  # pinned to a commit; a tag could be used instead
STATUS_COLORS = {UP_TO_DATE: "#2f9e44", UPDATE: "#e8590c", NEW_COMMITS: "#1c7ed6",
                 TAG_AVAILABLE: "#7048e8", INFO: "#868e96", ERROR: "#e03131"}


@dataclass
class DepCheck:
    name: str
    git: str
    ref_kind: str = ""          # "tag", "branch", "rev" or "" (default branch)
    ref: str = ""
    current: str = ""           # commit fpm checked out (build/cache.toml)
    declared_in: Path | None = None
    editable: bool = False      # the declaring fpm.toml is the user's (root / path dependency)
    latest_tag: str = ""
    latest_tag_commit: str = ""
    branch_commit: str = ""     # newest commit of the requested (or default) branch
    status: str = ""
    message: str = ""

    @property
    def requested(self) -> str:
        if self.ref_kind == "rev":
            return f"rev {self.ref[:7]}"
        return f"{self.ref_kind} {self.ref}" if self.ref_kind else "default branch"


# ------------------------------------------------------------------ collect
def git_dependencies(graph: m.DependencyGraph) -> list[DepCheck]:
    """Every git dependency, with how it is requested and where."""
    checks = []
    for pkg in graph.packages.values():
        if not pkg.git or pkg.kind == m.ROOT:
            continue
        check = DepCheck(pkg.name, pkg.git, current=pkg.git_rev)
        # Prefer a declaration in a manifest the user can edit.
        parents = [graph.packages[e.parent] for e in graph.parents(pkg.name) if e.parent in graph.packages]
        parents.sort(key=lambda p: p.kind not in (m.ROOT, m.PATH))
        for parent in parents:
            spec = _declared_spec(parent.manifest, pkg.name) if parent.manifest else None
            if spec is not None:
                check.declared_in = parent.manifest
                check.editable = parent.kind in (m.ROOT, m.PATH)
                check.git = str(spec.get("git", check.git))
                for key in ("tag", "branch", "rev"):
                    if key in spec:
                        check.ref_kind, check.ref = key, str(spec[key])
                        break
                break
        checks.append(check)
    return sorted(checks, key=lambda c: (not c.editable, c.name.lower()))


def _declared_spec(manifest: Path, name: str) -> dict | None:
    try:
        data = m.load_toml(manifest)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    for section in ("dependencies", "dev-dependencies"):
        spec = (data.get(section) or {}).get(name)
        if isinstance(spec, dict):
            return spec
    return None


# -------------------------------------------------------------------- check
def ls_remote(url: str) -> dict[str, str]:
    """{ref: commit} for HEAD, branches and tags (annotated tags resolved
    to their commit). Raises RuntimeError with git's message on failure."""
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")  # never wait for a password
    try:
        out = subprocess.run(["git", "ls-remote", url, "HEAD", "refs/heads/*", "refs/tags/*"],
                             capture_output=True, text=True, timeout=LS_REMOTE_TIMEOUT, env=env)
    except FileNotFoundError:
        raise RuntimeError("git was not found on PATH")
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"no answer from the server within {LS_REMOTE_TIMEOUT} s")
    if out.returncode != 0:
        lines = (out.stderr or "").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"git ls-remote failed ({out.returncode})")
    refs: dict[str, str] = {}
    for line in out.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        if ref.endswith("^{}"):
            refs[ref[:-3]] = sha  # the commit an annotated tag points to
        else:
            refs.setdefault(ref, sha)
    return refs


def version_key(tag: str) -> tuple:
    """Sort key for tags like v1.2.3 / 1.2 / release-2.0.1."""
    numbers = tuple(int(n) for n in re.findall(r"\d+", tag))
    return (numbers, not PRERELEASE_RE.search(tag), tag)


def newest_tag(tags: list[str], allow_prerelease: bool = False) -> str:
    candidates = [t for t in tags if re.search(r"\d", t)
                  and (allow_prerelease or not PRERELEASE_RE.search(t))]
    return max(candidates, key=version_key) if candidates else ""


def check_dependency(check: DepCheck) -> DepCheck:
    try:
        refs = ls_remote(check.git)
    except RuntimeError as exc:
        check.status, check.message = ERROR, f"Could not reach the repository: {exc}"
        return check
    tags = {ref[len("refs/tags/"):]: sha for ref, sha in refs.items() if ref.startswith("refs/tags/")}
    allow_pre = check.ref_kind == "tag" and bool(PRERELEASE_RE.search(check.ref))
    check.latest_tag = newest_tag(list(tags), allow_pre)
    check.latest_tag_commit = tags.get(check.latest_tag, "")
    if check.ref_kind == "branch":
        check.branch_commit = refs.get(f"refs/heads/{check.ref}", "")
    else:
        check.branch_commit = refs.get("HEAD", "")
    current = check.current

    if check.ref_kind == "tag":
        if check.ref not in tags:
            check.status, check.message = ERROR, f"Tag {check.ref} no longer exists in the repository"
        elif check.latest_tag and version_key(check.latest_tag) > version_key(check.ref):
            check.status = UPDATE
            check.message = f"Newer tag available: {check.ref} → {check.latest_tag}"
        else:
            check.status, check.message = UP_TO_DATE, f"{check.ref} is the newest tag"
    elif check.ref_kind == "rev":
        pinned = check.ref
        if not check.latest_tag:
            check.status, check.message = INFO, "Pinned to a commit (the repository has no tags)"
        elif check.latest_tag_commit and (check.latest_tag_commit.startswith(pinned)
                                          or pinned.startswith(check.latest_tag_commit)):
            check.status = TAG_AVAILABLE
            check.message = f"Pinned to commit {pinned[:7]}, which is tag {check.latest_tag}"
        else:
            check.status = TAG_AVAILABLE
            check.message = (f"Pinned to a commit; the newest tag is {check.latest_tag} "
                             "(Compare shows whether it is newer)")
    else:  # a branch, or the default branch
        where = f"branch {check.ref}" if check.ref_kind == "branch" else "the default branch"
        if not check.branch_commit:
            check.status, check.message = ERROR, f"Could not find {where} in the repository"
        elif not current:
            check.status, check.message = INFO, "Not downloaded yet — Update deps fetches it"
        elif check.branch_commit.startswith(current) or current.startswith(check.branch_commit):
            check.status, check.message = UP_TO_DATE, f"Up to date with {where}"
        else:
            check.status = NEW_COMMITS
            check.message = f"{where.capitalize()} has new commits — Update deps fetches them"
    return check


# --------------------------------------------------------------- edit tags
def set_tag(manifest: Path, name: str, old: str, new: str) -> bool:
    """Change ``tag = "old"`` to ``tag = "new"`` for one dependency."""
    return set_ref(manifest, name, "tag", old, "tag", new)


def set_ref(manifest: Path, name: str, old_key: str, old: str, new_key: str, new: str) -> bool:
    """Change ``old_key = "old"`` to ``new_key = "new"`` (e.g. a ``rev`` pin
    to a ``tag``) for one dependency, leaving everything else in the file
    (comments, layout) untouched. Returns False if the declaration couldn't
    be found unambiguously."""
    text = manifest.read_text()
    tag_re = re.compile(r"(\b)" + re.escape(old_key) + r"""(\s*=\s*)(["'])""" + re.escape(old) + r"""\3""")
    lines = text.split("\n")
    target = None
    # 1. Inline table:  name = { git = "...", tag = "old" }
    key = re.compile(r"""^\s*(?:["']?)""" + re.escape(name) + r"""(?:["']?)\s*=\s*\{""")
    for i, line in enumerate(lines):
        if key.match(line) and tag_re.search(line):
            target = i
            break
    # 2. Table form:  [dependencies.name] ... tag = "old"
    if target is None:
        header = re.compile(r"^\s*\[\s*(?:dev-)?dependencies\.(?:[\"']?)" + re.escape(name)
                            + r"(?:[\"']?)\s*\]")
        for i, line in enumerate(lines):
            if header.match(line):
                for j in range(i + 1, len(lines)):
                    if lines[j].lstrip().startswith("["):
                        break
                    if tag_re.search(lines[j]):
                        target = j
                        break
                break
    if target is None:
        return False
    lines[target] = tag_re.sub(lambda mt: f"{mt.group(1)}{new_key}{mt.group(2)}{mt.group(3)}{new}{mt.group(3)}",
                               lines[target], count=1)
    updated = "\n".join(lines)
    # Make sure the result parses and says what we meant.
    try:
        data = tomllib.loads(updated)
    except tomllib.TOMLDecodeError:
        return False
    spec = (data.get("dependencies") or {}).get(name) or (data.get("dev-dependencies") or {}).get(name)
    if not isinstance(spec, dict) or spec.get(new_key) != new or (old_key != new_key and old_key in spec):
        return False
    manifest.write_text(updated)
    return True


def read_cache_revs(project_dir: Path) -> dict[str, str]:
    """{dependency: commit} for the downloaded git dependencies, from each
    checkout itself (build/cache.toml's "rev" isn't updated by fpm update)."""
    revs: dict[str, str] = {}
    folder = project_dir / "build" / "dependencies"
    if folder.is_dir():
        for dep in folder.iterdir():
            commit = m.checked_out_commit(dep)
            if commit:
                revs[dep.name] = commit
    return revs


# ------------------------------------------------------------------ worker
class CheckWorker(QObject):
    result = Signal(int, object)   # row, DepCheck
    finished = Signal()

    def __init__(self, checks: list[DepCheck]):
        super().__init__()
        self.checks = checks

    @Slot()
    def run(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(check_dependency, c): i for i, c in enumerate(self.checks)}
            for future in as_completed(futures):
                self.result.emit(futures[future], future.result())
        self.finished.emit()


# ------------------------------------------------------------------ dialog
class UpdatesDialog(QDialog):
    """Results of a dependency check, with actions per row."""

    update_tag_requested = Signal(object)   # DepCheck
    update_deps_requested = Signal()

    COLUMNS = ["Dependency", "Requested", "Current", "Newest", "Status", ""]

    def __init__(self, checks: list[DepCheck], web_url, commit_url, compare_url, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Dependency updates")
        self.checks = checks
        self._web_url, self._commit_url, self._compare_url = web_url, commit_url, compare_url
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        note = QLabel("<span style='color:gray'>Asks each repository for its tags and branches "
                      "(<code>git ls-remote</code>); nothing is downloaded and no code is sent.</span>")
        note.setWordWrap(True)
        self.table = QTableWidget(len(checks), len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        # Compact: one line per row (long text is elided; tooltips have it all),
        # fixed row height, no grid, alternating shading.
        self.table.setWordWrap(False)
        self.table.setTextElideMode(Qt.ElideRight)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        self.row_height = self.table.fontMetrics().height() + 12
        self.table.verticalHeader().setSectionResizeMode(QHeaderView.Fixed)
        self.table.verticalHeader().setDefaultSectionSize(self.row_height)
        hh = self.table.horizontalHeader()
        for col in range(len(self.COLUMNS) - 1):
            hh.setSectionResizeMode(col, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        for row, c in enumerate(checks):
            self._set_row(row, c, checking=True)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        self.update_all = QPushButton("Update deps")
        self.update_all.setToolTip("Run fpm update (fetches new commits of branch dependencies)")
        self.update_all.clicked.connect(self.update_deps_requested)
        self.update_all.hide()
        buttons = QHBoxLayout()
        buttons.addWidget(self.update_all)
        buttons.addStretch(1)
        buttons.addWidget(close)
        layout = QVBoxLayout(self)
        layout.addWidget(self.summary)
        layout.addWidget(note)
        layout.addWidget(self.table, 1)
        layout.addLayout(buttons)
        header = self.table.horizontalHeader().sizeHint().height()
        self.resize(1120, 150 + header + self.row_height * max(3, min(len(checks), 12)))
        self._done = 0
        self._update_summary()

    def _set_row(self, row: int, c: DepCheck, checking: bool = False):
        def cell(col, text, tip="", color=None):
            item = QTableWidgetItem(text)
            if tip:
                item.setToolTip(tip)
            if color:
                item.setForeground(QColor(color))
            self.table.setItem(row, col, item)

        where = "" if c.declared_in is None else f"declared in {c.declared_in}"
        cell(0, c.name + ("" if c.editable else "  (indirect)"), f"{c.git}\n{where}")
        cell(1, c.requested, f"{c.ref_kind} {c.ref}" if c.ref_kind else "the repository's default branch")
        cell(2, c.current[:7] if c.current else "—", c.current)
        if checking:
            cell(3, "…")
            cell(4, "checking…", color="#868e96")
            return
        newest = c.latest_tag or "—"
        if c.ref_kind in ("branch", "") and c.branch_commit:
            newest = f"{c.branch_commit[:7]}" + (f"  (tag {c.latest_tag})" if c.latest_tag else "")
        cell(3, newest, c.latest_tag_commit or c.branch_commit)
        cell(4, c.message, c.message, STATUS_COLORS.get(c.status))
        actions = QWidget()
        row_layout = QHBoxLayout(actions)
        row_layout.setContentsMargins(4, 0, 4, 0)
        row_layout.setSpacing(4)
        new_commit = c.latest_tag_commit if c.status in (UPDATE, TAG_AVAILABLE) else c.branch_commit
        compare = self._compare_url(c.git, c.current, new_commit) if c.current and new_commit else None
        if c.status == UPDATE and c.editable:
            button = self._small_button(f"Update to {c.latest_tag}")
            button.setToolTip(f"Change tag = \"{c.ref}\" to \"{c.latest_tag}\" in {c.declared_in}")
            button.clicked.connect(lambda _=False, ch=c: self.update_tag_requested.emit(ch))
            row_layout.addWidget(button)
        elif c.status == TAG_AVAILABLE and c.editable:
            same = c.latest_tag_commit and (c.latest_tag_commit.startswith(c.ref) or c.ref.startswith(c.latest_tag_commit))
            button = self._small_button(f"{'Use' if same else 'Switch to'} tag {c.latest_tag}")
            button.setToolTip(f"Replace rev = \"{c.ref[:12]}…\" with tag = \"{c.latest_tag}\" in {c.declared_in}"
                              + ("" if same else "\n(Compare first: the tag may be older or newer than the pinned commit)"))
            button.clicked.connect(lambda _=False, ch=c: self.update_tag_requested.emit(ch))
            row_layout.addWidget(button)
        elif c.status in (UPDATE, TAG_AVAILABLE):
            note = QLabel("<span style='color:gray'>declared by another dependency</span>")
            row_layout.addWidget(note)
        if compare and c.status in (UPDATE, NEW_COMMITS) or (
                compare and c.status == TAG_AVAILABLE and not c.latest_tag_commit.startswith(c.current[:7])):
            link = self._small_button("Compare")
            link.setToolTip(compare)
            link.clicked.connect(lambda _=False, u=compare: QDesktopServices.openUrl(QUrl(u)))
            row_layout.addWidget(link)
        row_layout.addStretch(1)  # buttons keep their natural width
        self.table.setCellWidget(row, 5, actions)

    def _small_button(self, text: str) -> QPushButton:
        """A button that fits a compact row (macOS buttons are tall by default)."""
        button = QPushButton(text)
        button.setAttribute(Qt.WA_MacSmallSize)
        button.setAttribute(Qt.WA_LayoutUsesWidgetRect)
        button.setFixedHeight(self.row_height - 4)
        return button

    def show_result(self, row: int, check: DepCheck):
        self.checks[row] = check
        self._set_row(row, check)
        self._done += 1
        self.table.resizeColumnsToContents()
        # Size the buttons column to its widest row (cell widgets aren't
        # counted by resizeColumnsToContents); Status takes the rest.
        widest = max((w.sizeHint().width() for w in
                      (self.table.cellWidget(r, 5) for r in range(self.table.rowCount())) if w), default=0)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(5, QHeaderView.Fixed)
        self.table.setColumnWidth(5, widest + 8)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self._update_summary()

    def _update_summary(self):
        total = len(self.checks)
        if not total:
            self.summary.setText("<b>This project has no git dependencies.</b>")
            return
        if self._done < total:
            self.summary.setText(f"<b>Checking {total} git dependenc{'ies' if total != 1 else 'y'}…</b>"
                                 f" ({self._done} done)")
            return
        updates = sum(c.status == UPDATE for c in self.checks)
        commits = sum(c.status == NEW_COMMITS for c in self.checks)
        pinned = sum(c.status == TAG_AVAILABLE for c in self.checks)
        errors = sum(c.status == ERROR for c in self.checks)
        parts = []
        if updates:
            parts.append(f"{updates} with a newer tag")
        if commits:
            parts.append(f"{commits} with new commits")
        if pinned:
            parts.append(f"{pinned} pinned to a commit with a tag available")
        if errors:
            parts.append(f"{errors} could not be checked")
        self.summary.setText("<b>" + (", ".join(parts) if parts else "Everything is up to date") + "</b>")
        self.update_all.setVisible(commits > 0)


def start_check(checks: list[DepCheck], dialog: UpdatesDialog, parent: QObject) -> QThread:
    thread = QThread(parent)
    worker = CheckWorker(checks)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.result.connect(dialog.show_result)
    worker.finished.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread._worker = worker  # keep a reference
    thread.start()
    return thread
