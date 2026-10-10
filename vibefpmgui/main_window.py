"""Main window: pick an fpm.toml, run fpm commands, view the dependency graph."""

from __future__ import annotations

import html
import re
import shlex
import shutil
import tomllib
from pathlib import Path

from qtpy.QtCore import QEvent, QProcess, QProcessEnvironment, QSettings, Qt, QUrl
from qtpy.QtGui import QAction, QActionGroup, QColor, QPalette, QDesktopServices, QFont, QFontDatabase, QKeySequence, QTextCursor
from qtpy.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton, QSplitter,
    QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import __version__
from . import manifest as m
from .build_monitor import BuildMonitor
from .highlighter import DARK_STYLE, LIGHT_STYLE, PygmentsHighlighter, is_dark
from .graph_view import EDGE_STYLES, KIND_LABELS, LAYOUTS, THEME_CHOICES, DependencyGraphView

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# (label, fpm sub-command, uses build options, refresh graph afterwards)
COMMANDS = [
    ("Build", ["build"], True, True),
    ("Run", ["run"], True, True),
    ("Test", ["test"], True, True),
    ("Update deps", ["update"], False, True),
    ("Clean", ["clean", "--skip"], False, False),
    ("Clean all", ["clean", "--all"], False, True),
]


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
        self.command_buttons: list[QPushButton] = []

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
        reload_action.setShortcut(QKeySequence.Refresh)
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
        self.profile_combo.addItems(["(default)", "debug", "release"])
        form.addRow("Profile:", self.profile_combo)
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
        self.verbose_check = QCheckBox("Verbose (--verbose)")
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

        commands = QGroupBox("Commands")
        grid = QVBoxLayout(commands)
        for label, args, uses_options, refresh in COMMANDS:
            button = QPushButton(label)
            button.clicked.connect(
                lambda _=False, a=args, o=uses_options, r=refresh: self.run_fpm(a, o, r))
            grid.addWidget(button)
            self.command_buttons.append(button)
        self.stop_button = QPushButton("Stop")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self.stop)
        grid.addWidget(self.stop_button)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.addWidget(options)
        left_layout.addWidget(commands)
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
        console_box = QWidget()
        cl = QVBoxLayout(console_box)
        cl.setContentsMargins(0, 0, 0, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Output</b>"))
        header.addStretch(1)
        clear = QPushButton("Clear")
        clear.clicked.connect(self.console.clear)
        header.addWidget(clear)
        cl.addLayout(header)
        cl.addWidget(self.console)

        self.build_monitor = BuildMonitor()
        self.bottom_split = QSplitter(Qt.Horizontal)
        self.bottom_split.addWidget(self.build_monitor)
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

    def _set_project_enabled(self, enabled: bool):
        running = self.process is not None
        for b in self.command_buttons:
            b.setEnabled(enabled and not running)

    # ------------------------------------------------------------ settings
    def _restore_settings(self):
        s = self.settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        self.profile_combo.setCurrentText(s.value("profile", "(default)"))
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
        self.manifest_path = Path(filename).resolve()
        self.settings.setValue("last_manifest", str(self.manifest_path))
        self.setWindowFilePath(str(self.manifest_path))  # macOS title-bar proxy icon
        self.setWindowTitle(f"VibeFPMGUI — {self.manifest_path.parent.name}")
        self.reload()

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

    def build_arguments(self, sub: list[str], uses_options: bool) -> list[str]:
        args = list(sub)
        if uses_options:
            if self.profile_combo.currentIndex() > 0:
                args += ["--profile", self.profile_combo.currentText()]
            if self.compiler_edit.text().strip():
                args += ["--compiler", self.compiler_edit.text().strip()]
            if self.flag_edit.text().strip():
                args += ["--flag", self.flag_edit.text().strip()]
            if self.verbose_check.isChecked():
                args.append("--verbose")
            if sub[0] in ("run", "test") and self.target_edit.text().strip():
                args += shlex.split(self.target_edit.text())
            args += shlex.split(self.extra_edit.text())
            if sub[0] in ("run", "test") and self.runner_args_edit.text().strip():
                args += ["--"] + shlex.split(self.runner_args_edit.text())
        elif sub[0] == "update" and self.verbose_check.isChecked():
            args.append("--verbose")
        return args

    def run_fpm(self, sub: list[str], uses_options: bool, refresh: bool):
        if not self.manifest_path or self.process is not None:
            return
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
        self.process.setProcessEnvironment(env)
        self.process.readyReadStandardOutput.connect(self._read_output)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._process_error)
        self._refresh_after = refresh
        self._partial = ""
        # Only commands that compile get the per-file status grid.
        self._monitoring = sub[0] in ("build", "run", "test")
        if self._monitoring:
            profile = self.profile_combo.currentText() if self.profile_combo.currentIndex() > 0 else "debug"
            compiler = self.compiler_edit.text().strip() or "gfortran"
            self.build_monitor.start(self.graph, self.manifest_path.parent, f"{profile} · {compiler}")

        self._append(f"\n$ cd {shlex.quote(str(self.manifest_path.parent))}\n"
                     f"$ {shlex.join([program] + args)}\n", bold=True)
        self.stop_button.setEnabled(True)
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
        self._append(data)
        if self._monitoring:
            # Output can arrive mid-line; only parse complete lines.
            lines = (self._partial + data).split("\n")
            self._partial = lines.pop()
            for line in lines:
                self.build_monitor.feed_line(line)

    def _flush_monitor(self, exit_code: int, stopped: bool):
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
        self._cleanup_process()
        self.statusBar().showMessage("Stopped" if crashed else
                                     ("Finished successfully" if code == 0 else f"Failed (exit code {code})"))

    def _process_error(self, error):
        if error == QProcess.FailedToStart:
            self._append(f"\nCould not start '{self._fpm_executable()}'. "
                         "Set the fpm executable in Build options.\n", bold=True)
            self.statusBar().showMessage("fpm failed to start")
            self._flush_monitor(-1, True)
            self._cleanup_process()

    def _cleanup_process(self):
        if self.process is None:
            return
        self.process.deleteLater()
        self.process = None
        self.stop_button.setEnabled(False)
        self._set_project_enabled(self.graph is not None)
        if self._refresh_after:
            self.reload()


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
