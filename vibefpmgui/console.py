"""Starting console programs (git, fpm, fortitude, fprettify…) from the app
without a console window flashing up on Windows (no Qt).

A GUI app on Windows has no console; a console program it starts with
Python's subprocess gets a new one, shown for as long as the program runs,
unless it's started with CREATE_NO_WINDOW. (QProcess does that by itself
when the app has no console.)
"""

from __future__ import annotations

import subprocess
import sys
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
