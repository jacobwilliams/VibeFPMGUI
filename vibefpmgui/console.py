"""Starting console programs (git, fpm, fortitude, fprettify…) from the app
without a console window flashing up on Windows (no Qt).

A GUI app on Windows has no console; a console program it starts with
Python's subprocess gets a new one, shown for as long as the program runs,
unless it's started with CREATE_NO_WINDOW. (QProcess does that by itself
when the app has no console.)
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

# subprocess's keyword arguments for no console window: **NO_WINDOW in each call.
NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}


def gui_python() -> str:
    """The Python to start another window of the app with: on Windows its
    pythonw.exe (python.exe would open a console window beside it, there
    for as long as the window is)."""
    if sys.platform == "win32":
        windowless = Path(sys.executable).with_name("pythonw.exe")
        if windowless.is_file():
            return str(windowless)
    return sys.executable


def run(args: list[str], timeout: float, limit: int | None = None, env: dict | None = None,
        cwd: str | None = None) -> tuple[int, bytes | None, str]:
    """Run a program to its end, without a console window: (exit code, its
    output, or None if over ``limit`` bytes, its error message). Raises
    OSError if it can't be started, TimeoutError after ``timeout`` seconds
    (it's killed).

    Its output goes to temporary files, not pipes, and only it is waited for
    (without Python's lock: the window goes on): a pipe is only done when
    every process holding it has closed it, and a process started meanwhile
    by another thread (the Terminal's shell, fortls, fpm) could hold it too,
    for as long as it runs."""
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=out, stderr=err, env=env, cwd=cwd,
                                   **NO_WINDOW)
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise TimeoutError(f"{Path(args[0]).name} took over {timeout:.0f} s") from None
        size = out.seek(0, os.SEEK_END)
        out.seek(0)
        err.seek(0)
        output = None if limit is not None and size > limit else out.read()
        message = err.read().decode("utf-8", "replace").strip()
    return code, output, message

