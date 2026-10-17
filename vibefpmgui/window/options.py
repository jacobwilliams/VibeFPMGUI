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
from .. import editor as editor_mod
from .. import highlighter as hl
from .. import project_options as po
from .. import spelling, themes
from ..code_editor import CodeEditor
from ..graph_view import EDGE_STYLES, LAYOUTS, THEME_CHOICES


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

    def _use_config_file(self, file: bc.ConfigFile | None, manifest: Path | None):
        """Use ``file`` (the project ``manifest``'s) and its options from now on."""
        self.config_file, self._config_manifest = file, manifest
        keys = {name: project_key(manifest, name) for name in po.PER_PROJECT} if manifest is not None else {}
        options = file.options if file is not None else None
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
        read again, and its options and New configuration, if they were
        changed there, used and applied (the configuration shown stays)."""
        file = self.config_file
        if file is None or self.manifest_path is None:
            return
        if file.reread():
            self._use_config_file(file, self._config_manifest)
            self._apply_options()

    # ------------------------------------------------------------ New configuration
    def _saved_free_options(self) -> bc.Configuration:
        """New configuration's Build options as kept (not as shown: see
        free_options): the file's, or the app's settings."""
        file = self.config_file
        if self.options_in_use() and file is not None and file.new_configuration is not None:
            return file.new_configuration
        s = self.settings.app
        features = (s.value(self._project_key("features"), [], type=list) or []) if self.manifest_path else []
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
        if self.manifest_path is not None:
            s.setValue(self._project_key("features"), list(config.features))

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
        s.setValue("app_style", themes.chosen_style)
        s.setValue("app_theme", themes.current)
        s.setValue("syntax_style", hl.chosen_style)
        s.setValue("editor_font_family", editor_mod.editor_font_family)
        s.setValue("editor_font_size", editor_mod.editor_font_size)
        s.setValue("font_delta", self.font_delta)
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

    def _apply_options(self):
        """Every option, from the settings, to the window (another project's:
        the window was made with the first's; or the file's, changed in the
        editor)."""
        s = self.settings
        self._apply_build_options()
        # View
        style = str(s.value("app_style", themes.AUTOMATIC) or themes.AUTOMATIC)
        if style != themes.chosen_style:
            self.set_window_style(style)
        theme = str(s.value("app_theme", themes.SYSTEM) or themes.SYSTEM)
        if theme != themes.current:
            self.set_theme(theme)
        syntax = s.value("syntax_style", hl.AUTO) or hl.AUTO
        if syntax != hl.chosen_style:
            self.set_syntax_style(syntax)
        family = s.value("editor_font_family", "") or ""
        size = s.value("editor_font_size", 0.0, type=float) or 0.0
        if (family, size) != (editor_mod.editor_font_family, editor_mod.editor_font_size):
            self.set_editor_font(family, size)
        delta = s.value("font_delta", 0, type=int)
        if delta != self.font_delta:
            self.set_font_delta(delta, announce=False)
        for action, key, default in self._toggles():
            action.setChecked(s.value(key, default, type=bool))  # (its handler applies it)
        language = str(s.value("spelling_language", "en") or "en")
        if language != spelling.language:
            self.set_spelling_language(language)
        width = s.value("indent_width", 4, type=int)
        use_tabs = s.value("indent_tabs", False, type=bool)
        if (width, use_tabs) != (CodeEditor.indent_width, CodeEditor.use_tabs):
            self.set_indentation(width, use_tabs)
        self.set_tab_rows(s.value("tab_rows_wrap", False, type=bool), save=False)
        # Graph
        self._set_graph_layout(s.value("graph_layout", next(iter(LAYOUTS))))
        self._set_graph_theme(s.value("graph_theme", next(iter(THEME_CHOICES))))
        self._set_graph_edges(s.value("graph_edges", next(iter(EDGE_STYLES))))
        self.dependencies_dev_action.blockSignals(True)  # (the project is loaded next anyway)
        self.dependencies_dev_action.setChecked(s.value("graph_dependencies_dev", False, type=bool))
        self.dependencies_dev_action.blockSignals(False)
        # Panels' own choices
        self.complexity_panel.tests_check.setChecked(s.value("complexity_tests", False, type=bool))
        self.complexity_panel.dependencies_check.setChecked(s.value("complexity_dependencies", False, type=bool))
        self.coverage_panel.tests_check.setChecked(s.value("coverage_tests", False, type=bool))
        for source, button in self.problems_panel.buttons.items():
            button.setChecked(s.value(f"problems/{source}", True, type=bool))
        self.search_panel._restore_options()
        layout = s.value("panels")
        if layout:
            self.restore_layout(layout, s.value("panels_front", [], type=list) or [])
