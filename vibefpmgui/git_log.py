"""The Git panel's *History*: the repository's commits, newest first, with
their branches and tags, and the graph of how they join (each commit's
lane, the lines to its parents), and the two versions of a file a commit
changed.

The graph's layout (`graph`): each lane waits for a commit (a parent of one
above it). A commit goes in the lane waiting for it (the leftmost, if
several are: the others join it there); then that lane waits for its first
parent, and its other parents (a merge's) get lanes of their own, the
leftmost free one, unless one is already waiting for them. Lanes keep their
places (a free one is used again), so a line that passes a row goes
straight down; each lane keeps its color from where it started.

No Qt: `vibefpmgui.git_log_view` shows it; it's read on a pool thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .git_blame import FileChange
from .git_status import BINARY, MAX_DIFF_BYTES, _check, _git, _text, find_repository

MAX_COMMITS = 3000  # read at most (the newest)

# Ref kinds, as their badges show them.
HEAD, BRANCH, REMOTE, TAG = "head", "branch", "remote", "tag"


@dataclass
class Ref:
    name: str            # "main", "origin/main", "v1.0", "HEAD" (detached)
    kind: str            # HEAD, BRANCH, REMOTE or TAG
    current: bool = False  # the branch checked out (HEAD -> it)


@dataclass
class LogCommit:
    sha: str
    parents: list[str]
    author: str
    email: str
    time: int             # its author's date (seconds since 1970)
    subject: str
    refs: list[Ref] = field(default_factory=list)


@dataclass
class GraphRow:
    """A commit's row of the graph: lanes are numbered from the left, each
    line given as (lane, color number)."""
    lane: int                     # the commit's
    color: int                    # its lane's
    from_above: bool              # a line comes down into it (it's a parent of a commit above)
    through: list[tuple[int, int]]  # lanes passing by (top to bottom)
    joining: list[tuple[int, int]]  # lanes ending in it (from the top: a branch that started here)
    parents: list[tuple[int, int]]  # lanes leaving it (to the bottom: its parents')
    width: int                    # lanes in use, above or below it


@dataclass
class Log:
    root: Path
    head: str                     # the commit checked out
    commits: list[LogCommit]
    rows: list[GraphRow]
    all_branches: bool
    limited: bool                 # more commits than MAX_COMMITS: the newest shown
    remote: str = ""              # origin's address (the commits' web pages)


def parse_refs(decoration: str) -> list[Ref]:
    """The refs in ``%D`` (with ``--decorate=full``): "HEAD -> refs/heads/main,
    tag: refs/tags/v1, refs/remotes/origin/main". The branch checked out
    first, then the others, the remote ones, the tags."""
    refs = []
    for part in (p.strip() for p in decoration.split(",")):
        current = part.startswith("HEAD -> ")
        part = part.removeprefix("HEAD -> ")
        if part == "HEAD":
            refs.append(Ref("HEAD", HEAD, True))  # (detached)
        elif part.startswith("tag: refs/tags/"):
            refs.append(Ref(part.removeprefix("tag: refs/tags/"), TAG))
        elif part.startswith("refs/heads/"):
            refs.append(Ref(part.removeprefix("refs/heads/"), BRANCH, current))
        elif part.startswith("refs/remotes/") and not part.endswith("/HEAD"):
            refs.append(Ref(part.removeprefix("refs/remotes/"), REMOTE))
    order = {HEAD: 0, BRANCH: 1, REMOTE: 2, TAG: 3}
    return sorted(refs, key=lambda r: (not r.current, order[r.kind]))


FIELDS = "%H%x1f%P%x1f%an%x1f%ae%x1f%at%x1f%D%x1f%s%x1e"


def parse_log(output: bytes) -> list[LogCommit]:
    """``git log --format=FIELDS --decorate=full`` read."""
    commits = []
    for record in output.decode("utf-8", "replace").split("\x1e"):
        parts = record.strip("\n").split("\x1f")
        if len(parts) != 7 or len(parts[0]) < 7:
            continue
        sha, parents, author, email, when, refs, subject = parts
        commits.append(LogCommit(sha, parents.split(), author, email, int(when) if when.isdigit() else 0, subject,
                                 parse_refs(refs)))
    return commits


def graph(commits: list[LogCommit]) -> list[GraphRow]:
    """Each commit's row of the graph (see the module's description)."""
    lanes: list[str | None] = []  # the commit each lane waits for
    colors: list[int] = []
    next_color = 0
    rows = []

    def free_lane(avoid: int = -1) -> int:
        for i, waiting in enumerate(lanes):
            if waiting is None and i != avoid:
                return i
        lanes.append(None)
        colors.append(0)
        return len(lanes) - 1

    for commit in commits:
        above = len(lanes)
        from_above = commit.sha in lanes
        if from_above:
            lane = lanes.index(commit.sha)
        else:
            lane = free_lane()
            lanes[lane], colors[lane] = commit.sha, next_color
            next_color += 1
        joining = [(i, colors[i]) for i, waiting in enumerate(lanes) if waiting == commit.sha and i != lane]
        through = [(i, colors[i]) for i, waiting in enumerate(lanes) if waiting is not None and waiting != commit.sha]
        for i, _color in joining:
            lanes[i] = None
        parents = []
        if commit.parents:
            lanes[lane] = commit.parents[0]
            parents.append((lane, colors[lane]))
            for parent in commit.parents[1:]:
                if parent in lanes:
                    i = lanes.index(parent)
                else:
                    i = free_lane(avoid=lane)
                    lanes[i], colors[i] = parent, next_color
                    next_color += 1
                parents.append((i, colors[i]))
        else:
            lanes[lane] = None  # (the first commit: nothing below)
        rows.append(GraphRow(lane, colors[lane], from_above, through, joining, parents, max(above, len(lanes))))
        while lanes and lanes[-1] is None:
            lanes.pop()
            colors.pop()
    return rows


def read_log(path: Path, all_branches: bool = True, limit: int = MAX_COMMITS) -> Log | None:
    """The history of the repository ``path`` is in: every branch's (local,
    remote) and tag's, or only what's checked out (None: not in a
    repository, or no commits yet). Raises GitError if git can't read it."""
    root = find_repository(path)
    if root is None:
        return None
    code, head, _message = _git(root, "rev-parse", "--verify", "-q", "HEAD")
    if code != 0 or not head or not head.strip():
        return None
    which = ["--branches", "--remotes", "--tags", "HEAD"] if all_branches else ["HEAD"]
    output = _check(root, "log", f"--format={FIELDS}", "--decorate=full", "--date-order", f"--max-count={limit + 1}",
                    *which, "--")
    commits = parse_log(output)
    limited = len(commits) > limit
    commits = commits[:limit]
    _code, remote, _message = _git(root, "config", "--get", "remote.origin.url")
    return Log(root, head.decode().strip(), commits, graph(commits), all_branches, limited,
               (remote or b"").decode("utf-8", "replace").strip())


def file_versions(root: Path, commit: LogCommit, change: FileChange) -> tuple[str | None, str | None]:
    """(before, after) a commit of a file it changed: the text in its first
    parent (None: added, or the first commit), and in it (None: deleted).
    BINARY: it isn't text, or it's huge."""

    def blob(spec: str) -> str | None:
        code, output, _message = _git(root, "cat-file", "blob", spec, limit=MAX_DIFF_BYTES)
        if code != 0:
            return None
        return BINARY if output is None else _text(output)

    parent = commit.parents[0] if commit.parents else None
    before = blob(f"{parent}:{change.old_path or change.path}") if parent and change.status != "A" else None
    after = blob(f"{commit.sha}:{change.path}") if change.status != "D" else None
    return before, after
