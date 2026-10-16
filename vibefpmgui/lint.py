"""Linting Fortran files in the editor with fortitude.

`fortitude <https://fortitude.readthedocs.io>`_ reads the editor's text on
stdin (so unsaved changes are linted too) and reports its findings as JSON.
It runs in the folder of the file, so it finds the project's settings
(``fpm.toml``'s ``[extra.fortitude]`` table or a ``fortitude.toml``).
fortitude only understands free-form Fortran, so other files are skipped.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import NamedTuple

from .console import NO_WINDOW

LINT_EXTS = {".f90", ".f95", ".f03", ".f08", ".f18", ".f23"}  # any case, e.g. .F90


class FixEdit(NamedTuple):
    """One change a fix makes (0-based, end exclusive: the same fields as
    `vibefpmgui.lsp.TextEdit`, so the editor applies both the same way)."""
    line: int
    column: int
    end_line: int
    end_column: int
    new_text: str


class LintIssue(NamedTuple):
    line: int        # 1-based
    column: int      # 1-based
    end_line: int
    end_column: int
    code: str        # e.g. "C121"; E… are syntax errors
    message: str
    fix: str         # what fortitude --fix would do ("" if nothing)
    fix_safe: bool = False  # fortitude applies it with --fix (others need --unsafe-fixes)
    edits: tuple[FixEdit, ...] = ()  # the fix's changes (empty: no automatic fix)

    @property
    def fixable(self) -> bool:
        return bool(self.edits)

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
            edits = tuple(FixEdit(int(e["location"]["line"]) - 1, int(e["location"]["column"]) - 1,
                                  int(e["end_location"]["line"]) - 1, int(e["end_location"]["column"]) - 1,
                                  str(e.get("content", "")))
                          for e in fix.get("edits") or [])
            issues.append(LintIssue(int(start["line"]), int(start["column"]),
                                    int(end["line"]), int(end["column"]),
                                    str(item.get("code", "")), str(item.get("message", "")),
                                    str(fix.get("message") or ""), fix.get("applicability") == "safe", edits))
        except (KeyError, TypeError, ValueError):
            continue
    return issues


def fix_text(text: str, path: Path, unsafe: bool = False, timeout: float = 60) -> tuple[str | None, str]:
    """(the text with fortitude's fixes applied, "") or (None, why not).
    Safe fixes only, unless ``unsafe``. Runs in the file's folder (for its
    settings), on the given text rather than the file on disk."""
    program = executable()
    if program is None:
        return None, "fortitude was not found on the PATH."
    args = [program, "check", "--fix", "--stdin-filename", str(path), "-"]
    if unsafe:
        args.insert(3, "--unsafe-fixes")
    try:
        out = subprocess.run(args, input=text, capture_output=True, text=True, encoding="utf-8",
                             errors="replace", cwd=path.parent, timeout=timeout, check=False, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    # (The exit status says whether issues remain, so it isn't an error.)
    if text.strip() and not out.stdout.strip():
        lines = [line for line in out.stderr.splitlines() if line.strip()]
        return None, lines[-1] if lines else "fortitude returned no text."
    return out.stdout, ""
