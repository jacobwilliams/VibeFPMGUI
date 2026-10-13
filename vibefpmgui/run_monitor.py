"""The "Run" tab: progress of programs started with Run.

It is the Tests panel with different wording: programs are launched through
the same marker wrapper (`vibefpmgui.test_runner`), so each one's start,
end, exit code and output are known, and run times are remembered to
estimate the next run.
"""

from __future__ import annotations

from typing import ClassVar

from .test_monitor import PASSED, TestMonitor


class RunMonitor(TestMonitor):
    COLUMNS: ClassVar[list[str]] = ["PROGRAM", "STATUS", "TIME"]
    ICON = "run"
    WORDS: ClassVar[dict[str, str]] = {
        "title": "Run", "item": "program", "items": "programs",
        PASSED: "Finished", "passed": "finished",
        "idle": "Run a program (toolbar Run, or Run in the Build outputs tab) to see it here.",
        "idle_label": "Run a program to see progress",
        "collecting": "Building… (per-file progress is in the Build tab)",
        "collecting_label": "finding programs",
        "after_build": "programs start after the build",
        "none": "This project has no programs to run.", "none_label": "No programs",
        "unlisted": "The programs could not be built or listed — see the Build tab and output.",
        "row_tip": "Click to show only this program's output",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("RunMonitor")
        self.refresh_palette()
