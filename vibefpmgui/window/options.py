"""The project's configuration file (`vibefpmgui.build_configs.ConfigFile`):
which it is, and the GUI's options and New configuration's Build options in
it (see `vibefpmgui.project_options`): used while the project is open,
written back (when the window closes or another project is opened, if they
changed), and applied to the window when another project is opened.

A project whose file holds options (it has one that can be read) uses them;
without, the app's settings as before (nothing is written in a project only
looked at). A file made while the project is open (its first configuration
saved) holds its options from then on.

New configuration's Build options are one `Configuration`: the file's
"new_configuration", or, without options, the app's settings ("profile",
"compiler"… and the project's "features/<hash>").
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .. import build_configs as bc
from .. import project_options as po
from .. import spelling
from ..code_editor import CodeEditor
from ..graph_view import EDGE_STYLES, LAYOUTS, THEME_CHOICES

NEW_CONFIGURATION_KEY = "new_configuration"  # (for _apply_options: New configuration's Build options)


def project_key(manifest: Path, prefix: str) -> str:
    """The app's settings key for a project's ``prefix`` (its session, build
    configurations, features, test durations): "prefix/" and a short hash
    of its fpm.toml's path. (A name, not security: SHA-1 is fine.)"""
    digest = hashlib.sha1(str(manifest).encode(), usedforsecurity=False).hexdigest()[:16]
    return f"{prefix}/{digest}"


