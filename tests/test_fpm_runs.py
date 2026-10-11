"""Build, Test and Run with the real fpm and gfortran (on a small project
without git dependencies: no network needed)."""

from __future__ import annotations

import shutil

import pytest
from conftest import write_files

from vibefpmgui.build_monitor import DONE, FAILED, WARNING
from vibefpmgui.test_monitor import FAILED_T, NOT_RUN, PASSED

pytestmark = pytest.mark.skipif(not (shutil.which("fpm") and shutil.which("gfortran")),
                                reason="fpm and gfortran are needed")

CALC = """\
module calc
  implicit none
contains
  pure integer function add(a, b)
    integer, intent(in) :: a, b
    integer :: unused
    add = a + b
  end function add
end module calc
"""


@pytest.fixture
def fpm_project(tmp_path):
    return write_files(tmp_path / "calc", {
        "fpm.toml": 'name = "calc"\n[build]\nauto-executables = true\nauto-tests = true\n',
        "src/calc.f90": CALC,
        "app/main.f90": "program main\n  use calc\n  print '(a,i0)', 'sum = ', add(1, 2)\nend program main\n",
        "test/check_add.f90": "program check_add\n  use calc\n  if (add(1, 2) /= 3) error stop 1\n"
                              "  print '(a)', 'add works'\nend program check_add\n",
        "test/check_fail.f90": "program check_fail\n  print '(a)', 'about to fail'\n  error stop 3\n"
                               "end program check_fail\n",
    })


@pytest.fixture
def fpm_window(make_window, fpm_project):
    window = make_window(fpm_project / "fpm.toml")
    window.compiler_edit.setText("gfortran")
    return window


def wait_idle(qtbot, window, timeout=120_000):
    """Until fpm (and a test/run's second step) is done."""
    qtbot.waitUntil(lambda: window.process is None and getattr(window, "_phase", None) is None, timeout=timeout)


def console(window) -> str:
    return window.console.toPlainText()


def targets(window) -> dict[str, str]:
    return {t.name: t.status for t in window.build_monitor.targets}


def test_build(qtbot, fpm_window):
    fpm_window.run_fpm(["build"], True, True)
    wait_idle(qtbot, fpm_window)
    assert "exited with code 0" in console(fpm_window)
    statuses = targets(fpm_window)
    assert any("calc.f90" in name for name in statuses)
    calc = next(t for t in fpm_window.build_monitor.targets if "calc.f90" in t.name)
    assert calc.status in (WARNING, DONE)  # (the unused variable: a warning, if fpm's flags ask for it)
    if calc.status == WARNING:
        assert any(d.line == 6 for d in calc.diagnostics)
    assert fpm_window.statusBar().currentMessage().startswith(("Finished successfully", "Finished"))


def test_build_error(qtbot, fpm_window, fpm_project):
    (fpm_project / "src/calc.f90").write_text(CALC.replace("add = a + b", "add = a + undefined_name"))
    fpm_window.run_fpm(["build"], True, True)
    wait_idle(qtbot, fpm_window)
    calc = next(t for t in fpm_window.build_monitor.targets if "calc.f90" in t.name)
    assert calc.status == FAILED
    assert any(d.line == 7 and d.severity.startswith("error") for d in calc.diagnostics)
    assert fpm_window.monitor_tabs.currentWidget() is fpm_window.build_monitor
    # Opening it from the Build tab shows the error in the editor.
    fpm_window.show_build_details(calc.row)
    fpm_window.build_monitor.open_file_requested.emit(str(calc.path), calc.diagnostics)
    editor = fpm_window.current_editor()
    assert editor is not None and editor.path.name == "calc.f90"
    assert 7 in editor.editor.markers


def test_tests_tab(qtbot, fpm_window):
    fpm_window.run_tests()
    wait_idle(qtbot, fpm_window)
    monitor = fpm_window.test_monitor
    statuses = {t.name: t.status for t in monitor.tests}
    assert statuses == {"check_add": PASSED, "check_fail": FAILED_T}
    failing = monitor.by_name["check_fail"]
    assert failing.exit_code != 0 and any("about to fail" in line for line in failing.output)
    fpm_window.show_test_output("check_add")
    assert "add works" in fpm_window.test_output.toPlainText()
    assert monitor.durations()  # (remembered for the next run's estimates)


