"""The project: opening an fpm.toml, recent projects, the open files' session.

Also the File menu, the Project files panel and reloading the dependency graph.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import tomllib
from pathlib import Path

from qtpy.QtCore import QProcess, QProcessEnvironment, QTimer
from qtpy.QtGui import QAction, QKeySequence
from qtpy.QtWidgets import QFileDialog, QMessageBox, QVBoxLayout, QWidget

from .. import manifest as m
from ..editor import EditorTab
from ..project_tree import ProjectTree


def _signature(graph: m.DependencyGraph):
    return (sorted((p.name, p.kind, p.version) for p in graph.packages.values()),
            sorted((e.parent, e.child, e.dev) for e in graph.edges))


class ProjectMixin:
    """The open project and its session (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_file_menu(self):
        """File menu."""
        open_action = QAction("Open fpm.toml…", self)
        open_action.setShortcut(QKeySequence.Open)
        open_action.triggered.connect(self.browse)
        reload_action = QAction("Reload", self)
        reload_action.setShortcut(QKeySequence("Ctrl+Shift+R"))  # Ctrl/⌘+R is Run
        reload_action.triggered.connect(self.reload)
        open_new_action = QAction("Open fpm.toml in New Window…", self)
        open_new_action.setShortcut(QKeySequence("Ctrl+Shift+N"))  # (⌘⇧O: Open File in Editor)
        open_new_action.setStatusTip("Open another project in its own window, keeping this one open")
        open_new_action.triggered.connect(lambda: self.browse(new_window=True))
        file_menu = self.menuBar().addMenu("&File")
        file_menu.addAction(open_action)
        file_menu.addAction(open_new_action)
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
        clear_settings_action = QAction("Clear Settings and Quit…", self)
        clear_settings_action.setMenuRole(QAction.NoRole)  # (macOS: not moved to the app menu as Quit)
        clear_settings_action.setStatusTip("Forget everything the app has saved (recent projects, layout, "
                                           "options, dictionary…) and quit: it starts as if for the first time")
        clear_settings_action.triggered.connect(self.clear_settings_and_quit)
        file_menu.addAction(clear_settings_action)
        quit_action = QAction("Quit", self)
        quit_action.setShortcut(QKeySequence.Quit)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

    def _build_project_files(self):
        """The Project files panel."""
        self.project_tree = ProjectTree()
        self.project_tree.open_requested.connect(lambda path: self.open_file(path))
        self.project_tree.preview_requested.connect(self.preview_file)
        self._preview: EditorTab | None = None  # the preview tab (one click in Project files), if any
        self.project_tree.compile_command_requested.connect(self.show_compile_command)
        self.project_tree.rename_requested.connect(self.rename_file)
        self.project_tree.delete_requested.connect(self.delete_file)
        files_box = QWidget()  # (in the "Project files" panel)
        files_layout = QVBoxLayout(files_box)
        files_layout.setContentsMargins(4, 4, 4, 4)
        files_layout.addWidget(self.project_tree)
        return files_box

    def browse(self, new_window: bool = False):
        start = str(self.manifest_path.parent) if self.manifest_path else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(
            self, "Open fpm manifest in a new window" if new_window else "Open fpm manifest", start,
            "fpm manifest (fpm.toml);;TOML files (*.toml);;All files (*)")
        if filename:
            if new_window:
                self.open_in_new_window(filename)
            else:
                self.open_manifest(filename)

    def open_in_new_window(self, manifest: str | Path):
        """Open a project in another VibeFPMGUI window (a separate process, so
        each window has its own fpm runs, language server and so on), keeping
        this one as it is."""
        manifest = Path(manifest).resolve()
        process = QProcess()
        process.setProgram(sys.executable)
        process.setArguments(["-m", "vibefpmgui", "--new-window", str(manifest)])
        process.setWorkingDirectory(str(manifest.parent))
        # Make sure the new process finds this package even when it isn't
        # installed (run from a checkout, e.g. "pixi run start").
        env = QProcessEnvironment.systemEnvironment()
        package_root = str(Path(__file__).resolve().parents[2])  # (the folder vibefpmgui is in)
        env.insert("PYTHONPATH", os.pathsep.join(p for p in (package_root, env.value("PYTHONPATH")) if p))
        process.setProcessEnvironment(env)
        ok = process.startDetached()
        ok = ok[0] if isinstance(ok, tuple) else ok  # (PySide returns (ok, pid))
        if ok:
            self.statusBar().showMessage(f"Opening {manifest.parent.name} in a new window…", 5000)
        else:
            QMessageBox.warning(self, "Open in New Window",
                                f"Could not start a new VibeFPMGUI window for {manifest}.")

    def open_manifest(self, filename: str | Path):
        path = Path(filename).resolve()
        new_project = path != self.manifest_path
        if new_project:
            # Both questions before anything changes: No or Cancel keeps the
            # current project as it is, its files still open.
            if self.busy():
                answer = QMessageBox.question(
                    self, "fpm is running",
                    "An fpm command is still running for the current project. Stop it and open the new one?")
                if answer != QMessageBox.Yes:
                    return
            self._save_session()  # the old project's open files, for next time
            # The open files belong to the old project: ask about unsaved changes.
            if not self.close_all_editors():
                return
            if self.busy():
                self._stop_and_wait(3000)
            self._clear_run_panels()
            self.search_panel.reset()
            self.references_panel.reset()
        self.manifest_path = path
        if self.remember_last:
            self.settings.setValue("last_manifest", str(self.manifest_path))
        self._remember_manifest(self.manifest_path)
        self.settings.sync()  # write now, so it survives the app being killed
        self.setWindowFilePath(str(self.manifest_path))  # macOS title-bar proxy icon
        self.setWindowTitle(f"VibeFPMGUI — {self.manifest_path.parent.name}")
        self.reload()
        if new_project:
            self._restore_session()

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

    # Per project: the files open in the editor (with cursor and scroll
    # positions), the tab in front, and the Project files tree's open
    # folders, restored when the project is opened again.
    def _project_key(self, prefix: str) -> str:
        """A settings key for this project's ``prefix`` (its session, build
        configurations, features, test durations): "prefix/" and a short hash
        of its fpm.toml's path. (A name, not security: SHA-1 is fine.)"""
        digest = hashlib.sha1(str(self.manifest_path).encode(), usedforsecurity=False).hexdigest()[:16]
        return f"{prefix}/{digest}"

    def _session_key(self) -> str:
        return self._project_key("session")

    def _save_session(self):
        if self.manifest_path is None or self._session_busy:
            return
        files = []
        for editor in self.editors():
            cursor = editor.editor.textCursor()
            files.append({"path": str(editor.path), "line": cursor.blockNumber() + 1,
                          "column": cursor.positionInBlock(),
                          "scroll": editor.editor.verticalScrollBar().value()})
        current = self.current_editor()
        state = {"files": files, "current": str(current.path) if current else None}
        tree = self.project_tree.view_state()
        if tree is None:  # filtered: keep what was saved before
            try:
                tree = json.loads(self.settings.value(self._session_key(), "{}")).get("tree")
            except (TypeError, ValueError):
                tree = None
        state["tree"] = tree
        self.settings.setValue(self._session_key(), json.dumps(state))

    def _restore_session(self):
        try:
            state = json.loads(self.settings.value(self._session_key(), "") or "{}")
        except (TypeError, ValueError):
            return
        if state.get("tree"):
            self.project_tree.restore_view_state(state["tree"])
        self._session_busy = True
        try:
            for f in state.get("files") or []:
                path = Path(f.get("path", ""))
                if not path.is_file():
                    continue  # deleted or moved since
                self.open_file(str(path), force_editor=True)
                editor = self._open_editor_for(path)
                if editor is None:
                    continue
                editor.editor.go_to_position(int(f.get("line", 1)), int(f.get("column", 0)))
                scroll = int(f.get("scroll", 0))
                # (after the tab's first layout, or the scroll position is clamped)
                QTimer.singleShot(0, editor, lambda e=editor, v=scroll: e.editor.verticalScrollBar().setValue(v))
        finally:
            self._session_busy = False
        current = self._open_editor_for(Path(state["current"])) if state.get("current") else None
        if current is not None:
            self.tabs.setCurrentWidget(current)

    def _reveal_current_file(self, *_):
        editor = self.current_editor()
        if editor is not None:
            self.project_tree.reveal(str(editor.path))

    def _update_tree_marks(self):
        """Color files in the tree that had errors/warnings in the last build."""
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
        self._stop_coverage()  # (another project's: its result would land here)
        self._apply_coverage({})
        self.coverage_panel.clear()
        self.stop_review()
        self._ai_editor = None
        self._forget()
        self.review_monitor.reset()
        self.show_panel(self.build_monitor)

    def reload(self):
        if not self.manifest_path:
            return
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
        self._update_legend()  # (it mentions version conflicts when there are any)
        self.show_package(self.graph.root)
        self.project_tree.set_project(self.graph, self.manifest_path.parent)
        self.terminal_panel.set_directory(self.manifest_path.parent)
        self._update_language_server()
        self.refresh_build_outputs()
        self.refresh_git()
        self._set_project_enabled(True)
        self._load_features_and_profiles()
        missing = sum(1 for p in self.graph.packages.values() if p.kind == m.MISSING)
        msg = f"{len(self.graph.packages) - 1} dependencies"
        if missing:
            msg += f" ({missing} not downloaded — run Update deps or Build)"
        conflicts = m.conflicts(self.graph)
        if conflicts:
            msg += (f" · ⚠ {len(conflicts)} version conflict{'s' * (len(conflicts) != 1)}: "
                    + ", ".join(sorted(conflicts)) + " (see the graph)")
        self.statusBar().showMessage(msg)
