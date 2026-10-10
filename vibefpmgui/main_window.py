"""Main window: pick an fpm.toml, run fpm commands, view the dependency graph."""

from __future__ import annotations

import fnmatch
import hashlib
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from qtpy.QtCore import QEvent, QProcess, QProcessEnvironment, QSettings, QSize, Qt, QTimer, QUrl
from qtpy.QtGui import QAction, QActionGroup, QColor, QPalette, QDesktopServices, QFont, QFontDatabase, QKeySequence, QTextCursor
from qtpy.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog,
    QLabel, QLineEdit, QMainWindow, QMenu, QSizePolicy, QStackedWidget, QTabBar, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTabWidget, QTextBrowser, QToolBar, QToolButton, QVBoxLayout, QWidget,
)

from . import __version__
from . import manifest as m
from .build_monitor import BuildMonitor, set_ui_scale
from .test_monitor import MARKER_RE, TestMonitor, parse_test_list
from . import ai_review as ai
from .compat import join_args, quote_arg, set_windows_app_id, split_args
from .editor import EditorTab, monospace_font
from . import build_outputs as bo
from .about import FPM_DOCS_URL, AboutDialog
from . import dep_updates
from .project_tree import ProjectTree, reveal_in_file_manager
from .review_monitor import ReviewMonitor
from .run_monitor import RunMonitor
from .icons import app_icon, make_icon
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
        self.settings = make_settings()
        self.manifest_path: Path | None = None
        self.graph: m.DependencyGraph | None = None
        self.process: QProcess | None = None
        self._refresh_after = False
        self._monitoring = False
        self._partial = ""  # output text after the last newline
        # None, or while running Test/Run: "list" (fpm ... --list), "marked"
        # (fpm ... --runner <wrapper>) or "direct" (a built program, via the wrapper)
        self._phase: str | None = None
        self._captured: list[str] = []  # output of "fpm test --list"
        self._held_blank = False  # blank line that may precede a test marker
        self._test_args: tuple = ()
        self.command_actions: list[QAction] = []

        self._build_ui()
        self._restore_settings()
        self._update_editor_actions()

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
        self.recent_menu = file_menu.addMenu("Recent Manifests")
        self.recent_menu.aboutToShow.connect(self._fill_recent_menu)
        self.recent_menu.setToolTipsVisible(True)
        file_menu.addAction(reload_action)
        file_menu.addSeparator()
        open_file_action = QAction("Open File in Editor…", self)
        open_file_action.setShortcut(QKeySequence("Ctrl+Shift+O"))
        open_file_action.triggered.connect(self.browse_file)
        self.save_action = QAction("Save", self)
        self.save_action.setShortcut(QKeySequence.Save)
        self.save_action.triggered.connect(lambda: self._with_editor(lambda e: e.save()))
        self.save_as_action = QAction("Save As…", self)
        self.save_as_action.setShortcut(QKeySequence("Ctrl+Shift+S"))
        self.save_as_action.triggered.connect(lambda: self._with_editor(lambda e: e.save_as()))
        self.close_tab_action = QAction("Close Tab", self)
        self.close_tab_action.setShortcut(QKeySequence.Close)
        self.close_tab_action.triggered.connect(lambda: self.close_editor(self.tabs.currentIndex()))
        file_menu.addActions([open_file_action, self.save_action, self.save_as_action,
                              self.close_tab_action])
        file_menu.addSeparator()
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)
        self._build_edit_menu()

        # View menu: text size for the whole window.
        # The system font, recorded once: later windows would otherwise see the
        # already-enlarged application font as "normal".
        app = QApplication.instance()
        if app.property("vibefpm_base_font") is None:
            app.setProperty("vibefpm_base_font", QFont(app.font()))
        self._base_font = QFont(app.property("vibefpm_base_font"))
        self.font_delta = 0
        view_menu = self.menuBar().addMenu("&View")
        self.bigger_action = QAction("Bigger Text", self)
        self.bigger_action.setShortcuts([QKeySequence(QKeySequence.ZoomIn), QKeySequence("Ctrl+=")])
        self.bigger_action.triggered.connect(lambda: self.set_font_delta(self.font_delta + 1))
        self.smaller_action = QAction("Smaller Text", self)
        self.smaller_action.setShortcut(QKeySequence(QKeySequence.ZoomOut))
        self.smaller_action.triggered.connect(lambda: self.set_font_delta(self.font_delta - 1))
        self.reset_font_action = QAction("Actual Size", self)  # (Ctrl+0 is Graph → Fit to window)
        self.reset_font_action.triggered.connect(lambda: self.set_font_delta(0))
        view_menu.addActions([self.bigger_action, self.smaller_action, self.reset_font_action])
        view_menu.addSeparator()
        self.use_editor_action = QAction("Open Files in Built-in Editor", self, checkable=True)
        self.use_editor_action.setChecked(self.settings.value("use_editor", True, type=bool))
        self.use_editor_action.toggled.connect(lambda on: self.settings.setValue("use_editor", on))
        self.use_editor_action.setStatusTip(
            "Double-clicked files open in a tab here; turn off to use your default application")
        view_menu.addAction(self.use_editor_action)

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
        self._build_ai_menu()

        # Project files below the build options (drag the divider to resize).
        self.project_tree = ProjectTree()
        self.project_tree.open_requested.connect(lambda path: self.open_file(path))
        self.project_tree.compile_command_requested.connect(self.show_compile_command)
        files_box = QGroupBox("Project files")
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(6, 6, 6, 6)
        files_layout.addWidget(self.project_tree)
        left = QSplitter(Qt.Vertical)
        left.addWidget(options)
        left.addWidget(files_box)
        left.setStretchFactor(0, 0)
        left.setStretchFactor(1, 1)
        left.setCollapsible(0, False)

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
        self.tabs.setDocumentMode(True)
        self.tabs.addTab(graph_tab, "Dependency graph")
        self.tabs.addTab(self.manifest_view, "fpm.toml")
        self.outputs_view = bo.BuildOutputsView()
        self.outputs_view.run_requested.connect(self.run_program)
        self.outputs_view.reveal_requested.connect(reveal_in_file_manager)
        self.outputs_view.delete_requested.connect(self.delete_configurations)
        self.outputs_view.refresh_requested.connect(self.refresh_build_outputs)
        self.tabs.addTab(self.outputs_view, "Build outputs")
        # Files opened in the built-in editor get their own closable tabs.
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        for index in (0, 1, 2):  # graph, fpm.toml and build outputs stay open
            for side in (QTabBar.LeftSide, QTabBar.RightSide):
                self.tabs.tabBar().setTabButton(index, side, None)
        self.tabs.tabCloseRequested.connect(self.close_editor)
        self.tabs.currentChanged.connect(self._update_editor_actions)
        self.tabs.currentChanged.connect(self._reveal_current_file)
        self.tabs.currentChanged.connect(
            lambda _i: self.tabs.currentWidget() is self.outputs_view and self.refresh_build_outputs())

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
        cl.setSpacing(3)  # (the default spacing left a visible gap above the buttons)
        self.output_title = QLabel("<b>Output</b>")
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
        clear = QPushButton("Clear")
        clear.setToolTip("Clear the output")
        clear.clicked.connect(self.console.clear)
        footer.addWidget(clear)
        for button in (self.show_all_button, clear):
            # macOS buttons reserve extra room around themselves (for the
            # focus ring); lay them out by their visible size instead.
            button.setAttribute(Qt.WA_LayoutUsesWidgetRect)
        cl.addWidget(self.output_title)
        cl.addWidget(self.output_stack, 1)
        cl.addLayout(footer)

        self.build_monitor = BuildMonitor()
        self.test_monitor = TestMonitor()
        self.test_monitor.test_selected.connect(self.show_test_output)
        self.test_monitor.build_percent = lambda: self.build_monitor._percent
        self.build_monitor.details_requested.connect(self.show_build_details)
        self.build_monitor.open_file_requested.connect(self.open_file)
        self.build_monitor.compile_command_requested.connect(self.show_compile_command)
        self.monitor_tabs = QTabWidget()
        self.monitor_tabs.setDocumentMode(True)
        self.monitor_tabs.addTab(self.build_monitor, "Build")
        self.monitor_tabs.addTab(self.test_monitor, "Tests")
        self.run_monitor = RunMonitor()
        self.run_monitor.test_selected.connect(
            lambda name: self._show_marked_output(self.run_monitor, name))
        self.run_monitor.build_percent = lambda: self.build_monitor._percent
        self.monitor_tabs.addTab(self.run_monitor, "Run")
        self.review_monitor = ReviewMonitor()
        self.review_monitor.finding_selected.connect(self._go_to_finding)
        self.monitor_tabs.addTab(self.review_monitor, "Review")
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
        self._build_help_menu()

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
            self.run_monitor.refresh_palette()
            self.review_monitor.refresh_palette()
            for editor in self.editors():
                editor.refresh_style()
            self.project_tree._apply_marks()
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
                                     ("Re-layout", lambda: self.graph_view.relayout(), "Ctrl+Shift+L"),
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

    def _build_help_menu(self):
        """Help menu (added last, so it is the rightmost menu)."""
        menu = self.menuBar().addMenu("&Help")
        docs = menu.addAction("fpm Documentation")
        docs.setStatusTip(FPM_DOCS_URL)
        docs.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(FPM_DOCS_URL)))
        menu.addSeparator()
        about = menu.addAction("About VibeFPMGUI")
        about.setMenuRole(QAction.AboutRole)  # macOS: moved to the application menu
        about.triggered.connect(self.show_about)

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

    def show_about(self):
        compiler, source = self.effective_compiler()
        AboutDialog(self._fpm_executable(), compiler, source, self).exec()

    def _update_legend(self):
        self.legend.setText(self.graph_view.legend_html())

    # ------------------------------------------------------------ text size
    FONT_DELTA_RANGE = (-4, 12)  # points relative to the system size

    def set_font_delta(self, delta: int, announce: bool = True):
        """Make all text in the window ``delta`` points bigger (or smaller)
        than the system size."""
        lo, hi = self.FONT_DELTA_RANGE
        self.font_delta = max(lo, min(hi, int(delta)))
        base = self._base_font.pointSizeF()
        size = base + self.font_delta
        factor = size / base
        set_ui_scale(factor)

        font = QFont(self._base_font)
        font.setPointSizeF(size)
        QApplication.setFont(font)
        # Widgets with their own fonts don't follow the application font.
        mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        mono.setPointSizeF(mono.pointSizeF() * factor)
        for view in (self.manifest_view, self.console, self.test_output):
            view.setFont(mono)
        for editor in self.editors():
            editor.set_font(mono)
        self.command_toolbar.setIconSize(QSize(round(22 * factor), round(22 * factor)))
        self.build_monitor.refresh_fonts()
        self.test_monitor.refresh_fonts()
        self.run_monitor.refresh_fonts()
        self.review_monitor.refresh_fonts()
        self.graph_view.refresh_fonts()
        if self.graph:
            self.show_package(self.graph_view.selected or self.graph.root)

        self.bigger_action.setEnabled(self.font_delta < hi)
        self.smaller_action.setEnabled(self.font_delta > lo)
        self.reset_font_action.setEnabled(self.font_delta != 0)
        self.settings.setValue("font_delta", self.font_delta)
        if announce:
            self.statusBar().showMessage(f"Text size {round(factor * 100)}%", 3000)

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
        self.set_font_delta(s.value("font_delta", 0, type=int), announce=False)
        self._set_graph_layout(s.value("graph_layout", next(iter(LAYOUTS))))
        self._set_graph_theme(s.value("graph_theme", next(iter(THEME_CHOICES))))
        self._set_graph_edges(s.value("graph_edges", next(iter(EDGE_STYLES))))

    def closeEvent(self, event):
        for editor in self.editors():
            self.tabs.setCurrentWidget(editor)
            if not editor.maybe_save():
                event.ignore()
                return
        if self.process is not None:
            answer = QMessageBox.question(self, "fpm is running",
                                          "An fpm command is still running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            kill_process_tree(self.process)
            self.process.waitForFinished(2000)
        self._shutdown_ai()
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
            # The open files belong to the old project: ask about unsaved
            # changes first (Cancel keeps the current project open).
            if not self.close_all_editors():
                return
            if self.process is not None:
                answer = QMessageBox.question(
                    self, "fpm is running",
                    "An fpm command is still running for the current project. Stop it and open the new one?")
                if answer != QMessageBox.Yes:
                    return
                kill_process_tree(self.process)
                self.process.waitForFinished(3000)
            self._clear_run_panels()
        self.manifest_path = path
        self.settings.setValue("last_manifest", str(self.manifest_path))
        self._remember_manifest(self.manifest_path)
        self.settings.sync()  # write now, so it survives the app being killed
        self.setWindowFilePath(str(self.manifest_path))  # macOS title-bar proxy icon
        self.setWindowTitle(f"VibeFPMGUI — {self.manifest_path.parent.name}")
        self.reload()

    # ------------------------------------------------------ recent manifests
    MAX_RECENT = 10

    def recent_manifests(self) -> list[str]:
        value = self.settings.value("recent_manifests", [], type=list) or []
        return [str(p) for p in value if p]

    def _remember_manifest(self, path: Path):
        recent = [p for p in self.recent_manifests() if Path(p) != path]
        recent.insert(0, str(path))
        self.settings.setValue("recent_manifests", recent[:self.MAX_RECENT])

    def _fill_recent_menu(self):
        """Rebuilt each time it opens, so missing files show as such."""
        menu = self.recent_menu
        menu.clear()
        recent = self.recent_manifests()
        if not recent:
            menu.addAction("No recent manifests").setEnabled(False)
        home = str(Path.home())
        for path in recent:
            shown = "~" + path[len(home):] if path.startswith(home + os.sep) else path
            exists = Path(path).is_file()
            label = f"{Path(path).parent.name} — {shown}" + ("" if exists else "  (not found)")
            action = menu.addAction(label.replace("&", "&&"))  # "&" would be a mnemonic
            action.setToolTip(path if exists else f"{path}\nThis file no longer exists.")
            action.setEnabled(exists)
            action.setCheckable(True)
            action.setChecked(self.manifest_path is not None and Path(path) == self.manifest_path)
            action.triggered.connect(lambda _=False, p=path: self.open_recent(p))
        menu.addSeparator()
        clear = menu.addAction("Clear Menu")
        clear.setEnabled(bool(recent))
        clear.triggered.connect(self.clear_recent_manifests)

    def open_recent(self, path: str):
        if not Path(path).is_file():
            # It was there when the menu opened, but not any more.
            QMessageBox.warning(self, "File not found",
                                f"{path}\n\nno longer exists, so it was removed from Recent Manifests.")
            self.settings.setValue("recent_manifests",
                                   [p for p in self.recent_manifests() if p != path])
            return
        self.open_manifest(path)

    def clear_recent_manifests(self):
        self.settings.setValue("recent_manifests", [])

    # -------------------------------------------------------- build outputs
    def _config_records(self) -> bo.ConfigRecords:
        digest = hashlib.sha1(str(self.manifest_path).encode()).hexdigest()[:16]
        return bo.ConfigRecords(self.settings, f"build_configs/{digest}")

    def _record_build_folders(self):
        """Remember which build folders the finished build wrote, and with
        which settings, so the Build outputs tab can label them."""
        before, self._folder_snapshot = getattr(self, "_folder_snapshot", None), None
        if before is None or not self.manifest_path:
            return
        changed = bo.changed_folders(before, bo.snapshot(self.manifest_path.parent))
        flags = " ".join(x for x in (self.flag_edit.text().strip(),
                                     " ".join(split_runner(split_args(self.extra_edit.text()))[0]))
                         if x)
        if changed:
            self._config_records().record_build(self._build_description(), flags, changed)
        else:  # up to date: these settings are still the current configuration
            self._config_records().mark_used(self._build_description(), flags)
        self.refresh_build_outputs()

    def refresh_build_outputs(self):
        if not self.manifest_path:
            self.outputs_view.set_configurations([], None)
            return
        records = self._config_records()
        configs = bo.configurations(self.manifest_path.parent, records.load(), records.last_used())
        self.outputs_view.set_configurations(configs, self.manifest_path.parent)

    def delete_configurations(self, configs: list):
        if not configs:
            return
        if self.process is not None:
            QMessageBox.information(self, "Build outputs", "Wait for the running command to finish first.")
            return
        size = bo.human_size(sum(c.size for c in configs))
        names = "\n".join(f"• {c.label} ({bo.human_size(c.size)})" for c in configs)
        answer = QMessageBox.question(
            self, "Delete build outputs",
            f"Delete {len(configs)} configuration{'s' * (len(configs) != 1)} ({size})?\n\n{names}\n\n"
            "They are rebuilt automatically when needed.")
        if answer != QMessageBox.Yes:
            return
        folders = [f.path for c in configs for f in c.folders]
        errors = bo.delete_folders(folders)
        self._config_records().forget([f.name for f in folders])
        self.refresh_build_outputs()
        if errors:
            QMessageBox.warning(self, "Build outputs", "Some folders could not be deleted:\n" + "\n".join(errors))
        else:
            self.statusBar().showMessage(f"Deleted {size} of build outputs", 5000)

    def run_program(self, path: str):
        """Run an already-built program directly (no fpm, no rebuild)."""
        if self.process is not None or not self.manifest_path:
            return
        try:
            args = split_args(self.runner_args_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid arguments", str(exc))
            return
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(self.manifest_path.parent))
        self.process.setProcessChannelMode(QProcess.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._refresh_after = False
        self._partial = ""
        self._held_blank = False
        # Through the same marker wrapper as Test, so the Run tab can follow it.
        self._phase = "direct"
        self._marker_kind = "run"
        self._monitoring = False
        self._test_building = False
        self._folder_snapshot = None
        name = Path(path).name
        if name.lower().endswith(".exe"):
            name = name[:-4]
        self._process_label = name
        self.show_all_output()
        self.run_monitor.begin_listing(f"{Path(path).parent.parent.name} · run directly, no rebuild")
        self.run_monitor.start([name], self._load_durations("run"))
        self.monitor_tabs.setCurrentWidget(self.run_monitor)
        rel = os.path.relpath(path, self.manifest_path.parent)
        self._append(f"\n$ cd {quote_arg(str(self.manifest_path.parent))}\n"
                     f"$ {join_args([rel] + args)}\n", bold=True)
        self.stop_action.setEnabled(True)
        self._set_project_enabled(True)
        self.statusBar().showMessage(f"Running {name}…")
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_runner.py")
        self.process.start(sys.executable, ["-I", script, path] + args)

    def show_compile_command(self, source: str):
        """The exact compiler command fpm used for a source file."""
        if not self.manifest_path:
            return
        commands, _folder = bo.read_compile_commands(self.manifest_path.parent)
        args = commands.get(str(Path(source).resolve()))
        name = Path(source).name
        if args is None:
            QMessageBox.information(
                self, "Compile command",
                f"No compile command for {name} in build/compile_commands.json.\n\n"
                "That file lists the files of the most recent build; build the "
                "project (with this file in it) and try again.")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Compile command — {name}")
        layout = QVBoxLayout(dialog)
        note = QLabel("From <code>build/compile_commands.json</code>, which fpm writes when "
                      "it compiles something — so this is the last build that compiled files, "
                      "which may have used other settings than the current ones. "
                      "Run from the project folder.")
        note.setWordWrap(True)
        layout.addWidget(note)
        text = QPlainTextEdit(bo.format_command(args))
        text.setReadOnly(True)
        text.setFont(self._mono_font())
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        layout.addWidget(text, 1)
        repeated = sorted({a for a in args if a.startswith("-") and args.count(a) > 1})
        if repeated:
            hint = QLabel("<span style='color:gray'>fpm passes some flags more than once: "
                          + ", ".join(f"<code>{html.escape(r)}</code> ×{args.count(r)}" for r in repeated)
                          + " (harmless).</span>")
            hint.setWordWrap(True)
            layout.addWidget(hint)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        copy = QPushButton("Copy")
        copy.setToolTip("Copy as one line")
        copy.clicked.connect(lambda: (QApplication.clipboard().setText(join_args(args)),
                                      copy.setText("Copied")))
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(dialog.accept)
        buttons.addWidget(copy)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        dialog.resize(720, 420)
        dialog.exec()

    # --------------------------------------------------- dependency updates
    def check_dependency_updates(self):
        """Commands → Check for Dependency Updates… (only runs when asked)."""
        if not self.graph:
            return
        checks = dep_updates.git_dependencies(self.graph)
        dialog = dep_updates.UpdatesDialog(checks, repo_web_url, commit_web_url, compare_web_url, self)
        dialog.update_tag_requested.connect(lambda c: self._update_dependency_tag(c, dialog))
        dialog.update_deps_requested.connect(lambda: (dialog.accept(), self.run_fpm(["update"], False, True)))
        if checks:
            self._update_check_thread = dep_updates.start_check(checks, dialog, self)
        dialog.exec()

    def _update_dependency_tag(self, check, dialog):
        """Point one dependency at its newest tag in its fpm.toml."""
        if self.process is not None:
            QMessageBox.information(dialog, "Update dependency", "Wait for the running command to finish first.")
            return
        # A tag pin moves to the newest tag; a commit pin becomes that tag.
        if not dep_updates.set_ref(check.declared_in, check.name, check.ref_kind, check.ref,
                                   "tag", check.latest_tag):
            QMessageBox.warning(
                dialog, "Update dependency",
                f"Couldn't find {check.ref_kind} = \"{check.ref}\" for {check.name} in "
                f"{check.declared_in} to change it automatically; it is opened in the editor instead.")
            dialog.accept()
            self.open_file(str(check.declared_in), force_editor=True)
            return
        was = check.ref if check.ref_kind == "tag" else f"commit {check.ref[:7]}"
        self.statusBar().showMessage(f"{check.name}: {was} → tag {check.latest_tag} in "
                                     f"{Path(check.declared_in).name}", 6000)
        answer = QMessageBox.question(
            dialog, "Update dependency",
            f"{check.name} now asks for tag {check.latest_tag} (was {was}) in {check.declared_in}.\n\n"
            "Run Update deps now to download it?")
        dialog.accept()
        self.reload()  # fpm.toml changed
        if answer == QMessageBox.Yes:
            self.run_fpm(["update"], False, True)

    def _report_dependency_changes(self):
        """After "fpm update": which dependencies moved to another commit."""
        before, self._revs_before = getattr(self, "_revs_before", None), None
        if before is None or not self.manifest_path:
            return
        after = dep_updates.read_cache_revs(self.manifest_path.parent)
        changed = {n: (before.get(n), rev) for n, rev in after.items() if before.get(n) != rev}
        if not changed:
            self._append("\nNo dependency changed commit.\n")
            return
        lines = ["\nDependency changes:"]
        for name, (old, new) in sorted(changed.items()):
            pkg = self.graph.packages.get(name) if self.graph else None
            url = compare_web_url(pkg.git, old, new) if pkg and pkg.git and old else None
            lines.append(f"  {name}: {(old or 'new')[:7]} → {new[:7]}" + (f"  {url}" if url else ""))
        self._append("\n".join(lines) + "\n", bold=False)
        self.statusBar().showMessage(f"Update deps: {len(changed)} dependenc"
                                     f"{'ies' if len(changed) != 1 else 'y'} changed", 6000)

    def _reveal_current_file(self, *_):
        editor = self.current_editor()
        if editor is not None:
            self.project_tree.reveal(str(editor.path))

    def _update_tree_marks(self):
        """Colour files in the tree that had errors/warnings in the last build."""
        marks = {}
        for t in self.build_monitor.targets:
            if t.path is not None and t.status in ("Failed", "Warnings"):
                marks[str(t.path)] = "error" if t.status == "Failed" else "warning"
        self.project_tree.set_marks(marks)

    def _clear_run_panels(self):
        """Empty the output console and the build/test panels."""
        self.show_test_output("")
        self.console.clear()
        self.test_output.clear()
        self.build_monitor.reset()
        self.test_monitor.reset()
        self.run_monitor.reset()
        self.stop_review()
        self._ai_editor = None
        self.review_monitor.reset()
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
        self.project_tree.set_project(self.graph, self.manifest_path.parent)
        self.refresh_build_outputs()
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
                             ("Author", pkg.author)]:
            if value:
                rows.append((label, e(value)))
        if pkg.git:
            web = repo_web_url(pkg.git)
            rows.append(("Git", f'<a href="{e(web)}">{e(pkg.git)}</a>' if web else e(pkg.git)))
        if pkg.git_ref:
            rows.append(("Requested", e(pkg.git_ref)))
        if pkg.git_rev:
            # The exact commit fpm checked out (from build/cache.toml).
            commit_url = commit_web_url(pkg.git, pkg.git_rev)
            short = e(pkg.git_rev[:7])
            rows.append(("Commit", (f'<a href="{e(commit_url)}" title="{e(pkg.git_rev)}">{short}</a>'
                                    if commit_url else f'<span title="{e(pkg.git_rev)}">{short}</span>')
                         + f' <span style="color:gray">({e(pkg.git_rev)})</span>'))
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
            self.open_file(str(pkg.manifest))

    # -------------------------------------------------------------- editor
    def editors(self) -> list[EditorTab]:
        return [w for i in range(self.tabs.count())
                if isinstance(w := self.tabs.widget(i), EditorTab)]

    def current_editor(self) -> EditorTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, EditorTab) else None

    def _with_editor(self, action):
        editor = self.current_editor()
        if editor is not None:
            action(editor)

    def browse_file(self):
        start = str(self.manifest_path.parent) if self.manifest_path else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(self, "Open file in editor", start)
        if filename:
            self.open_file(filename, force_editor=True)

    def open_file(self, path: str, diagnostics: list | None = None, force_editor: bool = False):
        """Open a file in the built-in editor (or the default application),
        marking any compiler diagnostics and jumping to the first one."""
        file = Path(path).resolve()
        use_editor = force_editor or self.use_editor_action.isChecked()
        if not use_editor or not file.is_file() or not EditorTab.is_probably_text(file):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(file)))
            return
        editor = next((e for e in self.editors() if e.path == file), None)
        if editor is None:
            try:
                editor = EditorTab(file)
            except OSError as exc:
                QMessageBox.warning(self, "Can't open file", f"{file}:\n{exc}")
                return
            editor.set_font(self._mono_font())
            editor.dirty_changed.connect(lambda _dirty, e=editor: self._update_editor_title(e))
            editor.path_changed.connect(lambda _p, e=editor: self._update_editor_title(e))
            editor.saved.connect(self._editor_saved)
            self.tabs.addTab(editor, file.name)
            self._update_editor_title(editor)
        self.tabs.setCurrentWidget(editor)
        if diagnostics:
            editor.show_diagnostics(diagnostics)
            editor.editor.go_to_line(diagnostics[0].line)
        else:
            editor.editor.setFocus()

    def _mono_font(self) -> QFont:
        base = self._base_font.pointSizeF()
        return monospace_font((base + self.font_delta) / base)

    def _update_editor_title(self, editor: EditorTab):
        index = self.tabs.indexOf(editor)
        if index >= 0:
            self.tabs.setTabText(index, ("• " if editor.is_dirty() else "") + editor.path.name)
            self.tabs.setTabToolTip(index, str(editor.path))
        self._update_editor_actions()

    def _editor_saved(self, path: str):
        self.statusBar().showMessage(f"Saved {path}", 4000)
        if self.manifest_path and Path(path) == self.manifest_path:
            self.reload()  # graph, features and profiles may have changed

    def close_all_editors(self) -> bool:
        """Close every editor tab. Unsaved files are asked about first; if
        any answer is Cancel, nothing is closed and False is returned."""
        editors = self.editors()
        for editor in editors:
            if editor.is_dirty():
                self.tabs.setCurrentWidget(editor)
                if not editor.maybe_save():
                    return False
        for editor in editors:
            self.tabs.removeTab(self.tabs.indexOf(editor))
            editor.deleteLater()
        return True

    def close_editor(self, index: int):
        widget = self.tabs.widget(index)
        if not isinstance(widget, EditorTab):
            return
        self.tabs.setCurrentIndex(index)
        if widget.maybe_save():
            self.tabs.removeTab(index)
            widget.deleteLater()

    def _update_editor_actions(self, *_):
        has_editor = self.current_editor() is not None
        for action in (self.save_action, self.save_as_action, self.close_tab_action,
                       self.find_action, self.replace_action, self.find_next_action,
                       self.find_previous_action, self.goto_action):
            action.setEnabled(has_editor)
        self._update_review_actions()

    def _update_review_actions(self):
        """AI review needs an editor tab in front (and no review running)."""
        has_editor = self.current_editor() is not None
        self.review_action.setEnabled(has_editor and self._ai_thread is None)
        self.clear_review_action.setEnabled(has_editor)

    def _build_edit_menu(self):
        menu = self.menuBar().addMenu("&Edit")

        def focused(method):
            """Apply to the widget with focus (an editor, a text field…)."""
            def run():
                widget = QApplication.focusWidget()
                if widget is not None and hasattr(widget, method):
                    getattr(widget, method)()
                elif self.current_editor() is not None:
                    getattr(self.current_editor().editor, method)()
            return run

        for item in [("Undo", QKeySequence.Undo, "undo"),
                     ("Redo", QKeySequence.Redo, "redo"), None,
                     ("Cut", QKeySequence.Cut, "cut"),
                     ("Copy", QKeySequence.Copy, "copy"),
                     ("Paste", QKeySequence.Paste, "paste"),
                     ("Select All", QKeySequence.SelectAll, "selectAll")]:
            if item is None:
                menu.addSeparator()
                continue
            text, key, method = item
            action = menu.addAction(text)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(focused(method))
        menu.addSeparator()

        def editor_action(text, key, slot):
            action = menu.addAction(text)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(lambda: self._with_editor(slot))
            return action

        self.find_action = editor_action("Find…", QKeySequence.Find, lambda e: e.find_bar.open_bar())
        self.replace_action = editor_action("Find and Replace…", "Ctrl+Alt+F",
                                            lambda e: e.find_bar.open_bar(replace=True))
        self.find_next_action = editor_action("Find Next", QKeySequence.FindNext,
                                              lambda e: e.find_bar.find_next())
        self.find_previous_action = editor_action("Find Previous", QKeySequence.FindPrevious,
                                                  lambda e: e.find_bar.find_previous())
        menu.addSeparator()
        self.goto_action = editor_action("Go to Line…", "Ctrl+L", lambda e: e.go_to_line_dialog())

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
                args += split_args(self.target_edit.text())
            extra = split_args(self.extra_edit.text())
            if sub[0] in ("test", "run"):
                extra, _ = split_runner(extra)  # our runner wraps the user's  # the test runner is handled by run_tests
            args += extra
            if program_args and sub[0] in ("run", "test") and self.runner_args_edit.text().strip():
                args += ["--"] + split_args(self.runner_args_edit.text())
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
        compiler, _source = self.effective_compiler()
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
        # Commands that compile get the per-file build grid. "fpm test --list"
        # doesn't build; "fpm test" builds first, so its output goes to the
        # build grid until the first test starts (see _test_line).
        self._monitoring = sub[0] in ("build", "run", "test") and phase != "list"
        self._test_building = phase == "marked"  # fpm builds before running
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
            self.monitor_tabs.setCurrentWidget(self.build_monitor)

        self._append(f"\n$ cd {quote_arg(str(self.manifest_path.parent))}\n"
                     f"$ {join_args([program] + args)}\n", bold=True)
        self.stop_action.setEnabled(True)
        self._set_project_enabled(True)
        self.statusBar().showMessage(f"Running fpm {' '.join(sub)}…")
        self.process.start(program, args)

    def stop(self):
        if self.process is not None:
            kill_process_tree(self.process)

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
        if self._phase in ("marked", "direct"):
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
        return text + (shown + "\n" if shown is not None else "")

    def _build_part_done(self):
        """The build part of "fpm test" is over: show the tests."""
        self._test_building = False
        if self._monitoring:
            self.build_monitor.finish(0)
            self._update_tree_marks()
            self._monitoring = False
        self._marker_monitor().build_finished()
        self.monitor_tabs.setCurrentWidget(self._marker_monitor())

    def _flush_monitor(self, exit_code: int, stopped: bool):
        if self._phase in ("marked", "direct"):
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
            self._update_tree_marks()
            self._monitoring = False

    def _finished(self, code: int, status):
        self._read_output()
        crashed = status == QProcess.CrashExit
        self._flush_monitor(code, crashed)
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

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            if getattr(self, "_process_label", "fpm") == "fpm":
                self._append(f"\nCould not start '{self._fpm_executable()}'. "
                             "Set the fpm executable in Build options.\n", bold=True)
            else:
                self._append(f"\nCould not start '{self._process_label}'.\n", bold=True)
            self.statusBar().showMessage(f"{getattr(self, '_process_label', 'fpm')} failed to start")
            self._flush_monitor(-1, True)
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

    # ------------------------------------------------ tests and program runs
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
        if not self.manifest_path or self.process is not None:
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
        run_args = run_args[:cut] + ["--runner", runner] + run_args[cut:]
        patterns = split_args(self.target_edit.text())
        self._marker_kind = kind
        self._test_args = (run_args, inner_runner, patterns)
        description = self._build_description()
        if inner_runner:
            description += f" · runner: {inner_runner}"
        self.build_monitor.reset()  # the build progress shown next is this run's
        self._marker_monitor().begin_listing(description)
        self.show_all_output()
        self.run_fpm([kind], True, False, phase="list", args=list_args)

    def _after_listing(self, code: int, stopped: bool):
        monitor = self._marker_monitor()
        output = "".join(self._captured)
        names = parse_test_list(output)
        if stopped or code != 0:
            monitor.finish(code, stopped=stopped)
            self.monitor_tabs.setCurrentWidget(self.build_monitor)
            return
        if names == []:
            monitor.finish(0, no_tests=True)
            self.monitor_tabs.setCurrentWidget(monitor)
            self.statusBar().showMessage(monitor.word("none"))
            return
        run_args, inner_runner, patterns = self._test_args
        if names and patterns:
            # "--list" lists every name regardless of the names given, so apply
            # the Target names/globs ourselves, as fpm will.
            names = [n for n in names if any(fnmatch.fnmatchcase(n, p) for p in patterns)]
        # fpm builds first (the Build tab shows it), then runs them.
        monitor.start(names or [], self._load_durations(self._marker_kind), building=True)
        env = {"VIBEFPM_INNER_RUNNER": inner_runner} if inner_runner else {}
        self.run_fpm([self._marker_kind], True, True, phase="marked", args=run_args, env_extra=env)

    def _after_marked(self, code: int, stopped: bool):
        monitor = self._marker_monitor()
        monitor.finish(code, stopped=stopped)
        self._save_durations(self._marker_kind, monitor.durations())
        counts = monitor.counts()
        total = len(monitor.tests)
        title = monitor.word("title")
        if stopped:
            message = f"{title}: stopped"
        elif monitor._result == "buildfailed":
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
        digest = hashlib.sha1(str(self.manifest_path).encode()).hexdigest()[:16]
        return f"{kind}_durations/{digest}"

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
        self.test_output.setPlainText(text)
        self.output_title.setText(f"<b>Output</b> — {_breakable(html.escape(title))}")
        self.output_stack.setCurrentWidget(self.test_output)
        self.show_all_button.show()

    def show_all_output(self):
        self.output_title.setText("<b>Output</b>")
        self.output_stack.setCurrentWidget(self.console)
        self.show_all_button.hide()
        self.build_monitor.clear_selection()
        for monitor in (self.test_monitor, self.run_monitor):
            if monitor.selected:
                monitor.selected = ""
                monitor.table.viewport().update()

    # ------------------------------------------------------------ AI review
    def _build_ai_menu(self):
        self.ai_model = self.settings.value("ai_model", ai.DEFAULT_MODEL)
        self.ai_focus = self.settings.value("ai_focus", "general")
        self._ai_thread = None
        self._ai_worker = None
        self._ai_editor: EditorTab | None = None
        self._ollama_process: QProcess | None = None

        menu = self.menuBar().addMenu("&AI")
        self.review_action = menu.addAction("Review Current File")
        self.review_action.setShortcut(QKeySequence("Ctrl+Alt+R"))
        self.review_action.setStatusTip("Ask a local AI model (via Ollama) to review the file in the editor")
        self.review_action.triggered.connect(self.start_review)
        # Also on the toolbar, at the far right.
        self.review_action.setIconText("AI Review")
        keys = self.review_action.shortcut().toString(QKeySequence.NativeText)
        self.review_action.setToolTip(f"AI Review ({keys}) — review the file in the editor "
                                      "with a local AI model")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.command_toolbar.addWidget(spacer)
        self.command_toolbar.addAction(self.review_action)
        self.command_icons[self.review_action] = "robot"
        self._update_command_icons()
        self.stop_review_action = menu.addAction("Stop Review")
        self.stop_review_action.setEnabled(False)
        self.stop_review_action.triggered.connect(self.stop_review)
        self.clear_review_action = menu.addAction("Clear Review Markers")
        self.clear_review_action.triggered.connect(
            lambda: self._with_editor(lambda e: e.clear_review()))
        menu.addSeparator()
        self.model_menu = menu.addMenu("Model")
        self.model_menu.aboutToShow.connect(self._fill_model_menu)
        focus_menu = menu.addMenu("Focus")
        group = QActionGroup(self)
        for key, label in ai.FOCUS_LABELS.items():
            action = focus_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self.ai_focus)
            action.setStatusTip(ai.FOCUS[key])
            action.triggered.connect(lambda _=False, k=key: self._set_ai_focus(k))
            group.addAction(action)

    def _set_ai_focus(self, key: str):
        self.ai_focus = key
        self.settings.setValue("ai_focus", key)

    def _set_ai_model(self, name: str):
        self.ai_model = name
        self.settings.setValue("ai_model", name)
        self.statusBar().showMessage(f"AI model: {name}", 4000)

    def _fill_model_menu(self):
        """Installed models (if Ollama is running), plus the current choice."""
        menu = self.model_menu
        menu.clear()
        models = ai.installed_models() if ai.ollama_available() else []
        names = sorted(set(models) | {self.ai_model})
        group = QActionGroup(menu)
        for name in names:
            label = name if name in models else f"{name} (not downloaded yet)"
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(name == self.ai_model)
            action.triggered.connect(lambda _=False, n=name: self._set_ai_model(n))
            group.addAction(action)
        if not models:
            note = menu.addAction("(Ollama isn't running — installed models appear once it is)")
            note.setEnabled(False)
        menu.addSeparator()
        other = menu.addAction("Other Model…")
        other.triggered.connect(self._choose_other_model)

    def _choose_other_model(self):
        name, ok = QInputDialog.getText(
            self, "AI model", "Ollama model name (e.g. qwen2.5-coder:14b, qwen3-coder:30b, "
            "gpt-oss:20b). It's downloaded the first time it's used:", text=self.ai_model)
        if ok and name.strip():
            self._set_ai_model(name.strip())

    def start_review(self):
        editor = self.current_editor()
        if editor is None:
            QMessageBox.information(self, "AI review", "Open a file in the editor first "
                                    "(double-click a file, or File → Open File in Editor).")
            return
        if self._ai_thread is not None:
            return
        if not ai.ollama_available():
            QMessageBox.warning(self, "AI review", "The 'ollama' Python package isn't installed.\n"
                                "Run 'pixi install' (or 'pip install ollama').")
            return
        self._ai_editor = editor
        self._ensure_ollama(self._check_model)

    def _ensure_ollama(self, then):
        """Start "ollama serve" if no server answers, then call ``then``."""
        if ai.server_running():
            then()
            return
        exe = ai.ollama_executable()
        if exe is None:
            QMessageBox.warning(self, "AI review", "Ollama isn't running and the 'ollama' program "
                                "wasn't found. Run 'pixi install', or install Ollama from ollama.com.")
            return
        self.monitor_tabs.setCurrentWidget(self.review_monitor)
        self.review_monitor.begin("starting the local Ollama server", "Starting Ollama")
        self._ollama_process = QProcess(self)
        self._ollama_process.setProcessChannelMode(QProcess.MergedChannels)
        self._ollama_process.start(exe, ["serve"])
        deadline = time.monotonic() + 20

        def poll():
            if ai.server_running():
                then()
            elif time.monotonic() > deadline or self._ollama_process.state() == QProcess.NotRunning:
                self.review_monitor.finish(False, "Could not start the Ollama server.")
            else:
                QTimer.singleShot(300, poll)

        QTimer.singleShot(300, poll)

    def _check_model(self):
        model = self.ai_model if ":" in self.ai_model else self.ai_model + ":latest"
        installed = ai.installed_models()
        if model in installed or self.ai_model in installed:
            self._run_review()
            return
        answer = QMessageBox.question(
            self, "Download AI model",
            f"The model '{self.ai_model}' isn't downloaded yet. Download it now?\n\n"
            "Models are a few GB; this happens once, and everything runs locally afterwards.")
        if answer != QMessageBox.Yes:
            return
        self.monitor_tabs.setCurrentWidget(self.review_monitor)
        self.review_monitor.begin(f"downloading {self.ai_model}", "Downloading")
        worker = ai.ReviewWorker(ai.DEFAULT_HOST, self.ai_model, pull_only=True)
        worker.progress.connect(self._pull_progress)
        worker.finished.connect(lambda *_: self._ai_done(then=self._run_review))
        worker.failed.connect(self._review_failed)
        self._start_ai_worker(worker)

    def _pull_progress(self, status: str, fraction: float):
        if fraction >= 0:
            self.review_monitor.set_status(f"Downloading {self.ai_model} {fraction * 100:.0f}%", fraction)
        else:
            self.review_monitor.set_status(status.capitalize() or "Downloading")

    def _run_review(self):
        editor = self._ai_editor
        if editor is None or editor not in self.editors():
            return
        code = editor.editor.toPlainText()
        diagnostics = [(line, sev, msg) for line, notes in sorted(editor._compiler_markers.items())
                       for sev, msg in notes]
        prompt = ai.build_prompt(editor.path.name, editor.highlighter.lexer.name, code,
                                 self.ai_focus, diagnostics)
        focus = ai.FOCUS_LABELS.get(self.ai_focus, self.ai_focus)
        self.monitor_tabs.setCurrentWidget(self.review_monitor)
        self.review_monitor.begin(f"{editor.path.name} · {self.ai_model} · {focus.lower()}", "Thinking")
        self._show_single_output(f"AI review of {editor.path.name} ({self.ai_model})", "")
        self._review_text = ""
        self._review_keys = []
        editor.clear_review()  # markers from an earlier review would be misleading
        worker = ai.ReviewWorker(ai.DEFAULT_HOST, self.ai_model, prompt)
        worker.chunk.connect(self._review_chunk)
        worker.finished.connect(self._review_finished)
        worker.failed.connect(self._review_failed)
        self._start_ai_worker(worker)

    def _start_ai_worker(self, worker):
        self._ai_worker = worker
        self._ai_thread = ai.start_worker(worker, self)
        self._ai_thread.finished.connect(self._ai_thread_done)
        self._update_review_actions()
        self.stop_review_action.setEnabled(True)

    def _ai_thread_done(self):
        self._ai_thread = None
        self._ai_worker = None
        self._update_review_actions()
        self.stop_review_action.setEnabled(False)

    def _ai_done(self, then=None):
        # Let the download thread finish before starting the review thread.
        if then is not None:
            QTimer.singleShot(100, lambda: self._after_ai_thread(then))

    def _after_ai_thread(self, then):
        if self._ai_thread is not None:
            QTimer.singleShot(100, lambda: self._after_ai_thread(then))
        else:
            then()

    def _review_chunk(self, text: str):
        cursor = self.test_output.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self.test_output.setTextCursor(cursor)
        self.test_output.ensureCursorVisible()
        self.review_monitor.add_text(text)
        # Text arrives a line at a time: re-read the answer so far and show
        # the findings found so far (the list and the editor's markers).
        self._review_text += text
        editor = self._ai_editor
        max_line = editor.editor.blockCount() if editor in self.editors() else None
        findings, _summary = ai.parse_findings(self._review_text, max_line)
        keys = [(f.line, f.end_line, f.category, f.comment) for f in findings]
        if keys != self._review_keys:
            self._review_keys = keys
            self.review_monitor.set_findings(findings, final=False)
            if editor in self.editors():
                editor.show_review(findings)

    def _review_finished(self, text: str, stats: dict):
        editor = self._ai_editor
        max_line = editor.editor.blockCount() if editor in self.editors() else None
        findings, summary = ai.parse_findings(text, max_line)
        self.review_monitor.set_findings(findings, summary)
        detail = f"{stats.get('seconds', 0):.1f} s · {self.ai_model}" if stats else self.ai_model
        if stats.get("repeated"):
            detail += " · stopped a repetition loop"
        elif stats.get("truncated"):
            detail += " · answer cut off at the length limit"
        self.review_monitor.finish(True, detail)
        if editor in self.editors():
            editor.show_review(findings)
        self.statusBar().showMessage(f"AI review: {len(findings)} findings", 5000)

    def _review_failed(self, message: str):
        hint = ""
        if "not found" in message.lower():
            hint = f" (is '{self.ai_model}' downloaded? See AI → Model)"
        self.review_monitor.finish(False, message + hint)

    def stop_review(self):
        if self._ai_worker is not None:
            self._ai_worker.cancel()

    def _go_to_finding(self, line: int):
        editor = self._ai_editor
        if editor in self.editors():
            self.tabs.setCurrentWidget(editor)
            editor.editor.go_to_line(line, select=True)

    def _shutdown_ai(self):
        if self._ai_worker is not None:
            self._ai_worker.cancel()
        if self._ai_thread is not None:
            self._ai_thread.quit()
            self._ai_thread.wait(3000)
        if self._ollama_process is not None:  # only a server we started ourselves
            self._ollama_process.terminate()
            self._ollama_process.waitForFinished(3000)


