"""The Build outputs panel: what fpm keeps in build/, running a built program,
and the command that compiled a file."""

from __future__ import annotations

import hashlib
import html
import os
import sys
from pathlib import Path

from qtpy.QtCore import QProcess
from qtpy.QtWidgets import (
    QApplication,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
)

from .. import build_outputs as bo
from ..compat import join_args, quote_arg, split_args
from ..fpm_args import TEST_RUNNER, split_runner
from ..project_tree import reveal_in_file_manager
from ..widgets import run_dialog


class BuildOutputsMixin:
    """The Build outputs panel (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_outputs_view(self):
        """The Build outputs panel."""
        self.outputs_view = bo.BuildOutputsView()
        self.outputs_view.run_requested.connect(self.run_program)
        self.outputs_view.run_in_terminal_requested.connect(self.run_program_in_terminal)
        self.outputs_view.reveal_requested.connect(reveal_in_file_manager)
        self.outputs_view.delete_requested.connect(self.delete_configurations)
        self.outputs_view.refresh_requested.connect(self.refresh_build_outputs)

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
        if self.busy():
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

    def run_program_in_terminal(self, path: str):
        """Type the command to run an already-built program (with the Program
        args) in the Terminal panel: there it can read what you type."""
        try:
            args = split_args(self.runner_args_edit.text())
        except ValueError as exc:
            QMessageBox.warning(self, "Invalid arguments", str(exc))
            return
        self.show_panel(self.terminal_panel)
        self.terminal_panel.type_command(join_args([path, *args]))
        self.terminal_panel.focus()

    def run_program(self, path: str):
        """Run an already-built program directly (no fpm, no rebuild)."""
        if self.busy() or not self.manifest_path:
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
        self._esc_tail = ""
        self.console_writer.reset()
        self._begin_output()
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
        self.show_panel(self.run_monitor)
        rel = os.path.relpath(path, self.manifest_path.parent)
        self._append(f"\n$ cd {quote_arg(str(self.manifest_path.parent))}\n"
                     f"$ {join_args([rel] + args)}\n", bold=True)
        self.stop_action.setEnabled(True)
        self._set_project_enabled(True)
        self.statusBar().showMessage(f"Running {name}…")
        self.process.start(sys.executable, ["-I", TEST_RUNNER, path] + args)

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
        run_dialog(dialog)
