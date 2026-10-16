"""Git: a project's repository's status, and the two versions of a changed
file to compare, from the git program (``git status --porcelain=v2``, a
format made for programs; ``git cat-file``). Git must be installed (on the
PATH); without it the Git panel says so.

git runs as a process of its own, its output going to temporary files: this
process only waits for it (without Python's lock: the window goes on), and
only for it (a pipe is only done when every process holding it has closed
it, and one started meanwhile by another thread, e.g. the Terminal's shell
or fortls, could hold it too, for as long as it runs).

No Qt: `vibefpmgui.git_panel` shows it, `vibefpmgui.window.git` reads the
status on a pool thread.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
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
TIMEOUT = 120  # seconds a git command may take
BINARY = "\0binary"  # versions() marker: the file isn't text


@dataclass(frozen=True)
class Change:
    path: str           # relative to the repository's top folder, with "/"
    area: str           # STAGED, CHANGED, UNTRACKED or CONFLICT
    kind: str           # a key of LETTERS
    old_path: str | None = None  # a renamed file's name before

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


class GitError(RuntimeError):
    """git couldn't do it (its message)."""


def executable() -> str | None:
    """The git program, or None if it isn't installed (on the PATH)."""
    return shutil.which("git")


def available() -> bool:
    """Whether git is installed (the Git panel says so otherwise)."""
    return executable() is not None


def _git(folder: Path, *args: str, limit: int | None = None) -> tuple[int, bytes | None, str]:
    """Run ``git -C folder args`` (no optional locks: it never holds the
    index lock that your own git commands need); (exit code, its output,
    or None if over ``limit`` bytes, its error message)."""
    git = executable()
    if git is None:
        raise GitError("git isn't installed")
    env = dict(os.environ, GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0")
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        try:
            process = subprocess.Popen([git, "-C", str(folder), *args], stdin=subprocess.DEVNULL,
                                       stdout=out, stderr=err, env=env, **flags)
        except OSError as error:
            raise GitError(f"couldn't run git: {error}") from None
        try:
            code = process.wait(timeout=TIMEOUT)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise GitError(f"git {args[0]} took over {TIMEOUT} s") from None
        size = out.seek(0, os.SEEK_END)
        out.seek(0)
        err.seek(0)
        output = None if limit is not None and size > limit else out.read()
        message = err.read().decode("utf-8", "replace").strip()
    return code, output, message


def _check(folder: Path, *args: str) -> bytes:
    code, output, message = _git(folder, *args)
    if code != 0:
        lines = message.splitlines()
        raise GitError(lines[-1].removeprefix("fatal: ") if lines else f"git {args[0]} failed ({code})")
    return output or b""


def find_repository(path: Path) -> Path | None:
    """The top folder of the git repository ``path`` is in, or None (not in
    one, a bare one, or no git)."""
    if not available() or not Path(path).is_dir():
        return None
    code, output, _message = _git(Path(path), "rev-parse", "--show-toplevel")
    if code != 0 or not output or not output.strip():
        return None
    return Path(output.decode("utf-8", "replace").strip()).resolve()


# Each side of porcelain v2's "XY": what happened (index side: staged; work tree side: not staged).
KINDS = {"M": "modified", "A": "added", "D": "deleted", "R": "renamed", "C": "added", "T": "typechange"}


def read_status(path: Path) -> RepoStatus | None:
    """The status of the repository ``path`` is in (None: not in one). A file
    can be in two areas: some of its changes staged, some not. Raises
    GitError if git can't read it (its message)."""
    root = find_repository(path)
    if root is None:
        return None
    output = _check(root, "status", "--porcelain=v2", "--branch", "-z", "--untracked-files=all")
    return parse_status(root, output)


def parse_status(root: Path, output: bytes) -> RepoStatus:
    """``git status --porcelain=v2 --branch -z`` read."""
    status = RepoStatus(root)
    fields = output.decode("utf-8", "surrogateescape").split("\0")
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if entry.startswith("# "):
            key, _, value = entry[2:].partition(" ")
            if key == "branch.oid":
                status.head = None if value == "(initial)" else value[:7]
            elif key == "branch.head":
                status.branch = None if value == "(detached)" else value
            elif key == "branch.upstream":
                status.upstream = value
            elif key == "branch.ab":
                ahead, _, behind = value.partition(" ")
                status.ahead, status.behind = abs(int(ahead)), abs(int(behind))
        elif entry.startswith(("1 ", "2 ")):
            parts = entry.split(" ", 9 if entry[0] == "2" else 8)
            file, old = parts[-1], None
            if entry[0] == "2":  # (renamed or copied: the next field is its name before)
                old = fields[i]
                i += 1
            staged, changed = parts[1][0], parts[1][1]
            if staged != ".":
                status.changes.append(Change(file, STAGED, KINDS.get(staged, "modified"),
                                             old if staged in "RC" else None))
            if changed != ".":
                status.changes.append(Change(file, CHANGED, KINDS.get(changed, "modified")))
        elif entry.startswith("u "):
            status.changes.append(Change(entry.split(" ", 10)[-1], CONFLICT, "conflict"))
        elif entry.startswith("? "):
            status.changes.append(Change(entry[2:], UNTRACKED, "untracked"))
    status.changes.sort(key=lambda c: c.path)
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

    def blob(spec: str) -> bytes | str | None:
        code, output, _message = _git(root, "cat-file", "blob", spec, limit=MAX_DIFF_BYTES)
        if code != 0:
            return None  # (not in that version)
        return BINARY if output is None else output

    def committed() -> bytes | str | None:
        return blob(f"HEAD:{change.old_path or change.path}")

    def indexed() -> bytes | str | None:
        return blob(f":{change.path}")

    def on_disk() -> bytes | str | None:
        file = root / change.path
        try:
            if not file.is_file():
                return None
            return BINARY if file.stat().st_size > MAX_DIFF_BYTES else file.read_bytes()
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
    return (*(v if isinstance(v, str) else _text(v) for v in (old, new)), *labels)
