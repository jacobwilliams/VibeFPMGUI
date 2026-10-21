"""Valgrind, from the Build outputs panel: a program, test or example built
by fpm with the Build options and run under Valgrind's memory check
(``--runner``). The real thing runs where Valgrind is found (Linux: the pixi
environment, CI, the Docker image), the rest anywhere."""

from __future__ import annotations

import shutil
import subprocess

import pytest
from conftest import write_files
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QMessageBox, QToolButton

from vibefpmgui import build_outputs as bo
from vibefpmgui.fpm_args import VALGRIND_RUNNER, with_runner


@pytest.fixture
def card(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(bo, "valgrind_found", lambda: True)
    folder = tmp_path / "build" / "gfortran_0123456789ABCDEF"
    (folder / "test").mkdir(parents=True)
    program = folder / "test" / "check_leaks"
    program.write_text("")
    config = bo.Configuration("debug · gfortran", [bo.BuildFolder(folder, "gfortran",
                                                                  programs=[("test", program)])], True, "")
    card = bo.ConfigCard(config, bo.PALETTES["light"])
    qtbot.addWidget(card)
    card.show()
    qtbot.waitExposed(card)
    return card


def test_a_valgrind_button_beside_debug(qtbot, card):
    buttons = {b.text(): b for b in card.findChildren(QToolButton)}
    program_buttons = [text for text in buttons if text in ("Run", "Debug", "Valgrind")]
    assert program_buttons == ["Run", "Debug", "Valgrind"] and buttons["Valgrind"].isEnabled()
    for text in program_buttons:  # (icons, their tooltips saying what they do)
        assert buttons[text].toolButtonStyle() == Qt.ToolButtonIconOnly and not buttons[text].icon().isNull()
        assert buttons[text].toolTip()
    assert buttons["Valgrind"].toolTip().startswith("Valgrind: check")
    with qtbot.waitSignal(card.valgrind_requested) as blocker:
        buttons["Valgrind"].click()
    assert blocker.args == ["test", "check_leaks"]


def test_and_in_the_right_click_menu(qtbot, card):
    label = next(w for w, (k, _) in card.rows.items() if k == "test")
    menu = {a.text(): a for a in card.context_menu(label.geometry().center()).actions() if a.text()}
    with qtbot.waitSignal(card.valgrind_requested) as blocker:
        menu["Run with Valgrind"].trigger()
    assert blocker.args == ["test", "check_leaks"]


def test_without_valgrind_the_button_says_so(qtbot, tmp_path, monkeypatch):
    monkeypatch.setattr(bo, "valgrind_found", lambda: False)  # (macOS, Windows)
    program = tmp_path / "demo"
    program.write_text("")
    card = bo.ConfigCard(bo.Configuration("x", [bo.BuildFolder(tmp_path, "gfortran",
                                                               programs=[("program", program)])], True, ""),
                         bo.PALETTES["light"])
    qtbot.addWidget(card)
    button = next(b for b in card.findChildren(QToolButton) if b.text() == "Valgrind")
    assert not button.isEnabled() and "wasn't found" in button.toolTip()


def test_the_runner_goes_before_the_program_arguments():
    assert with_runner(["run", "demo", "--", "-n", "3"], "valgrind -q") == [
        "run", "demo", "--runner", "valgrind -q", "--", "-n", "3"]
    assert with_runner(["test", "check"], "valgrind") == ["test", "check", "--runner", "valgrind"]


# ----------------------------------------------------------------- the window
@pytest.fixture
def fpm_runs(window, monkeypatch):
    """What run_valgrind asked fpm to do (nothing runs)."""
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: f"/usr/bin/{name}")
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, sub, *a, **k: ran.append((sub, k["phase"], k["args"])))
    return ran


@pytest.mark.parametrize("kind, first", [("program", ["run"]), ("test", ["test"]),
                                         ("example", ["run", "--example"])])
def test_fpm_runs_it_under_valgrind(window, fpm_runs, kind, first):
    window.runner_args_edit.setText("-n 3")
    window.target_edit.setText("other")
    window.outputs_view.valgrind_requested.emit(kind, "demo")
    (sub, phase, args), = fpm_runs
    assert sub == ["run" if kind != "test" else "test"] and phase == "valgrind"
    assert [a for a in args if a not in ("--profile", "debug")][:len(first)] == first
    assert args[1:3] == ["--profile", "debug"]  # (no profile chosen: debug information, for files and lines)
    assert "demo" in args and "other" not in args  # (that one, not the Target's)
    assert args[args.index("--runner") + 1] == VALGRIND_RUNNER
    assert args.index("--runner") < args.index("--") and args[-2:] == ["-n", "3"]


