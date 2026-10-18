"""Debugging (see plan/debugger.md): the Debug menu, the Debug panel, and
breakpoints in the editors' gutters.

A program, a test or an example (*Start Debugging* (F5: the kind debugged
last), *Debug a Test…*, *Debug an Example…*, or a row's right-click in the
Tests or Run panel) is run as fpm runs it (``fpm run``, ``fpm test``,
``fpm run --example``, with the Build options), but with the collecting
runner (`vibefpmgui.test_runner` ``--collect``): fpm builds (the Build panel
shows it), then says which executable it would run, and how; that one then
runs under the debugger (`vibefpmgui.debug_session`). Several (no Target
given): which one is asked.

Breakpoints are kept per file (absolute path → lines), with the open files'
own, which move with their edits (`CodeEditor.breakpoint_lines`); they're
saved with the project's open files (its session).
"""

from __future__ import annotations

import json
from pathlib import Path

from qtpy.QtCore import QTimer
from qtpy.QtGui import QAction, QKeySequence, QPalette
from qtpy.QtWidgets import QApplication, QInputDialog, QMessageBox

from .. import ansi
from ..breakpoints_list import DISABLED, ENABLED, UNPLACED, BreakpointEntry
from ..complexity import FORTRAN
from ..debug_adapters import NOT_FOUND, find_adapter
from ..debug_panel import DebugPanel
from ..debug_session import IDLE, STOPPED, DebugSession, Frame
from ..editor import EditorTab
from ..fpm_args import runner_command, with_runner
from ..icons import make_icon
from ..output_links import OutputLinks
from ..test_monitor import EXE_RE

# What can be debugged: fpm's command for it, and what it's called.
KINDS = {"program": (["run"], "program"), "test": (["test"], "test"), "example": (["run", "--example"], "example")}

