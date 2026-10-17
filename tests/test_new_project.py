"""File → New fpm Project…: fpm new, with its options chosen in a dialog."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from qtpy.QtWidgets import QDialog, QMessageBox

from vibefpmgui import new_project as np


def test_names_as_fpm_allows_them(tmp_path):
    assert np.name_problem("orbit_tools-2", tmp_path) == ""
    assert np.name_problem("", tmp_path) == np.NO_NAME
    for bad in ("2fast", "has space", "dot.name", "x" * 64):
        assert "letters" in np.name_problem(bad, tmp_path), bad
    (tmp_path / "taken").mkdir()
    assert "already exists" in np.name_problem("taken", tmp_path)
    assert np.name_problem("taken", tmp_path, backfill=True) == ""  # (backfill: adds what's missing)
    assert "no folder" in np.name_problem("p", tmp_path / "nowhere")


def test_the_commands():
    assert np.new_command("fpm", "p") == ["fpm", "new", "p", "--full"]  # (full: the default)
    assert np.new_command("fpm", "p", np.CHOSEN, {"--test", "--lib"}) == ["fpm", "new", "p", "--lib", "--test"]
    assert np.new_command("fpm", "p", np.BARE, backfill=True) == ["fpm", "new", "p", "--bare", "--backfill"]


def test_the_dialog(qtbot, tmp_path):
    dialog = np.NewProjectDialog("fpm", tmp_path)
    qtbot.addWidget(dialog)
    assert not dialog.create_button.isEnabled() and dialog.problem_label.text() == ""  # (nothing typed: no nagging)
    dialog.name_edit.setText("orbit")
    assert dialog.create_button.isEnabled() and dialog.command() == ["fpm", "new", "orbit", "--full"]
    assert "fpm new orbit --full" in dialog.command_label.text()
    assert not any(c.isEnabled() for c in dialog.part_checks.values())  # (full: every folder)
    dialog.kind_buttons[np.CHOSEN].setChecked(True)
    assert all(c.isEnabled() for c in dialog.part_checks.values())
    assert dialog.command() == ["fpm", "new", "orbit", "--lib", "--app", "--test"]  # (fpm's own choice)
    for check in dialog.part_checks.values():
        check.setChecked(False)
    assert not dialog.create_button.isEnabled() and "at least one" in dialog.problem_label.text()
    dialog.name_edit.setText("bad name")
    assert "letters" in dialog.problem_label.text()
    assert dialog.project_folder() == tmp_path / "bad name"


@pytest.mark.skipif(shutil.which("fpm") is None, reason="fpm needed")
def test_a_new_project_opens_in_the_window(qtbot, window, tmp_path, monkeypatch):
    def fill(dialog):
        dialog.folder_edit.setText(str(tmp_path))
        dialog.name_edit.setText("made_here")
        return QDialog.Accepted

    monkeypatch.setattr(np.NewProjectDialog, "exec", fill)
    window.new_project()
    manifest = (tmp_path / "made_here" / "fpm.toml").resolve()
    qtbot.waitUntil(lambda: window.manifest_path == manifest, timeout=30_000)
    assert {p.name for p in manifest.parent.iterdir()} >= {"src", "app", "test", "example", "fpm.toml"}
    assert "full" in manifest.read_text().lower() or "[build]" in manifest.read_text()  # (the annotated one)


def test_fpm_s_complaint_is_shown(window, tmp_path, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: shown.append(a[2]))
    before = window.manifest_path
    window._project_made(tmp_path / "nope", False, "<ERROR> something fpm said")
    assert shown and "something fpm said" in shown[0] and window.manifest_path == before
    window._project_made(tmp_path / "nope", True, "")  # (it said it worked, but there's no fpm.toml)
    assert len(shown) == 2 and window.manifest_path == before
    assert Path(str(tmp_path)).is_dir()