def test_a_chosen_profile_is_kept(window, fpm_runs):
    window.profile_combo.setCurrentText("release")
    window.run_valgrind("program", "demo")
    (_sub, _phase, args), = fpm_runs
    assert args.count("--profile") == 1 and args[args.index("--profile") + 1] == "release"


def test_without_valgrind_nothing_runs(window, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name, *a, **k: None)
    said = []
    monkeypatch.setattr(QMessageBox, "information", lambda *a: said.append(a[2]))
    ran = []
    monkeypatch.setattr(type(window), "run_fpm", lambda self, *a, **k: ran.append(a))
    window.run_valgrind("program", "demo")
    assert not ran and said == [bo.NO_VALGRIND]


SUMMARY = "==371== ERROR SUMMARY: {} errors from 1 contexts (suppressed: 0 from 0)\n"


def test_valgrind_s_summaries_are_read():
    assert bo.valgrind_errors("built\n" + SUMMARY.format(0)) == 0
    assert bo.valgrind_errors(SUMMARY.format(2) + SUMMARY.format("1,000")) == 1002  # (one per program)
    assert bo.valgrind_errors("<ERROR> compilation failed\nSTOP 1\n") is None  # (it didn't run)


@pytest.mark.parametrize("output, code, says", [
    (SUMMARY.format(0), 0, "Valgrind: no errors in demo"),
    (SUMMARY.format(3), 1, "Valgrind found 3 errors in demo"),
    (SUMMARY.format(0), 2, "Valgrind: no memory errors in demo, but it failed (exit code 2)"),
    ("<ERROR> compilation failed\n", 1, "demo wasn't run under Valgrind"),  # (fpm's 1, but not Valgrind's)
])
def test_what_it_found_is_said(window, output, code, says):
    window._valgrind_name, window._captured = "demo", [output]
    window._after_valgrind(code, False)
    assert window.statusBar().currentMessage().startswith(says)
    assert says in window.console.toPlainText()


# ----------------------------------------------------------------- the real thing
CLEAN = """\
program clean
  implicit none
  integer, allocatable :: a(:)
  allocate(a(10))
  a = 1
  print *, sum(a)
  deallocate(a)
end program clean
"""

LEAKY = """\
program leaky
  implicit none
  integer, pointer :: p(:)
  allocate(p(1000))
  p = 2
  print *, sum(p)
  nullify(p)  ! (the memory's lost: a leak)
end program leaky
"""


def valgrind_starts() -> bool:
    """Whether Valgrind can run a program here: on Linux it needs the C
    library's debug symbols (libc6-dbg on Debian and Ubuntu), else it stops
    at startup ("Fatal error at startup: a function redirection…")."""
    valgrind, true = shutil.which("valgrind"), shutil.which("true")
    if not (valgrind and true):
        return False
    done = subprocess.run([valgrind, "-q", "--error-exitcode=99", true], capture_output=True, text=True,
                          timeout=120, check=False)
    return done.returncode == 0


@pytest.mark.skipif(not (shutil.which("fpm") and shutil.which("gfortran") and valgrind_starts()),
                    reason="needs fpm, gfortran and a Valgrind that starts (Linux, with libc6-dbg)")
@pytest.mark.parametrize("name, code, says", [("clean", 0, "Valgrind: no errors in clean"),
                                              ("leaky", 1, "Valgrind found 1 error in leaky")])
def test_valgrind_finds_a_leak(make_window, tmp_path, qtbot, name, code, says):
    root = write_files(tmp_path / "vg", {"fpm.toml": 'name = "vg"\n', "test/clean.f90": CLEAN, "test/leaky.f90": LEAKY,
                                         "app/main.f90": "program main\nend program main\n"})  # (fpm: tests alone aren't a project)
    window = make_window(root / "fpm.toml")
    finished = []
    window.statusBar().messageChanged.connect(
        lambda text: text.startswith(("Valgrind", name)) and finished.append(text))
    window.run_valgrind("test", name)
    qtbot.waitUntil(lambda: bool(finished) and window.process is None, timeout=180_000)  # (fpm builds first)
    console = window.console.toPlainText()
    assert finished[-1].startswith(says), console
    assert "valgrind" in console and "ERROR SUMMARY" in console
    assert ("definitely lost" in console) == (code == 1)
