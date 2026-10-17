"""A project's GUI options in its build configuration file's "options"
(vibefpmgui.project_options, vibefpmgui.window.options)."""

from __future__ import annotations

import json

import pytest
from conftest import open_editor, write_files
from qtpy.QtCore import QByteArray, QSettings
from qtpy.QtWidgets import QInputDialog

from vibefpmgui import build_configs as bc
from vibefpmgui import project_options as po


# ----------------------------------------------------------------- the layer
@pytest.fixture
def layered(tmp_path):
    return po.LayeredSettings(QSettings(str(tmp_path / "app.ini"), QSettings.IniFormat))


def test_without_a_projects_options_everything_is_the_apps(layered):
    layered.setValue("word_wrap", True)
    assert layered.app.value("word_wrap", type=bool) is True
    assert layered.value("word_wrap", False, type=bool) is True


def test_options_go_to_the_project_the_rest_to_the_app(layered, tmp_path):
    layered.setValue("word_wrap", False)
    layered.use({}, tmp_path)
    assert layered.value("word_wrap", True, type=bool) is False  # (not in its options yet: the app's)
    for key, value in (("word_wrap", True), ("font_delta", 3), ("features", ["mpi"]), ("compiler", "ifx"),
                       ("problems/fortitude", False), ("search/include", "*.f90")):
        layered.setValue(key, value)
        assert layered.options[key] == value
    assert layered.app.value("word_wrap", type=bool) is False  # (the app's unchanged)
    assert layered.value("font_delta", 0, type=int) == 3 and layered.value("features", [], type=list) == ["mpi"]
    # Not options: the app's, for this computer.
    for key in ("geometry", "recent_manifests", "last_manifest", "spelling_words", "ai_model", "fpm",
                "palette_recent", "build_config_file/abc"):
        layered.setValue(key, "x")
        assert key not in layered.options and layered.app.value(key) == "x"


def test_the_panels_layout_and_the_session(layered, tmp_path):
    layered.use({}, tmp_path)
    layered.setValue("panels", QByteArray(b"\x00\x01binary"))
    assert isinstance(layered.options["panels"], str)  # (base64: JSON has no bytes)
    assert bytes(layered.value("panels")) == b"\x00\x01binary"
    inside, outside = str(tmp_path / "src" / "a.f90"), str(tmp_path.parent / "elsewhere.f90")
    session = {"files": [{"path": inside, "line": 3}, {"path": outside}], "current": inside,
               "tree": {"expanded": [str(tmp_path / "src"), "group:Dependencies"], "selected": inside}}
    layered.setValue("session", json.dumps(session))
    stored = layered.options["session"]
    assert stored["files"][0]["path"] == "src/a.f90" and stored["files"][1]["path"] == outside
    assert stored["tree"]["expanded"] == ["src", "group:Dependencies"] and stored["current"] == "src/a.f90"
    assert json.loads(layered.value("session")) == session  # (absolute again, where it's opened)


def test_a_value_of_the_wrong_type_gets_the_default(layered, tmp_path):
    layered.use({"word_wrap": "yes", "font_delta": "big", "features": "mpi"}, tmp_path)
    assert layered.value("word_wrap", False, type=bool) is False
    assert layered.value("font_delta", 1, type=int) == 1
    assert layered.value("features", [], type=list) == []


def test_the_options_written_have_them_all(layered, tmp_path):
    layered.app.setValue("minimap", "false")  # (as an .ini file gives it back)
    layered.app.setValue("indent_width", "2")
    layered.app.setValue("features/abc", ["omp"])
    layered.app.setValue("problems/lint", "true")
    layered.app.setValue("geometry", "not an option")
    layered.use({"word_wrap": True}, tmp_path)
    options = layered.complete_options({"features": "features/abc"})
    assert options == {"word_wrap": True, "minimap": False, "indent_width": 2, "features": ["omp"],
                       "problems/lint": True}


# ----------------------------------------------------------------- the file
def test_the_options_key_keeps_the_configurations(tmp_path):
    path = tmp_path / bc.FILE_NAME
    bc.save(path, bc.Configuration("a", compiler="ifx"))
    assert bc.load_options(path) == {}
    bc.save_options(path, {"word_wrap": True})
    assert bc.load_options(path) == {"word_wrap": True}
    assert bc.load(path) == [bc.Configuration("a", compiler="ifx")]
    bc.save(path, bc.Configuration("b"))
    assert bc.load_options(path) == {"word_wrap": True}  # (saving a configuration keeps them)
    assert bc.load_options(tmp_path / "none.json") is None
    path.write_text('{"configurations": [], "options": []}')
    with pytest.raises(bc.ConfigError, match="isn't an object"):
        bc.load_options(path)


