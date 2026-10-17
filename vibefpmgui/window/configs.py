"""Build configurations (see `vibefpmgui.build_configs`): the Configuration
list at the top of Build options, and File → Open / Recent / Save Build
Configuration.

"New configuration" is the Build options as before: editable, kept in the
app's settings. Choosing a saved configuration shows its options, read-only
(the fpm executable stays editable: it isn't part of one); choosing New
configuration again brings back what was there. Save… adds what's shown to
the project's configuration file, as a configuration chosen from then on.
Edit makes a saved configuration's options editable, and Save puts them back
in the file under its name; leaving it (choosing another, opening another
project or file, quitting) with changes not saved asks first.
"""

from __future__ import annotations

import os
from pathlib import Path

from qtpy.QtWidgets import (
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QToolButton,
)

from .. import build_configs as bc

NEW_CONFIGURATION = "New configuration"
MAX_RECENT_CONFIGS = 10


class ConfigsMixin:
    """Build configurations (a part of `vibefpmgui.main_window.MainWindow`)."""

    config_path: Path | None = None  # the project's configuration file (it may not exist yet)
    configurations: list[bc.Configuration]  # (the file's: see _build_config_row)
    _config_error = ""  # why the file couldn't be read ("" if it could)
    _free_options: bc.Configuration | None = None  # New configuration's options, while a saved one is shown
    _config_project: Path | None = None  # the project config_path is for
    _editing = ""  # the saved configuration being edited (its options editable), if any

    def _build_config_row(self, form):
        """The Configuration row (the first of Build options) and the
        read-only note under it."""
        self.configurations = []
        self.config_combo = QComboBox()
        self.config_combo.addItem(NEW_CONFIGURATION)
        # (As narrow as the panel gets: not as wide as the longest name.)
        self.config_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.config_combo.setMinimumContentsLength(6)
        self.config_combo.currentIndexChanged.connect(self._config_chosen)
        self.config_edit_button = QToolButton()
        self.config_edit_button.setText("Edit")
        self.config_edit_button.clicked.connect(self._edit_clicked)
        self.config_save_button = QToolButton()
        self.config_save_button.setText("Save…")
        self.config_save_button.clicked.connect(self.save_configuration)
        more = QToolButton()
        more.setText("⋯")
        more.setToolTip("More: save as, edit a copy, delete, open a configuration file")
        menu = QMenu(more)
        self.config_save_as_action = menu.addAction("Save As…", self.save_configuration_as)
        self.config_save_as_action.setToolTip("Save the options shown as a configuration under another name")
        self.config_edit_action = menu.addAction("Edit as New Configuration", self.edit_configuration_as_new)
        self.config_edit_action.setToolTip("Copy this configuration's options to New configuration, to change "
                                           "them (and Save…, under its name or another)")
        self.config_delete_action = menu.addAction("Delete Configuration…", self.delete_configuration)
        menu.addSeparator()
        menu.addAction("Open Configuration File…", self.browse_configuration)
        self.config_show_action = menu.addAction("Show Configuration File in Editor", self._show_config_file)
        menu.setToolTipsVisible(True)
        # (Opened by hand: a button's own menu adds an arrow that squeezed the "⋯".)
        more.clicked.connect(lambda: menu.popup(more.mapToGlobal(more.rect().bottomLeft())))
        self._config_more_menu = menu
        row = QHBoxLayout()
        row.addWidget(self.config_combo, 1)
        row.addWidget(self.config_edit_button)
        row.addWidget(self.config_save_button)
        row.addWidget(more)
        form.addRow(row)  # (the panel's whole width: with a label, it made the panel wider than its fields)
        self.config_note = QLabel()
        self.config_note.setWordWrap(True)
        self.config_note.hide()
        form.addRow(self.config_note)
        self._update_config_ui()

    # ------------------------------------------------------------ options
    def _option_fields(self) -> list:
        """The fields a configuration sets (not the fpm executable)."""
        return [self.profile_combo, self.compiler_edit, self.flag_edit, self.target_edit, self.extra_edit,
                self.runner_args_edit, self.test_jobs_spin, self.coverage_check, self.verbose_check]

    def _watch_option_fields(self):
        """(After the fields are made:) a change shows whether there are unsaved ones."""
        for widget in self._option_fields():
            for signal in ("textChanged", "valueChanged", "toggled", "currentTextChanged"):
                if hasattr(widget, signal):
                    getattr(widget, signal).connect(self._options_changed)
                    break

    def _options_changed(self, *_):
        if self._editing:
            self._update_config_ui()

    def config_shown(self) -> bool:
        """A saved configuration is shown (read-only, or being edited)."""
        return self.config_combo.currentIndex() > 0

    def options_locked(self) -> bool:
        """A saved configuration is shown, read-only (not being edited)."""
        return self.config_shown() and not self._editing

    def has_unsaved_configuration(self) -> bool:
        """The configuration being edited has changes not in the file."""
        return bool(self._editing) and self.current_options(self._editing) != self._configuration(self._editing)

    def current_options(self, name: str = "") -> bc.Configuration:
        """The Build options shown, as a configuration named ``name``."""
        return bc.Configuration(
            name, profile=self.profile_combo.currentText() or bc.DEFAULT_PROFILE,
            features=list(self.selected_features), compiler=self.compiler_edit.text().strip(),
            flags=self.flag_edit.text().strip(), target=self.target_edit.text().strip(),
            extra_args=self.extra_edit.text().strip(), program_args=self.runner_args_edit.text().strip(),
            parallel_tests=self.test_jobs_spin.value(), coverage=self.coverage_check.isChecked(),
            verbose=self.verbose_check.isChecked())

    def free_options(self) -> bc.Configuration:
        """New configuration's options (those to remember), even while a saved one is shown."""
        return self._free_options if self._free_options is not None else self.current_options()

    def _show_options(self, config: bc.Configuration, features: list[str] | None = None):
        """Fill the fields from ``config`` (``features``: instead of its own)."""
        combo = self.profile_combo
        if combo.findText(config.profile) < 0:  # (a profile fpm.toml no longer has: shown as asked for)
            combo.addItem(config.profile)
        combo.setCurrentText(config.profile)
        self.compiler_edit.setText(config.compiler)
        self.flag_edit.setText(config.flags)
        self.target_edit.setText(config.target)
        self.extra_edit.setText(config.extra_args)
        self.runner_args_edit.setText(config.program_args)
        self.test_jobs_spin.setValue(config.parallel_tests)
        self.coverage_check.setChecked(config.coverage)
        self.verbose_check.setChecked(config.verbose)
        self.selected_features = list(config.features if features is None else features)
        self._update_features_ui()

    def _set_options_locked(self, locked: bool):
        """Greyed out (read-only fields looked the same as the others)."""
        for widget in self._option_fields():
            if widget is not self.profile_combo:  # (its state: see _update_features_ui)
                widget.setEnabled(not locked)
        self._update_features_ui()

    # ------------------------------------------------------------ choosing
    def _config_chosen(self, index: int):
        if self._editing:
            editing = self._editing
            if not self.maybe_save_configuration():
                self.config_combo.blockSignals(True)  # (Cancel: still on it, its changes kept)
                self.config_combo.setCurrentIndex(max(0, self.config_combo.findText(editing)))
                self.config_combo.blockSignals(False)
                return
            self._editing = ""
        if index <= 0:
            if self._free_options is not None:
                free, self._free_options = self._free_options, None
                self._show_options(free, self._saved_features())
        else:
            config = self._configuration(self.config_combo.itemText(index))
            if config is None:
                return
            if self._free_options is None:
                self._free_options = self.current_options()
            self._show_options(config)
        self._set_options_locked(index > 0)
        if self.manifest_path is not None:
            self.settings.setValue(self._project_key("build_configuration"),
                                   self.config_combo.currentText() if index > 0 else "")
        self._update_config_ui()

    def _configuration(self, name: str) -> bc.Configuration | None:
        return next((c for c in self.configurations if c.name == name), None)

    def _saved_features(self) -> list[str]:
        """New configuration's features: those last chosen in the Features menu."""
        if self.manifest_path is None:
            return []
        saved = self.settings.value(self._features_key(), [], type=list) or []
        return [f for f in saved if f in self.project_features]

    def select_configuration(self, name: str):
        """Show the configuration named ``name`` ("": New configuration)."""
        index = self.config_combo.findText(name) if name else 0
        self.config_combo.setCurrentIndex(max(0, index))

    # ------------------------------------------------------------ the file
    def _load_configurations(self):
        """Read the project's configuration file (after the project was
        opened or reloaded): the list again, the one shown kept (or, for a
        project just opened, the one shown last time)."""
        if self.manifest_path is None:
            return
        new_project = self._config_project != self.manifest_path
        if self._editing and not new_project:  # (its changes kept: only the list is read again)
            self._read_configurations()
            self._refill_items(self._editing)
            return
        if new_project:
            self._editing = ""  # (asked about before the project changed: see maybe_save_configuration)
            self._config_project = self.manifest_path
            remembered = self.settings.value(self._project_key("build_config_file"), "")
            path = Path(remembered) if remembered and Path(remembered).is_file() else None
            self.config_path = path or bc.default_path(self.manifest_path)
            wanted = self.settings.value(self._project_key("build_configuration"), "") or ""
        else:
            wanted = self.config_combo.currentText() if self.options_locked() else ""
        self._read_configurations()
        self._fill_config_combo(wanted)

    def _read_configurations(self):
        try:
            self.configurations = bc.load(self.config_path) if self.config_path else []
            self._config_error = ""
        except bc.ConfigError as exc:
            self.configurations, self._config_error = [], str(exc)
            self.statusBar().showMessage(f"Build configurations: {exc}", 8000)

    def _fill_config_combo(self, wanted: str = ""):
        """The list again, showing ``wanted`` (New configuration if it's gone):
        back to New configuration first (its options restored), then to
        ``wanted`` (its options read again: the file may have changed)."""
        self.select_configuration("")
        self._refill_items()
        if wanted and self._configuration(wanted) is not None:
            self.select_configuration(wanted)
        self._update_config_ui()

    def _refill_items(self, shown: str = ""):
        """The list's names again, ``shown`` chosen (no options changed)."""
        combo = self.config_combo
        combo.blockSignals(True)
        while combo.count() > 1:
            combo.removeItem(1)
        names = [config.name for config in self.configurations]
        combo.addItems(names + ([shown] if shown and shown not in names else []))  # (being edited: kept)
        combo.setCurrentIndex(max(0, combo.findText(shown)) if shown else 0)
        combo.blockSignals(False)
        self._update_config_ui()

    def _update_config_ui(self):
        locked, editing = self.options_locked(), bool(self._editing)
        unsaved = self.has_unsaved_configuration()
        has_project = getattr(self, "manifest_path", None) is not None
        self.config_edit_button.setText("Cancel" if editing else "Edit")
        self.config_edit_button.setEnabled(locked or editing)
        self.config_edit_button.setToolTip(
            "Stop editing: the configuration's options as saved" if editing else
            "Change this configuration's options (then Save them under its name)" if locked else
            "Choose a saved configuration to edit it")
        self.config_save_button.setText("Save" if editing else "Save…")
        self.config_save_button.setEnabled(has_project and (unsaved if editing else not locked))
        self.config_save_button.setToolTip(
            f"Save the changes to “{self._editing}”" if editing else
            f"Save these options as a configuration in the project's {bc.FILE_NAME} (beside fpm.toml)")
        self.config_save_as_action.setEnabled(has_project)
        self.config_edit_action.setEnabled(self.config_shown())
        self.config_delete_action.setEnabled(self.config_shown())
        self.config_show_action.setEnabled(bool(self.config_path and self.config_path.is_file()))
        where = self.config_path.name if self.config_path else bc.FILE_NAME
        if self._config_error:
            tip = f"⚠ {self._config_error}"
        elif self.config_path and self.configurations:
            tip = f"The configurations in {self.config_path}"
        else:
            tip = f"No saved configurations yet: Save… adds the options shown to {where}, beside fpm.toml"
        self.config_combo.setToolTip(tip)
        if locked:
            self.config_note.setText(f"<span style='color:gray'>Read-only, from {where}.</span>")
            self.config_note.setToolTip("Edit to change its options (then Save), or choose "
                                        f"{NEW_CONFIGURATION}.")
        elif editing:
            self.config_note.setText(f"<span style='color:#e8590c'>Editing; changes not saved in {where}.</span>"
                                     if unsaved else f"<span style='color:gray'>Editing (from {where}).</span>")
            self.config_note.setToolTip("Save puts the changes in the file; Cancel goes back to it as saved.")
        self.config_note.setVisible(locked or editing)

    def _use_config_file(self, path: Path):
        self.config_path = path
        self.settings.setValue(self._project_key("build_config_file"),
                               "" if path == bc.default_path(self.manifest_path) else str(path))
        self._remember_config(path)

    # ------------------------------------------------------------ commands
    def save_configuration(self) -> bool:
        """Build options → Save (File → Save Build Configuration…): the
        configuration being edited, under its name; otherwise Save As…."""
        if not self._editing:
            return self.save_configuration_as()
        name = self._editing
        if not self._write_configuration(self.current_options(name)):
            return False
        self._editing = ""
        self._fill_config_combo(name)
        return True

    def _config_file_for_saving(self) -> Path | None:
        """The file to save in (None: it can't be read, so isn't written over)."""
        path = self.config_path or bc.default_path(self.manifest_path)
        if self._config_error and path.exists():
            QMessageBox.warning(self, "Save Build Configuration",
                                f"{self._config_error}\n\nFix the file (or move it away) to save "
                                "configurations in it.")
            return None
        return path

    def _write_configuration(self, config: bc.Configuration) -> bool:
        path = self._config_file_for_saving()
        if path is None:
            return False
        try:
            self.configurations = bc.save(path, config)
        except (OSError, bc.ConfigError) as exc:
            QMessageBox.warning(self, "Save Build Configuration", f"Couldn't save {path}:\n\n{exc}")
            return False
        self._config_error = ""
        self._use_config_file(path)
        self.statusBar().showMessage(f"Saved build configuration “{config.name}” in {path.name}", 5000)
        return True

    def save_configuration_as(self) -> bool:
        """Save… / ⋯ → Save As…: what's shown, as a configuration in the
        project's file under a name asked for, chosen from then on."""
        if self.manifest_path is None:
            return False
        path = self._config_file_for_saving()
        if path is None:
            return False
        suggestion = self.config_combo.currentText() if self.config_shown() else ""
        name, ok = QInputDialog.getText(self, "Save Build Configuration",
                                        f"Save these Build options in {path.name} as:", text=suggestion)
        name = name.strip()
        if not ok or not name:
            return False
        if name == NEW_CONFIGURATION:
            QMessageBox.warning(self, "Save Build Configuration", f"“{NEW_CONFIGURATION}” is taken: choose "
                                "another name.")
            return False
        if self._configuration(name) is not None and name != self._editing and QMessageBox.question(
                self, "Save Build Configuration",
                f"{path.name} already has a configuration named “{name}”. Replace it?") != QMessageBox.Yes:
            return False
        if not self._write_configuration(self.current_options(name)):
            return False
        self._editing = ""  # (saved: under another name, the one edited is left as it was in the file)
        self._fill_config_combo(name)
        return True

    def _edit_clicked(self):
        if self._editing:
            self.cancel_editing()
        else:
            self.edit_configuration()

    def edit_configuration(self):
        """Edit: the saved configuration's options, editable (Save puts them back)."""
        if not self.options_locked():
            return
        self._editing = self.config_combo.currentText()
        self._set_options_locked(False)
        self._update_config_ui()

    def cancel_editing(self):
        """Cancel: the configuration as saved, read-only again (changes dropped)."""
        if not self._editing:
            return
        name, self._editing = self._editing, ""
        config = self._configuration(name)
        if config is None:  # (gone from the file while it was edited)
            self._fill_config_combo()
            return
        self._show_options(config)
        self._set_options_locked(True)
        self._update_config_ui()

    def maybe_save_configuration(self) -> bool:
        """Before leaving a configuration being edited with unsaved changes
        (choosing another, opening another project or file, quitting): Save,
        Discard or Cancel. False: Cancel (or saving failed): stay."""
        if not self.has_unsaved_configuration():
            return True
        answer = QMessageBox.question(
            self, "Unsaved Build Configuration",
            f"Save the changes to the build configuration “{self._editing}” in "
            f"{(self.config_path or bc.default_path(self.manifest_path)).name}?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
        if answer == QMessageBox.Cancel:
            return False
        if answer == QMessageBox.Save:
            name = self._editing
            if not self._write_configuration(self.current_options(name)):
                return False
        return True

    def edit_configuration_as_new(self):
        """⋯ → Edit as New Configuration: the configuration's options (as
        shown, with any changes being made), to change, as New
        configuration's (they replace what it had)."""
        if not self.config_shown():
            return
        self._editing = ""  # (the changes go to New configuration: nothing to ask)
        self._free_options = None  # (what's shown stays, editable)
        self.select_configuration("")
        self._save_features()

    def delete_configuration(self):
        if not self.config_shown() or self.config_path is None:
            return
        name = self.config_combo.currentText()
        if QMessageBox.question(self, "Delete Build Configuration",
                                f"Delete the configuration “{name}” from {self.config_path.name}?") \
                != QMessageBox.Yes:
            return
        try:
            self.configurations = bc.remove(self.config_path, name)
        except (OSError, bc.ConfigError) as exc:
            QMessageBox.warning(self, "Delete Build Configuration", f"Couldn't change {self.config_path}:\n\n{exc}")
            return
        self._editing = ""
        self.select_configuration("")
        self._fill_config_combo()

    def browse_configuration(self):
        start = str(self.manifest_path.parent) if self.manifest_path else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(self, "Open Build Configuration", start,
                                                  "VibeFPM configurations (*.json);;All files (*)")
        if filename:
            self.open_configuration(filename)

    def open_configuration(self, filename: str | Path):
        """Open a configuration file: its project (the fpm.toml beside it),
        if it isn't the one open, and its first configuration."""
        path = Path(filename).resolve()
        manifest = bc.manifest_for(path)
        if manifest is None:
            QMessageBox.warning(self, "Open Build Configuration",
                                f"There's no fpm.toml beside {path.name}: a configuration file goes in the "
                                "project's folder, next to its fpm.toml.")
            return
        try:
            configs = bc.load(path)
        except bc.ConfigError as exc:
            QMessageBox.warning(self, "Open Build Configuration", str(exc))
            return
        if not self.maybe_save_configuration():
            return
        self._editing = ""
        if manifest.resolve() != self.manifest_path:
            self.open_manifest(manifest)
            if self.manifest_path != manifest.resolve():
                return  # (not opened: Cancel, or fpm running)
        self._use_config_file(path)
        self._read_configurations()
        self._fill_config_combo(configs[0].name if configs else "")
        self.statusBar().showMessage(f"{path.name}: {len(configs)} configuration"
                                     f"{'s' * (len(configs) != 1)}", 5000)

    def _show_config_file(self):
        if self.config_path and self.config_path.is_file():
            self.open_file(str(self.config_path), force_editor=True)

    # ------------------------------------------------------------ recent
    def recent_configs(self) -> list[str]:
        value = self.settings.value("recent_configs", [], type=list) or []
        return [str(p) for p in value if p]

    def _remember_config(self, path: Path):
        recent = [p for p in self.recent_configs() if Path(p) != path]
        self.settings.setValue("recent_configs", [str(path), *recent][:MAX_RECENT_CONFIGS])

    def _fill_recent_configs_menu(self):
        """Rebuilt each time it opens, so missing files show as such."""
        menu = self.recent_configs_menu
        menu.clear()
        recent = self.recent_configs()
        if not recent:
            menu.addAction("No recent configurations").setEnabled(False)
        home = str(Path.home())
        for path in recent:
            shown = "~" + path[len(home):] if path.startswith(home + os.sep) else path
            exists = Path(path).is_file()
            label = f"{Path(path).parent.name} — {shown}" + ("" if exists else "  (not found)")
            action = menu.addAction(label.replace("&", "&&"))
            action.setToolTip(path if exists else f"{path}\nThis file no longer exists.")
            action.setEnabled(exists)
            action.setCheckable(True)
            action.setChecked(self.config_path is not None and Path(path) == self.config_path)
            action.triggered.connect(lambda _=False, p=path: self.open_recent_config(p))
        menu.addSeparator()
        clear = menu.addAction("Clear Menu")
        clear.setEnabled(bool(recent))
        clear.triggered.connect(lambda: self.settings.setValue("recent_configs", []))

    def open_recent_config(self, path: str):
        if not Path(path).is_file():
            QMessageBox.warning(self, "File not found", f"{path}\n\nno longer exists, so it was removed from "
                                "Recent Build Configurations.")
            self.settings.setValue("recent_configs", [p for p in self.recent_configs() if p != path])
            return
        self.open_configuration(path)

