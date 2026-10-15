"""Ending processes: a QProcess and everything it started."""

from __future__ import annotations

import os
import signal
import subprocess

from qtpy.QtCore import QProcess


def kill_process_tree(process: QProcess):
    """Kill a QProcess *and everything it started* (fpm's compilers, the test
    runner and the test program): killing only fpm would leave them running."""
    pid = int(process.processId() or 0)
    if pid > 0:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        else:
            for child in reversed(_descendants(pid)):  # deepest first
                try:
                    os.kill(child, signal.SIGKILL)
                except OSError:
                    pass  # already gone
    process.kill()


def _descendants(pid: int) -> list[int]:
    """All processes below ``pid``, parents before children (POSIX)."""
    try:
        out = subprocess.run(["ps", "-A", "-o", "pid=", "-o", "ppid="],
                             capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    children: dict[int, list[int]] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    found, queue = [], [pid]
    while queue:
        for child in children.get(queue.pop(0), []):
            found.append(child)
            queue.append(child)
    return found