def repo_web_url(git_url: str) -> str | None:
    """The web page of a GitHub/GitLab/Codeberg/Bitbucket repository."""
    url = git_url.strip()
    match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?/?$", url)
    if match:
        host, path = match.groups()
    else:
        match = re.match(r"^(?:https?|git|ssh)://(?:[^@/]+@)?([^/:]+)(?::\d+)?/(.+?)(?:\.git)?/?$", url)
        if not match:
            return None
        host, path = match.groups()
    if not any(h in host for h in ("github.", "gitlab.", "codeberg.", "bitbucket.")):
        return None
    return f"https://{host}/{path}"


def commit_web_url(git_url: str, rev: str) -> str | None:
    base = repo_web_url(git_url)
    if not base or not re.fullmatch(r"[0-9a-fA-F]{7,40}", rev or ""):
        return None
    if "bitbucket." in base:
        return f"{base}/commits/{rev}"
    if "gitlab." in base:
        return f"{base}/-/commit/{rev}"
    return f"{base}/commit/{rev}"


def compare_web_url(git_url: str, old: str, new: str) -> str | None:
    """A page listing the commits between two revisions (GitHub/GitLab)."""
    base = repo_web_url(git_url)
    if not base or not old or not new:
        return None
    if "gitlab." in base:
        return f"{base}/-/compare/{old}...{new}"
    if "github." in base or "codeberg." in base:
        return f"{base}/compare/{old}...{new}"
    return None


