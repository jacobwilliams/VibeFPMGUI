"""Test and Run: listing the tests or programs, running them, in parallel too.

Each one runs through the marker wrapper (`vibefpmgui.test_runner`), so the
Tests and Run panels know where each one starts and ends.
"""

from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import Path

from qtpy.QtCore import QProcessEnvironment
from qtpy.QtWidgets import QMessageBox

from .. import ansi
from .. import coverage as cov
from ..build_monitor import BuildMonitor
from ..compat import join_args, split_args
from ..coverage_panel import CoveragePanel
from ..fpm_args import runner_command, split_runner
from ..parallel_tests import ParallelTests
from ..processes import kill_process_tree
from ..run_monitor import RunMonitor
from ..test_monitor import EXE_RE, MARKER_RE, TestMonitor, parse_test_list
from ..test_runner import split_runner as split_inner_runner


class TestingMixin:
    """Test and Run (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_run_panels(self):
        """The Build, Tests, Run and Coverage panels."""
        self.build_monitor = BuildMonitor()
        self.test_monitor = TestMonitor()
        self.test_monitor.test_selected.connect(self.show_test_output)
        self.test_monitor.debug_requested.connect(lambda name: self.debug_named("test", name))
        self.test_monitor.build_percent = lambda: self.build_monitor.percent
        self.build_monitor.details_requested.connect(self.show_build_details)
        self.build_monitor.open_file_requested.connect(self.open_file)
        self.build_monitor.compile_command_requested.connect(self.show_compile_command)
        self.run_monitor = RunMonitor()
        self.run_monitor.debug_requested.connect(lambda name: self.debug_named("program", name))
        self.run_monitor.test_selected.connect(
            lambda name: self._show_marked_output(self.run_monitor, name))
        self.run_monitor.build_percent = lambda: self.build_monitor.percent
        self.coverage_panel = CoveragePanel()
        self.coverage_panel.open_requested.connect(
            lambda path, line: self.navigate_to(path, line, 0, preview=True))
        self.coverage_panel.tests_check.setChecked(self.settings.value("coverage_tests", False, type=bool))
        self.coverage_panel.tests_check.toggled.connect(lambda on: self.settings.setValue("coverage_tests", on))
        self.coverage: dict[Path, cov.FileCoverage] = {}  # the last test run's (see _collect_coverage)
        self._coverage_started = 0.0  # when that run started
        self._coverage_collector: cov.Collector | None = None

    def _marker_monitor(self):
        return self.run_monitor if getattr(self, "_marker_kind", "test") == "run" else self.test_monitor

    def run_tests(self):
        self._start_marked("test")

    def run_programs(self):
        self._start_marked("run")

    def _start_marked(self, kind: str):
        """Test or Run in two steps: "fpm <kind> --list" (the names), then
        "fpm <kind> --runner <our wrapper>", which builds first (shown in the
        Build tab) and then runs each one; the wrapper marks where each starts
        and ends, so the Tests / Run tab can follow them."""
        if not self.manifest_path or self.busy():
            return
        try:
            list_args = self.build_arguments([kind, "--list"], True, program_args=False)
            run_args = self.build_arguments([kind], True)
            _, inner_runner = split_runner(split_args(self.extra_edit.text()))
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid arguments", str(exc))
            return
        runner = runner_command()
        # Our runner goes before the target names and any "-- program args".
        cut = run_args.index("--") if "--" in run_args else len(run_args)
        collect_args = run_args[:cut] + ["--runner", runner_command(collect=True)] + run_args[cut:]
        run_args = run_args[:cut] + ["--runner", runner] + run_args[cut:]
        patterns = split_args(self.target_edit.text())
        self._marker_kind = kind
        self._test_args = (run_args, inner_runner, patterns, collect_args)
        description = self._build_description()
        if inner_runner:
            description += f" · runner: {inner_runner}"
        self.build_monitor.reset()  # the build progress shown next is this run's
        if kind == "test" and self.coverage_check.isChecked():
            cov.clear_counts(self.manifest_path.parent / "build")  # (this run's counts only)
            self._coverage_started = time.time()  # (files changed after this aren't what was compiled)
        self._marker_monitor().begin_listing(description)
        self.show_all_output()
        self.run_fpm([kind], True, False, phase="list", args=list_args)

    def _after_listing(self, code: int, stopped: bool):
        monitor = self._marker_monitor()
        output = "".join(self._captured)
        names = parse_test_list(output)
        if stopped or code != 0:
            monitor.finish(code, stopped=stopped)
            self.show_panel(self.build_monitor)
            return
        if names == []:
            monitor.finish(0, no_tests=True)
            self.show_panel(monitor)
            self.statusBar().showMessage(monitor.word("none"))
            return
        run_args, inner_runner, patterns, collect_args = self._test_args
        if names and patterns:
            # "--list" lists every name regardless of the names given, so apply
            # the Target names/globs ourselves, as fpm will.
            names = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)]
        # fpm builds first (the Build tab shows it), then runs them.
        monitor.start(names or [], self._load_durations(self._marker_kind), building=True)
        jobs = self.test_jobs_spin.value() if self._marker_kind == "test" else 1
        monitor.jobs = jobs
        if jobs > 1:
            monitor.description += f" · {jobs} at a time"
            monitor.update_header()  # fpm only builds them and says how to run each; we run them (see _after_collect)
            self._collected: list[tuple[str, list[str]]] = []
            self.run_fpm(["test"], True, False, phase="collect", args=collect_args)
            return
        env = {"VIBEFPM_INNER_RUNNER": inner_runner} if inner_runner else {}
        self.run_fpm([self._marker_kind], True, True, phase="marked", args=run_args, env_extra=env)

    def _after_collect(self, code: int, stopped: bool):
        """fpm has built the tests and said how to run each: run them, a few
        at a time (Build options → Parallel tests)."""
        if stopped or code != 0 or not self._collected:
            self.reload()  # (first, as after "fpm test": then the message is the tests')
            self._after_marked(code, stopped)  # (stopped, the build failed, or no tests)
            return
        _run_args, inner_runner, _patterns, _collect = self._test_args
        try:
            inner = split_inner_runner(inner_runner)
        except ValueError:
            inner = []
        expected = self._load_durations("test")
        # The longest first (the ones never timed first of all): the run ends sooner.
        commands = sorted(self._collected, key=lambda c: -expected.get(c[0], float("inf")))
        env = QProcessEnvironment.systemEnvironment()
        env.insert("NO_COLOR", "1")
        workers = min(self.test_jobs_spin.value(), len(commands))
        tests = ParallelTests(commands, workers, str(self.manifest_path.parent), env, inner, kill_process_tree, self)
        tests.started.connect(self._parallel_started)
        tests.output.connect(self._parallel_output)
        tests.finished.connect(self._parallel_finished)
        tests.done.connect(self._parallel_done)
        self.parallel_tests = tests
        self._parallel_raw: dict[str, list[str]] = {}
        self._parallel_failed = False
        self._append(f"\n[Running {len(commands)} tests, {workers} at a time. Each test's output is shown "
                     "here when it finishes; click a test in the Tests panel to watch it as it runs.]\n", bold=True)
        self.stop_action.setEnabled(True)
        self._set_project_enabled(True)
        self.statusBar().showMessage(f"Running tests, {workers} at a time…")
        tests.start()

    def _parallel_started(self, name: str):
        self._parallel_raw[name] = []
        self.test_monitor.test_started(name)

    def _parallel_output(self, name: str, text: str):
        self._parallel_raw.setdefault(name, []).append(text)
        plain = ansi.strip(text)
        self.test_monitor.test_output(name, plain)
        if self.test_monitor.selected == name:  # shown on its own: follow it
            self.single_writer.write(plain)

    def _parallel_finished(self, name: str, code: int, seconds: float, stopped: bool):
        self.test_monitor.test_finished(name, code, seconds, stopped)
        self._parallel_failed |= code != 0 and not stopped
        # Its output, all in one piece: the tests' outputs never mix here.
        status = "stopped" if stopped else "passed" if code == 0 else f"failed (exit code {code})"
        command = self.parallel_tests.command(dict(self._collected).get(name, [name])) \
            if self.parallel_tests is not None else [name]
        rel = [os.path.relpath(a, self.manifest_path.parent) if os.path.isabs(a) else a for a in command]
        self._append(f"\n── {name} · {status} · {seconds:.2f} s ── $ {join_args(rel)}\n", bold=True)
        text = "".join(self._parallel_raw.pop(name, []))
        if text:
            self._append(text if text.endswith("\n") else text + "\n", colored=True)

    def _parallel_done(self, stopped: bool):
        if self.parallel_tests is None:
            return
        self.parallel_tests.deleteLater()
        self.parallel_tests = None
        self.stop_action.setEnabled(False)
        self._set_project_enabled(self.graph is not None)
        code = 1 if self._parallel_failed else 0
        self._append("\n[tests stopped]\n" if stopped else f"\n[tests finished: exit code {code}]\n", bold=True)
        self.reload()  # (first, as after "fpm test": then the message is the tests')
        self._after_marked(code, stopped)

    def _after_marked(self, code: int, stopped: bool):
        monitor = self._marker_monitor()
        monitor.finish(code, stopped=stopped)
        self._save_durations(self._marker_kind, monitor.durations())
        if self._marker_kind == "test" and self.coverage_check.isChecked() and not stopped \
                and not monitor.build_failed:
            self._collect_coverage()
        counts = monitor.counts()
        total = len(monitor.tests)
        title = monitor.word("title")
        if stopped:
            message = f"{title}: stopped"
        elif monitor.build_failed:
            message = f"{title}: the build failed (see the Build tab)"
        elif counts["Failed"]:
            failed = [t for t in monitor.tests if t.status == "Failed"]
            if self._marker_kind == "run" and len(failed) == 1 and failed[0].exit_code is not None:
                message = f"{title}: {failed[0].name} failed (exit code {failed[0].exit_code})"
            else:
                message = f"{title}: {counts['Failed']} of {total} failed"
        elif counts["Not run"]:
            message = f"{title}: {counts['Passed']} {monitor.word('passed')}, {counts['Not run']} not run"
        elif total == 1:
            message = f"{title}: {monitor.tests[0].name} {monitor.word('passed')}"
        else:
            message = f"{title}: all {total} {monitor.word('passed')}"
        self.statusBar().showMessage(message)

    def _durations_key(self, kind: str = "test") -> str:
        return self._project_key(f"{kind}_durations")

    def _load_durations(self, kind: str = "test") -> dict[str, float]:
        try:
            data = json.loads(self.settings.value(self._durations_key(kind), "{}"))
            return {str(k): float(v) for k, v in data.items()}
        except (TypeError, ValueError, AttributeError):
            return {}

    def _save_durations(self, kind: str, durations: dict[str, float]):
        if durations and self.manifest_path:
            merged = {**self._load_durations(kind), **durations}
            self.settings.setValue(self._durations_key(kind), json.dumps(merged))

    def _test_line(self, raw: str) -> str:
        """Pass a line of test output through the test monitor; returns the
        text to show (with its color codes). The runner prints a blank line
        before each marker, which is dropped along with the marker."""
        line = ansi.strip(raw)
        if self._test_building:
            if MARKER_RE.search(line):
                self._build_part_done()  # the first test is starting
            else:
                self.build_monitor.feed_line(line)  # fpm is still compiling
        text = ""
        if self._held_blank:
            self._held_blank = False
            if not MARKER_RE.search(line):  # the blank was real output
                self._marker_monitor().feed_line("")
                text = "\n"
        if line == "":
            self._held_blank = True
            return text
        shown = self._marker_monitor().feed_line(line)
        if shown == line:
            shown = raw
        return text + (shown + "\n" if shown is not None else "")

    def _collect_line(self, raw: str) -> str:
        """A line of "fpm test --runner '… --collect'": the build's output,
        or a test's command line (taken out, kept for _after_collect)."""
        line = ansi.strip(raw)
        match = EXE_RE.search(line)
        if match is None:
            if self._test_building:
                self.build_monitor.feed_line(line)
            return raw + "\n"
        if self._test_building:
            self._build_part_done()
        try:
            argv = json.loads(match.group(2))
        except ValueError:
            return raw + "\n"
        self._collected.append((match.group(1), [str(a) for a in argv]))
        return ""
