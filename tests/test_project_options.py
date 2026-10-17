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
    for key, value in (("word_wrap", True), ("indent_width", 3), ("problems/fortitude", False),
                       ("search/include", "*.f90")):
        layered.setValue(key, value)
        assert layered.options[key] == value
    assert layered.app.value("word_wrap", type=bool) is False  # (the app's unchanged)
    assert layered.value("indent_width", 0, type=int) == 3
    # Not options: the app's, for this computer.
    for key in ("geometry", "recent_manifests", "last_manifest", "spelling_words", "ai_model", "fpm",
                "palette_recent", "build_config_file/abc", "app_theme", "app_style", "syntax_style",
                "editor_font_family", "editor_font_size", "font_delta"):
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
    layered.use({"word_wrap": "yes", "indent_width": "big", "panels_front": "build"}, tmp_path)
    assert layered.value("word_wrap", False, type=bool) is False
    assert layered.value("indent_width", 1, type=int) == 1
    assert layered.value("panels_front", [], type=list) == []


def test_the_options_written_have_them_all(layered, tmp_path):
    layered.app.setValue("minimap", "false")  # (as an .ini file gives it back)
    layered.app.setValue("indent_width", "2")
    layered.app.setValue("session/abc", '{"files": []}')
    layered.app.setValue("problems/lint", "true")
    layered.app.setValue("geometry", "not an option")
    layered.app.setValue("compiler", "a build option: not with the options")
    layered.use({"word_wrap": True}, tmp_path, {"session": "session/abc", "build_configuration": "bc/abc"})
    assert layered.complete_options() == {"word_wrap": True, "minimap": False, "indent_width": 2,
                                          "session": {"files": []}, "problems/lint": True}


def test_a_projects_own_options_without_a_file_are_the_apps_per_project_keys(layered):
    layered.use(None, None, {"session": "session/abc", "build_configuration": "bc/abc"})
    layered.setValue("session", "{}")
    assert layered.app.value("session/abc") == "{}" and not layered.app.contains("session")
    assert layered.value("session") == "{}" and layered.contains("session")
    layered.use({}, None, {"session": "session/abc"})  # (a file without them yet: the app's, for the project)
    assert layered.value("session") == "{}"
    layered.use(None)  # (no project)
    assert layered.value("session", "none") == "none"
    layered.setValue("session", "ignored")
    assert layered.app.value("session/abc") == "{}"


# ----------------------------------------------------------------- the file
def test_the_file_is_read_whole_and_written_a_part_at_a_time(tmp_path):
    path = tmp_path / bc.FILE_NAME
    missing = bc.ConfigFile(path).read()
    assert missing.options is None and missing.configurations == [] and not missing.error
    bc.save(path, bc.Configuration("a", compiler="ifx"))
    file = bc.ConfigFile(path).read()
    assert file.options == {} and file.new_configuration is None
    new = bc.Configuration("", flags="-g", parallel_tests=2)
    assert file.write_options({"word_wrap": True}, new)
    assert not file.write_options({"word_wrap": True}, new)  # (the same: not written again)
    data = json.loads(path.read_text())
    assert list(data) == ["vibefpm", "configurations", "new_configuration", "options"]
    assert data["new_configuration"] == new.to_json(named=False) and "name" not in data["new_configuration"]
    again = bc.ConfigFile(path).read()
    assert again.options == {"word_wrap": True} and again.new_configuration == new
    assert again.configurations == [bc.Configuration("a", compiler="ifx")]
    bc.save(path, bc.Configuration("b"))  # (saving a configuration keeps the rest)
    assert bc.ConfigFile(path).read().options == {"word_wrap": True}


def test_new_configurations_values_are_checked(tmp_path):
    path = tmp_path / bc.FILE_NAME
    path.write_text(json.dumps({"configurations": [], "new_configuration": {"parallel_tests": 0, "flags": 3,
                                                                             "compiler": "ifx"}}))
    assert bc.ConfigFile(path).read().new_configuration == bc.Configuration("", compiler="ifx")


def test_a_file_from_before_has_its_build_options_moved(tmp_path):
    path = tmp_path / bc.FILE_NAME
    path.write_text(json.dumps({"configurations": [], "options": {
        "word_wrap": True, "compiler": "old", "test_jobs": 3, "features": ["omp"], "extra": "--x"}}))
    file = bc.ConfigFile(path).read()
    assert file.options == {"word_wrap": True}
    assert file.new_configuration == bc.Configuration("", compiler="old", parallel_tests=3, features=["omp"],
                                                      extra_args="--x")
    assert file.write_options(file.options, file.new_configuration)  # (moved: written so)
    assert "compiler" not in json.loads(path.read_text())["options"]