def kill_process_tree(process: QProcess):
    """Kill a QProcess *and everything it started* (fpm's compilers, the test
    runner and the test program): killing only fpm would leave them running."""
    pid = int(process.processId() or 0)
    if pid > 0:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
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
                             capture_output=True, text=True, timeout=5).stdout
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


def make_settings() -> QSettings:
    """The app's saved settings. VIBEFPMGUI_SETTINGS=/path/file.ini uses a
    separate file instead (e.g. for testing without touching your own)."""
    path = os.environ.get("VIBEFPMGUI_SETTINGS")
    if path:
        return QSettings(path, QSettings.IniFormat)
    return QSettings("VibeFPMGUI", "VibeFPMGUI")


def _breakable(text: str) -> str:
    """Let long names wrap: add zero-width spaces after _ . / - (word wrap
    otherwise only breaks at spaces). For display only: text copied from the
    label may include them."""
    return re.sub(r"([_./\\-])(?=\w)", "\\1\u200b", text)


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
    return join_args(parts)  # quoted for the platform's shell


def main(argv: list[str] | None = None) -> int:
    import sys

    argv = sys.argv if argv is None else argv
    set_windows_app_id()  # Windows: our own taskbar icon, not python.exe's
    app = QApplication.instance() or QApplication(argv)
    app.setApplicationName("VibeFPMGUI")
    app.setWindowIcon(app_icon())  # also the Dock icon on macOS
    app.setApplicationVersion(__version__)
    path = next((a for a in argv[1:] if not a.startswith("-")), None)
    if path and Path(path).is_dir():
        path = str(Path(path) / "fpm.toml")
    window = MainWindow(path)
    window.show()
    return app.exec()
