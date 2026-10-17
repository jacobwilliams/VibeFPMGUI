"""Main window: pick an fpm.toml, run fpm commands, view the dependency graph.

`MainWindow` is put together from the mixins in `vibefpmgui.window` (one
per area of the window); here it's started, built in order and closed,
and `main` starts the app.
"""

from __future__ import annotations

import codecs
import os
import sys
import tempfile
from collections import deque
from pathlib import Path

from qtpy.QtCore import QProcess, QSettings, Qt
from qtpy.QtGui import QAction
from qtpy.QtWidgets import QApplication, QMainWindow, QMessageBox

from . import __version__, lsp, themes
from . import manifest as m
from .compat import set_macos_bundle_name, set_macos_display_name, set_windows_app_id
from .graph_view import EDGE_STYLES, LAYOUTS, THEME_CHOICES
from .icons import app_icon
from .parallel_tests import ParallelTests
from .project_options import LayeredSettings
from .window.ai import AIMixin
from .window.ai_chat import AIChatMixin
from .window.build_outputs import BuildOutputsMixin
from .window.complexity import ComplexityMixin
from .window.configs import ConfigsMixin
from .window.coverage import CoverageMixin
from .window.editing import EditingMixin
from .window.editor_tabs import EditorTabsMixin
from .window.git import GitMixin
from .window.language import LanguageMixin
from .window.options import ProjectOptionsMixin
from .window.output import OUTPUT_TAIL_LINES, OutputMixin
from .window.packages import PackagesMixin
from .window.panels import PanelsMixin
from .window.project import ProjectMixin
from .window.running import RunningMixin
from .window.search import SearchMixin
from .window.testing import TestingMixin
from .window.view import ViewMixin