@pytest.mark.parametrize("key", ["options", "new_configuration"])
def test_a_file_whose_options_cant_be_read_isnt_used(tmp_path, key):
    path = tmp_path / bc.FILE_NAME
    path.write_text(json.dumps({"configurations": [{"name": "a"}], key: []}))
    file = bc.ConfigFile(path).read()
    assert f'"{key}" isn\'t an object' in file.error
    assert file.options is None and file.configurations == []
    text = path.read_text()
    for change in (lambda: file.save(bc.Configuration("b")), lambda: file.remove("a")):
        with pytest.raises(bc.ConfigError):
            change()
    assert path.read_text() == text  # (not written over)


# ----------------------------------------------------------------- the window
@pytest.fixture
def configured(project):
    """The sample project, with a configuration file (no options in it yet)."""
    (project / bc.FILE_NAME).write_text(json.dumps({"configurations": [{"name": "a"}]}))
    return project


def file_data(project) -> dict:
    return json.loads((project / bc.FILE_NAME).read_text())


def file_options(project) -> dict:
    return file_data(project)["options"]


def put(project, options: dict, new: dict | None = None):
    """The file's options (and New configuration), as if written before."""
    data = file_data(project)
    data["options"] = options
    if new is not None:
        data["new_configuration"] = new
    (project / bc.FILE_NAME).write_text(json.dumps(data))


def test_closing_writes_the_options_in_the_file(make_window, configured):
    window = make_window(configured / "fpm.toml")
    window.word_wrap_action.setChecked(True)
    open_editor(window, configured / "src/foo.f90")
    window.close()
    options = file_options(configured)
    assert options["word_wrap"] is True
    assert options["session"]["files"][0]["path"] == "src/foo.f90"  # (relative: the file can be shared)
    assert options["session"]["current"] == "src/foo.f90"
    assert options["panels"] and "tab_rows_wrap" in options
    for build_option in ("compiler", "flags", "profile", "test_jobs", "features", "extra"):
        assert build_option not in options  # (beside the configurations: see below)
    new = file_data(configured)["new_configuration"]
    assert {"compiler", "flags", "profile", "parallel_tests", "features", "extra_args", "target",
            "program_args", "coverage", "verbose"} <= set(new) and "name" not in new
    assert all(f"problems/{source}" in options for source in window.problems_panel.buttons)
    for kept_out in ("geometry", "recent_manifests", "fpm", "spelling_words", "ai_model", "app_theme",
                     "app_style", "syntax_style", "editor_font_family", "editor_font_size", "font_delta"):
        assert kept_out not in options
    assert not window.settings.app.value("word_wrap", False, type=bool)  # (the app's own: unchanged)
    assert [c.name for c in bc.load(configured / bc.FILE_NAME)] == ["a"]


def test_opening_uses_the_files_options(make_window, configured):
    put(configured, {
        "word_wrap": True, "minimap": False, "tab_rows_wrap": True,
        "session": {"files": [{"path": "src/foo.f90", "line": 2, "column": 0, "scroll": 0}],
                    "current": "src/foo.f90"}}, {"compiler": "nvfortran", "target": "check*"})
    window = make_window(configured / "fpm.toml")
    assert window.word_wrap_action.isChecked() and not window.minimap_action.isChecked()
    assert window.compiler_edit.text() == "nvfortran" and window.target_edit.text() == "check*"
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
    put(configured, {"word_wrap": True, "compiler": "ifx"})
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


def test_a_file_from_before_has_its_build_options_moved_when_the_window_closes(make_window, configured):
    put(configured, {"word_wrap": True, "compiler": "ifx", "features": []})
    window = make_window(configured / "fpm.toml")
    assert window.compiler_edit.text() == "ifx" and window.word_wrap_action.isChecked()
    window.close()
    assert "compiler" not in file_options(configured)
    assert file_data(configured)["new_configuration"]["compiler"] == "ifx"
    assert list(file_data(configured)) == ["configurations", "new_configuration", "options"]


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


# ----------------------------------------------------------------- found in review
def test_new_configurations_features_survive_saving_the_first_configuration(make_window, tmp_path, monkeypatch):
    """Saving the first configuration starts the file's options: New
    configuration's features were then read from the wrong key (lost)."""
    root = write_files(tmp_path / "feat", {"fpm.toml": 'name = "feat"\n[features]\nmpi.flags = "-DMPI"\n'})
    window = make_window(root / "fpm.toml")
    window.feature_actions["mpi"].trigger()
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("first", True))
    window.save_configuration()
    window.select_configuration("")
    assert window.selected_features == ["mpi"]


