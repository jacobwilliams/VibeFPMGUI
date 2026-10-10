"""Small helpers that differ between macOS, Linux and Windows."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys

WINDOWS = os.name == "nt"


def split_args(text: str) -> list[str]:
    """Split a command-line string typed by the user into arguments.

    On Windows backslashes are path separators, not escapes, so POSIX
    ``shlex.split`` would turn ``C:\\data\\in.txt`` into ``C:datain.txt``;
    use non-POSIX splitting there and drop the surrounding quotes.
    """
    if not WINDOWS:
        return shlex.split(text)
    parts = shlex.split(text, posix=False)
    return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]


def join_args(args: list[str]) -> str:
    """A command line to show the user, quoted the way their shell expects."""
    return subprocess.list2cmdline(args) if WINDOWS else shlex.join(args)


def quote_arg(arg: str) -> str:
    return subprocess.list2cmdline([arg]) if WINDOWS else shlex.quote(arg)


def file_manager_name() -> str:
    """What the platform's file manager is called (for menu items)."""
    return {"darwin": "Finder", "win32": "Explorer"}.get(sys.platform, "the file manager")


def reveal_label() -> str:
    return {"darwin": "Show in Finder", "win32": "Show in Explorer"}.get(
        sys.platform, "Open Containing Folder")


def set_windows_app_id(app_id: str = "VibeFPMGUI.VibeFPMGUI"):
    """On Windows, give the process its own taskbar identity, so the taskbar
    shows the app's icon instead of grouping it under python.exe."""
    if not WINDOWS:
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except (AttributeError, OSError):
        pass
