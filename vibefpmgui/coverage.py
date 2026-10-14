"""Test coverage (Build options → Coverage): which lines the tests ran.

With coverage on, fpm compiles with gfortran's ``--coverage`` (a build
folder of its own, as fpm does for any flags), and the test programs write
how often each line ran into ``.gcda`` files next to the objects. After a
test run, ``lcov --capture`` (with the ``gcov`` that matches the compiler)
reads them into a tracefile (``.info``), read here into `FileCoverage`
objects: the editor's gutter shows covered and missed lines, and the
*Coverage* panel the totals and each file's numbers.

Pure functions and data (no Qt), apart from running nothing: the commands
are built here and run by `vibefpmgui.main_window`.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

FLAG = "--coverage"  # gfortran's (compile and link)
# lcov 2 stops at what it calls inconsistencies (common with Fortran: line
# numbers of `end function` and the like); they don't matter for line counts.
IGNORE = "mismatch,inconsistent,negative,unused,empty,source,gcov,range,unsupported,format"


@dataclass
class FunctionCoverage:
    name: str
    line: int
    end_line: int
    hits: int


@dataclass
class FileCoverage:
    path: Path
    lines: dict[int, int] = field(default_factory=dict)  # line -> times run (lines with code only)
    functions: list[FunctionCoverage] = field(default_factory=list)
    branches_found: int = 0
    branches_hit: int = 0
    line_branches: dict[int, tuple[int, int]] = field(default_factory=dict)  # line -> (taken, total)

    @property
    def lines_found(self) -> int:
        return len(self.lines)

    @property
    def lines_hit(self) -> int:
        return sum(1 for n in self.lines.values() if n > 0)

    @property
    def functions_found(self) -> int:
        return len(self.functions)

    @property
    def functions_hit(self) -> int:
        return sum(1 for f in self.functions if f.hits > 0)

    @property
    def percent(self) -> float:
        return 100.0 * self.lines_hit / self.lines_found if self.lines_found else 0.0

    def partial(self, line: int) -> bool:
        """The line ran, but not every one of its branches was taken."""
        taken, total = self.line_branches.get(line, (0, 0))
        return self.lines.get(line, 0) > 0 and taken < total

    def missed(self) -> list[tuple[int, int]]:
        """The lines not run, as (first, last) ranges: lines in between
        without code don't break a range."""
        ranges: list[tuple[int, int]] = []
        for line in sorted(self.lines):
            if self.lines[line] > 0:
                start = None
                continue
            if ranges and start is not None:
                ranges[-1] = (ranges[-1][0], line)
            else:
                ranges.append((line, line))
            start = line
        return ranges


PROCEDURE_RE = re.compile(r"^(?!\s*end\b)\s*(?:[\w\s(),=*:]*?\s)?(program|subroutine|function)\s+\w+",
                          re.IGNORECASE)


def procedure_kind(lines: list[str], line: int) -> str | None:
    """What a function of the coverage starts at (1-based ``line``):
    "program", "subroutine" or "function" (lcov only knows gfortran's names
    for them), read from its first line; None if it doesn't say."""
    if not 1 <= line <= len(lines):
        return None
    text = lines[line - 1].split("!", 1)[0]
    found = PROCEDURE_RE.match(text)
    return found.group(1).lower() if found else None


def ranges_text(ranges: list[tuple[int, int]], limit: int = 12) -> str:
    """ "8, 10–15, 22" (the first ``limit`` ranges, then "…")."""
    parts = [str(a) if a == b else f"{a}–{b}" for a, b in ranges[:limit]]
    return ", ".join(parts) + (", …" if len(ranges) > limit else "")


