"""Commands → Build Tests: ``fpm build --tests`` (fpm's plain build leaves
the tests out), with the Build options; the tests aren't run."""

from __future__ import annotations

import shutil

import pytest
from conftest import write_files


def build_tests_action(window):
    return next(a for a in window.command_actions if a.text() == "Build Tests")


def test_it_s_beside_build_in_the_toolbar_and_the_menu(window):
    action = build_tests_action(window)
    texts = [a.text() for a in window.command_toolbar.actions()]
    assert texts[texts.index("Build") + 1] == "Build Tests"
    assert action.shortcut().toString() == "Ctrl+Shift+B"


def test_it_builds_with_the_build_options(window, monkeypatch):
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, sub, *a, **k: ran.append(sub))
    build_tests_action(window).trigger()
    assert ran == [["build", "--tests"]]
    window.profile_combo.setCurrentText("release")
    window.target_edit.setText("check_one")
    window.runner_args_edit.setText("-n 3")
    args = window.build_arguments(["build", "--tests"], True)
    assert args[:4] == ["build", "--tests", "--profile", "release"]
    assert "check_one" not in args and "--" not in args  # (Target and Program args: for running)


@pytest.mark.skipif(not (shutil.which("fpm") and shutil.which("gfortran")), reason="fpm and gfortran are needed")
def test_the_tests_are_built_not_run(make_window, tmp_path, qtbot):
    root = write_files(tmp_path / "bt", {
        "fpm.toml": 'name = "bt"\n',
        "src/bt.f90": "module bt\ncontains\n  integer function one()\n    one = 1\n  end function one\nend module bt\n",
        "test/check_one.f90": "program check_one\n  use bt\n  print '(a)', 'check_one ran'\n"
                              "  if (one() /= 1) error stop 1\nend program check_one\n"})
    window = make_window(root / "fpm.toml")
    window.compiler_edit.setText("gfortran")

    def built_tests():
        return [p.name for p in (root / "build").glob("gfortran_*/test/check_one*")]

    def run(action):
        action.trigger()
        qtbot.waitUntil(lambda: not window.busy(), timeout=120_000)

    run(next(a for a in window.command_actions if a.text() == "Build"))
    assert not built_tests()  # (fpm's plain build: no tests)
    run(build_tests_action(window))
    console = window.console.toPlainText()
    assert "build --tests" in console and "exited with code 0" in console
    assert built_tests() and "check_one ran" not in console  # (built, not run)
    assert not window.test_monitor.tests
