"""The Output panel: a command's output (limited in size), or one item's."""

from __future__ import annotations

import html
import re

from qtpy.QtCore import Qt
from qtpy.QtGui import QFontDatabase, QTextCursor
from qtpy.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import ansi
from ..compat import reveal_label
from ..console_view import ConsoleView
from ..editor import EditorTab
from ..output_links import OutputLinks
from ..project_tree import reveal_in_file_manager

# A command's output beyond this many characters isn't added to the Output
# panel (it would freeze the app: an lfortran crash, for one, prints the whole
# compiled program, 100+ MB); the last lines are shown when it finishes and
# everything is saved to a file.
OUTPUT_SHOW_LIMIT = 2_000_000


OUTPUT_TAIL_LINES = 200


MAX_LINE_SHOWN = 20_000  # longer lines are shortened


def _breakable(text: str) -> str:
    """Let long names wrap: add zero-width spaces after _ . / - (word wrap
    otherwise only breaks at spaces). For display only: text copied from the
    label may include them."""
    return re.sub(r"([_./\\-])(?=\w)", "\\1\u200b", text)


class OutputMixin:
    """The Output panel (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_output_panel(self):
        """The Output panel: all of a command's output, or one item's."""
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.console = ConsoleView()  # (fpm's "[ 25%]" progress drawn as badges)
        self.console.setReadOnly(True)
        self.console.setFont(mono)
        self.console.setMaximumBlockCount(20000)
        # Output of a single test, shown when a test row is clicked.
        self.test_output = ConsoleView()
        self.test_output.setReadOnly(True)
        self.test_output.setFont(mono)
        # Compiler messages may be colored (lfortran always colors them).
        self.console_writer = ansi.AnsiWriter(self.console)
        self.single_writer = ansi.AnsiWriter(self.test_output)
        # File locations in the output ("At line 12 of file src/x.f90", "src/x.f90:12:5:") open on a click.
        self.output_links = []
        for view in (self.console, self.test_output):
            links = OutputLinks(view, lambda: self.manifest_path.parent if self.manifest_path else None)
            links.open_requested.connect(lambda path, line, column: self.navigate_to(path, line, column,
                                                                                    preview=True))
            self.output_links.append(links)
        self.output_stack = QStackedWidget()
        self.output_stack.addWidget(self.console)
        self.output_stack.addWidget(self.test_output)
        self._set_output_wrap(EditorTab.word_wrap)
        console_box = QWidget()
        cl = QVBoxLayout(console_box)
        cl.setContentsMargins(0, 0, 0, 0)
        # (No spacing: macOS buttons already keep some room around themselves,
        # for the focus ring, which the layout counts as part of the button.)
        cl.setSpacing(0)
        # What's shown, when it's one item's output (a test, a file's compile,
        # an AI review); hidden for all the output ("Output" is the panel's title).
        self.output_title = QLabel()
        self.output_title.hide()
        # Titles like "AI review of a_very_long_file_name.f90 (model)" wrap
        # instead of forcing the panel wider.
        self.output_title.setWordWrap(True)
        self.output_title.setMinimumWidth(40)
        self.output_title.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.output_title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        # Title alone at the top; the buttons sit below the output.
        footer = QHBoxLayout()
        footer.setContentsMargins(0, 0, 0, 0)
        footer.setSpacing(6)
        footer.addStretch(1)
        self.show_all_button = QPushButton("Show all output")
        self.show_all_button.clicked.connect(self.show_all_output)
        self.show_all_button.hide()
        footer.addWidget(self.show_all_button)
        self.full_output_button = QPushButton("Full Output…")
        self.full_output_button.setToolTip("The last command's output was too long to show in full; "
                                           f"show the file it was saved to in {reveal_label()}")
        self.full_output_button.clicked.connect(lambda: reveal_in_file_manager(str(self._full_output_path)))
        self.full_output_button.hide()
        footer.addWidget(self.full_output_button)
        clear = QPushButton("Clear")
        clear.setToolTip("Clear the output")
        clear.clicked.connect(self.console.clear)
        footer.addWidget(clear)
        cl.addWidget(self.output_title)
        cl.addWidget(self.output_stack, 1)
        cl.addLayout(footer)
        return console_box

    def _append(self, text: str, bold: bool = False, colored: bool = False):
        """Add text to the Output panel; ``colored``: program output, whose
        terminal color codes are shown as colors (and which is limited to
        OUTPUT_SHOW_LIMIT characters per command)."""
        if not colored:
            self.console_writer.write(text, bold, ansi=False)
            return
        if self._full_output is not None:
            try:
                self._full_output.write(ansi.strip(text))
            except (OSError, ValueError):
                self._full_output = None
        if not self._hidden_chars and self._shown_chars + len(text) <= OUTPUT_SHOW_LIMIT:
            self._shown_chars += len(text)
            if len(text) > MAX_LINE_SHOWN and max(map(len, text.split("\n"))) > MAX_LINE_SHOWN:
                text = "\n".join(line if len(line) <= MAX_LINE_SHOWN
                                 else line[:MAX_LINE_SHOWN] + f" … [{len(line) - MAX_LINE_SHOWN} more characters]"
                                 for line in text.split("\n"))
            self.console_writer.write(text, bold, ansi=True)
            return
        if not self._hidden_chars:
            self.console_writer.write(
                f"\n[The output is very long, so the rest isn't shown here (that would freeze the app). "
                f"The last {OUTPUT_TAIL_LINES} lines are shown when the command finishes, and all of "
                f"it is saved to {self._full_output_path} (Full Output… below).]\n", True, ansi=False)
            self.full_output_button.show()
        self._hidden_chars += len(text)
        lines = (self._tail_partial + text).split("\n")
        self._tail_partial = lines.pop()
        self._hidden_lines += len(lines)
        self._tail.extend(line[:MAX_LINE_SHOWN] for line in lines)

    def _begin_output(self):
        """A command is starting: reset the output limit and the saved copy."""
        self._end_output(show_tail=False)
        self._decoder.reset()
        for links in self.output_links:
            links.forget()  # (files may have come or gone)
        self._shown_chars = self._hidden_chars = self._hidden_lines = 0
        self._tail.clear()
        self._tail_partial = ""
        self.full_output_button.hide()
        try:
            self._full_output = open(self._full_output_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
        except OSError:
            self._full_output = None

    def _end_output(self, show_tail: bool = True):
        """The command has finished: show the end of any output that wasn't shown."""
        if self._full_output is not None:
            try:
                self._full_output.close()
            except OSError:
                pass
            self._full_output = None
        if show_tail and self._hidden_chars:
            tail = list(self._tail) + ([self._tail_partial] if self._tail_partial else [])
            skipped = self._hidden_lines + bool(self._tail_partial) - len(tail)
            self.console_writer.write(
                f"\n[… {skipped:,} lines ({self._hidden_chars / 1e6:.1f} MB) not shown — "
                f"full output: {self._full_output_path} …]\n", True, ansi=False)
            self.console_writer.write("\n".join(tail) + "\n", ansi=True)
            self._hidden_chars = 0

    def _show_marked_output(self, monitor, name: str):
        """Show one test's or program's output (or everything when empty)."""
        if not name:
            self.show_all_output()
            return
        self.build_monitor.clear_selection()
        other = self.test_monitor if monitor is self.run_monitor else self.run_monitor
        if other.selected:
            other.selected = ""
            other.table.viewport().update()
        self._show_single_output(name, monitor.output_for(name) or "(no output)")

    def show_test_output(self, name: str):
        """Show one test's output (or everything when name is empty)."""
        self._show_marked_output(self.test_monitor, name)

    def show_build_details(self, row: int):
        """Show one file's compiler messages (or everything when row is -1)."""
        if row < 0:
            self.show_all_output()
            return
        for monitor in (self.test_monitor, self.run_monitor):
            if monitor.selected:
                monitor.selected = ""
                monitor.table.viewport().update()
        self._show_single_output(self.build_monitor.details_title(row),
                                 self.build_monitor.details_text(row))

    def _show_single_output(self, title: str, text: str):
        self.test_output.clear()
        self.single_writer.reset()
        self.single_writer.write(text)
        self.test_output.moveCursor(QTextCursor.Start)
        self.output_title.setText(f"<b>{_breakable(html.escape(title))}</b>")
        self.output_title.show()
        self.output_stack.setCurrentWidget(self.test_output)
        self.show_all_button.show()

    def show_all_output(self):
        self.output_title.hide()
        self.output_stack.setCurrentWidget(self.console)
        self.show_all_button.hide()
        self.build_monitor.clear_selection()
        for monitor in (self.test_monitor, self.run_monitor):
            if monitor.selected:
                monitor.selected = ""
                monitor.table.viewport().update()
