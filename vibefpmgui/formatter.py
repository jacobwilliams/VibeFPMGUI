"""Formatting Fortran files in the editor with fprettify.

`fprettify <https://github.com/fortran-lang/fprettify>`_ reads the editor's
text on stdin and writes the formatted text to stdout, so unsaved changes are
formatted too and the result is one undoable edit. It runs in the folder of
the file, so a ``.fprettify.rc`` there (or in a parent folder, or ``~``)
applies. Like fortitude, it only understands free-form Fortran.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .console import NO_WINDOW
from .lint import LINT_EXTS

FORMAT_EXTS = LINT_EXTS


def executable() -> str | None:
    return shutil.which("fprettify")


def can_format(path: Path) -> bool:
    return path.suffix.lower() in FORMAT_EXTS


def format_text(text: str, path: Path, timeout: float = 60) -> tuple[str | None, str]:
    """(formatted text, "") or (None, why it failed)."""
    program = executable()
    if program is None:
        return None, "fprettify was not found on the PATH."
    # fprettify's own regexes trigger SyntaxWarnings on newer Pythons.
    env = dict(os.environ, PYTHONWARNINGS="ignore")
    try:
        out = subprocess.run([program, "-"], input=text, capture_output=True, text=True,
                             encoding="utf-8", errors="replace", cwd=path.parent,
                             timeout=timeout, env=env, check=False, **NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return None, str(exc)
    if out.returncode != 0:
        lines = [line for line in out.stderr.splitlines() if line.strip()]
        return None, lines[-1] if lines else f"fprettify exited with code {out.returncode}"
    if text.strip() and not out.stdout.strip():
        return None, "fprettify returned no text."
    return out.stdout, ""
