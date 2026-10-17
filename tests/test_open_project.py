"""File → Open Project… and Recent Projects: a project is its folder."""

from __future__ import annotations

import json

import pytest
from conftest import write_files
from qtpy.QtWidgets import QFileDialog, QMessageBox

from vibefpmgui import build_configs as bc
from vibefpmgui.command_palette import collect
from vibefpmgui.window.options import project_key


@pytest.fixture
def warned(monkeypatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: messages.append(a[2]))
    return messages


def test_a_folder_with_an_fpm_toml(window, conflict_project):
    assert window.open_project(conflict_project)
    assert window.manifest_path == (conflict_project / "fpm.toml").resolve()
    assert window.recent_projects()[0] == str(conflict_project.resolve())


def test_a_folder_with_a_vibefpm_json_opens_with_it(window, conflict_project):
    other = conflict_project / "other.json"  # (a configuration file opened for it before)
    other.write_text(json.dumps({"configurations": [{"name": "o"}]}))
    window.open_configuration(other)
    window.open_manifest(window.manifest_path.parent.parent / "proj" / "fpm.toml")
    bc.save(conflict_project / bc.FILE_NAME, bc.Configuration("mine", compiler="ifx"))
    assert window.open_project(conflict_project)
    assert window.config_path == (conflict_project / bc.FILE_NAME).resolve()
    assert window.config_combo.findText("mine") > 0


def test_the_project_open_switches_to_its_vibefpm_json(window, project):
    other = project / "other.json"
    other.write_text(json.dumps({"configurations": [{"name": "o"}]}))
    window.open_configuration(other)
    assert window.config_path == other.resolve()
    bc.save(project / bc.FILE_NAME, bc.Configuration("mine"))
    assert window.open_project(project)
    assert window.config_path == (project / bc.FILE_NAME).resolve() and window.config_combo.findText("mine") > 0


@pytest.mark.parametrize("files, why", [
    ({"readme.txt": "x"}, "it has no fpm.toml."),
    ({bc.FILE_NAME: '{"configurations": []}'}, f"It has a {bc.FILE_NAME}, but that goes beside a project's fpm.toml."),
])
def test_a_folder_that_isnt_a_project(window, project, tmp_path, warned, files, why):
    folder = write_files(tmp_path / "nothing", files)
    assert not window.open_project(folder)
    assert "isn't an fpm project" in warned[0] and why in warned[0]
    assert window.manifest_path == (project / "fpm.toml").resolve()  # (still the one open)


def test_in_a_new_window(window, conflict_project, monkeypatch):
    opened = []
    monkeypatch.setattr(type(window), "open_in_new_window", lambda self, manifest: opened.append(manifest))
    other = conflict_project / "other.json"
    other.write_text(json.dumps({"configurations": []}))
    window.settings.app.setValue(project_key((conflict_project / "fpm.toml").resolve(), "build_config_file"),
                                 str(other))
    (conflict_project / bc.FILE_NAME).write_text('{"configurations": []}')
    assert window.open_project(conflict_project, new_window=True)
    assert opened == [(conflict_project / "fpm.toml").resolve()]
    # (The new window uses its vibefpm.json, not the file opened for it before.)
    assert window.settings.app.value(project_key(opened[0], "build_config_file")) == ""


def test_open_project_asks_for_a_folder(window, conflict_project, monkeypatch):
    asked = []
    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *a, **k: asked.append(a[1]) or str(conflict_project))
    window.browse_project()
    assert asked == ["Open Project"] and window.manifest_path == (conflict_project / "fpm.toml").resolve()


def test_recent_projects(make_window, project, conflict_project, tmp_path):
    window = make_window(project / "fpm.toml")
    window.open_project(conflict_project)
    gone = tmp_path / "gone"
    window.settings.setValue("recent_projects", [*window.recent_projects(), str(gone)])
    window._fill_recent_menu()
    entries = [a for a in window.recent_menu.actions() if a.isCheckable()]
    assert [e.text().split(" — ")[0] for e in entries] == ["conflict", "proj", "gone"]
    assert entries[0].isChecked() and not entries[1].isChecked()
    assert not entries[2].isEnabled() and "(not found)" in entries[2].text()
    entries[1].trigger()
    assert window.manifest_path == (project / "fpm.toml").resolve()
    window.clear_recent_projects()
    window._fill_recent_menu()
    assert window.recent_menu.actions()[0].text() == "No recent projects"


def test_recent_lists_from_before_become_their_folders(window, project, conflict_project):
    s = window.settings
    s.remove("recent_projects")
    s.setValue("recent_manifests", [str(project / "fpm.toml"), str(conflict_project / "fpm.toml")])
    s.setValue("recent_configs", [str(project / bc.FILE_NAME)])
    assert window.recent_projects() == [str(project), str(conflict_project)]
    assert not s.contains("recent_manifests") and not s.contains("recent_configs")


def test_the_file_menu(window):
    titles = {c.title for c in collect(window.menuBar())}
    assert {"File › Open Project…", "File › Open Project in New Window…", "File › Open File in Editor…"} <= titles
    assert not {t for t in titles if "Manifest" in t or "Open Build Configuration" in t
                or "Recent Build Configurations" in t}
