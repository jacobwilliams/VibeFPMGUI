"""File locations in the Output panel open the file at that line."""

from __future__ import annotations

from conftest import write_files
from qtpy.QtCore import QPoint, Qt
from qtpy.QtGui import QTextCursor
from qtpy.QtTest import QTest
from qtpy.QtWidgets import QPlainTextEdit

from vibefpmgui.output_links import OutputLinks


def links_for(qtbot, root) -> OutputLinks:
    view = QPlainTextEdit()
    qtbot.addWidget(view)
    return OutputLinks(view, lambda: root)


def test_the_locations_in_a_line(qtbot, tmp_path):
    write_files(tmp_path, {"build/dependencies/slsqp/src/slsqp_core.f90": "", "src/foo.f90": "",
                           "src/c.c": ""})
    links = links_for(qtbot, tmp_path)

    def found(text):
        return [(text[loc.start:loc.end], loc.path.relative_to(tmp_path).as_posix(), loc.line, loc.column)
                for loc in links.locations(text)]

    text = "At line 1753 of file build/dependencies/slsqp/src/slsqp_core.f90 (unit = 6, file = 'stdout')"
    assert found(text) == [("At line 1753 of file build/dependencies/slsqp/src/slsqp_core.f90",
                            "build/dependencies/slsqp/src/slsqp_core.f90", 1753, 0)]
    assert found("src/foo.f90:12:5:") == [("src/foo.f90:12:5", "src/foo.f90", 12, 4)]  # (column: 0-based)
    assert found(f"#1  0x1004 in __foo_MOD_bump at {tmp_path}/src/foo.f90:7") == [
        (f"{tmp_path}/src/foo.f90:7", "src/foo.f90", 7, 0)]  # (a backtrace's, absolute)
    assert found("src/foo.f90(30): error #6404") == [("src/foo.f90(30)", "src/foo.f90", 30, 0)]
    assert found("src/c.c:3:1: warning") == [("src/c.c:3:1", "src/c.c", 3, 0)]
    assert found("At line 5 of file src/missing.f90") == []  # (no such file: no link)
    assert found("x = 1:2, time: 3.5 s") == []


def test_clicking_a_location_opens_it(qtbot, tmp_path):
    write_files(tmp_path, {"src/foo.f90": ""})
    links = links_for(qtbot, tmp_path)
    view = links.view
    view.resize(600, 200)
    view.setPlainText("STOP\nAt line 3 of file src/foo.f90\nthe end")
    underlined = [(r.start, r.length) for r in view.document().findBlockByNumber(1).layout().formats()
                  if r.format.fontUnderline()]
    assert underlined == [(0, len("At line 3 of file src/foo.f90"))]
    opened = []
    links.open_requested.connect(lambda *args: opened.append(args))
    cursor = QTextCursor(view.document().findBlockByNumber(1))
    cursor.movePosition(QTextCursor.Right, n=5)
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, view.cursorRect(cursor).center())
    assert opened == [(str(tmp_path / "src/foo.f90"), 3, 0)]
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, QPoint(5, view.cursorRect(cursor).center().y()
                                                                           + 3 * view.fontMetrics().height()))
    assert len(opened) == 1  # (another line: nothing)
    cursor.movePosition(QTextCursor.EndOfBlock)
    past_the_end = view.cursorRect(cursor).center() + QPoint(200, 0)
    QTest.mouseClick(view.viewport(), Qt.LeftButton, Qt.NoModifier, past_the_end)
    assert len(opened) == 1  # (beside the link, past the end of the line: nothing)


def test_the_output_panel_opens_locations(window, project):
    window.console.setPlainText("At line 5 of file src/foo.f90\n")
    links = window.output_links[0]
    (loc,) = links.locations(window.console.document().firstBlock().text())
    links.open_requested.emit(str(loc.path), loc.line, loc.column)
    editor = window.current_editor()
    assert editor.path.name == "foo.f90" and editor.editor.textCursor().blockNumber() == 4


IFX_TRACEBACK = """\
forrtl: severe (170): Program Exception - stack overflow
Image              PC                Routine            Line        Source             
test_large_sparse  00007FF7011B1BC7  Unknown               Unknown  Unknown
test_large_sparse  00007FF7010C7F25  DENSE_REDUCED_STE        1266  sqpopt_qp_reduced_hessian_module.f90
test_large_sparse  00007FF7010ED93E  SQPOPT_ITERATE            393  sqpopt_iterate_module.f90
test_large_sparse  00007FF7010767A8  SQPOPT_SOLVE              262  sqpopt_module.F90
test_large_sparse  00007FF70106112B  TEST_LARGE_SPARSE          61  test_large_sparse.f90
test_large_sparse  00007FF7011B1DE4  Unknown               Unknown  Unknown
KERNEL32.DLL       00007FFEAD58CD87  Unknown               Unknown  Unknown
"""


def test_an_ifx_traceback(qtbot, tmp_path):
    """ifx's traceback names each file without its folder: it's looked for
    in the project (its own files before a dependency's; not other build output)."""
    write_files(tmp_path, {
        "src/sqpopt_qp_reduced_hessian_module.f90": "", "src/sqpopt_module.F90": "",
        "test/test_large_sparse.f90": "",
        "build/dependencies/dep/src/sqpopt_iterate_module.f90": "",  # (only in a dependency)
        "build/dependencies/dep/src/sqpopt_module.F90": "",           # (also the project's: not this one)
        "build/gfortran_123/sqpopt/sqpopt_qp_reduced_hessian_module.f90": "",  # (build output: never)
    })
    links = links_for(qtbot, tmp_path)
    lines = IFX_TRACEBACK.splitlines()

    def found(n):
        text = lines[n]
        return [(text[loc.start:loc.end], loc.path.relative_to(tmp_path).as_posix(), loc.line)
                for loc in links.locations(text)]

    assert found(3) == [("1266  sqpopt_qp_reduced_hessian_module.f90", "src/sqpopt_qp_reduced_hessian_module.f90", 1266)]
    assert found(4) == [("393  sqpopt_iterate_module.f90", "build/dependencies/dep/src/sqpopt_iterate_module.f90", 393)]
    assert found(5) == [("262  sqpopt_module.F90", "src/sqpopt_module.F90", 262)]
    assert found(6) == [("61  test_large_sparse.f90", "test/test_large_sparse.f90", 61)]
    for n in (0, 1, 2, 7, 8):  # (the message, the header, rows without a source)
        assert found(n) == []
    assert links.find_by_name("nowhere.f90") is None
