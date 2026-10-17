"""The project's GUI options in its configuration file (see
`vibefpmgui.project_options`): which file's are in use, writing them back
(when the window closes or another project is opened, if they changed), and
applying another project's to the window.

A project uses the "options" of its configuration file (the one
`vibefpmgui.window.configs` uses) if it has one; without, the app's settings
as before (nothing is written in a project only looked at). A configuration
file made while the project is open starts holding its options from then on.
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
    """The project's options (a part of `vibefpmgui.main_window.MainWindow`)."""

    _options_project: Path | None = None  # the project whose options are in use (if any)
    _options_file: Path | None = None  # the file they're from, and written to
    _options_written: dict | None = None  # what the file has (written only if they differ)

    def _project_key(self, prefix: str) -> str:
        """The settings key for this project's ``prefix``: plain while its
        options are in use and it's one of them (the file is the project's
        own), else the app's per-project key (see `project_key`)."""
        if self.settings.options is not None and prefix in po.PER_PROJECT:
            return prefix
        return project_key(self.manifest_path, prefix)

    def _options_file_for(self, manifest: Path) -> Path | None:
        """The configuration file whose options a project uses (None: it has none)."""
        remembered = self.settings.app.value(project_key(manifest, "build_config_file"), "") or ""
        path = Path(remembered) if remembered and Path(remembered).is_file() else bc.default_path(manifest)
        return path if path.is_file() else None

    def _use_project_options(self, manifest: Path | None):
        """Read and use a project's options (none: the app's settings)."""
        path = self._options_file_for(manifest) if manifest is not None else None
        options = None
        if path is not None:
            try:
                options = bc.load_options(path)
            except bc.ConfigError as exc:  # (its options aren't used, nor written over)
                self._options_problem = str(exc)
        self.settings.use(options, manifest.parent if options is not None else None)
        self._options_project = manifest
        self._options_file = path if options is not None else None
        self._options_written = dict(options) if options is not None else None

    def _options_file_made(self):
        """The project's configuration file was just made: its options go there from now on."""
        if self._options_file is None and self.manifest_path is not None:
            self._use_project_options(self.manifest_path)

    def _toggles(self) -> list[tuple]:
        """(action, settings key, default) of the View menu's on/off choices."""
        return [(self.use_editor_action, "use_editor", True), (self.lint_action, "lint", True),
                (self.lsp_diagnostics_action, "lsp_diagnostics", True), (self.spelling_action, "spelling", True),
                (self.gremlins_action, "gremlins", True), (self.minimap_action, "minimap", True),
                (self.coverage_action, "show_coverage", True), (self.matches_action, "highlight_matches", True),
                (self.word_wrap_action, "word_wrap", False), (self.fortls_action, "fortls", True),
                (self.trim_on_save_action, "trim_on_save", True)]

    def _store_window_options(self):
        """Every option as the window has it now, in the settings (the
        project's options, or the app's): those changed are there already,
        but not those never changed (a file's options have them all)."""
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
        self.snap_closed.expand_all()  # (collapsed panels are saved open: nothing comes back hidden)
        s.setValue("panels", self.saveState(self.LAYOUT_VERSION))
        front, self._front_at_close = getattr(self, "_front_at_close", None), None
        s.setValue("panels_front", self.front_panels() if front is None else front)
        free = self.free_options()  # (New configuration's, not a saved one's shown)
        s.setValue("profile", free.profile)
        s.setValue("compiler", free.compiler)
        s.setValue("flags", free.flags)
        s.setValue("extra", free.extra_args)
        s.setValue("verbose", free.verbose)
        s.setValue("coverage", free.coverage)
        s.setValue("test_jobs", free.parallel_tests)

    def _write_project_options(self):
        """Write the project's options to its file, if they changed."""
        if self._options_file is None or self.settings.options is None or self._options_project is None:
            return
        app_keys = {name: project_key(self._options_project, name) for name in po.PER_PROJECT}
        options = self.settings.complete_options(app_keys)
        if options == self._options_written:
            return
        try:
            bc.save_options(self._options_file, options)
            self._options_written = options
        except (OSError, bc.ConfigError) as exc:
            self.statusBar().showMessage(f"Couldn't save the options in {self._options_file.name}: {exc}", 8000)

    def _switch_project_options(self, manifest: Path, force: bool = False):
        """Before another project is opened (or, ``force``, another
        configuration file of this one): this one's options written, the
        other's read and applied (if either has any)."""
        if manifest == self._options_project and not force:
            return
        self._store_window_options()
        self._write_project_options()
        had = self.settings.options is not None
        self._use_project_options(manifest)
        if had or self.settings.options is not None:
            self._apply_options()

    def reload_project_options(self):
        """The configuration file was saved in the editor: its options, if
        they were changed there, are used (and applied) from now on."""
        if self._options_file is None or self.manifest_path is None:
            return
        try:
            options = bc.load_options(self._options_file)
        except bc.ConfigError:
            return  # (the list says why: see reload_configurations)
        if options is None or options == self._options_written:
            return
        self.settings.use(options, self.manifest_path.parent)
        self._options_written = dict(options)
        self._apply_options()

    # ------------------------------------------------------------ applying
    def _apply_build_options(self):
        """Build options' fields from the settings (New configuration's)."""
        s = self.settings
        self._wanted_profile = s.value("profile", "(default)") or "(default)"
        self.profile_combo.setCurrentText(self._wanted_profile)
        self.compiler_edit.setText(s.value("compiler", "") or "")
        self.flag_edit.setText(s.value("flags", "") or "")
        self.extra_edit.setText(s.value("extra", "") or "")
        self.verbose_check.setChecked(s.value("verbose", False, type=bool))
        self.coverage_check.setChecked(s.value("coverage", False, type=bool))
        self.test_jobs_spin.setValue(s.value("test_jobs", 1, type=int))

    def _apply_options(self):
        """Every option, from the settings, to the window (another project's:
        the window was made with the first's)."""
        s = self.settings
        # Build options: New configuration's (the project's configuration is chosen again when it loads).
        self.config_combo.blockSignals(True)
        self.config_combo.setCurrentIndex(0)
        self.config_combo.blockSignals(False)
        self._free_options, self._editing, self._config_project = None, "", None
        self._set_options_locked(False)
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
