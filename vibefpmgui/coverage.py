"""Test coverage (Build options → Coverage): which lines the tests ran.

With coverage on, fpm compiles with gfortran's ``--coverage`` (a build
folder of its own, as fpm does for any flags), and the test programs write
how often each line ran into ``.gcda`` files next to the objects. After a
test run, a `Collector` has ``gcov`` (the one that matches the compiler)
read them, as JSON, several at a time; `read_gcov` turns that into
`FileCoverage` objects: the editor's gutter shows covered and missed lines,
and the *Coverage* panel the totals and each file's numbers.

No Qt here: `vibefpmgui.window.coverage` runs the collector on a pool thread.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

FLAG = "--coverage"  # gfortran's (compile and link)
FILES_PER_GCOV = 10  # .gcda files given to each gcov run


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
    "program", "subroutine" or "function" (gcov only knows gfortran's names
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


def read_gcov(outputs: Iterable[str]) -> dict[Path, FileCoverage]:
    """gcov's JSON (``--stdout --json-format``: a document per line, one per
    .gcda file), by source file (absolute paths). A file in several (built
    for the library and for the tests, say) has its counts added."""
    lines: dict[Path, dict[int, int]] = {}
    functions: dict[Path, dict[tuple[str, int], list[int]]] = {}  # (name, start) -> [end, hits]
    branches: dict[Path, dict[tuple, int]] = {}  # (line, function, branch) -> times taken
    for text in outputs:
        for raw in text.splitlines():
            if not raw.startswith("{"):
                continue
            try:
                doc = json.loads(raw)
            except ValueError:
                continue
            folder = Path(doc.get("current_working_directory") or ".")  # (where it was compiled)
            for entry in doc.get("files", []):
                path = (folder / entry["file"]).resolve()
                counts = lines.setdefault(path, {})
                for line in entry.get("lines", []):
                    number = line["line_number"]
                    counts[number] = counts.get(number, 0) + max(line["count"], 0)
                    taken = branches.setdefault(path, {})
                    for n, branch in enumerate(b for b in line.get("branches", []) if not b.get("throw")):
                        key = (number, line.get("function_name", ""), n)
                        taken[key] = taken.get(key, 0) + branch["count"]
                found = functions.setdefault(path, {})
                for fn in entry.get("functions", []):
                    end_hits = found.setdefault((fn["name"], fn["start_line"]), [fn["end_line"], 0])
                    end_hits[1] += fn["execution_count"]
    files = {}
    for path, counts in lines.items():
        per_line: dict[int, tuple[int, int]] = {}
        for (number, _fn, _n), count in branches.get(path, {}).items():
            taken, total = per_line.get(number, (0, 0))
            per_line[number] = (taken + (count > 0), total + 1)
        fns = sorted((FunctionCoverage(name, start, end, hits)
                      for (name, start), (end, hits) in functions.get(path, {}).items()), key=lambda f: f.line)
        files[path] = FileCoverage(path, counts, fns, sum(t for _h, t in per_line.values()),
                                   sum(h for h, _t in per_line.values()), per_line)
    return files


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
        name, ext = Path(found).stem, Path(found).suffix  # (".exe" on Windows)
        prefix = name[:-len("gfortran")] if name.endswith("gfortran") else ""
        for candidate in (f"{prefix}gcov{ext}", f"gcov{ext}"):
            if (folder / candidate).is_file():
                return str(folder / candidate)
        for candidate in sorted(folder.glob(f"*-gcov{ext}")):
            return str(candidate)
    return shutil.which("gcov")


def gcov_command(gcov: str, data_files: list[Path]) -> list[str]:
    return [gcov, "--stdout", "--json-format", "--branch-probabilities", *map(str, data_files)]


class Collector:
    """gcov reading every .gcda file under the project's build folder,
    ``jobs`` runs at a time (default: one per CPU). `run` blocks (call it on
    a pool thread); `stop`, from another thread, ends it early."""

    def __init__(self, gcov: str, build_dir: Path, jobs: int | None = None):
        self.gcov, self.build_dir = gcov, build_dir
        self.jobs = jobs or os.cpu_count() or 1
        self._stopped = False
        self._running: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def run(self) -> tuple[dict[Path, FileCoverage], str]:
        """The coverage, and an error message ("" if none)."""
        data = sorted(self.build_dir.rglob("*.gcda")) if self.build_dir.is_dir() else []
        chunks = [data[i:i + FILES_PER_GCOV] for i in range(0, len(data), FILES_PER_GCOV)]
        with ThreadPoolExecutor(self.jobs) as pool:
            results = list(pool.map(self._gcov, chunks))
        if self._stopped:
            return {}, "stopped"
        files = read_gcov(out for out, _err in results)
        errors = [err for _out, err in results if err]
        return files, ("" if files or not errors else errors[-1])

    def _gcov(self, chunk: list[Path]) -> tuple[str, str]:
        """One gcov run's output, and its error ("" if it worked)."""
        if self._stopped:
            return "", ""
        try:
            process = subprocess.Popen(
                gcov_command(self.gcov, chunk), cwd=self.build_dir, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))  # (no console window on Windows)
        except OSError as error:
            return "", str(error)
        with self._lock:
            self._running.add(process)
        out, err = process.communicate()
        with self._lock:
            self._running.discard(process)
        message = err.strip().splitlines()[-1] if err.strip() else f"gcov exited with {process.returncode}"
        return out, ("" if process.returncode == 0 else message)

    def stop(self):
        self._stopped = True
        with self._lock:
            for process in self._running:
                process.kill()


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
