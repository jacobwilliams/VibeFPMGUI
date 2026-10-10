"""Linting Fortran files in the editor with fortitude.

`fortitude <https://fortitude.readthedocs.io>`_ reads the editor's text on
stdin (so unsaved changes are linted too) and reports its findings as JSON.
It runs in the folder of the file, so it finds the project's settings
(``fpm.toml``'s ``[extra.fortitude]`` table or a ``fortitude.toml``).
fortitude only understands free-form Fortran, so other files are skipped.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import NamedTuple

LINT_EXTS = {".f90", ".f95", ".f03", ".f08", ".f18", ".f23"}  # any case, e.g. .F90


class LintIssue(NamedTuple):
    line: int        # 1-based
    column: int      # 1-based
    end_line: int
    end_column: int
    code: str        # e.g. "C121"; E… are syntax errors
    message: str
    fix: str         # what fortitude --fix would do ("" if nothing)

    @property
    def severity(self) -> str:
        return "lint error" if self.code.startswith("E") else "lint"

    def describe(self) -> str:
        fix = f" (fix: {self.fix})" if self.fix else ""
        return f"fortitude {self.code}: {self.message}{fix}"


def executable() -> str | None:
    return shutil.which("fortitude")


def can_lint(path: Path) -> bool:
    return path.suffix.lower() in LINT_EXTS


def command(path: Path) -> list[str]:
    """Arguments for fortitude (after the executable); the text goes to stdin."""
    return ["check", "--output-format", "json", "--stdin-filename", str(path), "-"]


def parse(output: str) -> list[LintIssue]:
    """fortitude's JSON output -> issues (empty if it can't be read)."""
    try:
        data = json.loads(output)
    except ValueError:
        return []
    issues = []
    for item in data if isinstance(data, list) else []:
        try:
            start, end = item["location"], item.get("end_location") or item["location"]
            fix = item.get("fix") or {}
            issues.append(LintIssue(int(start["line"]), int(start["column"]),
                                    int(end["line"]), int(end["column"]),
                                    str(item.get("code", "")), str(item.get("message", "")),
                                    str(fix.get("message") or "")))
        except (KeyError, TypeError, ValueError):
            continue
    return issues
