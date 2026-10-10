"""Main window: pick an fpm.toml, run fpm commands, view the dependency graph."""

from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from qtpy.QtCore import QEvent, QProcess, QProcessEnvironment, QSettings, QSize, Qt, QUrl
from qtpy.QtGui import QAction, QActionGroup, QColor, QPalette, QDesktopServices, QFont, QFontDatabase, QKeySequence, QTextCursor
from qtpy.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QStackedWidget, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTabWidget, QTextBrowser, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from . import __version__
from . import manifest as m
from .build_monitor import BuildMonitor
from .test_monitor import MARKER_RE, TestMonitor, parse_test_list
from .icons import make_icon
from .highlighter import DARK_STYLE, LIGHT_STYLE, PygmentsHighlighter, is_dark
from .graph_view import EDGE_STYLES, KIND_LABELS, LAYOUTS, THEME_CHOICES, DependencyGraphView

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

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


def _signature(graph: m.DependencyGraph):
    return (sorted((p.name, p.kind, p.version) for p in graph.packages.values()),
            sorted((e.parent, e.child, e.dev) for e in graph.edges))


class MainWindow(QMainWindow):
    def __init__(self, manifest_path: str | None = None):
        super().__init__()
        self.setWindowTitle("VibeFPMGUI")
        self.resize(1300, 850)
        self.settings = QSettings("VibeFPMGUI", "VibeFPMGUI")
        self.manifest_path: Path | None = None
        self.graph: m.DependencyGraph | None = None
        self.process: QProcess | None = None
        self._refresh_after = False
        self._monitoring = False
        self._partial = ""  # output text after the last newline
        self._phase: str | None = None  # None, "list" or "tests" while running Test
        self._captured: list[str] = []  # output of "fpm test --list"
        self._held_blank = False  # blank line that may precede a test marker
        self._test_args: tuple = ()
        self.command_actions: list[QAction] = []

        self._build_ui()
        self._restore_settings()

        start = manifest_path or self.settings.value("last_manifest", "")
        if start and Path(start).is_file():
            self.open_manifest(start)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        open_action = QAction("Open fpm.toml…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.browse)
        reload_action = QAction("Reload", self)
        reload_action.setShortcut(QKeySequence("Ctrl+Shift+R"))  # Ctrl/⌘+R is Run
        reload_action.triggered.connect(self.reload)
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(open_action)
        file_menu.addAction(reload_action)
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        # --- left: build options + commands
        options = QGroupBox("Build options")
        form = QFormLayout(options)
        # macOS defaults to fields staying at their size hint; let them widen with the panel.
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.profile_combo = QComboBox()
        self.profile_combo.addItems(BUILTIN_PROFILES)
        form.addRow("Profile:", self.profile_combo)
        self.features_label = QLabel()
        self.features_label.setWordWrap(True)
        form.addRow("Features:", self.features_label)
        self.compiler_edit = QLineEdit()
        self.compiler_edit.setPlaceholderText("gfortran (default)")
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
        form.addRow("fpm executable:", fpm_row)

        # Hovering an option (or its label) shows the fpm flag it becomes.
        for widget, flag, text in [
            (self.profile_combo, "--profile PROF",
             "Build profile: debug or release. (default) leaves it to fpm, which uses debug."),
            (self.features_label, "--features LIST",
             "Features to enable, chosen in the Features menu. Can't be combined with a profile."),
            (self.compiler_edit, "--compiler NAME", "Fortran compiler to use. fpm uses gfortran by default."),
            (self.flag_edit, "--flag FFLAGS", "Extra Fortran compiler flags."),
            (self.target_edit, "fpm run|test NAME…",
             "Names of the programs or tests to run (wildcards allowed). Used by Run and Test."),
            (self.extra_edit, "(added as typed)",
             "Any other fpm options, e.g. --runner \"mpirun -np 4\" or --c-compiler gcc."),
            (self.runner_args_edit, "-- ARGS",
             "Arguments passed to the program or tests by Run and Test."),
            (self.verbose_check, "--verbose", "Print more detail about what fpm is doing."),
            (self.fpm_edit, "(not a flag)", "The fpm program to run. Leave empty to use the fpm on your PATH."),
        ]:
            tip = f"<b><code>{html.escape(flag)}</code></b><br>{html.escape(text)}"
            widget.setToolTip(tip)
            label = form.labelForField(widget) or form.labelForField(fpm_row)
            if label is not None and widget is not self.verbose_check:
                label.setToolTip(tip)

        self._build_commands()
        self.features_menu = self.menuBar().addMenu("Fea&tures")
        self.project_features: dict[str, list[str]] = {}
        self.selected_features: list[str] = []
        self._wanted_profile = "(default)"
        self._rebuild_features_menu()

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(options)
        left_layout.addStretch(1)

        # --- right: tabs (graph / manifest) over the output console
        self.graph_view = DependencyGraphView()
        self.graph_view.package_selected.connect(self.show_package)
        self.graph_view.package_activated.connect(self.open_package_manifest)

        self.graph_menu = self._build_graph_menu()
        self.menuBar().addMenu(self.graph_menu)

        self.legend = QLabel()
        self.legend.setWordWrap(True)

        self.details = QTextBrowser()
        self.details.setOpenLinks(False)
        self.details.anchorClicked.connect(QDesktopServices.openUrl)
        graph_split = QSplitter(Qt.Horizontal)
        graph_split.addWidget(self.graph_view)
        graph_split.addWidget(self.details)
        graph_split.setStretchFactor(0, 3)
        graph_split.setStretchFactor(1, 1)
        graph_split.setSizes([700, 300])

        graph_tab = QWidget()
        gl = QVBoxLayout(graph_tab)
        gl.addWidget(graph_split, 1)
        gl.addWidget(self.legend)

        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        self.manifest_view = QPlainTextEdit()
        self.manifest_view.setReadOnly(True)
        self.manifest_view.setFont(mono)
        self.toml_highlighter = PygmentsHighlighter(self.manifest_view.document())
        self._apply_highlight_style()

        self.tabs = QTabWidget()
        self.tabs.addTab(graph_tab, "Dependency graph")
        self.tabs.addTab(self.manifest_view, "fpm.toml")

        self.console = QPlainTextEdit()
        self.console.setReadOnly(True)
        self.console.setFont(mono)
        self.console.setMaximumBlockCount(20000)
        # Output of a single test, shown when a test row is clicked.
        self.test_output = QPlainTextEdit()
        self.test_output.setReadOnly(True)
        self.test_output.setFont(mono)
        self.output_stack = QStackedWidget()
        self.output_stack.addWidget(self.console)
        self.output_stack.addWidget(self.test_output)
        console_box = QWidget()
        cl = QVBoxLayout(console_box)
        cl.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        self.output_title = QLabel("<b>Output</b>")
        header.addWidget(self.output_title)
        header.addStretch(1)
        self.show_all_button = QPushButton("Show all output")
        self.show_all_button.clicked.connect(lambda: self.show_test_output(""))
        self.show_all_button.hide()
        header.addWidget(self.show_all_button)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.console.clear)
        header.addWidget(clear)
        cl.addLayout(header)
        cl.addWidget(self.output_stack)

        self.build_monitor = BuildMonitor()
        self.test_monitor = TestMonitor()
        self.test_monitor.test_selected.connect(self.show_test_output)
        self.monitor_tabs = QTabWidget()
        self.monitor_tabs.setDocumentMode(True)
        self.monitor_tabs.addTab(self.build_monitor, "Build")
        self.monitor_tabs.addTab(self.test_monitor, "Tests")
        self.bottom_split = QSplitter(Qt.Horizontal)
        self.bottom_split.addWidget(self.monitor_tabs)
        self.bottom_split.addWidget(console_box)
        self.bottom_split.setStretchFactor(0, 2)
        self.bottom_split.setStretchFactor(1, 3)
        self.bottom_split.setSizes([620, 480])

        self.right_split = QSplitter(Qt.Vertical)
        self.right_split.addWidget(self.tabs)
        self.right_split.addWidget(self.bottom_split)
        self.right_split.setStretchFactor(0, 3)
        self.right_split.setStretchFactor(1, 2)

        self.main_split = QSplitter(Qt.Horizontal)
        self.main_split.addWidget(left)
        self.main_split.addWidget(self.right_split)
        self.main_split.setStretchFactor(1, 1)
        self.main_split.setSizes([300, 1000])
        self.setCentralWidget(self.main_split)
        self.statusBar().showMessage("Ready")
        self._set_project_enabled(False)

    def _apply_highlight_style(self):
        """Pick a light or dark Pygments style to match the system palette."""
        dark = is_dark(QApplication.palette())
        self.toml_highlighter.set_style(DARK_STYLE if dark else LIGHT_STYLE)
        palette = self.manifest_view.palette()
        palette.setColor(QPalette.Base, self.toml_highlighter.background())
        palette.setColor(QPalette.Text, QColor("#f8f8f2") if dark else QColor("#1b1b1b"))
        self.manifest_view.setPalette(palette)

    def changeEvent(self, event):
        if event.type() == QEvent.PaletteChange and hasattr(self, "toml_highlighter"):
            self._apply_highlight_style()
            self.graph_view.refresh_theme()  # "Auto" theme follows the system
            self._update_legend()
            self.build_monitor.refresh_palette()
            self.test_monitor.refresh_palette()
            self._update_command_icons()
            self.build_monitor.set_package_colors(self.graph_view.package_colors())
        super().changeEvent(event)

    def _build_graph_menu(self) -> QMenu:
        """Graph menu: layout, theme and edge style choices (radio groups)."""
        menu = QMenu("&Graph", self)
        self.graph_style_actions: dict[str, dict[str, QAction]] = {}
        for title, key, choices, setter in [
            ("Layout", "layout", LAYOUTS, self._set_graph_layout),
            ("Theme", "theme", THEME_CHOICES, self._set_graph_theme),
            ("Edges", "edges", EDGE_STYLES, self._set_graph_edges),
        ]:
            sub = menu.addMenu(title)
            group = QActionGroup(self)
            group.setExclusive(True)
            self.graph_style_actions[key] = {}
            for value, label in choices.items():
                action = QAction(label, self, checkable=True)
                action.triggered.connect(lambda _=False, v=value, f=setter: f(v))
                group.addAction(action)
                sub.addAction(action)
                self.graph_style_actions[key][value] = action
        menu.addSeparator()
        for text, slot, shortcut in [("Fit to window", lambda: self.graph_view.fit(), "Ctrl+0"),
                                     ("Re-layout", lambda: self.graph_view.relayout(), "Ctrl+L"),
                                     ("Export image…", self.export_graph, "Ctrl+E")]:
            action = QAction(text, self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(slot)
            menu.addAction(action)
        return menu

    def _check_style_action(self, key: str, value: str):
        action = self.graph_style_actions[key].get(value)
        if action:
            action.setChecked(True)

    def _set_graph_layout(self, value: str):
        self.graph_view.set_layout(value)
        self._check_style_action("layout", value)
        self.settings.setValue("graph_layout", value)

    def _set_graph_theme(self, value: str):
        self.graph_view.set_theme(value)
        self._check_style_action("theme", value)
        self.settings.setValue("graph_theme", value)
        self._update_legend()
        self.build_monitor.set_package_colors(self.graph_view.package_colors())

    def _set_graph_edges(self, value: str):
        self.graph_view.set_edge_style(value)
        self._check_style_action("edges", value)
        self.settings.setValue("graph_edges", value)

    def _update_legend(self):
        self.legend.setText(self.graph_view.legend_html())

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
        self.addToolBar(Qt.TopToolBarArea, toolbar)
        self.setUnifiedTitleAndToolBarOnMac(True)
        self.command_toolbar = toolbar
        self._update_command_icons()

    def _update_command_icons(self):
        """(Re)draw the toolbar icons in the current text colour."""
        text = QApplication.palette().color(QPalette.WindowText)
        for action, name in self.command_icons.items():
            action.setIcon(make_icon(name, STOP_COLOR if name == "stop" else text))
        hover = "rgba(128,128,128,45)"
        self.command_toolbar.setStyleSheet(f"""
            QToolBar#CommandsToolbar {{ border: none; spacing: 2px; padding: 2px 6px; }}
            QToolBar#CommandsToolbar QToolButton {{ border: none; border-radius: 7px;
                padding: 4px 10px; }}
            QToolBar#CommandsToolbar QToolButton:hover {{ background: {hover}; }}
            QToolBar#CommandsToolbar QToolButton:pressed {{ background: rgba(128,128,128,80); }}
        """)

    def _set_project_enabled(self, enabled: bool):
        running = self.process is not None
        for action in self.command_actions:
            action.setEnabled(enabled and not running)

    # ------------------------------------------------------------ settings
    def _restore_settings(self):
        s = self.settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        # Applied once the project's own [profiles] are in the list.
        self._wanted_profile = s.value("profile", "(default)")
        self.profile_combo.setCurrentText(self._wanted_profile)
        self.compiler_edit.setText(s.value("compiler", ""))
        self.flag_edit.setText(s.value("flags", ""))
        self.extra_edit.setText(s.value("extra", ""))
        self.fpm_edit.setText(s.value("fpm", ""))
        self.verbose_check.setChecked(s.value("verbose", False, type=bool))
        self._set_graph_layout(s.value("graph_layout", next(iter(LAYOUTS))))
        self._set_graph_theme(s.value("graph_theme", next(iter(THEME_CHOICES))))
        self._set_graph_edges(s.value("graph_edges", next(iter(EDGE_STYLES))))

    def closeEvent(self, event):
        if self.process is not None:
            answer = QMessageBox.question(self, "fpm is running",
                                          "An fpm command is still running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.process.kill()
            self.process.waitForFinished(2000)
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("profile", self.profile_combo.currentText())
        s.setValue("compiler", self.compiler_edit.text())
        s.setValue("flags", self.flag_edit.text())
        s.setValue("extra", self.extra_edit.text())
        s.setValue("fpm", self.fpm_edit.text())
        s.setValue("verbose", self.verbose_check.isChecked())
        super().closeEvent(event)

    # ------------------------------------------------------------- project
    def browse(self):
        start = str(self.manifest_path.parent) if self.manifest_path else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open fpm manifest", start, "fpm manifest (fpm.toml);;TOML files (*.toml);;All files (*)")
        if filename:
            self.open_manifest(filename)

    def _browse_fpm(self):
        filename, _ = QFileDialog.getOpenFileName(self, "Select fpm executable")
        if filename:
            self.fpm_edit.setText(filename)

    def open_manifest(self, filename: str | Path):
        path = Path(filename).resolve()
        if path != self.manifest_path:
            if self.process is not None:
                answer = QMessageBox.question(
                    self, "fpm is running",
                    "An fpm command is still running for the current project. Stop it and open the new one?")
                if answer != QMessageBox.Yes:
                    return
                self.process.kill()
                self.process.waitForFinished(3000)
            self._clear_run_panels()
        self.manifest_path = path
        self.settings.setValue("last_manifest", str(self.manifest_path))
        self.setWindowFilePath(str(self.manifest_path))  # macOS title-bar proxy icon
        self.setWindowTitle(f"VibeFPMGUI — {self.manifest_path.parent.name}")
        self.reload()

    def _clear_run_panels(self):
        """Empty the output console and the build/test panels."""
        self.show_test_output("")
        self.console.clear()
        self.test_output.clear()
        self.build_monitor.reset()
        self.test_monitor.reset()
        self.monitor_tabs.setCurrentWidget(self.build_monitor)

    def reload(self):
        if not self.manifest_path:
            return
        try:
            self.manifest_view.setPlainText(self.manifest_path.read_text())
        except OSError as exc:
            self.manifest_view.setPlainText(str(exc))
        try:
            self.graph = m.resolve(self.manifest_path)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            self.graph = None
            self.graph_view.clear(f"Could not read {self.manifest_path.name}:\n{exc}")
            self.details.clear()
            self._set_project_enabled(False)
            self.statusBar().showMessage(f"Error reading manifest: {exc}")
            return
        # Keep the current layout (and any nodes the user moved) unless the
        # set of packages or their relationships actually changed.
        if self.graph_view.graph is None or _signature(self.graph_view.graph) != _signature(self.graph):
            self.graph_view.set_graph(self.graph)
        else:
            self.graph_view.update_packages(self.graph)
        self.show_package(self.graph.root)
        self._set_project_enabled(True)
        self._load_features_and_profiles()
        missing = sum(1 for p in self.graph.packages.values() if p.kind == m.MISSING)
        msg = f"{len(self.graph.packages) - 1} dependencies"
        if missing:
            msg += f" ({missing} not downloaded — run Update deps or Build)"
        self.statusBar().showMessage(msg)

    def show_package(self, name: str):
        if not self.graph or name not in self.graph.packages:
            return
        pkg = self.graph.packages[name]
        e = html.escape
        rows = [("Type", KIND_LABELS[pkg.kind])]
        for label, value in [("Version", pkg.version), ("License", pkg.license),
                             ("Author", pkg.author), ("Git", pkg.git),
                             ("Requested", pkg.git_ref), ("Revision", pkg.git_rev)]:
            if value:
                rows.append((label, e(value)))
        if pkg.homepage:
            rows.append(("Homepage", f'<a href="{e(pkg.homepage)}">{e(pkg.homepage)}</a>'))
        if pkg.directory:
            url = QUrl.fromLocalFile(str(pkg.directory)).toString()
            rows.append(("Location", f'<a href="{e(url)}">{e(str(pkg.directory))}</a>'))
        deps = self.graph.children(name)
        users = self.graph.parents(name)
        if deps:
            rows.append(("Depends on", ", ".join(
                e(d.child) + (" <i>(dev)</i>" if d.dev else "") for d in deps)))
        if users:
            rows.append(("Used by", ", ".join(e(u.parent) for u in users)))
        table = "".join(f"<tr><td valign='top'><b>{k}</b>&nbsp;</td><td>{v}</td></tr>"
                        for k, v in rows)
        body = f"<h3>{e(pkg.name)}</h3>"
        if pkg.description:
            body += f"<p>{e(pkg.description)}</p>"
        body += f"<table>{table}</table>"
        if pkg.error:
            body += f"<p style='color:#c92a2a'>{e(pkg.error)}</p>"
        if pkg.manifest:
            body += "<p><i>Double-click the node to open its fpm.toml.</i></p>"
        self.details.setHtml(body)

    def open_package_manifest(self, name: str):
        pkg = self.graph.packages.get(name) if self.graph else None
        if pkg and pkg.manifest:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(pkg.manifest)))

    def export_graph(self):
        if not self.graph:
            return
        default = str((self.manifest_path.parent if self.manifest_path else Path.home())
                      / f"{self.graph.root}-dependencies.png")
        filename, _ = QFileDialog.getSaveFileName(
            self, "Export dependency graph", default, "PNG image (*.png);;SVG image (*.svg)")
        if filename:
            if self.graph_view.export_image(filename):
                self.statusBar().showMessage(f"Saved {filename}")
            else:
                QMessageBox.warning(self, "Export failed", f"Could not write {filename}")

    # ------------------------------------------------------------- running
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
            if self.flag_edit.text().strip():
                args += ["--flag", self.flag_edit.text().strip()]
            if self.verbose_check.isChecked():
                args.append("--verbose")
            if sub[0] in ("run", "test") and self.target_edit.text().strip():
                args += shlex.split(self.target_edit.text())
            extra = shlex.split(self.extra_edit.text())
            if sub[0] == "test":
                extra, _ = split_runner(extra)  # the test runner is handled by run_tests
            args += extra
            if program_args and sub[0] in ("run", "test") and self.runner_args_edit.text().strip():
                args += ["--"] + shlex.split(self.runner_args_edit.text())
        elif sub[0] == "update" and self.verbose_check.isChecked():
            args.append("--verbose")
        return args

    # ------------------------------------------------------------ features
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

        saved = self.settings.value(self._features_key(), [], type=list) or []
        self.selected_features = [f for f in saved if f in self.project_features]
        self._rebuild_features_menu()

    def _features_key(self) -> str:
        digest = hashlib.sha1(str(self.manifest_path).encode()).hexdigest()[:16]
        return f"features/{digest}"

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
        if self.manifest_path:
            self.settings.setValue(self._features_key(), self.selected_features)

    def _update_features_ui(self):
        """Show the selection, and disable Profile while features are on."""
        for name, action in getattr(self, "feature_actions", {}).items():
            if action.isChecked() != (name in self.selected_features):
                action.blockSignals(True)
                action.setChecked(name in self.selected_features)
                action.blockSignals(False)
        if getattr(self, "clear_features_action", None) is not None:
            self.clear_features_action.setEnabled(bool(self.selected_features))
        if self.selected_features:
            self.features_label.setText(", ".join(self.selected_features))
        elif self.project_features:
            self.features_label.setText("<span style='color:gray'>none (see Features menu)</span>")
        else:
            self.features_label.setText("<span style='color:gray'>none defined</span>")
        using = bool(self.selected_features)
        self.profile_combo.setEnabled(not using)
        self.profile_combo.setToolTip(
            "<b><code>--profile PROF</code></b><br>"
            + ("Disabled while features are selected: fpm can't combine --profile and --features."
               if using else "Build profile: debug, release, or a feature profile from fpm.toml."))

    def _build_description(self) -> str:
        compiler = self.compiler_edit.text().strip() or "gfortran"
        if self.selected_features:
            return f"features: {', '.join(self.selected_features)} · {compiler}"
        profile = self.profile_combo.currentText() if self.profile_combo.currentIndex() > 0 else "debug"
        return f"{profile} · {compiler}"

    def run_fpm(self, sub: list[str], uses_options: bool, refresh: bool,
                phase: str | None = None, args: list[str] | None = None,
                env_extra: dict[str, str] | None = None):
        if not self.manifest_path or self.process is not None:
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
        # Commands that compile get the per-file status grid (the test run
        # itself doesn't: "fpm test --list" already built everything).
        self._monitoring = sub[0] in ("build", "run", "test") and phase != "tests"
        if self._monitoring:
            self.build_monitor.start(self.graph, self.manifest_path.parent, self._build_description())
            if phase is None:
                self.monitor_tabs.setCurrentWidget(self.build_monitor)

        self._append(f"\n$ cd {shlex.quote(str(self.manifest_path.parent))}\n"
                     f"$ {shlex.join([program] + args)}\n", bold=True)
        self.stop_action.setEnabled(True)
        self._set_project_enabled(True)
        self.statusBar().showMessage(f"Running fpm {' '.join(sub)}…")
        self.process.start(program, args)

    def stop(self):
        if self.process is not None:
            self.process.kill()

    def _append(self, text: str, bold: bool = False):
        cursor = self.console.textCursor()
        cursor.movePosition(QTextCursor.End)
        fmt = cursor.charFormat()
        fmt.setFontWeight(QFont.Bold if bold else QFont.Normal)
        cursor.setCharFormat(fmt)
        cursor.insertText(text)
        self.console.setTextCursor(cursor)
        self.console.ensureCursorVisible()

    def _read_output(self):
        if self.process is None:
            return
        data = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        # fpm redraws progress lines with \r; keep only the final state of each line.
        data = ANSI_RE.sub("", data).replace("\r\n", "\n")
        data = "\n".join(line.rsplit("\r", 1)[-1] for line in data.split("\n"))
        if self._phase == "tests":
            # Line-buffered so the test markers can be taken out of the output.
            lines = (self._partial + data).split("\n")
            self._partial = lines.pop()
            self._append("".join(self._test_line(line) for line in lines))
            return
        self._append(data)
        if self._phase == "list":
            self._captured.append(data)
        if self._monitoring:
            # Output can arrive mid-line; only parse complete lines.
            lines = (self._partial + data).split("\n")
            self._partial = lines.pop()
            for line in lines:
                self.build_monitor.feed_line(line)

    def _test_line(self, line: str) -> str:
        """Pass a line of test output through the test monitor; returns the
        text to show. The runner prints a blank line before each marker,
        which is dropped along with the marker."""
        text = ""
        if self._held_blank:
            self._held_blank = False
            if not MARKER_RE.search(line):  # the blank was real output
                self.test_monitor.feed_line("")
                text = "\n"
        if line == "":
            self._held_blank = True
            return text
        shown = self.test_monitor.feed_line(line)
        return text + (shown + "\n" if shown is not None else "")

    def _flush_monitor(self, exit_code: int, stopped: bool):
        if self._phase == "tests":
            if self._partial:
                self._append(self._test_line(self._partial))
            if self._held_blank:
                self._append("\n")
            self._partial = ""
            self._held_blank = False
        if self._monitoring:
            if self._partial:
                self.build_monitor.feed_line(self._partial)
            self._partial = ""
            self.build_monitor.finish(exit_code, stopped)
            self._monitoring = False

    def _finished(self, code: int, status):
        self._read_output()
        crashed = status == QProcess.CrashExit
        self._flush_monitor(code, crashed)
        if crashed:
            self._append("\n[process was stopped]\n", bold=True)
        else:
            self._append(f"\n[fpm exited with code {code}]\n", bold=True)
        phase, self._phase = self._phase, None
        self._cleanup_process()
        self.statusBar().showMessage("Stopped" if crashed else
                                     ("Finished successfully" if code == 0 else f"Failed (exit code {code})"))
        if phase == "list":
            self._after_listing(code, crashed)
        elif phase == "tests":
            self._after_tests(code, crashed)

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            self._append(f"\nCould not start '{self._fpm_executable()}'. "
                         "Set the fpm executable in Build options.\n", bold=True)
            self.statusBar().showMessage("fpm failed to start")
            self._flush_monitor(-1, True)
            if self._phase is not None:
                self.test_monitor.finish(-1, stopped=True)
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

    # --------------------------------------------------------------- tests
    def run_tests(self):
        """Test in two steps: "fpm test --list" (builds, and tells us the
        test names), then "fpm test --runner <our wrapper>" so each test's
        start, end and exit code can be followed."""
        if not self.manifest_path or self.process is not None:
            return
        try:
            list_args = self.build_arguments(["test", "--list"], True, program_args=False)
            run_args = self.build_arguments(["test"], True)
            _, inner_runner = split_runner(shlex.split(self.extra_edit.text()))
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid arguments", str(exc))
            return
        runner = runner_command()
        # Our runner goes before the target names and any "-- program args".
        cut = run_args.index("--") if "--" in run_args else len(run_args)
        run_args = run_args[:cut] + ["--runner", runner] + run_args[cut:]
        patterns = shlex.split(self.target_edit.text())
        self._test_args = (run_args, inner_runner, patterns)
        description = self._build_description()
        if inner_runner:
            description += f" · runner: {inner_runner}"
        self.test_monitor.begin_listing(description)
        self.show_test_output("")
        self.run_fpm(["test"], True, False, phase="list", args=list_args)

    def _after_listing(self, code: int, stopped: bool):
        output = "".join(self._captured)
        names = parse_test_list(output)
        if stopped or code != 0:
            self.test_monitor.finish(code, stopped=stopped)
            self.monitor_tabs.setCurrentWidget(self.build_monitor)
            return
        if names == []:
            self.test_monitor.finish(0, no_tests=True)
            self.monitor_tabs.setCurrentWidget(self.test_monitor)
            self.statusBar().showMessage("No tests to run")
            return
        run_args, inner_runner, patterns = self._test_args
        if names and patterns:
            # "fpm test --list" lists every test regardless of the names given,
            # so apply the Target names/globs ourselves, as fpm will.
            names = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)]
        self.test_monitor.start(names or [], self._load_test_durations())
        self.monitor_tabs.setCurrentWidget(self.test_monitor)
        env = {"VIBEFPM_INNER_RUNNER": inner_runner} if inner_runner else {}
        self.run_fpm(["test"], True, True, phase="tests", args=run_args, env_extra=env)

    def _after_tests(self, code: int, stopped: bool):
        self.test_monitor.finish(code, stopped=stopped)
        self._save_test_durations(self.test_monitor.durations())
        counts = self.test_monitor.counts()
        total = len(self.test_monitor.tests)
        if stopped:
            message = "Tests stopped"
        elif counts["Failed"]:
            message = f"Tests: {counts['Failed']} of {total} failed"
        elif counts["Not run"]:
            message = f"Tests: {counts['Passed']} passed, {counts['Not run']} not run"
        else:
            message = f"Tests: all {total} passed"
        self.statusBar().showMessage(message)

    def _durations_key(self) -> str:
        digest = hashlib.sha1(str(self.manifest_path).encode()).hexdigest()[:16]
        return f"test_durations/{digest}"

    def _load_test_durations(self) -> dict[str, float]:
        try:
            data = json.loads(self.settings.value(self._durations_key(), "{}"))
            return {str(k): float(v) for k, v in data.items()}
        except (TypeError, ValueError, AttributeError):
            return {}

    def _save_test_durations(self, durations: dict[str, float]):
        if durations and self.manifest_path:
            merged = {**self._load_test_durations(), **durations}
            self.settings.setValue(self._durations_key(), json.dumps(merged))

    def show_test_output(self, name: str):
        """Show one test's output (or everything when name is empty)."""
        if name:
            text = self.test_monitor.output_for(name)
            self.test_output.setPlainText(text or "(no output)")
            self.output_title.setText(f"<b>Output</b> — {html.escape(name)}")
            self.output_stack.setCurrentWidget(self.test_output)
            self.show_all_button.show()
        else:
            self.output_title.setText("<b>Output</b>")
            self.output_stack.setCurrentWidget(self.console)
            self.show_all_button.hide()
            if self.test_monitor.selected:
                self.test_monitor.selected = ""
                self.test_monitor.table.viewport().update()


def split_runner(args: list[str]) -> tuple[list[str], str]:
    """Take "--runner CMD" / "--runner=CMD" out of an fpm argument list."""
    rest, runner = [], ""
    i = 0
    while i < len(args):
        if args[i] == "--runner" and i + 1 < len(args):
            runner = args[i + 1]
            i += 2
            continue
        if args[i].startswith("--runner="):
            runner = args[i].split("=", 1)[1]
        else:
            rest.append(args[i])
        i += 1
    return rest, runner


def runner_command() -> str:
    """The command fpm should put in front of each test executable."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_runner.py")
    parts = [sys.executable, "-I", script]
    return subprocess.list2cmdline(parts) if os.name == "nt" else shlex.join(parts)


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = sys.argv if argv is None else argv
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("VibeFPMGUI")
    app.setApplicationVersion(__version__)
    path = next((a for a in argv[1:] if not a.startswith("-")), None)
    if path and Path(path).is_dir():
        path = str(Path(path) / "fpm.toml")
    window = MainWindow(path)
    window.show()
    return app.exec()