# ----------------------------------------------------------------- the window
@pytest.fixture
def configured(project):
    """The sample project, with a configuration file (no options in it yet)."""
    (project / bc.FILE_NAME).write_text(json.dumps({"configurations": [{"name": "a"}]}))
    return project


def file_options(project) -> dict:
    return bc.load_options(project / bc.FILE_NAME)


def test_closing_writes_the_options_in_the_file(make_window, configured):
    window = make_window(configured / "fpm.toml")
    window.word_wrap_action.setChecked(True)
    open_editor(window, configured / "src/foo.f90")
    window.close()
    options = file_options(configured)
    assert options["word_wrap"] is True
    assert options["session"]["files"][0]["path"] == "src/foo.f90"  # (relative: the file can be shared)
    assert options["session"]["current"] == "src/foo.f90"
    assert options["panels"] and "tab_rows_wrap" in options and "compiler" in options
    assert all(f"problems/{source}" in options for source in window.problems_panel.buttons)
    for kept_out in ("geometry", "recent_manifests", "fpm", "spelling_words", "ai_model"):
        assert kept_out not in options
    assert not window.settings.app.value("word_wrap", False, type=bool)  # (the app's own: unchanged)
    assert [c.name for c in bc.load(configured / bc.FILE_NAME)] == ["a"]


def test_opening_uses_the_files_options(make_window, configured):
    bc.save_options(configured / bc.FILE_NAME, {
        "word_wrap": True, "minimap": False, "compiler": "nvfortran", "tab_rows_wrap": True,
        "session": {"files": [{"path": "src/foo.f90", "line": 2, "column": 0, "scroll": 0}],
                    "current": "src/foo.f90"}})
    window = make_window(configured / "fpm.toml")
    assert window.word_wrap_action.isChecked() and not window.minimap_action.isChecked()
    assert window.compiler_edit.text() == "nvfortran"
    assert window.tab_rows_actions[True].isChecked()
    editor = window.current_editor()
    assert editor is not None and editor.path == (configured / "src/foo.f90").resolve()


def test_a_project_without_a_file_gets_none(make_window, project):
    window = make_window(project / "fpm.toml")
    window.word_wrap_action.setChecked(True)
    window.close()
    assert not (project / bc.FILE_NAME).exists()
    assert window.settings.app.value("word_wrap", False, type=bool)  # (the app's, as before)


def test_switching_projects_applies_each_ones_options(make_window, configured, conflict_project):
    bc.save_options(configured / bc.FILE_NAME, {"word_wrap": True, "compiler": "ifx"})
    window = make_window(configured / "fpm.toml")
    assert window.word_wrap_action.isChecked()
    window.minimap_action.setChecked(False)  # (this project's)
    window.open_manifest(conflict_project / "fpm.toml")  # (no configuration file: the app's settings)
    assert not window.word_wrap_action.isChecked() and window.minimap_action.isChecked()
    assert window.compiler_edit.text() == ""
    assert file_options(configured)["minimap"] is False  # (written when it was left)
    window.open_manifest(configured / "fpm.toml")
    assert window.word_wrap_action.isChecked() and not window.minimap_action.isChecked()
    assert window.compiler_edit.text() == "ifx"


def test_the_first_configuration_saved_starts_the_options(make_window, project, monkeypatch):
    window = make_window(project / "fpm.toml")
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("first", True))
    window.save_configuration()
    window.word_wrap_action.setChecked(True)
    window.close()
    assert file_options(project)["word_wrap"] is True


def test_options_changed_in_the_editor_are_applied(make_window, configured):
    window = make_window(configured / "fpm.toml")
    assert not window.word_wrap_action.isChecked()
    editor = open_editor(window, configured / bc.FILE_NAME)
    data = json.loads(editor.editor.toPlainText())
    data["options"] = {"word_wrap": True}
    editor.editor.setPlainText(json.dumps(data, indent=2))
    editor.save()
    assert window.word_wrap_action.isChecked()


def test_nothing_written_when_nothing_changed(make_window, configured):
    window = make_window(configured / "fpm.toml")
    window.close()
    first = (configured / bc.FILE_NAME).read_text()
    again = make_window(configured / "fpm.toml")
    again.close()
    assert (configured / bc.FILE_NAME).read_text() == first


def test_a_shared_file_works_where_the_project_is(make_window, tmp_path):
    """A project and its file copied elsewhere (another computer): the open
    files are found where the project is now."""
    files = {"fpm.toml": 'name = "moved"\n', "src/m.f90": "module m\nend module m\n",
             bc.FILE_NAME: json.dumps({"configurations": [], "options": {
                 "session": {"files": [{"path": "src/m.f90"}], "current": "src/m.f90"}}})}
    root = write_files(tmp_path / "copy", files)
    window = make_window(root / "fpm.toml")
    assert window.current_editor().path == (root / "src/m.f90").resolve()