def parse_info(text: str) -> dict[Path, FileCoverage]:
    """An lcov tracefile, by source file (absolute paths). Records for the
    same file (e.g. from several test programs) are added together."""
    files: dict[Path, FileCoverage] = {}
    current: FileCoverage | None = None
    names: dict[str, tuple[int, int]] = {}   # (old format) function name -> (line, end)
    by_index: dict[str, tuple[int, int]] = {}  # (lcov 2) function index -> (line, end)
    functions: dict[tuple[str, int], int] = {}
    branches: dict[tuple, int] = {}

    def finish():
        if current is None:
            return
        known = {(f.name, f.line): f for f in current.functions}
        for (name, line), hits in functions.items():
            if (name, line) in known:
                known[(name, line)].hits += hits
            else:
                end = next((e for n, (s, e) in names.items() if n == name and s == line), line)
                current.functions.append(FunctionCoverage(name, line, end, hits))
        found = {key for key in branches}
        current.branches_found += len(found)
        current.branches_hit += sum(1 for key in found if branches[key] > 0)
        per_line: dict[int, list[int]] = {}
        for key in found:
            counts = per_line.setdefault(_int(key[0]), [0, 0])
            counts[0] += branches[key] > 0
            counts[1] += 1
        for number, (taken, total) in per_line.items():
            old_taken, old_total = current.line_branches.get(number, (0, 0))
            current.line_branches[number] = (max(taken, old_taken), max(total, old_total))

    for raw in text.splitlines():
        line = raw.strip()
        if not line or ":" not in line and line != "end_of_record":
            continue
        if line.startswith("SF:"):
            path = Path(line[3:]).resolve()
            current = files.setdefault(path, FileCoverage(path))
            names, by_index, functions, branches = {}, {}, {}, {}
        elif current is None:
            continue
        elif line.startswith("DA:"):
            parts = line[3:].split(",")
            try:
                number, count = int(parts[0]), int(float(parts[1]))
            except (ValueError, IndexError):
                continue
            current.lines[number] = current.lines.get(number, 0) + max(count, 0)
        elif line.startswith("FNL:"):  # lcov 2: FNL:index,start[,end]
            parts = line[4:].split(",")
            try:
                by_index[parts[0]] = (int(parts[1]), int(parts[2]) if len(parts) > 2 else int(parts[1]))
            except (ValueError, IndexError):
                continue
        elif line.startswith("FNA:"):  # lcov 2: FNA:index,hits,name
            parts = line[4:].split(",", 2)
            if len(parts) == 3 and parts[0] in by_index:
                start, end = by_index[parts[0]]
                names[parts[2]] = (start, end)
                key = (parts[2], start)
                functions[key] = functions.get(key, 0) + _int(parts[1])
        elif line.startswith("FN:"):  # old: FN:line[,end],name
            parts = line[3:].split(",")
            if len(parts) >= 2:
                start = _int(parts[0])
                end = _int(parts[1]) if len(parts) > 2 else start
                names[parts[-1]] = (start, end)
                functions.setdefault((parts[-1], start), 0)
        elif line.startswith("FNDA:"):  # old: FNDA:hits,name
            parts = line[5:].split(",", 1)
            if len(parts) == 2 and parts[1] in names:
                key = (parts[1], names[parts[1]][0])
                functions[key] = functions.get(key, 0) + _int(parts[0])
        elif line.startswith("BRDA:"):  # BRDA:line,block,branch,taken ("-": never reached)
            parts = line[5:].split(",")
            if len(parts) >= 4:
                key = tuple(parts[:3])
                branches[key] = branches.get(key, 0) + (0 if parts[3] == "-" else _int(parts[3]))
        elif line == "end_of_record":
            finish()
            current = None
    finish()
    return files


def _int(text: str) -> int:
    try:
        return int(float(text))
    except ValueError:
        return 0


def totals(files: list[FileCoverage]) -> dict[str, tuple[int, int]]:
    """(hit, found) for lines, functions and branches, over ``files``."""
    return {"lines": (sum(f.lines_hit for f in files), sum(f.lines_found for f in files)),
            "functions": (sum(f.functions_hit for f in files), sum(f.functions_found for f in files)),
            "branches": (sum(f.branches_hit for f in files), sum(f.branches_found for f in files))}


def find_gcov(compiler: str) -> str | None:
    """The gcov that goes with the compiler (the same GCC: a gcov of another
    version can't read the counts): next to it, plain or with the same
    prefix (``arm64-apple-darwin20.0.0-gcov``), else the one on the PATH."""
    found = shutil.which(compiler or "gfortran")
    if found:
        folder = Path(found).resolve().parent
        name = Path(found).name
        prefix = name[:-len("gfortran")] if name.endswith("gfortran") else ""
        for candidate in (f"{prefix}gcov", "gcov"):
            if (folder / candidate).is_file():
                return str(folder / candidate)
        for candidate in sorted(folder.glob("*-gcov")):
            return str(candidate)
    return shutil.which("gcov")


def capture_command(lcov: str, gcov: str, build_dir: Path, output: Path, jobs: int | None = None) -> list[str]:
    """lcov reading every .gcda under the project's build folder, ``jobs``
    files at a time (default: one per CPU; lcov alone reads them one by
    one, about 3x slower on a large project)."""
    jobs = jobs or os.cpu_count() or 1
    return [lcov, "--capture", "--directory", str(build_dir), "--output-file", str(output),
            "--gcov-tool", gcov, "--branch-coverage", "--parallel", str(jobs), "--ignore-errors", IGNORE,
            "--quiet"]


def clear_counts(build_dir: Path) -> int:
    """Remove the counts of earlier runs (.gcda files), so the next run's
    coverage is its own. Returns how many there were."""
    removed = 0
    if build_dir.is_dir():
        for path in build_dir.rglob("*.gcda"):
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
    return removed
