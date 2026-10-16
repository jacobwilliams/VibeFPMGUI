"""Git: a project's repository's status, and the two versions of a changed
file to compare (with pygit2, libgit2's Python bindings: no git process,
and no command line built from what's in the repository).

No Qt: `vibefpmgui.git_panel` shows it, `vibefpmgui.window.git` reads it on
a pool thread, in a process of its own (`read_status_apart`: pygit2 holds
Python's lock while libgit2 reads the status, so the window would wait for
it all the same; seconds for a big project on Windows).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .source_text import decode

# Where a change is: what `git status` calls "Changes to be committed",
# "Changes not staged for commit", "Untracked files" and "Unmerged paths".
STAGED, CHANGED, UNTRACKED, CONFLICT = "staged", "changed", "untracked", "conflict"
AREAS = (CONFLICT, STAGED, CHANGED, UNTRACKED)  # (the order they're listed in)
AREA_TITLES = {CONFLICT: "Conflicts", STAGED: "Staged Changes", CHANGED: "Changes", UNTRACKED: "Untracked"}

# What happened to the file, and its letter (as in `git status --short`).
LETTERS = {"modified": "M", "added": "A", "deleted": "D", "renamed": "R", "typechange": "T",
           "untracked": "U", "conflict": "!"}
KIND_WORDS = {"modified": "modified", "added": "added", "deleted": "deleted", "renamed": "renamed",
              "typechange": "type changed", "untracked": "untracked", "conflict": "conflict"}

MAX_DIFF_BYTES = 4 * 1024 * 1024  # (a bigger version isn't shown: see versions)
BINARY = "\0binary"  # versions() marker: the file isn't text


def available() -> bool:
    """Whether pygit2 can be imported (the Git panel says so otherwise)."""
    try:
        import pygit2  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass(frozen=True)
class Change:
    path: str           # relative to the repository's top folder, with "/"
    area: str           # STAGED, CHANGED, UNTRACKED or CONFLICT
    kind: str           # a key of LETTERS

    @property
    def letter(self) -> str:
        return LETTERS[self.kind]


@dataclass
class RepoStatus:
    root: Path                  # the repository's top folder (its work tree)
    branch: str | None = None   # None: detached HEAD (see head)
    head: str | None = None     # the commit checked out (short), None before the first commit
    upstream: str | None = None  # e.g. "origin/main"
    ahead: int = 0              # commits here that the upstream doesn't have
    behind: int = 0             # and the other way around
    changes: list[Change] = field(default_factory=list)

    def in_area(self, area: str) -> list[Change]:
        return [c for c in self.changes if c.area == area]

    def summary(self) -> str:
        """ "main → origin/main · 2 ahead, 1 behind" and the like."""
        if self.branch is None:
            where = f"detached at {self.head}" if self.head else "detached HEAD"
        else:
            where = self.branch + ("" if self.head else " (no commits yet)")
        if self.upstream:
            where += f" → {self.upstream}"
            counts = [f"{n} {word}" for n, word in ((self.ahead, "ahead"), (self.behind, "behind")) if n]
            where += f" · {', '.join(counts) if counts else 'up to date'}"
        return where


def find_repository(path: Path) -> Path | None:
    """The top folder of the git repository ``path`` is in, or None (not in
    one, a bare one, or pygit2 missing)."""
    try:
        import pygit2
    except ImportError:
        return None
    found = pygit2.discover_repository(str(path))
    if not found:
        return None
    workdir = pygit2.Repository(found).workdir
    return Path(workdir).resolve() if workdir else None


def _kind(flags, prefix: str) -> str | None:
    from pygit2.enums import FileStatus

    for name, kind in (("NEW", "added"), ("MODIFIED", "modified"), ("DELETED", "deleted"),
                       ("RENAMED", "renamed"), ("TYPECHANGE", "typechange")):
        if flags & getattr(FileStatus, f"{prefix}_{name}"):
            return kind
    return None


def read_status(path: Path) -> RepoStatus | None:
    """The status of the repository ``path`` is in (None: not in one). A file
    can be in two areas: some of its changes staged, some not."""
    import pygit2
    from pygit2.enums import FileStatus

    root = find_repository(path)
    if root is None:
        return None
    repo = pygit2.Repository(str(root))
    status = RepoStatus(root)
    if not repo.head_is_unborn:
        status.head = str(repo.head.target)[:7]
    if repo.head_is_detached:
        status.branch = None
    elif repo.head_is_unborn:
        status.branch = repo.references["HEAD"].target.removeprefix("refs/heads/")
    else:
        status.branch = repo.head.shorthand
        branch = repo.branches.local.get(status.branch)
        upstream = branch.upstream if branch is not None else None
        if upstream is not None:
            status.upstream = upstream.shorthand
            status.ahead, status.behind = repo.ahead_behind(repo.head.target, upstream.target)
    for file, flags in sorted(repo.status().items()):
        flags = FileStatus(flags)
        if flags & FileStatus.CONFLICTED:
            status.changes.append(Change(file, CONFLICT, "conflict"))
            continue
        staged = _kind(flags, "INDEX")
        if staged:
            status.changes.append(Change(file, STAGED, staged))
        if flags & FileStatus.WT_NEW:  # (also a file whose deletion is staged, still on disk)
            status.changes.append(Change(file, UNTRACKED, "untracked"))
            continue
        changed = _kind(flags, "WT")
        if changed:
            status.changes.append(Change(file, CHANGED, changed))
    return status


def _text(data: bytes | None) -> str | None:
    if data is None:
        return None
    if len(data) > MAX_DIFF_BYTES or b"\0" in data[:8192]:
        return BINARY
    return decode(data)[0].replace("\r\n", "\n")


def versions(root: Path, change: Change) -> tuple[str | None, str | None, str, str]:
    """(old text, new text, old label, new label) of a change: what `git diff`
    compares. Staged: the last commit and the index; not staged: the index
    and the file; untracked: nothing and the file; a conflict: the last
    commit and the file (with its conflict markers). None: there's no such
    version (added, deleted); BINARY: it isn't text, or it's huge."""
    import pygit2

    repo = pygit2.Repository(str(root))

    def committed() -> bytes | None:
        if repo.head_is_unborn:
            return None
        try:
            return repo[repo.revparse_single("HEAD").tree[change.path].id].data
        except KeyError:
            return None

    def indexed() -> bytes | None:
        try:
            return repo[repo.index[change.path].id].data
        except KeyError:
            return None

    def on_disk() -> bytes | None:
        file = root / change.path
        try:
            return file.read_bytes() if file.is_file() else None
        except OSError:
            return None

    if change.area == STAGED:
        old, new, labels = committed(), indexed(), ("last commit", "staged")
    elif change.area == CHANGED:
        old, new, labels = indexed(), on_disk(), ("staged", "working tree")
    elif change.area == UNTRACKED:
        old, new, labels = None, on_disk(), ("", "working tree")
    else:
        old, new, labels = committed(), on_disk(), ("last commit", "working tree")
    return _text(old), _text(new), *labels


# ------------------------------------------------------------------ apart
def read_status_apart(path: Path, timeout: float = 120) -> RepoStatus | None:
    """`read_status` in a Python process of its own (run with ``-m``): while
    it reads, this process only waits, without Python's lock, so the window
    goes on. Raises RuntimeError with its message if it fails."""
    env = dict(os.environ)
    package_parent = str(Path(__file__).resolve().parents[1])  # (vibefpmgui importable, wherever it's run from)
    env["PYTHONPATH"] = os.pathsep.join(p for p in (package_parent, env.get("PYTHONPATH", "")) if p)
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    # Its output goes to files, not pipes, and only it is waited for: a pipe is
    # only done when every process holding it has closed it, and a process
    # started meanwhile by another thread (the Terminal's shell, fortls, fpm)
    # could hold it too (inherited, on Windows), for as long as it runs.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen([sys.executable, "-m", "vibefpmgui.git_status", str(path)],
                                   stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, **flags)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise RuntimeError(f"reading the status took over {timeout:.0f} s") from None
        out.seek(0)
        err.seek(0)
        output, errors = out.read(), err.read()
    if code != 0:
        lines = errors.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(lines[-1] if lines else f"the status reader stopped ({code})")
    data = json.loads(output.decode("utf-8"))
    if data is None:
        return None
    data["root"] = Path(data["root"])
    data["changes"] = [Change(**c) for c in data["changes"]]
    return RepoStatus(**data)


def _print_status(path: str):
    """(``python -m vibefpmgui.git_status PATH``: the status as JSON.)"""
    status = read_status(Path(path))
    data = None
    if status is not None:
        data = asdict(status)
        data["root"] = str(status.root)
    sys.stdout.buffer.write(json.dumps(data).encode("utf-8"))


if __name__ == "__main__":
    _print_status(sys.argv[1])