def test_options_changed_in_the_editor_keep_the_configuration_chosen(make_window, configured):
    bc.save(configured / bc.FILE_NAME, bc.Configuration("a", compiler="ifx"))
    window = make_window(configured / "fpm.toml")
    window.select_configuration("a")
    editor = open_editor(window, configured / bc.FILE_NAME)
    data = json.loads(editor.editor.toPlainText())
    data["options"] = {"word_wrap": True}
    editor.editor.setPlainText(json.dumps(data, indent=2))
    editor.save()
    assert window.word_wrap_action.isChecked()
    assert window.config_combo.currentText() == "a" and window.compiler_edit.text() == "ifx"


def test_switching_projects_without_options_leaves_collapsed_panels(qtbot, make_window, project, conflict_project):
    from qtpy.QtCore import QPoint
    from test_dock_snap import collapsed, drag, ready

    window = make_window(project / "fpm.toml")
    ready(qtbot, window)
    files = window.docks["files"]
    y, edge = files.geometry().center().y(), files.geometry().right() + 3
    drag(qtbot, window, QPoint(edge, y), QPoint(edge - 100, y), QPoint(5, y))
    assert collapsed(window, "files")
    window.open_manifest(conflict_project / "fpm.toml")
    assert collapsed(window, "files")


@pytest.mark.parametrize("key", ["options", "new_configuration"])
def test_options_that_cant_be_read_say_why(make_window, configured, key):
    data = json.loads((configured / bc.FILE_NAME).read_text())
    data[key] = ["not", "an", "object"]
    text = json.dumps(data)
    (configured / bc.FILE_NAME).write_text(text)
    window = make_window(configured / "fpm.toml")
    assert f'"{key}" isn\'t an object' in window.config_combo.toolTip()
    window.close()
    assert (configured / bc.FILE_NAME).read_text() == text  # (not written over)


# ----------------------------------------------------------------- found in the second review
def test_saving_options_in_the_editor_keeps_the_windows_other_changes(make_window, configured):
    """Only what was changed in the file is taken: the panels' layout and
    the View menu's choices changed since are kept."""
    make_window(configured / "fpm.toml").close()  # (the file has the options now, the layout among them)
    window = make_window(configured / "fpm.toml")
    window.docks["outputs"].hide()
    window.minimap_action.setChecked(False)
    editor = open_editor(window, configured / bc.FILE_NAME)
    data = json.loads(editor.editor.toPlainText())
    data["options"]["word_wrap"] = True
    editor.editor.setPlainText(json.dumps(data, indent=2))
    editor.save()
    assert window.word_wrap_action.isChecked()
    assert window.docks["outputs"].isHidden() and not window.minimap_action.isChecked()
    window.close()
    options = file_options(configured)
    assert options["word_wrap"] is True and options["minimap"] is False


def test_a_project_without_a_file_gets_its_own_new_configuration(make_window, tmp_path):
    """Opened after one with a file, while a saved configuration was shown:
    its New configuration's features were read from the other project's key."""
    from vibefpmgui.window.options import project_key

    toml = 'name = "{}"\n[features]\nmpi.flags = "-DMPI"\n'
    a = write_files(tmp_path / "a", {"fpm.toml": toml.format("a"),
                                     bc.FILE_NAME: json.dumps({"configurations": [{"name": "c"}]})})
    b = write_files(tmp_path / "b", {"fpm.toml": toml.format("b")})
    window = make_window(a / "fpm.toml")
    window.settings.app.setValue(project_key((a / "fpm.toml").resolve(), "features"), ["mpi"])  # (a's, from before)
    window.select_configuration("c")
    window.open_manifest(b / "fpm.toml")
    assert window.config_combo.currentIndex() == 0 and window.selected_features == []


def test_a_reload_reads_the_configurations_but_leaves_the_options_to_the_window(make_window, configured):
    put(configured, {}, {"compiler": "mine"})
    window = make_window(configured / "fpm.toml")
    data = file_data(configured)
    data["new_configuration"] = {"compiler": "theirs"}
    data["configurations"].append({"name": "added"})
    (configured / bc.FILE_NAME).write_text(json.dumps(data))  # (changed outside the app: a git pull)
    window.reload()  # (as after a build)
    assert window.config_combo.findText("added") > 0
    assert window.compiler_edit.text() == "mine" and window._saved_free_options().compiler == "mine"


def test_a_configuration_named_like_new_configuration(make_window, configured):
    from vibefpmgui.window.configs import NEW_CONFIGURATION

    bc.save(configured / bc.FILE_NAME, bc.Configuration(NEW_CONFIGURATION, compiler="ifx"))
    window = make_window(configured / "fpm.toml")
    window.select_configuration(NEW_CONFIGURATION)
    assert window.config_combo.currentIndex() > 0 and window.compiler_edit.text() == "ifx"