# The files a debugger can stop in: Fortran (and its include files), and C and C++.
DEBUGGABLE = FORTRAN | {".inc", ".fi", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh"}


def debuggable(path: Path) -> bool:
    return path.suffix.lower() in DEBUGGABLE


class DebuggingMixin:
    """Debugging (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_debugging(self):
        """The Debug menu, its actions and the Debug panel's widget."""
        self.debug_session = DebugSession(self)
        self.breakpoints: dict[str, list[int]] = {}  # file (absolute) → lines, the closed files' too
        self.disabled_breakpoints: dict[str, list[int]] = {}  # file → those of its lines disabled (kept, hollow)
        self._breakpoint_problems: dict[str, dict[int, str]] = {}  # file → {line: why it wasn't placed}
        self._debug_candidates: list[tuple[str, list[str]]] = []  # (name, command line) fpm said it would run
        self._debug_adapter = None  # the debugger the session being started uses
        self._debug_kind, self._debug_targets = "program", None  # what F5 debugs: the kind debugged last

        def action(text, keys, slot, tip):
            item = QAction(text, self)
            if keys:
                item.setShortcut(QKeySequence(keys))
            item.setStatusTip(tip)
            item.setToolTip(f"{text} ({QKeySequence(keys).toString(QKeySequence.NativeText)})" if keys else text)
            item.triggered.connect(slot)
            return item

        self.debug_start_action = action("Start Debugging", "F5", lambda: self.start_debugging(),
                                         "Build, then run the program under the debugger (Target: which one; "
                                         "a test or an example, if one was debugged last)")
        self.debug_test_action = action("Debug a Test…", "", lambda: self.start_debugging("test"),
                                        "Build the tests, then run one under the debugger (Target: which)")
        self.debug_example_action = action("Debug an Example…", "", lambda: self.start_debugging("example"),
                                           "Build the examples, then run one under the debugger (Target: which)")
        self.debug_stop_action = action("Stop Debugging", "Shift+F5", self.debug_session.stop,
                                        "End the program being debugged")
        self.debug_pause_action = action("Pause", "", self.debug_session.pause, "Pause the program where it is")
        self.step_over_action = action("Step Over", "F10", self.debug_session.step_over,
                                       "Run the current line, calls and all")
        self.step_in_action = action("Step Into", "F11", self.debug_session.step_in,
                                     "Into the procedure the current line calls")
        self.step_out_action = action("Step Out", "Shift+F11", self.debug_session.step_out,
                                      "On to where the current procedure returns")
        self.toggle_breakpoint_action = action("Toggle Breakpoint", "F9", self.toggle_breakpoint,
                                               "Set or remove a breakpoint on the cursor's line")
        self.enable_breakpoint_action = action("Enable/Disable Breakpoint", "Ctrl+F9", self.toggle_breakpoint_enabled,
                                               "Disable the breakpoint on the cursor's line (kept, drawn hollow: "
                                               "the debugger doesn't stop there), or enable it again")
        self.disable_all_breakpoints_action = action("Disable All Breakpoints", "",
                                                     lambda: self.set_all_breakpoints_enabled(),
                                                     "Every breakpoint, in every file, disabled (kept, hollow); "
                                                     "Enable All Breakpoints, when none is enabled")
        self.clear_breakpoints_action = action("Remove All Breakpoints", "", self.remove_all_breakpoints,
                                               "Every breakpoint, in every file")
        menu = self.menuBar().addMenu("&Debug")
        menu.addActions([self.debug_start_action, self.debug_test_action, self.debug_example_action])
        menu.addSeparator()
        menu.addActions([self.debug_pause_action, self.debug_stop_action])
        menu.addSeparator()
        menu.addActions([self.step_over_action, self.step_in_action, self.step_out_action])
        menu.addSeparator()
        menu.addActions([self.toggle_breakpoint_action, self.enable_breakpoint_action,
                         self.disable_all_breakpoints_action, self.clear_breakpoints_action])
        # Icons (the panel's buttons show only them; their tooltips say what they do), drawn
        # in the theme's text color as the toolbar's are, and again when it changes.
        for item, icon in ((self.debug_start_action, "run"), (self.debug_pause_action, "pause"),
                           (self.step_over_action, "step-over"), (self.step_in_action, "step-into"),
                           (self.step_out_action, "step-out"), (self.debug_stop_action, "stop")):
            self.command_icons[item] = icon
        self._update_command_icons()
        self.debug_panel = DebugPanel(self.debug_session, [
            self.debug_start_action, self.debug_pause_action, self.step_over_action, self.step_in_action,
            self.step_out_action, self.debug_stop_action],
            [self.disable_all_breakpoints_action, self.clear_breakpoints_action])
        listed = self.debug_panel.breakpoints
        listed.open_requested.connect(lambda path, line: self.navigate_to(path, line, 0))
        listed.enabled_toggled.connect(self.toggle_listed_breakpoint)
        listed.remove_requested.connect(self.remove_breakpoint)
        listed.mono = self._mono_font()  # (the code: as the Search panel's lines; see window.view for changes)
        listed.lines_for = self._source_lines_of  # (its syntax colors: the lines before for context)
        self._source_lines: dict[str, tuple[float, list[str]]] = {}  # closed file -> (mtime, lines), for the list
        # (An open file's edits move its breakpoints and change their code: the list again, once typing pauses.)
        self._breakpoint_list_timer = QTimer(self)
        self._breakpoint_list_timer.setSingleShot(True)
        self._breakpoint_list_timer.setInterval(300)
        self._breakpoint_list_timer.timeout.connect(self._refresh_breakpoint_list)
        self.debug_panel.frame_chosen.connect(self._frame_chosen)
        # gdb's "Breakpoint 3 at 0x…: file test/x.f90, line 155." (and any other location) open on a click.
        self.debug_links = OutputLinks(self.debug_panel.console,
                                       lambda: self.manifest_path.parent if self.manifest_path else None)
        self.debug_links.open_requested.connect(lambda path, line, column: self.navigate_to(path, line, column,
                                                                                           preview=True))
        s = self.debug_session
        s.state_changed.connect(self._debug_state_changed)
        s.stopped_at.connect(self._debug_stopped)
        s.breakpoints_checked.connect(self._breakpoints_checked)
        s.ended.connect(self._debug_ended)
        self._debug_state_changed(IDLE)

    # ---------------------------------------------------------- starting
    def start_debugging(self, kind: str | None = None, targets: list[str] | None = None):
        """Debug → Start Debugging (Continue, while paused): a ``kind`` of
        executable ("program", "test", "example"; None: the kind debugged
        last), ``targets``: these (else the Target field's)."""
        session = self.debug_session
        if session.state == STOPPED and kind is None:
            session.continue_()
            return
        if kind is None:
            kind, targets = self._debug_kind, self._debug_targets
        if session.is_active() or not self.manifest_path or self.busy():
            return
        adapter = find_adapter()
        if adapter is None:
            QMessageBox.information(self, "Start Debugging", NOT_FOUND)
            return
        if not self.selected_features and self.profile_combo.currentText() == "release" and QMessageBox.question(
                self, "Start Debugging",
                "The release profile builds without debug information: the debugger can't show lines or "
                "variables. Debug anyway? (Choose the debug profile in Build options to debug properly.)",
        ) != QMessageBox.Yes:
            return
        sub, _word = KINDS[kind]
        args = self.debug_arguments(kind, targets, runner_command(collect=True))
        if args is None:
            return
        self._debug_adapter, self._debug_candidates = adapter, []
        self._debug_kind, self._debug_targets = kind, targets
        self.debug_panel.begin("…")
        self.debug_links.forget()  # (which files exist: looked at again)
        self.statusBar().showMessage("Building, to debug…")
        self.run_fpm(sub[:1], True, False, phase="debug", args=args)

    def debug_arguments(self, kind: str, targets: list[str] | None, runner: str) -> list[str] | None:
        """fpm's arguments to build and run a ``kind`` of executable (`KINDS`)
        with the Build options, with debug information, through ``runner``
        (None: a field's quotes don't match, which is said)."""
        try:
            args = self.build_arguments(KINDS[kind][0], True, targets=targets)
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid arguments", str(exc))
            return None
        if not self.selected_features and self.profile_combo.currentIndex() <= 0:
            # (No profile: fpm (0.13) then passes the compiler none of its flags, -g
            # among them: no debug information. The debug profile's, then.)
            args[1:1] = ["--profile", "debug"]
        return with_runner(args, runner)

    def debug_named(self, kind: str, name: str):
        """A test (or program) to debug, picked in the Tests (or Run) panel."""
        if self.debug_session.is_active():
            self.debug_session.stop()
        self.start_debugging(kind, [name])

    def _debug_line(self, raw: str) -> str:
        """A line of "fpm run --runner '… --collect'": the build's output, or a
        program's command line (taken out, kept for _after_debug_build)."""
        line = ansi.strip(raw)
        match = EXE_RE.search(line)
        if match is None:
            if self._test_building:
                self.build_monitor.feed_line(line)
            return raw + "\n"
        if self._test_building:  # (built: fpm runs the programs now)
            self._test_building = False
            if self._monitoring:
                self.build_monitor.finish(0)
                self._update_tree_marks()
                self._monitoring = False
        try:
            argv = [str(a) for a in json.loads(match.group(2))]
        except ValueError:
            return raw + "\n"
        self._debug_candidates.append((match.group(1), argv))
        return ""

    def _after_debug_build(self, code: int, stopped: bool):
        """fpm has built the programs and said how it would run them: debug one."""
        candidates = self._debug_candidates
        if stopped or code != 0 or not candidates:
            word = KINDS[self._debug_kind][1]
            why = ("stopped" if stopped else "the build failed (see Build and Output)" if code != 0
                   else f"fpm ran no {word} (Target: a {word}'s name)")
            self.debug_panel.append("console", f"[not debugging: {why}]\n")
            self.statusBar().showMessage(f"Not debugging: {why}", 6000)
            return
        if len(candidates) > 1:
            names = [name for name, _argv in candidates]
            name, ok = QInputDialog.getItem(self, "Start Debugging", f"Debug which {KINDS[self._debug_kind][1]}?",
                                            names, 0, False)
            if not ok:
                return
            candidates = [c for c in candidates if c[0] == name]
        name, argv = candidates[0]
        self.debug_panel.begin(name)
        self.show_panel(self.debug_panel)
        self.debug_session.start(self._debug_adapter, argv[0], argv[1:], str(self.manifest_path.parent),
                                 self.all_breakpoints())
        self.statusBar().showMessage(f"Debugging {name} ({self._debug_adapter.title})", 5000)

    # ---------------------------------------------------------- while debugging
    def _debug_state_changed(self, state: str):
        active, stopped = state != IDLE, state == STOPPED
        start = self.debug_start_action
        start.setText("Continue" if stopped else "Start Debugging")
        start.setToolTip(f"{start.text()} ({start.shortcut().toString(QKeySequence.NativeText)})")
        icon = "continue" if stopped else "run"
        if self.command_icons.get(start) != icon:
            self.command_icons[start] = icon
            start.setIcon(make_icon(icon, QApplication.palette().color(QPalette.WindowText)))
        self.debug_start_action.setEnabled(stopped or not active)
        self.debug_test_action.setEnabled(not active)
        self.debug_example_action.setEnabled(not active)
        self.debug_stop_action.setEnabled(active)
        self.debug_pause_action.setEnabled(state == "running")
        for step in (self.step_over_action, self.step_in_action, self.step_out_action):
            step.setEnabled(stopped)
        if not stopped:
            self._show_execution_line(None)
            self.debug_panel.breakpoints.show_hit(None)

    def _debug_stopped(self, frames: list[Frame], why: str):
        self.show_panel(self.debug_panel)
        self._show_frame(self.debug_session.frame_index)
        self.debug_panel.breakpoints.show_hit(self._stopped_at_breakpoint(frames))
        if why == "exception":
            self.statusBar().showMessage("Stopped: an error in the program (see the Debug panel's call stack)", 8000)

    def _stopped_at_breakpoint(self, frames: list[Frame]) -> tuple[str, int] | None:
        """The breakpoint the program stopped at (file, line as set), if any:
        the one the debugger says it hit (maybe moved by it to the next line
        with code), else one on the line it stopped at."""
        self._refresh_breakpoints()
        for path, line in self.debug_session.hit_breakpoints:
            if line in self.breakpoints.get(path, ()):
                return path, line
        top = frames[0] if frames else None
        if top is not None and top.path:
            path = str(Path(top.path).resolve())
            if top.line in self.breakpoints.get(path, ()):
                return path, top.line
        return None

    def _frame_chosen(self, index: int):
        self.debug_session.select_frame(index)
        self._show_frame(index)

    def _show_frame(self, index: int):
        """A frame's place: its file opened at its line, the arrow there."""
        frames = self.debug_session.frames
        frame = frames[index] if 0 <= index < len(frames) else None
        if frame is None or not frame.path or not Path(frame.path).is_file():
            self._show_execution_line(None)
            return
        self.navigate_to(frame.path, frame.line, 0, record=False)
        self._show_execution_line((Path(frame.path).resolve(), frame.line))

    def _show_execution_line(self, where: tuple[Path, int] | None):
        if not hasattr(self, "tabs"):
            return  # (the window being built: no editors yet)
        for editor in self.editors():
            editor.editor.set_execution_line(where[1] if where and not editor.untitled and editor.path == where[0]
                                             else None)

    def _debug_ended(self, why: str):
        self._show_execution_line(None)
        self.statusBar().showMessage(f"Debugging ended: {why}", 6000)

    # ---------------------------------------------------------- breakpoints
    def _debug_editor_added(self, editor: EditorTab):
        """A file opened in the editor: its breakpoint column and breakpoints."""
        code = editor.editor
        code.debuggable = not editor.untitled and debuggable(editor.path)
        code.update_gutter_width()
        if code.debuggable:
            key = str(editor.path)
            code.set_breakpoint_lines(self.breakpoints.get(key, []), self.disabled_breakpoints.get(key, []))
            code.set_breakpoint_problems(self._breakpoint_problems.get(key, {}))
        code.breakpoints_changed.connect(lambda e=editor: self._editor_breakpoints_changed(e))
        editor.path_changed.connect(lambda _p, e=editor: self._debug_path_changed(e))
        code.document().contentsChanged.connect(
            lambda c=code: c.breakpoint_lines() and self._breakpoint_list_timer.start())

    def _debug_path_changed(self, editor: EditorTab):
        """Saved under another name (or a new file saved): a source file or not now."""
        editor.editor.debuggable = not editor.untitled and debuggable(editor.path)
        editor.editor.refresh_gutter()
        self._refresh_breakpoint_list()

    def _editor_breakpoints_changed(self, editor: EditorTab):
        if editor.untitled:
            return
        self._keep_breakpoints(editor)
        key = str(editor.path)
        self._breakpoint_problems.pop(key, None)
        editor.editor.set_breakpoint_problems({})
        self.debug_session.set_breakpoints(key, editor.editor.enabled_breakpoints())
        self._update_breakpoint_actions()

    def _keep_breakpoints(self, editor: EditorTab):
        """An open file's breakpoints, where its edits moved them, kept (for
        when it's closed, saved, or debugged)."""
        if editor.untitled or not editor.editor.debuggable:
            return
        key, code = str(editor.path), editor.editor
        for store, lines in ((self.breakpoints, code.breakpoint_lines()),
                             (self.disabled_breakpoints, code.disabled_breakpoints())):
            if lines:
                store[key] = lines
            else:
                store.pop(key, None)

    def _debug_editor_closing(self, editor: EditorTab):
        self._keep_breakpoints(editor)

    def _refresh_breakpoints(self):
        for editor in self.editors():
            self._keep_breakpoints(editor)

    def all_breakpoints(self) -> dict[str, list[int]]:
        """The breakpoints the debugger stops at: every enabled one (the open
        files' where their edits moved them)."""
        self._refresh_breakpoints()
        out = {}
        for path, lines in self.breakpoints.items():
            off = set(self.disabled_breakpoints.get(path, ()))
            enabled = [n for n in lines if n not in off]
            if enabled:
                out[path] = enabled
        return out

    def _breakpoints_checked(self, path: str, states: dict):
        problems = self._breakpoint_problems.setdefault(path, {})
        for line, state in states.items():
            if state.verified:
                problems.pop(line, None)
            else:
                problems[line] = state.message
        editor = self._open_editor_for(Path(path))
        if editor is not None:
            editor.editor.set_breakpoint_problems(problems)
        self._refresh_breakpoint_list()

    def _cursor_line(self) -> tuple[EditorTab | None, int]:
        editor = self.current_editor()
        return editor, (editor.editor.textCursor().blockNumber() + 1 if editor is not None else 0)

    def toggle_breakpoint(self):
        """Debug → Toggle Breakpoint (F9): set or remove the one on the cursor's line."""
        editor, line = self._cursor_line()
        if editor is not None:
            editor.editor.toggle_breakpoint(line)

    def toggle_breakpoint_enabled(self):
        """Debug → Enable/Disable Breakpoint (Ctrl+F9): the one on the cursor's
        line disabled (kept, hollow) or enabled again; none there: one set."""
        editor, line = self._cursor_line()
        if editor is not None:
            editor.editor.toggle_breakpoint_enabled(line)

    def any_enabled_breakpoint(self) -> bool:
        return bool(self.all_breakpoints())

    def set_all_breakpoints_enabled(self, enabled: bool | None = None):
        """Debug → Disable All Breakpoints (Enable All, when none is enabled):
        every breakpoint, in every file, kept."""
        if enabled is None:
            enabled = not self.any_enabled_breakpoint()
        self._refresh_breakpoints()
        for path, lines in list(self.breakpoints.items()):
            if enabled:
                self.disabled_breakpoints.pop(path, None)
            else:
                self.disabled_breakpoints[path] = list(lines)
            editor = self._open_editor_for(Path(path))
            if editor is not None:
                editor.editor.set_breakpoints_enabled(lines, enabled)  # (it tells the debugger)
            else:
                self.debug_session.set_breakpoints(path, list(lines) if enabled else [])
        self._update_breakpoint_actions()

    def remove_all_breakpoints(self):
        self._refresh_breakpoints()
        for path in list(self.breakpoints):
            self.debug_session.set_breakpoints(path, [])
        self.breakpoints.clear()
        self.disabled_breakpoints.clear()
        self._breakpoint_problems.clear()
        for editor in self.editors():
            editor.editor.set_breakpoint_lines([])
        self._update_breakpoint_actions()

    def _update_breakpoint_actions(self):
        """Disable All, or Enable All when none is enabled (but some are disabled)."""
        if not hasattr(self, "disable_all_breakpoints_action"):
            return
        none_enabled = not self.any_enabled_breakpoint() and any(self.breakpoints.values())
        self.disable_all_breakpoints_action.setText("Enable All Breakpoints" if none_enabled
                                                    else "Disable All Breakpoints")
        self._refresh_breakpoint_list()

    # ---------------------------------------------------------- the breakpoints list
    def _refresh_breakpoint_list(self):
        """The Debug panel's list: every breakpoint, its state, place and code."""
        if not hasattr(self, "debug_panel"):
            return
        self._refresh_breakpoints()
        root = self.manifest_path.parent.resolve() if self.manifest_path else None
        entries = []
        for path, lines in self.breakpoints.items():
            off = set(self.disabled_breakpoints.get(path, ()))
            problems = self._breakpoint_problems.get(path, {})
            code = self._source_lines_of(path)
            try:
                shown = Path(path).relative_to(root).as_posix() if root else Path(path).name
            except ValueError:  # (outside the project: a dependency's, say)
                shown = Path(path).name
            for line in lines:
                state = DISABLED if line in off else UNPLACED if line in problems else ENABLED
                entries.append(BreakpointEntry(path, line, state, f"{shown}:{line}",
                                               code[line - 1] if 0 < line <= len(code) else "", problems.get(line, "")))
        entries.sort(key=lambda e: (e.shown.rsplit(":", 1)[0].lower(), e.line))
        self.debug_panel.breakpoints.set_entries(entries)

    def _source_lines_of(self, path: str) -> list[str]:
        """A file's lines: the editor's if it's open, else the file's (read once, until it changes)."""
        editor = self._open_editor_for(Path(path))
        if editor is not None:
            return editor.editor.toPlainText().split("\n")
        try:
            mtime = Path(path).stat().st_mtime
            cached = self._source_lines.get(path)
            if cached is None or cached[0] != mtime:
                cached = (mtime, Path(path).read_text(errors="replace").splitlines())
                self._source_lines[path] = cached
            return cached[1]
        except OSError:
            return []

    def toggle_listed_breakpoint(self, path: str, line: int):
        """The list's Enable/Disable Breakpoint."""
        self.set_breakpoint_enabled(path, line, line in self.disabled_breakpoints.get(path, ()))

    def set_breakpoint_enabled(self, path: str, line: int, enabled: bool):
        """One breakpoint enabled or disabled, its file open or not."""
        editor = self._open_editor_for(Path(path))
        if editor is not None:
            editor.editor.set_breakpoints_enabled([line], enabled)  # (it tells the debugger, and the list)
            return
        if line not in self.breakpoints.get(path, ()):
            return
        off = set(self.disabled_breakpoints.get(path, ()))
        if enabled:
            off.discard(line)
        else:
            off.add(line)
        if off:
            self.disabled_breakpoints[path] = sorted(off)
        else:
            self.disabled_breakpoints.pop(path, None)
        self._closed_file_breakpoints_changed(path)

    def remove_breakpoint(self, path: str, line: int):
        """One breakpoint removed, its file open or not."""
        editor = self._open_editor_for(Path(path))
        if editor is not None:
            if line in editor.editor.breakpoint_lines():
                editor.editor.toggle_breakpoint(line)  # (it tells the debugger, and the list)
            return
        lines = [n for n in self.breakpoints.get(path, ()) if n != line]
        off = [n for n in self.disabled_breakpoints.get(path, ()) if n != line]
        for store, kept in ((self.breakpoints, lines), (self.disabled_breakpoints, off)):
            if kept:
                store[path] = kept
            else:
                store.pop(path, None)
        self._closed_file_breakpoints_changed(path)

    def _closed_file_breakpoints_changed(self, path: str):
        self._breakpoint_problems.pop(path, None)
        off = set(self.disabled_breakpoints.get(path, ()))
        self.debug_session.set_breakpoints(path, [n for n in self.breakpoints.get(path, ()) if n not in off])
        self._update_breakpoint_actions()

    def set_session_breakpoints(self, entries: list):
        """The breakpoints saved with the project's session (see _save_session)."""
        self.breakpoints, self.disabled_breakpoints = {}, {}
        for entry in entries or []:
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                key = str(Path(entry["path"]))
                lines = sorted({int(n) for n in entry.get("lines") or [] if isinstance(n, int) and n > 0})
                off = sorted({int(n) for n in entry.get("disabled") or [] if isinstance(n, int)} & set(lines))
                if lines:
                    self.breakpoints[key] = lines
                if off:
                    self.disabled_breakpoints[key] = off
        self._breakpoint_problems = {}
        self._update_breakpoint_actions()

    def session_breakpoints(self) -> list[dict]:
        self._refresh_breakpoints()
        return [{"path": path, "lines": lines, **({"disabled": self.disabled_breakpoints[path]}
                                                  if self.disabled_breakpoints.get(path) else {})}
                for path, lines in sorted(self.breakpoints.items()) if lines]
