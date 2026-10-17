"""Build configurations: named Build options in a .json file beside fpm.toml
(vibefpmgui.build_configs, and the Configuration list in Build options)."""

from __future__ import annotations

import json

import pytest
from conftest import write_files
from qtpy.QtWidgets import QInputDialog, QMessageBox

from vibefpmgui import build_configs as bc
from vibefpmgui.command_palette import collect
from vibefpmgui.window.configs import NEW_CONFIGURATION


# ----------------------------------------------------------------- the file
def test_saved_and_read_back(tmp_path):
    path = tmp_path / bc.FILE_NAME
    assert bc.load(path) == []  # (no file yet: none)
    release = bc.Configuration("release", profile="release", compiler="ifx", parallel_tests=4, coverage=True)
    assert bc.save(path, release) == [release]
    debug = bc.Configuration("debug", features=["mpi"], flags="-g -O0")
    assert [c.name for c in bc.save(path, debug)] == ["release", "debug"]
    data = json.loads(path.read_text())
    assert data["vibefpm"] == bc.FORMAT
    assert data["configurations"][1]["profile"] == ""  # ((default): nothing asked of fpm)
    assert bc.load(path)[1].profile == bc.DEFAULT_PROFILE
    # Replaced by name, in its place.
    faster = bc.Configuration("release", profile="release", compiler="gfortran")
    assert bc.save(path, faster) == [faster, debug]
    assert bc.remove(path, "release") == [debug]


def test_what_this_app_doesnt_know_is_kept(tmp_path):
    path = tmp_path / bc.FILE_NAME
    path.write_text(json.dumps({"vibefpm": 1, "comment": "ours", "configurations": [
        {"name": "a", "compiler": "ifx", "note": "keep me"}]}))
    bc.save(path, bc.Configuration("a", compiler="nvfortran"))
    data = json.loads(path.read_text())
    assert data["comment"] == "ours"
    assert data["configurations"] == [{**bc.Configuration("a", compiler="nvfortran").to_json(), "note": "keep me"}]


def test_values_of_the_wrong_type_get_their_default(tmp_path):
    path = tmp_path / bc.FILE_NAME
    path.write_text(json.dumps({"configurations": [
        {"name": " x ", "parallel_tests": 0, "coverage": "yes", "features": "mpi", "flags": 3, "verbose": True}]}))
    assert bc.load(path) == [bc.Configuration("x", verbose=True)]


@pytest.mark.parametrize("text, why", [
    ("{not json", "isn't valid JSON"),
    ("[]", "isn't a VibeFPM configuration file"),
    ('{"configurations": [1]}', "isn't an object"),
    ('{"configurations": [{"compiler": "ifx"}]}', "has no name"),
    ('{"configurations": [{"name": "a"}, {"name": "a"}]}', "two configurations are named"),
])
def test_a_file_that_cant_be_read_says_why(tmp_path, text, why):
    path = tmp_path / bc.FILE_NAME
    path.write_text(text)
    with pytest.raises(bc.ConfigError, match=why):
        bc.load(path)
    with pytest.raises(bc.ConfigError):
        bc.save(path, bc.Configuration("b"))  # (not written over)
    assert path.read_text() == text


def test_the_project_it_belongs_to(tmp_path, project):
    assert bc.manifest_for(project / bc.FILE_NAME) == project / "fpm.toml"
    assert bc.manifest_for(tmp_path / "elsewhere.json") is None
    assert bc.default_path(project / "fpm.toml") == project / bc.FILE_NAME


# ----------------------------------------------------------------- the window
def name_it(monkeypatch, name: str):
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: (name, True))


def editable(window) -> bool:
    return window.compiler_edit.isEnabled() and window.verbose_check.isEnabled()


def test_save_choose_and_go_back_to_new(window, project, monkeypatch):
    assert window.config_combo.currentText() == NEW_CONFIGURATION and editable(window)
    window.compiler_edit.setText("ifx")
    window.flag_edit.setText("-O3")
    window.test_jobs_spin.setValue(3)
    name_it(monkeypatch, "fast")
    window.save_configuration()
    path = project / bc.FILE_NAME
    assert bc.load(path) == [bc.Configuration("fast", compiler="ifx", flags="-O3", parallel_tests=3)]
    # Chosen now: read-only (but the fpm executable, which isn't part of it).
    assert window.config_combo.currentText() == "fast" and not editable(window)
    assert not window.test_jobs_spin.isEnabled() and not window.coverage_check.isEnabled()
    assert window.fpm_edit.isEnabled()
    assert not window.config_note.isHidden() and bc.FILE_NAME in window.config_note.text()
    assert window.build_arguments(["build"], True) == ["build", "--compiler", "ifx", "--flag", "-O3"]

    # New configuration: editable, as it was; another choice of it there.
    window.select_configuration("")
    assert editable(window) and window.config_note.isHidden()
    window.compiler_edit.setText("nvfortran")
    window.flag_edit.clear()
    window.select_configuration("fast")
    assert window.compiler_edit.text() == "ifx" and window.flag_edit.text() == "-O3"
    assert window.free_options().compiler == "nvfortran"  # (what's remembered at quitting)
    window.select_configuration("")
    assert window.compiler_edit.text() == "nvfortran" and window.flag_edit.text() == ""