def test_tests_with_a_target(qtbot, fpm_window):
    fpm_window.target_edit.setText("check_add")
    fpm_window.run_tests()
    wait_idle(qtbot, fpm_window)
    assert [(t.name, t.status) for t in fpm_window.test_monitor.tests] == [("check_add", PASSED)]


def test_run_tab(qtbot, fpm_window):
    fpm_window.run_programs()
    wait_idle(qtbot, fpm_window)
    runs = fpm_window.run_monitor.tests
    assert [(t.name, t.status) for t in runs] == [("calc", PASSED)]
    assert any("sum = 3" in line for line in runs[0].output)


def test_stop(qtbot, fpm_window, fpm_project):
    (fpm_project / "test/check_slow.f90").write_text(
        "program check_slow\n  call execute_command_line('sleep 30')\nend program check_slow\n")
    fpm_window.target_edit.setText("check_slow")
    fpm_window.run_tests()
    monitor = fpm_window.test_monitor
    qtbot.waitUntil(lambda: any(t.status == "Running" for t in monitor.tests), timeout=120_000)
    fpm_window.stop()
    wait_idle(qtbot, fpm_window, timeout=30_000)
    assert monitor.tests[0].status in (NOT_RUN, FAILED_T)
    assert "Stopped" in fpm_window.statusBar().currentMessage() or "stopped" in console(fpm_window)


def test_clean(qtbot, fpm_window, fpm_project, monkeypatch):
    from qtpy.QtWidgets import QMessageBox

    fpm_window.run_fpm(["build"], True, True)
    wait_idle(qtbot, fpm_window)
    assert (fpm_project / "build").is_dir()
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    fpm_window.run_fpm(["clean", "--skip"], False, False)
    wait_idle(qtbot, fpm_window)
    assert not any((fpm_project / "build").glob("gfortran_*"))


# ------------------------------------------------------- after a build
@pytest.fixture
def built(qtbot, fpm_window):
    fpm_window.run_fpm(["build"], True, True)
    wait_idle(qtbot, fpm_window)
    return fpm_window


def test_compile_command(built, fpm_project, monkeypatch):
    from qtpy.QtWidgets import QDialog, QMessageBox, QPlainTextEdit

    shown = []
    monkeypatch.setattr(QDialog, "exec", lambda self: shown.append(
        self.findChild(QPlainTextEdit).toPlainText()) or 0)
    built.show_compile_command(str(fpm_project / "src/calc.f90"))
    assert "gfortran" in shown[0] and "calc.f90" in shown[0]
    info = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: info.append(a[2]))
    built.show_compile_command(str(fpm_project / "src/not_built.f90"))
    assert "No compile command" in info[0]


def test_build_outputs_and_running_a_program(qtbot, built, monkeypatch):
    from qtpy.QtWidgets import QMessageBox

    built.tabs.setCurrentWidget(built.outputs_view)  # (refreshes the tab)
    configs = built.outputs_view.configs
    assert configs and configs[0].programs
    kind, program = next(p for p in configs[0].programs if p[1].name.startswith("calc"))
    assert kind == "program"
    built.run_program(str(program))  # (the built program, directly: no fpm)
    qtbot.waitUntil(lambda: built.process is None and "sum = 3" in console(built), timeout=30_000)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    built.delete_configurations(configs)
    built.refresh_build_outputs()
    assert not built.outputs_view.configs


def test_fpm_missing(qtbot, fpm_window):
    fpm_window.fpm_edit.setText("/no/such/fpm")
    fpm_window.run_fpm(["build"], True, True)
    qtbot.waitUntil(lambda: fpm_window.process is None, timeout=10_000)
    assert "Could not start '/no/such/fpm'" in console(fpm_window)


def test_build_options_become_arguments(fpm_window):
    fpm_window.flag_edit.setText("-O0 -g")
    fpm_window.extra_edit.setText("--verbose")
    fpm_window.runner_args_edit.setText("one 'two words'")
    args = fpm_window.build_arguments(["run"], True)
    assert args[:1] == ["run"] and ["--compiler", "gfortran"] == args[args.index("--compiler"):][:2]
    assert ["--flag", "-O0 -g"] == args[args.index("--flag"):][:2]
    assert args[-3:] == ["--", "one", "two words"]
    assert fpm_window.build_arguments(["update"], False) == ["update"]
