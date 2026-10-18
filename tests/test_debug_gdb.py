"""Debugging with the real thing: an fpm project built with gfortran, run
under gdb (≥ 14, its DAP interface), driven by the window as a user would.
Skipped where gdb, fpm or gfortran isn't found (macOS has no gdb: see
plan/debugger.md); it runs on Linux, CI and the Docker image (docker/)."""

from __future__ import annotations

import shutil

import pytest
from conftest import open_editor, write_files

from vibefpmgui import debug_adapters
from vibefpmgui.debug_session import IDLE, STOPPED

pytestmark = pytest.mark.skipif(
    not (shutil.which("fpm") and shutil.which("gfortran") and debug_adapters.gdb_adapter()),
    reason="needs fpm, gfortran and gdb 14 or newer (Linux)")

SHAPES = """\
module shapes
  implicit none
  integer :: counter = 0
  type :: point
    real :: x, y
  end type point
contains
  subroutine bump(p, a, n)
    type(point), intent(inout) :: p
    real, intent(inout) :: a(:)
    integer, intent(in) :: n
    integer :: i
    do i = 1, n
      a(i) = a(i) + p%x
    end do
    p%y = p%y + 1.0
    counter = counter + 1
  end subroutine bump
end module shapes
"""

MAIN = """\
program main
  use shapes
  implicit none
  type(point) :: p
  real :: a(5)
  logical :: ok
  p = point(2.0, 3.0)
  a = [1.0, 2.0, 3.0, 4.0, 5.0]
  ok = .true.
  call bump(p, a, 3)
  print *, "sum", sum(a), counter
end program main
"""
CALL_LINE = 10  # (call bump(p, a, 3))


@pytest.fixture
def debugged(make_window, tmp_path, qtbot, monkeypatch):
    """A project stopped at its breakpoint, under gdb."""
    monkeypatch.setattr(debug_adapters, "_found", {})  # (looked for here: gdb)
    root = write_files(tmp_path / "dbg", {"fpm.toml": 'name = "dbg"\n', "src/shapes.f90": SHAPES,
                                          "app/main.f90": MAIN})
    window = make_window(root / "fpm.toml")
    open_editor(window, root / "app/main.f90").editor.toggle_breakpoint(CALL_LINE)
    window.start_debugging()
    qtbot.waitUntil(lambda: window.debug_session.state == STOPPED, timeout=180_000)  # (fpm builds first)
    qtbot.waitUntil(lambda: bool(window.debug_panel.variable_rows()), timeout=20_000)
    yield window, root
    window.debug_session.stop()


def test_gdb_stops_at_the_breakpoint_with_the_variables(debugged, qtbot):
    window, root = debugged
    assert window.debug_session.adapter.kind == "gdb"
    editor = window.current_editor()
    assert editor.path == (root / "app/main.f90").resolve() and editor.editor.execution_line == CALL_LINE
    assert window.debug_panel.stack.item(0).text().startswith("main")
    rows = dict(window.debug_panel.variable_rows())
    assert rows.get("ok", "").lower() == ".true."
    # The array's elements and the derived type's components, as they're expanded.
    panel = window.debug_panel
    locals_item = panel.variables.topLevelItem(0)
    for name in ("a", "p"):
        item = next(locals_item.child(i) for i in range(locals_item.childCount()) if locals_item.child(i).text(0) == name)
        item.setExpanded(True)
    qtbot.waitUntil(lambda: ("2", "2") in panel.variable_rows() and ("x", "2") in panel.variable_rows(),
                    timeout=20_000)


def test_gdb_steps_into_a_module_procedure_and_runs_to_the_end(debugged, qtbot):
    window, root = debugged
    window.step_in_action.trigger()
    qtbot.waitUntil(lambda: window.current_editor() is not None
                    and window.current_editor().path == (root / "src/shapes.f90").resolve()
                    and window.current_editor().editor.execution_line is not None, timeout=20_000)
    qtbot.waitUntil(lambda: ("n", "3") in window.debug_panel.variable_rows(), timeout=20_000)  # (an argument)
    evaluated = []
    window.debug_session.evaluate("counter", lambda ok, text, ref: evaluated.append((ok, text)))
    qtbot.waitUntil(lambda: bool(evaluated), timeout=20_000)
    assert evaluated[0] == (True, "0")  # (a module variable)
    window.debug_start_action.trigger()  # (Continue: no more breakpoints)
    qtbot.waitUntil(lambda: window.debug_session.state == IDLE, timeout=30_000)
    console = window.debug_panel.console.toPlainText()
    assert "sum" in console and "exited with code 0" in console
