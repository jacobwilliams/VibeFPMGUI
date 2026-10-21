"""The Git panel's *Stats*: numbers about the project's repository (its
commits, the people who made them, branches, tags, files, size) and each
person's commits over time, from the first commit on, for the history of
what's checked out (HEAD).

People are told apart by name, as git log shows them (with the
repository's ``.mailmap``, if it has one): one person with two email
addresses is one; the dates are the commits' author dates.

No Qt: `vibefpmgui.git_stats_view` shows them; they're read on a pool thread.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .git_status import GitError, _check, _git, find_repository

DAY = 24 * 3600


@dataclass
class Author:
    name: str
    email: str                  # (the one of their latest commit)
    times: list[int] = field(default_factory=list)  # their commits' dates (seconds since 1970), oldest first

    @property
    def commits(self) -> int:
        return len(self.times)

    @property
    def first(self) -> int:
        return self.times[0]

    @property
    def last(self) -> int:
        return self.times[-1]


@dataclass
class RepoStats:
    root: Path
    head: str                   # the commit checked out (its hash)
    authors: list[Author]       # most commits first
    merges: int = 0             # commits merging others
    branches: int = 0           # local ones
    remote_branches: int = 0
    tags: int = 0
    files: int = 0              # tracked by git
    size: int = 0               # bytes the repository's objects take on disk (.git)

    @property
    def commits(self) -> int:
        return sum(a.commits for a in self.authors)

    @property
    def first(self) -> int:
        return min(a.first for a in self.authors)

    @property
    def last(self) -> int:
        return max(a.last for a in self.authors)

    def since(self, seconds: int, now: float | None = None) -> int:
        """The commits made in the last ``seconds`` (up to ``now``)."""
        now = time.time() if now is None else now
        return sum(1 for a in self.authors for t in a.times if now - seconds <= t <= now)


def parse_log(output: bytes) -> list[Author]:
    """The people in ``git log --format=%at%x1f%aN%x1f%aE`` (newest commit
    first, as git lists them), most commits first (then by name)."""
    people: dict[str, Author] = {}
    for line in output.decode("utf-8", "replace").splitlines():
        parts = line.split("\x1f")
        if len(parts) != 3 or not parts[0].strip().isdigit():
            continue
        when, name, email = int(parts[0]), parts[1].strip() or "(no name)", parts[2].strip()
        person = people.setdefault(name, Author(name, email))
        person.times.append(when)
    for person in people.values():
        person.times.sort()
    return sorted(people.values(), key=lambda a: (-a.commits, a.name.lower()))


def parse_size(output: bytes) -> int:
    """Bytes on disk from ``git count-objects -v`` (its loose and packed
    objects' sizes, in KiB)."""
    sizes = {}
    for line in output.decode("utf-8", "replace").splitlines():
        key, _, value = line.partition(":")
        if value.strip().isdigit():
            sizes[key.strip()] = int(value)
    return (sizes.get("size", 0) + sizes.get("size-pack", 0)) * 1024


def _count(output: bytes, separator: bytes = b"\n") -> int:
    return sum(1 for part in output.split(separator) if part.strip())


def read_stats(path: Path) -> RepoStats | None:
    """The statistics of the repository ``path`` is in (None: not in one, or
    it has no commits yet). Raises GitError if git can't read it."""
    root = find_repository(path)
    if root is None:
        return None
    code, head, _message = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if code != 0 or not head or not head.strip():
        return None  # (no commits yet)
    log = _check(root, "log", "--format=%at%x1f%aN%x1f%aE", "HEAD")
    refs = _check(root, "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags")
    names = refs.decode("utf-8", "replace").splitlines()
    stats = RepoStats(root, head.decode().strip(), parse_log(log))
    if not stats.authors:
        raise GitError("git log listed no commits")
    stats.merges = int(_check(root, "rev-list", "--count", "--merges", "HEAD").strip() or 0)
    stats.branches = sum(1 for n in names if n.startswith("refs/heads/"))
    stats.remote_branches = sum(1 for n in names if n.startswith("refs/remotes/") and not n.endswith("/HEAD"))
    stats.tags = sum(1 for n in names if n.startswith("refs/tags/"))
    stats.files = _count(_check(root, "ls-files", "-z"), b"\0")
    stats.size = parse_size(_check(root, "count-objects", "-v"))
    return stats


def span_words(seconds: float) -> str:
    """A length of time in words: "3 years, 2 months", "5 days"."""
    days = int(seconds // DAY)
    years, days = divmod(days, 365)
    months, days = divmod(days, 30)
    parts = [(years, "year"), (months, "month")] if years else [(months, "month"), (days, "day")]
    words = [f"{n} {unit}{'s' * (n != 1)}" for n, unit in parts if n]
    return ", ".join(words) if words else "less than a day"
