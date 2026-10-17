"""Running fpm: the Build options, the commands and the features.

The output goes to `vibefpmgui.window.output`; Test and Run's steps are in
`vibefpmgui.window.testing`.
"""

from __future__ import annotations

import html
import os
import shutil
import tomllib

from qtpy.QtCore import QProcess, QProcessEnvironment, QSize, Qt
from qtpy.QtGui import QAction, QKeySequence, QPalette
from qtpy.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLineEdit,
    QMessageBox,
    QSpinBox,
    QToolBar,
    QToolButton,
    QWidget,
)

from .. import ansi, dep_updates
from .. import build_outputs as bo
from .. import coverage as cov
from .. import manifest as m
from ..compat import join_args, quote_arg, split_args
from ..fpm_args import split_runner, with_link_flag
from ..icons import make_icon
from ..processes import kill_process_tree
from ..widgets import MenuButton, StayOpenMenu

# (label, fpm sub-command, uses build options, refresh graph afterwards, icon, shortcut, description)
COMMANDS = [
    ("Build", ["build"], True, True, "build", "Ctrl+B", "Compile the project"),
    ("Run", ["run"], True, True, "run", "Ctrl+R", "Build and run the program(s)"),
    ("Test", ["test"], True, True, "test", "Ctrl+U", "Build and run the tests"),
    ("Update deps", ["update"], False, True, "update", "", "Fetch or update the dependencies"),
    ("Clean", ["clean", "--skip"], False, False, "clean", "",
     "Delete the build output, keeping downloaded dependencies"),
    ("Clean all", ["clean", "--all"], False, True, "clean-all", "",
     "Delete the build output including downloaded dependencies"),
]


STOP_COLOR = "#e03131"


BUILTIN_PROFILES = ["(default)", "debug", "release"]