def test_the_same_name_replaces_it(window, project, monkeypatch):
    name_it(monkeypatch, "a")
    window.save_configuration()
    window.edit_configuration_as_new()  # (its options, to change)
    assert window.config_combo.currentText() == NEW_CONFIGURATION and editable(window)
    window.compiler_edit.setText("ifx")
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    window.save_configuration()
    assert "already has a configuration named “a”" in asked[0]
    assert bc.load(project / bc.FILE_NAME) == [bc.Configuration("a", compiler="ifx")]


def test_delete(window, project, monkeypatch):
    name_it(monkeypatch, "a")
    window.save_configuration()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    window.delete_configuration()
    assert bc.load(project / bc.FILE_NAME) == []
    assert window.config_combo.count() == 1 and editable(window)


def test_a_configurations_features_and_profile(make_window, tmp_path, monkeypatch):
    root = write_files(tmp_path / "feat", {
        "fpm.toml": 'name = "feat"\n[features]\nmpi.flags = "-DMPI"\nomp.flags = "-fopenmp"\n'
                    '[profiles]\nfast = ["omp"]\n',
        bc.FILE_NAME: json.dumps({"configurations": [
            {"name": "with mpi", "features": ["mpi"]}, {"name": "fast", "profile": "fast"}]}),
    })
    window = make_window(root / "fpm.toml")
    window.select_configuration("with mpi")
    assert window.selected_features == ["mpi"]
    assert not window.feature_actions["mpi"].isEnabled()  # (the Features menu: read-only too)
    assert window.build_arguments(["build"], True) == ["build", "--features", "mpi"]
    window.select_configuration("fast")
    assert window.build_arguments(["build"], True) == ["build", "--profile", "fast"]
    assert not window.profile_combo.isEnabled()
    window.select_configuration("")
    assert window.selected_features == [] and window.feature_actions["mpi"].isEnabled()


def test_reloading_reads_the_file_again_and_keeps_the_choice(window, project, monkeypatch):
    name_it(monkeypatch, "a")
    window.save_configuration()
    bc.save(project / bc.FILE_NAME, bc.Configuration("a", compiler="changed"))  # (edited elsewhere)
    window.reload()
    assert window.config_combo.currentText() == "a" and window.compiler_edit.text() == "changed"


def test_remembered_for_the_project(make_window, project, monkeypatch):
    first = make_window(project / "fpm.toml")
    name_it(monkeypatch, "kept")
    first.save_configuration()
    first.close()
    again = make_window(project / "fpm.toml")
    assert again.config_combo.currentText() == "kept" and not editable(again)


def test_open_a_configuration_file_and_its_project(window, project, conflict_project):
    path = conflict_project / "builds.json"
    path.write_text(json.dumps({"configurations": [{"name": "other", "compiler": "lfortran"}]}))
    window.open_configuration(path)
    assert window.manifest_path == (conflict_project / "fpm.toml").resolve()
    assert window.config_path == path.resolve()
    assert window.config_combo.currentText() == "other" and window.compiler_edit.text() == "lfortran"
    assert window.recent_configs()[0] == str(path.resolve())
    window._fill_recent_configs_menu()
    entries = [a for a in window.recent_configs_menu.actions() if a.isCheckable()]
    assert entries[0].isChecked() and "conflict" in entries[0].text()


def test_a_configuration_file_not_beside_an_fpm_toml(window, tmp_path, monkeypatch):
    path = tmp_path / "lost.json"
    path.write_text('{"configurations": []}')
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    window.open_configuration(path)
    assert "no fpm.toml beside" in warned[0]


def test_a_file_that_cant_be_read(make_window, project, monkeypatch):
    (project / bc.FILE_NAME).write_text("{oops")
    window = make_window(project / "fpm.toml")
    assert window.config_combo.count() == 1 and "isn't valid JSON" in window.config_combo.toolTip()
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    window.save_configuration()
    assert "isn't valid JSON" in warned[0] and (project / bc.FILE_NAME).read_text() == "{oops"


