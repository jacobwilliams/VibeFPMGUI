"""Test coverage: reading lcov's tracefiles, the Coverage panel, the editor's
gutter bars, and (with fpm, gfortran and lcov installed) a real run."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from conftest import open_editor, write_files
from qtpy.QtCore import Qt
from qtpy.QtGui import QTextCursor

from vibefpmgui import coverage as cov
from vibefpmgui.coverage_panel import CoveragePanel, function_name, shown_functions
from vibefpmgui.main_window import with_link_flag

LCOV2 = """\
SF:{root}/src/calc.f90
FNL:0,4,7
FNA:0,1,__calc_MOD_add
FNL:1,8,15
FNA:1,0,__calc_MOD_never_called
FNF:2
FNH:1
BRDA:10,0,0,-
BRDA:10,0,1,-
DA:4,1
DA:6,1
DA:7,1
DA:8,0
DA:10,0
DA:11,0
DA:13,0
DA:15,0
LF:8
LH:3
end_of_record
SF:{root}/test/check.f90
FNL:0,1,6
FNA:0,1,MAIN__
FNL:1,2,2
FNA:1,1,main
DA:1,1
DA:4,1
DA:6,1
end_of_record
SF:{root}/build/dependencies/lib/src/lib.f90
DA:1,0
DA:2,0
end_of_record
"""


def tracefile(root: Path) -> dict[Path, cov.FileCoverage]:
    return cov.parse_info(LCOV2.format(root=root))


# ------------------------------------------------------------ the module
def test_parse_lcov2(tmp_path):
    files = tracefile(tmp_path)
    calc = files[(tmp_path / "src/calc.f90").resolve()]
    assert (calc.lines_hit, calc.lines_found, round(calc.percent, 1)) == (3, 8, 37.5)
    assert [(f.name, f.line, f.end_line, f.hits) for f in calc.functions] == [
        ("__calc_MOD_add", 4, 7, 1), ("__calc_MOD_never_called", 8, 15, 0)]
    assert (calc.branches_hit, calc.branches_found) == (0, 2)
    assert calc.missed() == [(8, 15)]  # (lines without code in between don't break a range)
    assert cov.ranges_text([(8, 15), (20, 20)]) == "8–15, 20"


def test_parse_the_old_format_and_add_up_records(tmp_path):
    text = (f"SF:{tmp_path}/a.f90\nFN:3,s\nFNDA:2,s\nDA:3,2\nDA:4,0\nend_of_record\n"
            f"SF:{tmp_path}/a.f90\nFN:3,s\nFNDA:1,s\nDA:3,1\nDA:4,5\nend_of_record\n")
    (a,) = cov.parse_info(text).values()
    assert a.lines == {3: 3, 4: 5}  # (two test programs ran it: counts added)
    assert [(f.name, f.hits) for f in a.functions] == [("s", 3)]


def test_totals_and_function_names(tmp_path):
    files = list(tracefile(tmp_path).values())
    assert cov.totals(files)["lines"] == (6, 13)
    assert function_name("__calc_MOD_add") == "add (module calc)"
    assert function_name("MAIN__") == "(main program)"
    check = next(f for f in files if f.path.name == "check.f90")
    assert [f.name for f in shown_functions(check)] == ["MAIN__"]  # (gfortran's own main: left out)


def test_find_gcov_next_to_the_compiler(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("x86_64-linux-gfortran", "x86_64-linux-gcov"):
        (bin_dir / name).write_text("")
        (bin_dir / name).chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    assert cov.find_gcov("x86_64-linux-gfortran") == str((bin_dir / "x86_64-linux-gcov").resolve())


def test_clear_counts(tmp_path):
    write_files(tmp_path, {"build/gfortran_1/p/a.gcda": b"x", "build/gfortran_1/p/a.gcno": b"y"})
    assert cov.clear_counts(tmp_path / "build") == 1
    assert not (tmp_path / "build/gfortran_1/p/a.gcda").exists() and (tmp_path / "build/gfortran_1/p/a.gcno").exists()


def test_the_row_tooltip_lists_every_missed_line(tmp_path):
    from vibefpmgui.coverage_panel import row_tooltip

    path = tmp_path / "a.f90"
    lines = {n: 0 for i in range(1, 31) for n in (i * 10, i * 10 + 1, i * 10 + 2)}
    lines |= {i * 10 + 5: 1 for i in range(1, 31)}  # (a line that ran between each: 30 ranges)
    f = cov.FileCoverage(path, lines)
    assert cov.ranges_text(f.missed()).endswith("…")  # (more than the first ranges)
    tip = row_tooltip(f)
    assert str(path) in tip and "300–302" in tip and "…" not in tip and "90 in 30 ranges" in tip
    assert "Every line with code ran" in row_tooltip(cov.FileCoverage(path, {5: 1}))


def test_procedure_kind():
    lines = ["program main", "  integer function add(a, b)  ! adds", "  pure real(wp) function f(x) result(y)",
             "  recursive subroutine walk(n)", "  end subroutine walk", "  ! subroutine in a comment",
             "  module procedure solve"]
    assert [cov.procedure_kind(lines, n) for n in range(1, 8)] == [
        "program", "function", "function", "subroutine", None, None, None]
    assert cov.procedure_kind(lines, 99) is None


def test_the_functions_get_the_outlines_badges(qtbot, tmp_path):
    from vibefpmgui.icons import symbol_icon

    source = tmp_path / "src/calc.f90"
    source.parent.mkdir()
    source.write_text("module calc\ncontains\n  subroutine s()\n  end subroutine s\n"
                      "  integer function add(a, b)\n    integer, intent(in) :: a, b\n    add = a + b\n"
                      "  end function add\nend module calc\n")
    f = cov.FileCoverage(source.resolve(), {3: 1, 5: 2, 7: 2}, [
        cov.FunctionCoverage("__calc_MOD_s", 3, 4, 1), cov.FunctionCoverage("__calc_MOD_add", 5, 8, 2)])
    panel = CoveragePanel()
    qtbot.addWidget(panel)
    panel.show_report({f.path: f}, tmp_path)
    panel.table.selectRow(0)
    kinds = [panel.functions.item(r, 0).toolTip() for r in range(panel.functions.rowCount())]
    assert kinds == ["subroutine", "function"]
    assert panel.functions.item(0, 0).icon().cacheKey() == symbol_icon("subroutine").cacheKey()


def test_with_link_flag():
    assert with_link_flag([], "--coverage") == ["--link-flag", "--coverage"]
    assert with_link_flag(["--link-flag", "-lm"], "--coverage") == ["--link-flag", "-lm --coverage"]
    assert with_link_flag(["--link-flag=-lm"], "--coverage") == ["--link-flag=-lm --coverage"]


# ------------------------------------------------------------- the panel
def test_the_panel(qtbot, tmp_path):
    panel = CoveragePanel()
    qtbot.addWidget(panel)
    panel.show_report(tracefile(tmp_path), tmp_path, "now · debug · gfortran", [tmp_path / "test"])
    names = [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())]
    assert names == ["src/calc.f90"]  # (no tests or dependencies, unless asked)
    assert "3 of 8" in panel.totals.text()
    panel.tests_check.setChecked(True)
    names = [panel.table.item(r, 0).text() for r in range(panel.table.rowCount())]
    assert names == ["src/calc.f90", "test/check.f90"] and "6 of 11" in panel.totals.text()
    panel.dependencies_check.setChecked(True)
    assert panel.table.rowCount() == 3 and "6 of 13" in panel.totals.text()
    panel.dependencies_check.setChecked(False)
    row = names.index("src/calc.f90")
    panel.table.selectRow(row)
    assert [panel.table.horizontalHeaderItem(c).text() for c in range(panel.table.columnCount())] == [
        "FILE", "COVERAGE", "LINES", "FUNCTIONS"]
    assert panel.table.item(row, 1).text() == "37.5%" and panel.table.item(row, 2).text() == "3 / 8"
    for column in range(4):  # (the whole row's tooltip: its path and missed lines)
        tip = panel.table.item(row, column).toolTip()
        assert "calc.f90" in tip and "8–15" in tip
    assert [panel.functions.item(r, 0).text() for r in range(panel.functions.rowCount())] == [
        "add (module calc)", "never_called (module calc)"]
    opened = []
    panel.open_requested.connect(lambda path, line: opened.append((Path(path).name, line)))
    panel._row_clicked(row, 0)
    assert opened == [("calc.f90", 8)]  # (at its first missed line)
    panel.table.horizontalHeader().setSortIndicator(1, Qt.DescendingOrder)  # (by coverage, highest first)
    assert panel.table.item(0, 0).text() == "test/check.f90"
    panel.show_report(tracefile(tmp_path), tmp_path, "later", [tmp_path / "test"])  # (the user's order kept)
    assert panel.table.item(0, 0).text() == "test/check.f90"
    panel.clear()
    assert panel.table.rowCount() == 0 and panel.empty.isVisibleTo(panel)


# ------------------------------------------------------------ the editor
def test_the_gutter_shows_coverage(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    width = code.gutter_width()
    code.set_coverage({5: 3, 6: 0})
    assert code.gutter_width() > width  # (a column for the bars)
    assert code.coverage_note(5) == "Run 3 times by the tests" and code.coverage_note(6) == "Not run by the tests"
    assert code.coverage_note(1) == ""
    window.coverage_action.trigger()  # (View → Show Coverage in the Editor: off)
    assert code.gutter_width() == width
    window.coverage_action.trigger()
    code.appendPlainText("! an edit")  # (the lines may have moved: the bars go)
    assert code.coverage == {} and code.gutter_width() == width


def test_coverage_flags(window):
    window.coverage_check.setChecked(True)
    window.flag_edit.setText("-O2")
    args = window.build_arguments(["test"], True)
    assert args[args.index("--flag") + 1] == "-O2 --coverage"
    assert args[args.index("--link-flag") + 1] == "--coverage"
    window.coverage_check.setChecked(False)
    assert "--coverage" not in " ".join(window.build_arguments(["test"], True))


def test_include_tests_is_remembered(window, project):
    assert not window.coverage_panel.tests_check.isChecked()  # (off by default)
    window.coverage_panel.tests_check.setChecked(True)
    assert window.settings.value("coverage_tests", type=bool) is True


def test_the_panel_shows_file_icons(window, project):
    path = (project / "src/foo.f90").resolve()
    window.coverage_panel.show_report({path: cov.FileCoverage(path, {5: 1, 6: 0})}, project)
    item = window.coverage_panel.table.item(0, 0)
    assert item.text() == "src/foo.f90" and not item.icon().isNull()  # (Project files' icon for it)


def a_run_now(window, path: Path) -> dict:
    """Coverage of ``path`` from a test run starting now (after the file
    was last written, as when it's compiled)."""
    import time

    window._coverage_started = time.time() + 1
    return {path: cov.FileCoverage(path, {5: 1, 6: 0})}


def test_a_new_editor_gets_the_coverage(window, project):
    path = (project / "src/foo.f90").resolve()
    window._apply_coverage(a_run_now(window, path))
    editor = open_editor(window, project / "src/foo.f90")
    assert editor.editor.coverage == {5: 1, 6: 0}


def test_no_stale_bars_on_a_file_changed_since_the_run(window, project):
    """(Edited, saved, closed and opened again, it got the old bars back,
    on lines that had moved.)"""
    import os

    path = (project / "src/foo.f90").resolve()
    window._apply_coverage(a_run_now(window, path))
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.moveCursor(QTextCursor.Start)
    editor.editor.insertPlainText("! a new first line\n")  # (every line moves down one)
    assert editor.editor.coverage == {}  # (open: dropped at the edit)
    editor.save()
    os.utime(path, (window._coverage_started + 5, window._coverage_started + 5))  # (saved after the run started)
    window.close_editor(window.tabs.indexOf(editor))
    again = open_editor(window, project / "src/foo.f90")
    assert again.editor.coverage == {}  # (not the old bars on moved lines)


def test_no_bars_on_unsaved_changes(window, project):
    path = (project / "src/foo.f90").resolve()
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.appendPlainText("! not saved")  # (not what was compiled)
    window._apply_coverage(a_run_now(window, path))
    assert editor.editor.coverage == {}
    other = open_editor(window, project / "app/main.f90")
    window._apply_coverage(a_run_now(window, path) | a_run_now(window, (project / "app/main.f90").resolve()))
    assert other.editor.coverage == {5: 1, 6: 0} and editor.editor.coverage == {}


# ------------------------------------------------- a real run (fpm, gfortran, lcov)
TOOLS = all(shutil.which(t) for t in ("fpm", "gfortran", "lcov"))


@pytest.mark.skipif(not TOOLS, reason="fpm, gfortran and lcov are needed")
def test_coverage_of_a_real_test_run(qtbot, make_window, tmp_path):
    root = write_files(tmp_path / "calc", {
        "fpm.toml": 'name = "calc"\n[build]\nauto-tests = true\n',
        "src/calc.f90": "module calc\n  implicit none\ncontains\n  integer function add(a, b)\n"
                        "    integer, intent(in) :: a, b\n    add = a + b\n  end function add\n"
                        "  integer function unused(a)\n    integer, intent(in) :: a\n    unused = -a\n"
                        "  end function unused\nend module calc\n",
        "test/check.f90": "program check\n  use calc\n  if (add(1, 2) /= 3) error stop 1\nend program check\n",
    })
    window = make_window(root / "fpm.toml")
    window.compiler_edit.setText("gfortran")
    window.coverage_check.setChecked(True)
    editor = open_editor(window, root / "src/calc.f90")
    window.run_tests()
    qtbot.waitUntil(lambda: bool(window.coverage), timeout=180_000)
    calc = window.coverage[(root / "src/calc.f90").resolve()]
    assert calc.lines.get(6, 0) > 0 and calc.lines.get(10) == 0  # (add ran; unused didn't)
    assert editor.editor.coverage == calc.lines  # (in the gutter)
    names = [window.coverage_panel.table.item(r, 0).text() for r in range(window.coverage_panel.table.rowCount())]
    assert "src/calc.f90" in names