class ProjectOptionsMixin:
    """The project's configuration file and its options (a part of
    `vibefpmgui.main_window.MainWindow`)."""

    config_file: bc.ConfigFile | None = None  # the open project's (it may not exist yet)
    _config_manifest: Path | None = None  # the project config_file is for

    @property
    def config_path(self) -> Path | None:
        return self.config_file.path if self.config_file is not None else None

    def _project_key(self, prefix: str) -> str:
        """The app's settings key for this project's ``prefix`` (see `project_key`)."""
        return project_key(self.manifest_path, prefix)

    def options_in_use(self) -> bool:
        """The project's options are its file's (not the app's settings)."""
        return self.settings.options is not None

    # ------------------------------------------------------------ the file
    def _config_file_for(self, manifest: Path) -> bc.ConfigFile:
        """A project's configuration file, read: the one opened for it last
        (File → Open Build Configuration…), or the one beside its fpm.toml."""
        remembered = self.settings.app.value(project_key(manifest, "build_config_file"), "") or ""
        path = Path(remembered) if remembered and Path(remembered).is_file() else bc.default_path(manifest)
        return bc.ConfigFile(path).read()

    def _use_config_file(self, file: bc.ConfigFile | None, manifest: Path | None, options: dict | None = None):
        """Use ``file`` (the project ``manifest``'s) and its options (or
        ``options``) from now on."""
        self.config_file, self._config_manifest = file, manifest
        keys = {name: project_key(manifest, name) for name in po.PER_PROJECT} if manifest is not None else {}
        if options is None and file is not None:
            options = file.options
        self.settings.use(options, manifest.parent if options is not None else None, keys)

    def _config_file_saved(self):
        """A configuration was saved: a file just made holds the project's options from now on."""
        file = self.config_file
        if file is not None and file.options is None:
            file.start_options()
            self._use_config_file(file, self._config_manifest)

    def _switch_config_file(self, manifest: Path, force: bool = False):
        """Before another project is opened (or, ``force``, another
        configuration file of this one): this one's options written, the
        other's used and applied. (Neither with options: nothing to do but
        keep New configuration's, which are this project's.)"""
        if manifest == self._config_manifest and not force:
            return
        file = self._config_file_for(manifest)
        involved = self.options_in_use() or file.options is not None
        if involved:
            self._store_window_options()
            self._write_config_file()
        elif self.manifest_path is not None:
            self._keep_free_options(self.free_options())
        self._use_config_file(file, manifest)
        if involved:
            self._apply_options()

    def _write_config_file(self):
        """Write the project's options to its file, if they changed."""
        file = self.config_file
        if file is None or not self.options_in_use():
            return
        try:
            file.write_options(self.settings.complete_options(), self._saved_free_options())
        except (OSError, bc.ConfigError) as exc:
            self.statusBar().showMessage(f"Couldn't save the options in {file.path.name}: {exc}", 8000)

    def reload_config_file(self):
        """The configuration file was saved in the editor: its configurations
        read again, and the options changed there (and New configuration, if
        it was) used and applied; the window's other options, changed since
        it was read, stay (and the configuration shown)."""
        file = self.config_file
        if file is None or self.manifest_path is None:
            return
        changed, new_changed = file.reread()
        if not (changed or new_changed):
            return
        if not self.options_in_use():  # (it couldn't be read when the project was opened: fixed now)
            self._use_config_file(file, self._config_manifest)
            self._apply_options()
            return
        options = dict(self.settings.options)
        for key in changed:
            if key in file.options:
                options[key] = file.options[key]
            else:
                options.pop(key, None)
        self._use_config_file(file, self._config_manifest, options)
        self._apply_options(changed | ({NEW_CONFIGURATION_KEY} if new_changed else set()))

    # ------------------------------------------------------------ New configuration
    def _saved_free_options(self) -> bc.Configuration:
        """New configuration's Build options as kept (not as shown: see
        free_options): the file's, or the app's settings."""
        file, manifest = self.config_file, self._config_manifest  # (the project the settings are for)
        if self.options_in_use() and file is not None and file.new_configuration is not None:
            return file.new_configuration
        s = self.settings.app
        features = (s.value(project_key(manifest, "features"), [], type=list) or []) if manifest else []
        return bc.Configuration(
            "", profile=s.value("profile", bc.DEFAULT_PROFILE) or bc.DEFAULT_PROFILE,
            features=[str(f) for f in features], compiler=s.value("compiler", "") or "",
            flags=s.value("flags", "") or "", target=s.value("target", "") or "",
            extra_args=s.value("extra", "") or "", program_args=s.value("program_args", "") or "",
            parallel_tests=max(1, s.value("test_jobs", 1, type=int)), coverage=s.value("coverage", False, type=bool),
            verbose=s.value("verbose", False, type=bool))

    def _keep_free_options(self, config: bc.Configuration):
        """Keep New configuration's Build options: in the file (written with
        the options), or the app's settings."""
        file = self.config_file
        if self.options_in_use() and file is not None:
            file.new_configuration = bc.Configuration.from_json(config.to_json(named=False), name="")
            return
        s = self.settings.app
        for key, value in (("profile", config.profile), ("compiler", config.compiler), ("flags", config.flags),
                           ("extra", config.extra_args), ("target", config.target),
                           ("program_args", config.program_args), ("verbose", config.verbose),
                           ("coverage", config.coverage), ("test_jobs", config.parallel_tests)):
            s.setValue(key, value)
        if self._config_manifest is not None:
            s.setValue(project_key(self._config_manifest, "features"), list(config.features))

    def _apply_build_options(self):
        """Build options' fields: New configuration's, as kept (a saved
        configuration shown stays: they're shown when it's left)."""
        free = self._saved_free_options()
        self._wanted_profile = free.profile
        if self.config_shown():
            self._free_options = free
        else:
            self._show_options(free)

    # ------------------------------------------------------------ the window
    def _toggles(self) -> list[tuple]:
        """(action, settings key, default) of the View menu's on/off choices."""
        return [(self.use_editor_action, "use_editor", True), (self.lint_action, "lint", True),
                (self.lsp_diagnostics_action, "lsp_diagnostics", True), (self.spelling_action, "spelling", True),
                (self.gremlins_action, "gremlins", True), (self.minimap_action, "minimap", True),
                (self.coverage_action, "show_coverage", True), (self.matches_action, "highlight_matches", True),
                (self.word_wrap_action, "word_wrap", False), (self.fortls_action, "fortls", True),
                (self.trim_on_save_action, "trim_on_save", True)]

    def _store_window_options(self, front: list[str] | None = None):
        """Every option as the window has it now, in the settings (the
        project's options, or the app's), and New configuration's Build
        options: those changed are there already, not those never changed (a
        file's options have them all). ``front``: the panels in front of
        their stacks (as they were before closing began: see closeEvent).
        (Collapsed panels are opened first: a layout is saved with them open.)"""
        s = self.settings
        for action, key, _default in self._toggles():
            s.setValue(key, action.isChecked())
        s.setValue("spelling_language", spelling.language)
        s.setValue("indent_width", CodeEditor.indent_width)
        s.setValue("indent_tabs", CodeEditor.use_tabs)
        s.setValue("tab_rows_wrap", self.tab_rows_actions[True].isChecked())
        for key, actions in self.graph_style_actions.items():
            chosen = next((value for value, action in actions.items() if action.isChecked()), None)
            if chosen is not None:
                s.setValue(f"graph_{key}", chosen)
        s.setValue("graph_dependencies_dev", self.dependencies_dev_action.isChecked())
        s.setValue("complexity_tests", self.complexity_panel.tests_check.isChecked())
        s.setValue("complexity_dependencies", self.complexity_panel.dependencies_check.isChecked())
        s.setValue("coverage_tests", self.coverage_panel.tests_check.isChecked())
        for source, button in self.problems_panel.buttons.items():
            s.setValue(f"problems/{source}", button.isChecked())
        self.search_panel._save_options()
        front = self.front_panels() if front is None else front
        self.snap_closed.expand_all()
        s.setValue("panels", self.saveState(self.LAYOUT_VERSION))
        s.setValue("panels_front", front)
        self._keep_free_options(self.free_options())

    def _apply_options(self, only: set[str] | None = None):
        """Options, from the settings, to the window: every one (another
        project's: the window was made with the first's), or ``only`` those
        (changed in the file, in the editor; NEW_CONFIGURATION_KEY: New
        configuration's Build options)."""
        s = self.settings

        def wanted(*keys: str) -> bool:
            return only is None or any(key in only for key in keys)

        if wanted(NEW_CONFIGURATION_KEY):
            self._apply_build_options()
        # View
        for action, key, default in self._toggles():
            if wanted(key):
                action.setChecked(s.value(key, default, type=bool))  # (its handler applies it)
        if wanted("spelling_language"):
            language = str(s.value("spelling_language", "en") or "en")
            if language != spelling.language:
                self.set_spelling_language(language)
        if wanted("indent_width", "indent_tabs"):
            width = s.value("indent_width", 4, type=int)
            use_tabs = s.value("indent_tabs", False, type=bool)
            if (width, use_tabs) != (CodeEditor.indent_width, CodeEditor.use_tabs):
                self.set_indentation(width, use_tabs)
        if wanted("tab_rows_wrap"):
            self.set_tab_rows(s.value("tab_rows_wrap", False, type=bool), save=False)
        # Graph
        if wanted("graph_layout"):
            self._set_graph_layout(s.value("graph_layout", next(iter(LAYOUTS))))
        if wanted("graph_theme"):
            self._set_graph_theme(s.value("graph_theme", next(iter(THEME_CHOICES))))
        if wanted("graph_edges"):
            self._set_graph_edges(s.value("graph_edges", next(iter(EDGE_STYLES))))
        if wanted("graph_dependencies_dev"):
            dev = s.value("graph_dependencies_dev", False, type=bool)
            if only is None:  # (another project: it's loaded next anyway)
                self.dependencies_dev_action.blockSignals(True)
            self.dependencies_dev_action.setChecked(dev)
            self.dependencies_dev_action.blockSignals(False)
        # Panels' own choices
        if wanted("complexity_tests"):
            self.complexity_panel.tests_check.setChecked(s.value("complexity_tests", False, type=bool))
        if wanted("complexity_dependencies"):
            self.complexity_panel.dependencies_check.setChecked(s.value("complexity_dependencies", False, type=bool))
        if wanted("coverage_tests"):
            self.coverage_panel.tests_check.setChecked(s.value("coverage_tests", False, type=bool))
        for source, button in self.problems_panel.buttons.items():
            if wanted(f"problems/{source}"):
                button.setChecked(s.value(f"problems/{source}", True, type=bool))
        if any(wanted(key) for key in po.OPTIONS if key.startswith("search/")):
            self.search_panel._restore_options()
        if wanted("panels", "panels_front"):
            layout = s.value("panels")
            if layout:
                self.restore_layout(layout, s.value("panels_front", [], type=list) or [])
        if only is not None and "build_configuration" in only and not self._editing:
            # (Changed in the file. Another project's: chosen when its configurations are listed.)
            self.select_configuration(s.value("build_configuration", "") or "")