class MainWindow(PanelsMixin, ViewMixin, ProjectMixin, PackagesMixin,
                 EditorTabsMixin, EditingMixin, LanguageMixin, SearchMixin, GitMixin,
                 RunningMixin, ConfigsMixin, ProjectOptionsMixin, BuildOutputsMixin, OutputMixin, TestingMixin, CoverageMixin, ComplexityMixin,
                 AIMixin, AIChatMixin,
                 QMainWindow):
    """The window. Each part of it is a mixin in `vibefpmgui.window`; this
    class starts it, builds it (in order: `_build_ui`), and closes it."""

    def __init__(self, manifest_path: str | None = None, remember_last: bool = True):
        """``remember_last`` False: an extra window (File → Open in New Window)
        doesn't become the project reopened at the next start."""
        super().__init__()
        self.remember_last = remember_last
        self.setWindowTitle("VibeFPMGUI")
        self.resize(1300, 850)
        self.settings = LayeredSettings(make_settings())
        # The project opened first: its options (if its configuration file has
        # them) are the settings the window is made with.
        start = manifest_path or self.settings.value("last_manifest", "")
        start = Path(start).resolve() if start and Path(start).is_file() else None
        self._use_project_options(start)
        themes.chosen_style = str(self.settings.value("app_style", themes.AUTOMATIC) or themes.AUTOMATIC)
        themes.apply(str(self.settings.value("app_theme", themes.SYSTEM) or themes.SYSTEM))  # (before the widgets)
        self._session_busy = False  # restoring the open files (don't save meanwhile)
        self.lsp_client = lsp.LspClient(self)  # fortls, for the editor (see _update_language_server)
        self.lsp_client.stopped.connect(self._language_server_stopped)
        self.lsp_client.message.connect(lambda text: self.statusBar().showMessage(f"fortls: {text}", 8000))
        self.lsp_client.diagnostics.connect(self._lsp_diagnostics)
        self._lsp_diagnostics_by_file: dict[Path, list] = {}  # the latest, to show again when turned on
        self.manifest_path: Path | None = None
        self.graph: m.DependencyGraph | None = None
        self.process: QProcess | None = None
        self.parallel_tests: ParallelTests | None = None  # tests run by us, several at once
        self._refresh_after = False
        self._monitoring = False
        self._partial = ""  # output text after the last newline
        # The running command's output as text: a character whose bytes arrive
        # in two reads is decoded whole (see _begin_output).
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._esc_tail = ""  # color code cut off at the end of the last output chunk
        self._full_output_path = Path(tempfile.gettempdir()) / f"vibefpmgui-output-{os.getpid()}.log"
        self._full_output = None  # the file the running command's whole output is saved to
        self._shown_chars = 0     # how much of it the Output panel shows (see OUTPUT_SHOW_LIMIT)
        self._hidden_chars = 0
        self._hidden_lines = 0
        self._tail: deque[str] = deque(maxlen=OUTPUT_TAIL_LINES)
        self._tail_partial = ""
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

        if start is not None:
            self.open_manifest(start)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        """The menus, the panels and the editor, in this order (later steps
        connect to what earlier ones made)."""
        self._build_file_menu()
        self._build_edit_menu()
        self._build_view_menu()
        options = self._build_options_panel()
        self._build_commands()
        self._build_features_menu()
        self._build_ai_menu()
        files_box = self._build_project_files()
        graph_tab = self._build_graph_panel()
        self._build_editor_tabs()
        self._build_outputs_view()
        console_box = self._build_output_panel()
        self._build_run_panels()
        self._build_review_panel()
        self._build_search_panels()
        self._build_terminal()
        self._build_git_panel()
        self._build_complexity_panel()
        self._link_file_lists()
        self._build_central()
        self._build_docks(options, files_box, graph_tab, console_box)
        self.statusBar().showMessage("Ready")
        self._set_project_enabled(False)
        self._build_help_menu()

    # ------------------------------------------------------------ settings
    def _restore_settings(self):
        s = self.settings
        geometry = s.value("geometry")
        if geometry is not None:
            self.restoreGeometry(geometry)
        layout = s.value("panels")  # (where each panel was: docked, stacked, floating, hidden)
        self._layout_restored = layout is not None and self.restore_layout(
            layout, s.value("panels_front", [], type=list) or [])
        # A layout saved before a panel existed: the panel goes in the stack of
        # the one it's next to by default.
        for name, beside in (("coverage", "tests"), ("git", "search"), ("complexity", "coverage")):
            dock = self.docks[name]
            if self._layout_restored and self.dockWidgetArea(dock) == Qt.NoDockWidgetArea:
                self.addDockWidget(Qt.BottomDockWidgetArea, dock)
                self.tabifyDockWidget(self.docks[beside], dock)
        # Applied once the project's own [profiles] are in the list.
        self._apply_build_options()
        self.fpm_edit.setText(s.value("fpm", ""))
        self.set_font_delta(s.value("font_delta", 0, type=int), announce=False)
        self._set_graph_layout(s.value("graph_layout", next(iter(LAYOUTS))))
        self._set_graph_theme(s.value("graph_theme", next(iter(THEME_CHOICES))))
        self._set_graph_edges(s.value("graph_edges", next(iter(EDGE_STYLES))))

    def clear_settings_and_quit(self):
        """Forget every saved setting and quit, so the next start is like the
        first (after asking; nothing happens if a window can't close)."""
        answer = QMessageBox.warning(
            self, "Clear Settings and Quit",
            "Clear all of VibeFPMGUI's saved settings and quit?\n\n"
            "Recent projects, open files, window and panel layout, build options, View menu "
            "choices and your spelling dictionary are all forgotten; the next start is like "
            "the first. Your files are not touched.\n\n"
            "Close any other VibeFPMGUI windows first: they save their settings when they close.",
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if answer != QMessageBox.Yes:
            return
        self._clearing_settings = True  # (closeEvent: nothing saved)
        if not self.close():  # (unsaved files: Cancel keeps the window and the settings)
            self._clearing_settings = False
            return
        self.settings.sync()  # (anything written while closing, then all of it removed)
        self.settings.clear()
        self.settings.sync()
        QApplication.quit()

    def closeEvent(self, event):
        clearing = getattr(self, "_clearing_settings", False)
        if not clearing:
            self._save_session()
            # (Before closing brings other panels to the front: e.g. stopping a review shows Build.)
            self._front_at_close = self.front_panels()
        # Every question first: answering Cancel or No leaves everything running.
        if not self.maybe_save_configuration():
            event.ignore()
            return
        for editor in self.editors():
            self.tabs.setCurrentWidget(editor)
            if not editor.maybe_save():
                event.ignore()
                return
        if self.busy():
            answer = QMessageBox.question(self, "fpm is running",
                                          "An fpm command is still running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self._stop_and_wait(2000)
        for editor in self.editors():
            editor.close_down()
        self.lsp_client.stop()
        self.search_panel.shutdown()
        self.terminal_panel.shutdown()
        self._shutdown_ai()
        self._stop_coverage()
        self._end_output(show_tail=False)
        self._full_output_path.unlink(missing_ok=True)  # (the full-output log: only for this run)
        if clearing:
            super().closeEvent(event)
            return
        s = self.settings
        s.setValue("geometry", self.saveGeometry())
        s.setValue("fpm", self.fpm_edit.text())
        self._store_window_options()  # (the project's options, or the app's settings)
        self._write_project_options()
        super().closeEvent(event)


def make_settings() -> QSettings:
    """The app's saved settings. VIBEFPMGUI_SETTINGS=/path/file.ini uses a
    separate file instead (e.g. for testing without touching your own)."""
    path = os.environ.get("VIBEFPMGUI_SETTINGS")
    if path:
        return QSettings(path, QSettings.IniFormat)
    return QSettings("VibeFPMGUI", "VibeFPMGUI")


_APP_NAME = "VibeFPMGUI"


def qt_argv(argv: list[str]) -> list[str]:
    """The command line for QApplication: the app's name in place of the
    script's ("…/vibefpmgui/__main__.py" when started with python -m)."""
    return [_APP_NAME, *argv[1:]]


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv if argv is None else argv
    set_windows_app_id()  # Windows: our own taskbar icon, not python.exe's
    set_macos_bundle_name(_APP_NAME)  # macOS: our name in the menu bar, not "Python" (before Qt starts)
    # (argv[0] is the name Qt uses for macOS's About/Hide/Quit items: "__main__.py" otherwise.)
    app = QApplication.instance() or QApplication(qt_argv(argv))
    set_macos_display_name(_APP_NAME)  # and in the Dock, not "python3.14"
    app.setApplicationName(_APP_NAME)
    app.setWindowIcon(app_icon())  # also the Dock icon on macOS
    app.setApplicationVersion(__version__)
    path = next((a for a in argv[1:] if not a.startswith("-")), None)
    if path and Path(path).is_dir():
        path = str(Path(path) / "fpm.toml")
    # --new-window: opened from another window, so not the project to reopen next time
    window = MainWindow(path, remember_last="--new-window" not in argv[1:])
    window.show()
    return app.exec()