class RunningMixin:
    """Running fpm (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_options_panel(self):
        """The Build options panel's fields (each one's tooltip: the fpm flag it becomes)."""
        options = QWidget()  # (in the "Build options" panel)
        form = QFormLayout(options)
        # macOS defaults to fields staying at their size hint; let them widen with the panel.
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(BUILTIN_PROFILES)
        self._build_config_row(form)  # (see vibefpmgui.window.configs)
        form.addRow("Profile:", self.profile_combo)
        self.features_button = MenuButton()  # (its menu: see _build_features_menu)
        form.addRow("Features:", self.features_button)
        self.compiler_edit = QLineEdit()
        env_fc = os.environ.get("FPM_FC", "").strip()
        self.compiler_edit.setPlaceholderText(f"{env_fc} (from FPM_FC)" if env_fc else "gfortran (default)")
        form.addRow("Compiler:", self.compiler_edit)
        self.flag_edit = QLineEdit()
        self.flag_edit.setPlaceholderText("e.g. -O3 -fcheck=all")
        form.addRow("Fortran flags:", self.flag_edit)
        self.target_edit = QLineEdit()
        self.target_edit.setPlaceholderText("all targets")
        form.addRow("Target:", self.target_edit)
        self.extra_edit = QLineEdit()
        self.extra_edit.setPlaceholderText("extra fpm arguments")
        form.addRow("Extra args:", self.extra_edit)
        self.runner_args_edit = QLineEdit()
        self.runner_args_edit.setPlaceholderText("arguments passed to the program")
        form.addRow("Program args:", self.runner_args_edit)
        self.test_jobs_spin = QSpinBox()
        self.test_jobs_spin.setRange(1, 256)
        self.test_jobs_spin.setSpecialValueText("off (one at a time)")
        self.test_jobs_spin.setSuffix(" at a time")
        form.addRow("Parallel tests:", self.test_jobs_spin)
        self.coverage_check = QCheckBox("Coverage")
        form.addRow(self.coverage_check)
        self.verbose_check = QCheckBox("Verbose")
        form.addRow(self.verbose_check)
        fpm_row = QHBoxLayout()
        self.fpm_edit = QLineEdit()
        self.fpm_edit.setPlaceholderText(shutil.which("fpm") or "fpm")
        fpm_browse = QToolButton()
        fpm_browse.setText("…")
        fpm_browse.clicked.connect(self._browse_fpm)
        fpm_row.addWidget(self.fpm_edit)
        fpm_row.addWidget(fpm_browse)
        # A line above it: the fpm executable isn't part of a build configuration (a path on this computer).
        divider = QFrame()
        divider.setFrameShape(QFrame.HLine)
        divider.setFrameShadow(QFrame.Sunken)
        form.addRow(divider)
        form.addRow("fpm executable:", fpm_row)

        # Hovering an option (or its label) shows the fpm flag it becomes.
        for widget, flag, text in [
            (self.profile_combo, "--profile PROF",
             "Build profile: debug or release. (default) leaves it to fpm, which uses debug."),
            (self.features_button, "--features LIST",
             "Features to enable (the project's fpm.toml declares them). Can't be combined with a profile."),
            (self.compiler_edit, "--compiler NAME", "Fortran compiler to use. fpm uses gfortran by default."),
            (self.flag_edit, "--flag FFLAGS", "Extra Fortran compiler flags."),
            (self.target_edit, "fpm run|test NAME…",
             "Names of the programs or tests to run (wildcards allowed). Used by Run and Test."),
            (self.extra_edit, "(added as typed)",
             "Any other fpm options, e.g. --runner \"mpirun -np 4\" or --c-compiler gcc."),
            (self.runner_args_edit, "-- ARGS",
             "Arguments passed to the program or tests by Run and Test."),
            (self.test_jobs_spin, "(not a flag)",
             (f"Run this many tests at once (this computer has {os.cpu_count() or 1} cores). fpm builds "
              "them; the app runs them, each with its own output. Off: fpm runs them one after another.")),
            (self.coverage_check, "--flag --coverage --link-flag --coverage",
             ("Build with gfortran's coverage counting (a build of its own): after Test, the Coverage panel "
              "and the editor's gutter show which lines the tests ran.")),
            (self.verbose_check, "--verbose", "Print more detail about what fpm is doing."),
            (self.fpm_edit, "(not a flag)",
             ("The fpm program to run. Leave empty to use the fpm on your PATH. Not part of a build "
              "configuration: it's kept for this computer.")),
        ]:
            tip = f"<b><code>{html.escape(flag)}</code></b><br>{html.escape(text)}"
            widget.setToolTip(tip)
            label = form.labelForField(widget) or form.labelForField(fpm_row)
            if label is not None and widget is not self.verbose_check:
                label.setToolTip(tip)
        self._watch_option_fields()

        return options

    def _build_commands(self):
        """The fpm commands as actions, shown in a toolbar and a Commands menu."""
        self.command_icons: dict[QAction, str] = {}

        def make(label, icon, shortcut, description, slot):
            action = QAction(label, self)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
                keys = QKeySequence(shortcut).toString(QKeySequence.NativeText)
                action.setToolTip(f"{label} ({keys}) — {description}")
            else:
                action.setToolTip(f"{label} — {description}")
            action.setStatusTip(description)
            action.triggered.connect(slot)
            self.command_icons[action] = icon
            return action

        actions = {}
        for label, args, uses_options, refresh, icon, shortcut, description in COMMANDS:
            if args == ["test"]:
                slot = self.run_tests
            elif args == ["run"]:
                slot = self.run_programs
            else:
                slot = lambda _=False, a=args, o=uses_options, r=refresh: self.run_fpm(a, o, r)
            actions[icon] = make(label, icon, shortcut, description, slot)
            self.command_actions.append(actions[icon])
        self.stop_action = make("Stop", "stop", "Ctrl+.", "Stop the running fpm command", self.stop)
        self.stop_action.setEnabled(False)

        groups = [[actions["build"], actions["run"], actions["test"], self.stop_action],
                  [actions["update"]],
                  [actions["clean"], actions["clean-all"]]]
        toolbar = QToolBar("Commands")
        toolbar.setObjectName("CommandsToolbar")
        toolbar.setMovable(False)
        toolbar.setFloatable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        toolbar.setIconSize(QSize(22, 22))
        toolbar.toggleViewAction().setEnabled(False)  # don't let it be hidden by accident
        menu = self.menuBar().addMenu("&Commands")
        for i, group in enumerate(groups):
            if i:
                toolbar.addSeparator()
                menu.addSeparator()
            toolbar.addActions(group)
            menu.addActions(group)
        menu.addSeparator()
        self.check_updates_action = menu.addAction("Check for Dependency Updates…")
        self.check_updates_action.setStatusTip(
            "Ask each git dependency's repository for newer tags and commits")
        self.check_updates_action.triggered.connect(self.check_dependency_updates)
        self.addToolBar(Qt.TopToolBarArea, toolbar)
        self.setUnifiedTitleAndToolBarOnMac(True)
        self.command_toolbar = toolbar
        self._update_command_icons()

    def _update_command_icons(self):
        """(Re)draw the toolbar icons in the current text color."""
        text = QApplication.palette().color(QPalette.WindowText)
        for action, name in self.command_icons.items():
            action.setIcon(make_icon(name, STOP_COLOR if name == "stop" else text))
        # The ">>" button shown when the window is too narrow for every
        # button. Qt 6.11 draws its default arrow with a macOS symbol image,
        # which crashes in AppKit on macOS 27, so give it our own icon.
        extension = self.command_toolbar.findChild(QToolButton, "qt_toolbar_ext_button")
        if extension is not None:
            extension.setIcon(make_icon("more", text))
        hover = "rgba(128,128,128,45)"
        self.command_toolbar.setStyleSheet(f"""
            QToolBar#CommandsToolbar {{ border: none; spacing: 2px; padding: 2px 6px; }}
            QToolBar#CommandsToolbar QToolButton {{ border: none; border-radius: 7px;
                padding: 4px 10px; }}
            QToolBar#CommandsToolbar QToolButton:hover {{ background: {hover}; }}
            QToolBar#CommandsToolbar QToolButton:pressed {{ background: rgba(128,128,128,80); }}
            QToolBar#CommandsToolbar QToolButton#qt_toolbar_ext_button {{ padding: 0px; }}
        """)

    def busy(self) -> bool:
        """A command is running (fpm, a program, or tests run in parallel)."""
        return self.process is not None or self.parallel_tests is not None

    def _set_project_enabled(self, enabled: bool):
        running = self.busy()
        for action in self.command_actions:
            action.setEnabled(enabled and not running)

    def effective_compiler(self) -> tuple[str, str]:
        """(compiler, where the choice comes from), the way fpm picks it:
        --compiler (the Compiler field), else $FPM_FC, else gfortran."""
        typed = self.compiler_edit.text().strip()
        if typed:
            return typed, "Compiler field in Build options"
        env = os.environ.get("FPM_FC", "").strip()
        if env:
            return env, "FPM_FC environment variable"
        return "gfortran", "fpm's default"

    def _browse_fpm(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select fpm executable")
        if filename:
            self.fpm_edit.setText(filename)

    def _fpm_executable(self) -> str:
        return self.fpm_edit.text().strip() or shutil.which("fpm") or "fpm"

    def build_arguments(self, sub: list[str], uses_options: bool,
                        program_args: bool = True) -> list[str]:
        args = list(sub)
        if uses_options:
            if self.selected_features:  # fpm rejects --features together with --profile
                args += ["--features", ",".join(self.selected_features)]
            elif self.profile_combo.currentIndex() > 0:
                args += ["--profile", self.profile_combo.currentText()]
            if self.compiler_edit.text().strip():
                args += ["--compiler", self.compiler_edit.text().strip()]
            coverage = self.coverage_check.isChecked() and sub[0] in ("build", "run", "test", "install")
            flags = " ".join(f for f in (self.flag_edit.text().strip(), cov.FLAG if coverage else "") if f)
            if flags:
                args += ["--flag", flags]
            if self.verbose_check.isChecked():
                args.append("--verbose")
            if sub[0] in ("run", "test") and self.target_edit.text().strip():
                args += split_args(self.target_edit.text())
            extra = split_args(self.extra_edit.text())
            if sub[0] in ("test", "run"):
                extra, _ = split_runner(extra)  # (the user's --runner: run inside ours, see _start_marked)
            if coverage:  # (linked with it too: the user's own --link-flag gets it added)
                extra = with_link_flag(extra, cov.FLAG)
            args += extra
            if program_args and sub[0] in ("run", "test") and self.runner_args_edit.text().strip():
                args += ["--"] + split_args(self.runner_args_edit.text())
        elif sub[0] == "update" and self.verbose_check.isChecked():
            args.append("--verbose")
        return args

    def _build_features_menu(self):
        """The Features button's menu in Build options (filled from the project's manifest)."""
        self.features_menu = StayOpenMenu(self)
        self.features_button.setMenu(self.features_menu)
        self.project_features: dict[str, list[str]] = {}
        self.selected_features: list[str] = []
        self._wanted_profile = "(default)"
        self._rebuild_features_menu()

    def _load_features_and_profiles(self):
        """Read [features] and [profiles] from the project's fpm.toml."""
        try:
            data = m.load_toml(self.manifest_path)
        except (OSError, tomllib.TOMLDecodeError):
            data = {}
        self.project_features = m.project_features(data)
        profiles = m.project_profiles(data)

        # Profile list: built-in ones plus the project's feature profiles.
        current = self.profile_combo.currentText() if self.profile_combo.count() else self._wanted_profile
        self.profile_combo.blockSignals(True)
        self.profile_combo.clear()
        self.profile_combo.addItems(BUILTIN_PROFILES)
        for name, features in profiles.items():
            if name not in BUILTIN_PROFILES:
                self.profile_combo.addItem(name)
                index = self.profile_combo.count() - 1
                self.profile_combo.setItemData(
                    index, f"Feature profile: {', '.join(features) or '(no features)'}", Qt.ToolTipRole)
        if self.profile_combo.findText(current) >= 0:
            self.profile_combo.setCurrentText(current)
        self.profile_combo.blockSignals(False)

        if not self.config_shown():  # (a configuration's own features stay; see vibefpmgui.window.configs)
            saved = self._saved_free_options().features
            self.selected_features = [f for f in saved if f in self.project_features]
        self._rebuild_features_menu()

    def _rebuild_features_menu(self):
        menu = self.features_menu
        menu.clear()
        if not self.project_features:
            none = menu.addAction("No features defined in fpm.toml")
            none.setEnabled(False)
        self.feature_actions: dict[str, QAction] = {}
        for name, props in self.project_features.items():
            action = menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(name in self.selected_features)
            self.feature_actions[name] = action
            shown = ", ".join(props[:8]) + (" …" if len(props) > 8 else "")
            action.setToolTip(f"Sets: {shown}" if props else name)
            action.setStatusTip(f"Feature {name} — sets {shown}" if props else f"Feature {name}")
            action.toggled.connect(lambda on, n=name: self._toggle_feature(n, on))
        menu.setToolTipsVisible(True)
        self.clear_features_action = None
        if self.project_features:
            menu.addSeparator()
            self.clear_features_action = menu.addAction("Clear all features")
            self.clear_features_action.triggered.connect(self._clear_features)
        self._update_features_ui()

    def _toggle_feature(self, name: str, on: bool):
        if on and name not in self.selected_features:
            # Keep the order they're declared in fpm.toml.
            chosen = set(self.selected_features) | {name}
            self.selected_features = [f for f in self.project_features if f in chosen]
        elif not on and name in self.selected_features:
            self.selected_features.remove(name)
        self._save_features()
        # Only update the existing actions: rebuilding the menu here would
        # delete the action whose signal is being handled.
        self._update_features_ui()

    def _clear_features(self):
        self.selected_features = []
        self._save_features()
        self._update_features_ui()

    def _save_features(self):
        """New configuration's features (not a saved configuration's, being edited)."""
        if self.manifest_path and not self.config_shown():
            self._keep_free_options(self.current_options())

    def _update_features_ui(self):
        """Show the selection, and disable Profile while features are on."""
        locked = self.options_locked()  # (a saved configuration's: read-only)
        for name, action in getattr(self, "feature_actions", {}).items():
            if action.isChecked() != (name in self.selected_features):
                action.blockSignals(True)
                action.setChecked(name in self.selected_features)
                action.blockSignals(False)
            action.setEnabled(not locked)
        if getattr(self, "clear_features_action", None) is not None:
            self.clear_features_action.setEnabled(bool(self.selected_features) and not locked)
        if self.selected_features:
            shown = ", ".join(self.selected_features)
        else:
            shown = "none" if self.project_features else "none defined in fpm.toml"
        self.features_button.set_full_text(shown)
        self.features_button.setEnabled(bool(self.project_features) and not locked)
        self.features_button.setToolTip(f"<b><code>--features LIST</code></b><br>{html.escape(shown)}<br>"
                                        "Features to enable (the project's fpm.toml declares them). Can't be "
                                        "combined with a profile.")
        using = bool(self.selected_features)
        self.profile_combo.setEnabled(not using and not locked)
        self.profile_combo.setToolTip(
            "<b><code>--profile PROF</code></b><br>"
            + ("Disabled while features are selected: fpm can't combine --profile and --features."
               if using else "Build profile: debug, release, or a feature profile from fpm.toml."))
        self._options_changed()  # (the features are options too)

    def _build_description(self) -> str:
        compiler, _source = self.effective_compiler()
        if self.selected_features:
            return f"features: {', '.join(self.selected_features)} · {compiler}"
        profile = self.profile_combo.currentText() if self.profile_combo.currentIndex() > 0 else "debug"
        return f"{profile} · {compiler}"

    def run_fpm(self, sub: list[str], uses_options: bool, refresh: bool,
                phase: str | None = None, args: list[str] | None = None,
                env_extra: dict[str, str] | None = None):
        if not self.manifest_path or self.busy():
            return
        if args is None:
            try:
                args = self.build_arguments(sub, uses_options)
            except ValueError as exc:  # unbalanced quotes in a text field
                QMessageBox.warning(self, "Invalid arguments", str(exc))
                return
        program = self._fpm_executable()
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(self.manifest_path.parent))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        env.insert("NO_COLOR", "1")
        for key, value in (env_extra or {}).items():
            env.insert(key, value)
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._refresh_after = refresh
        self._partial = ""
        self._phase = phase
        self._captured = []
        self._held_blank = False
        # Commands that compile get the per-file build grid. "fpm test --list"
        # doesn't build; "fpm test" builds first, so its output goes to the
        # build grid until the first test starts (see _test_line).
        self._monitoring = sub[0] in ("build", "run", "test") and phase != "list"
        self._test_building = phase in ("marked", "collect")  # fpm builds before running
        self._process_label = "fpm"
        self._revs_before = (dep_updates.read_cache_revs(self.manifest_path.parent)
                             if sub[0] == "update" else None)
        # Note which build folders exist now, to record which ones this
        # build writes (see _record_build_folders).
        self._folder_snapshot = (bo.snapshot(self.manifest_path.parent)
                                 if self._monitoring else None)
        if self._monitoring:
            self.build_monitor.start(self.graph, self.manifest_path.parent, self._build_description())
            if phase is None:
                self.show_all_output()  # any file shown on its own is gone
            # Show the build's progress (Test switches to its tab once the
            # tests themselves start).
            self.show_panel(self.build_monitor)

        self._append(f"\n$ cd {quote_arg(str(self.manifest_path.parent))}\n"
                     f"$ {join_args([program] + args)}\n", bold=True)
        self.stop_action.setEnabled(True)
        self._set_project_enabled(True)
        self.statusBar().showMessage(f"Running fpm {' '.join(sub)}…")
        self._esc_tail = ""
        self.console_writer.reset()
        self._begin_output()
        self.process.start(program, args)

    def stop(self):
        if self.process is not None:
            kill_process_tree(self.process)
        if self.parallel_tests is not None:
            self.parallel_tests.stop()

    def _stop_and_wait(self, msecs: int):
        if self.process is not None:
            kill_process_tree(self.process)
            self.process.waitForFinished(msecs)
        if self.parallel_tests is not None:
            self.parallel_tests.stop()
            self.parallel_tests.wait(msecs)

    def _read_output(self, final: bool = False):
        """New output from the running command; ``final``: it has finished
        (bytes of an unfinished character are shown as "�")."""
        if self.process is None:
            return
        data = self._decoder.decode(bytes(self.process.readAllStandardOutput()), final=final)
        if not data:
            return
        data, self._esc_tail = ansi.split_partial(self._esc_tail + data)
        # fpm redraws progress lines with \r; keep only the final state of each line.
        data = data.replace("\r\n", "\n")
        data = "\n".join(line.rsplit("\r", 1)[-1] for line in data.split("\n"))
        if self._phase in ("marked", "direct", "collect"):
            # Line-buffered so the test markers can be taken out of the output.
            lines = (self._partial + data).split("\n")
            self._partial = lines.pop()
            handle = self._collect_line if self._phase == "collect" else self._test_line
            self._append("".join(handle(line) for line in lines), colored=True)
            return
        self._append(data, colored=True)
        plain = ansi.strip(data)  # color codes would get in the way of parsing
        if self._phase == "list":
            self._captured.append(plain)
        if self._monitoring:
            # Output can arrive mid-line; only parse complete lines.
            lines = (self._partial + plain).split("\n")
            self._partial = lines.pop()
            for line in lines:
                self.build_monitor.feed_line(line)

    def _build_part_done(self):
        """The build part of "fpm test" is over: show the tests."""
        self._test_building = False
        if self._monitoring:
            self.build_monitor.finish(0)
            self._update_tree_marks()
            self._monitoring = False
        self._marker_monitor().build_finished()
        self.show_panel(self._marker_monitor())

    def _flush_monitor(self, exit_code: int, stopped: bool):
        if self._phase in ("marked", "direct", "collect"):
            if self._partial:
                handle = self._collect_line if self._phase == "collect" else self._test_line
                self._append(handle(self._partial), colored=True)
            if self._held_blank:
                self._append("\n")
            self._partial = ""
            self._held_blank = False
        if self._monitoring:
            if self._partial:
                self.build_monitor.feed_line(self._partial)
            self._partial = ""
            self.build_monitor.finish(exit_code, stopped)
            self._update_tree_marks()
            self._monitoring = False

    def _finished(self, code: int, status):
        self._read_output(final=True)
        crashed = status == QProcess.CrashExit
        self._flush_monitor(code, crashed)
        self._end_output()
        if crashed:
            self._append("\n[process was stopped]\n", bold=True)
        else:
            self._append(f"\n[{self._process_label} exited with code {code}]\n", bold=True)
        self._record_build_folders()
        phase, self._phase = self._phase, None
        self._cleanup_process()
        self.statusBar().showMessage("Stopped" if crashed else
                                     ("Finished successfully" if code == 0 else f"Failed (exit code {code})"))
        self._report_dependency_changes()  # after the generic message, so it shows
        if phase == "list":
            self._after_listing(code, crashed)
        elif phase in ("marked", "direct"):
            self._after_marked(code, crashed)
        elif phase == "collect":
            self._after_collect(code, crashed)

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            if getattr(self, "_process_label", "fpm") == "fpm":
                self._append(f"\nCould not start '{self._fpm_executable()}'. "
                             "Set the fpm executable in Build options.\n", bold=True)
            else:
                self._append(f"\nCould not start '{self._process_label}'.\n", bold=True)
            self.statusBar().showMessage(f"{getattr(self, '_process_label', 'fpm')} failed to start")
            self._flush_monitor(-1, True)
            self._end_output()
            if self._phase is not None:
                self._marker_monitor().finish(-1, stopped=True)
            self._phase = None
            self._cleanup_process()

    def _cleanup_process(self):
        if self.process is None:
            return
        self.process.deleteLater()
        self.process = None
        self.stop_action.setEnabled(False)
        self._set_project_enabled(self.graph is not None)
        if self._refresh_after:
            self.reload()