def test_a_file_broken_while_open_says_its_options_arent_saved(make_window, configured):
    window = make_window(configured / "fpm.toml")
    (configured / bc.FILE_NAME).write_text("{oops")
    window.reload()
    tip = window.config_combo.toolTip()
    assert "isn't valid JSON" in tip and "options aren't saved" in tip
    window.close()
    assert (configured / bc.FILE_NAME).read_text() == "{oops"


def test_a_file_fixed_in_the_editor_has_its_options_used(make_window, configured):
    (configured / bc.FILE_NAME).write_text('{"configurations": [], "options": []}')  # (broken)
    window = make_window(configured / "fpm.toml")
    assert not window.options_in_use()
    editor = open_editor(window, configured / bc.FILE_NAME)
    editor.editor.setPlainText('{"configurations": [{"name": "a"}], "options": {"word_wrap": true}}')
    editor.save()
    assert window.options_in_use() and window.word_wrap_action.isChecked()
    assert window.config_combo.findText("a") > 0


# ----------------------------------------------------------------- found in the third review
@pytest.fixture
def editing_then_blocked(make_window, project, conflict_project, monkeypatch):
    """Configuration "a" being edited (compiler changed), and a file with
    unsaved changes whose question is answered Cancel: opening another
    project's configuration file asks about "a" (answered Discard), then
    stops at the file."""
    from qtpy.QtWidgets import QMessageBox

    bc.save(project / bc.FILE_NAME, bc.Configuration("a", compiler="ifx"))
    other = conflict_project / "other.json"
    other.write_text(json.dumps({"configurations": [{"name": "o"}]}))
    window = make_window(project / "fpm.toml")
    window.select_configuration("a")
    window.edit_configuration()
    window.compiler_edit.setText("edited")
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.insertPlainText("! not saved\n")  # (so it's asked about)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Discard)
    monkeypatch.setattr(type(editor), "maybe_save", lambda self: False)
    window.open_configuration(other)
    return window, other


def test_discard_puts_the_configuration_back_as_saved(editing_then_blocked, project):
    window, _other = editing_then_blocked
    assert window.manifest_path.parent == project.resolve()  # (not opened: the file's Cancel)
    assert window.config_combo.currentText() == "a" and window.compiler_edit.text() == "ifx"
    assert not window.compiler_edit.isEnabled() and not window.has_unsaved_configuration()
    assert window.build_arguments(["build"], True) == ["build", "--compiler", "ifx"]


def test_an_open_that_didnt_happen_isnt_a_recent_configuration(editing_then_blocked):
    window, other = editing_then_blocked
    assert str(other.resolve()) not in window.recent_configs()


def test_the_configuration_chosen_changed_in_the_editor_is_chosen(make_window, configured):
    bc.save(configured / bc.FILE_NAME, bc.Configuration("b", compiler="nvfortran"))
    window = make_window(configured / "fpm.toml")
    window.select_configuration("a")
    window.close()
    window = make_window(configured / "fpm.toml")
    assert window.config_combo.currentText() == "a"
    editor = open_editor(window, configured / bc.FILE_NAME)
    data = json.loads(editor.editor.toPlainText())
    data["options"]["build_configuration"] = "b"
    editor.editor.setPlainText(json.dumps(data, indent=2))
    editor.save()
    assert window.config_combo.currentText() == "b" and window.compiler_edit.text() == "nvfortran"


# ----------------------------------------------------------------- the look: the person's, not the project's
def test_the_look_isnt_the_projects(make_window, configured, conflict_project):
    """Theme, window style, syntax colors, the editor's font and the text
    size are the app's settings: the same in every project, never in a file."""
    from vibefpmgui import themes

    put(configured, {"app_theme": "dark", "font_delta": 4, "word_wrap": True})  # (an older file had them)
    window = make_window(configured / "fpm.toml")
    assert window.word_wrap_action.isChecked()  # (an option: the file's)
    assert themes.current != "dark" and window.font_delta == 0  # (not the file's: the app's)
    window.set_font_delta(2, announce=False)
    window.set_syntax_style("monokai")
    window.open_manifest(conflict_project / "fpm.toml")  # (no configuration file)
    assert window.font_delta == 2 and window.settings.app.value("syntax_style") == "monokai"
    window.open_manifest(configured / "fpm.toml")
    assert window.font_delta == 2
    window.close()
    options = file_options(configured)
    assert not {"app_theme", "app_style", "syntax_style", "editor_font_family", "editor_font_size",
                "font_delta"} & set(options)
    assert window.settings.app.value("font_delta", type=int) == 2