def test_the_file_menu(window):
    titles = {c.title for c in collect(window.menuBar())}
    assert {"File › Open Build Configuration…", "File › Save Build Configuration…"} <= titles


# ----------------------------------------------------------------- editing
@pytest.fixture
def saved(window, project, monkeypatch):
    """The window with "a" (compiler ifx) saved and chosen; no name asked from here on."""
    window.compiler_edit.setText("ifx")
    name_it(monkeypatch, "a")
    window.save_configuration()

    def no_name(*a, **k):
        raise AssertionError("a name asked for")

    monkeypatch.setattr(QInputDialog, "getText", no_name)
    return window


def answer(monkeypatch, button) -> list:
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or button)
    return asked


def test_edit_and_save_under_its_name(saved, project):
    window = saved
    assert window.config_edit_button.isEnabled() and not editable(window)
    window.config_edit_button.click()
    assert editable(window) and window.config_combo.currentText() == "a"
    assert window.config_edit_button.text() == "Cancel"
    assert not window.config_save_button.isEnabled()  # (nothing changed yet)
    window.compiler_edit.setText("nvfortran")
    assert window.has_unsaved_configuration() and window.config_save_button.isEnabled()
    assert "not saved" in window.config_note.text()
    window.config_save_button.click()
    assert bc.load(project / bc.FILE_NAME) == [bc.Configuration("a", compiler="nvfortran")]
    assert not editable(window) and window.config_edit_button.text() == "Edit"


def test_cancel_puts_it_back(saved):
    saved.edit_configuration()
    saved.flag_edit.setText("-g")
    saved.config_edit_button.click()  # (Cancel)
    assert saved.flag_edit.text() == "" and saved.compiler_edit.text() == "ifx" and not editable(saved)


def test_features_edited_are_the_configurations(make_window, tmp_path, monkeypatch):
    root = write_files(tmp_path / "feat", {
        "fpm.toml": 'name = "feat"\n[features]\nmpi.flags = "-DMPI"\n',
        bc.FILE_NAME: json.dumps({"configurations": [{"name": "plain"}]}),
    })
    window = make_window(root / "fpm.toml")
    window.select_configuration("plain")
    window.edit_configuration()
    window.feature_actions["mpi"].trigger()
    assert window.has_unsaved_configuration()
    window.save_configuration()
    assert bc.load(root / bc.FILE_NAME)[0].features == ["mpi"]
    window.select_configuration("")
    assert window.selected_features == []  # (New configuration's own: not changed by it)


@pytest.mark.parametrize("button", [QMessageBox.Cancel, QMessageBox.Discard, QMessageBox.Save])
def test_leaving_unsaved_changes_asks(saved, project, monkeypatch, button):
    window = saved
    window.edit_configuration()
    window.compiler_edit.setText("changed")
    asked = answer(monkeypatch, button)
    window.select_configuration("")
    assert "Save the changes to the build configuration “a”" in asked[0]
    in_file = bc.load(project / bc.FILE_NAME)[0].compiler
    if button == QMessageBox.Cancel:  # (still on it, the change kept)
        assert window.config_combo.currentText() == "a" and window.compiler_edit.text() == "changed"
        assert editable(window) and in_file == "ifx"
    else:
        assert window.config_combo.currentText() == NEW_CONFIGURATION
        assert in_file == ("changed" if button == QMessageBox.Save else "ifx")
        window.select_configuration("a")
        assert window.compiler_edit.text() == in_file and not editable(window)


def test_no_question_without_changes(saved, monkeypatch):
    asked = answer(monkeypatch, QMessageBox.Cancel)
    saved.edit_configuration()
    saved.select_configuration("")
    assert not asked and saved.config_combo.currentText() == NEW_CONFIGURATION


def test_closing_asks(saved, monkeypatch):
    saved.edit_configuration()
    saved.compiler_edit.setText("changed")
    asked = answer(monkeypatch, QMessageBox.Cancel)
    assert not saved.close() and saved.isVisible() and len(asked) == 1
    answer(monkeypatch, QMessageBox.Discard)
    assert saved.close()


def test_opening_another_project_asks(saved, conflict_project, monkeypatch):
    saved.edit_configuration()
    saved.compiler_edit.setText("changed")
    answer(monkeypatch, QMessageBox.Cancel)
    old = saved.manifest_path
    saved.open_manifest(conflict_project / "fpm.toml")
    assert saved.manifest_path == old and saved.compiler_edit.text() == "changed"


def test_reloading_while_editing_keeps_the_changes(saved):
    saved.edit_configuration()
    saved.compiler_edit.setText("changed")
    saved.reload()
    assert saved.compiler_edit.text() == "changed" and editable(saved) and saved.has_unsaved_configuration()
