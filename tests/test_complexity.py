"""Code complexity: the Fortran reader's metrics, the Complexity panel, the window."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import open_editor, write_files
from qtpy.QtCore import Qt

from vibefpmgui import complexity as cx

TWO = """module m
contains
    subroutine first({args})
{body}
    end subroutine first
    subroutine second(x)
        integer, intent(inout) :: x
        x = x + 1
    end subroutine second
end module m
"""


def names(text: str, path: str = "x.f90") -> list[str]:
    return [p.qualified for p in cx.analyze_text(text, path).procedures]


def one(text: str, path: str = "x.f90") -> cx.Procedure:
    (proc,) = cx.analyze_text(text, path).procedures
    return proc


# ------------------------------------------------------------ lizard's Fortran bugs
@pytest.mark.parametrize("args, body", [
    ("name", "        character(len=*), intent(in) :: name\n        call show('hello '//name)"),   # // in ()
    ("a", "        real, intent(inout) :: a(:)\n        where (a < 0.0) a = 0.0"),              # one-line where
    ("p, data", ("        class(*), pointer, intent(out) :: p\n        class(*), target, intent(in) :: data\n"
                 "        p => data")),                                                        # a variable "data"
    ("v", ("        class(*), intent(in) :: v\n        select type (v)\n        type is (integer)\n"
           "            print *, v\n        end select")),                                     # type is
])
def test_lizard_s_four_fortran_bugs(args, body):
    assert names(TWO.format(args=args, body=body)) == ["m::first", "m::second"]


# ------------------------------------------------------------ the metrics
def test_ccn_counts_each_decision():
    proc = one("""subroutine s(a, b, n)
    integer :: a, b, n, i
    if (a > 0 .and. b > 0) then
        a = 1
    else if (a < 0 .or. b < 0) then
        a = 2
    else
        a = 3
    end if
    if (n == 0) return
    do i = 1, n
        select case (i)
        case (1)
            a = 1
        case (2:3)
            a = 2
        case default
            a = 0
        end select
    end do
    do while (a > 0)
        a = a - 1
    end do
    if (a) 10, 20, 30
10  continue
20  continue
30  continue
end subroutine s
""")
    # if, .and., else if, .or., one-line if, do, case, case, do while, arithmetic if (2)
    assert proc.ccn == 1 + 11
    assert proc.params == 3 and proc.depth == 2  # (do > select: a case isn't a block)


def test_where_forall_and_select_type_guards():
    proc = one("""subroutine s(a, v)
    real :: a(:)
    class(*) :: v
    where (a > 0)
        a = 1
    elsewhere (a < 0)
        a = -1
    elsewhere
        a = 0
    end where
    forall (i = 1:3) a(i) = 0
    select type (v)
    type is (integer)
    class is (base)
    class default
    end select
end subroutine s
""")
    assert proc.ccn == 1 + 5  # (where, elsewhere(mask), forall, type is, class is)
    assert proc.depth == 1


def test_lines_tokens_and_length():
    proc = one("""subroutine s(x)   ! a comment
    ! a comment line

    real :: x
    x = 1.0e-8_wp * 2   ! more
end subroutine s
""")
    assert (proc.start, proc.end, proc.length, proc.nloc) == (1, 6, 6, 4)
    assert proc.tokens == 5 + 3 + 5 + 3  # ("subroutine s ( x )" "real :: x" "x = 1.0e-8_wp * 2" "end subroutine s")


def test_keywords_and_bangs_in_strings_are_not_code():
    proc = one("""subroutine s()
    print *, 'if (x) then do ! not a comment'; print *, "it""s"
    print *, 'a long &
              &string, with if (y) in it'
end subroutine s
""")
    assert proc.ccn == 1 and proc.nloc == 5


def test_internal_procedures_are_measured_apart():
    procs = cx.analyze_text("""module m
contains
    subroutine host(x)
        integer :: x
        if (x > 0) x = 1
    contains
        subroutine inner(y)
            integer :: y
            do while (y > 0)
                y = y - 1
            end do
        end subroutine inner
    end subroutine host
end module m
""").procedures
    host, inner = procs
    assert (host.qualified, inner.qualified) == ("m::host", "m > host::inner")
    assert (host.ccn, inner.ccn) == (2, 2)
    assert host.length == 11 and host.nloc == 5 and inner.nloc == 6


def test_interfaces_types_and_submodules():
    text = """module m
    interface
        subroutine external_one(a)
            real :: a
        end subroutine external_one
        pure function external_two(b) result(c)
            real, intent(in) :: b
            real :: c
        end function
    end interface
    type :: t
    contains
        procedure :: go
    end type t
contains
    integer function go(self)
        class(t) :: self
        go = 1
    end function go
end module m
submodule (m) impl
contains
    module procedure separate
        print *, 1
    end procedure separate
end submodule impl
"""
    procs = cx.analyze_text(text).procedures
    assert [(p.qualified, p.kind, p.params) for p in procs] == [("m::go", "function", 1),
                                                                ("impl::separate", "procedure", 0)]


def test_fixed_form():
    procs = cx.analyze_text("""C     A fixed-form file
      SUBROUTINE TP1(MODE, X,
     &               Y)
      DOUBLE PRECISION X, Y
      DO 10 I = 1, 3
         IF (X .GT. Y) X = Y
   10 CONTINUE
      END

      SUBROUTINE TP2(MODE)
*     another comment
      IF (MODE .EQ. 1) GOTO 20
   20 RETURN
      END
""", "legacy.f").procedures
    assert [(p.name, p.params, p.ccn, p.depth, p.start, p.end) for p in procs] == [
        ("tp1", 3, 3, 1, 2, 8), ("tp2", 1, 2, 0, 10, 14)]


def test_preprocessor_lines_are_not_code():
    proc = one("""subroutine s(x)
    integer :: x
#ifdef DEBUG
    print *, x
#endif
end subroutine s
""", "x.F90")
    assert proc.nloc == 4 and proc.ccn == 1


def test_a_main_program_and_warnings():
    proc = one("program main\n" + "".join(f"  if (x == {i}) y = {i}\n" for i in range(20)) + "end program main\n")
    assert proc.kind == "program" and proc.ccn == 21
    assert proc.warnings() == ["ccn"] and proc.warnings({"ccn": 30, "length": 1000, "params": 100, "depth": 5}) == []


def test_analyze_file(tmp_path):
    path = write_files(tmp_path, {"a.f90": "subroutine s()\nend subroutine s\n"}) / "a.f90"
    found = cx.analyze_file(path)
    assert found.nloc == 2 and [p.name for p in found.procedures] == ["s"]
    assert cx.analyze_file(tmp_path / "missing.f90").procedures == []
    assert cx.is_fortran(Path("x.F90")) and cx.is_fortran(Path("y.for")) and not cx.is_fortran(Path("z.c"))


# ------------------------------------------------------------ the panel
SOURCES = {
    "src/calc.f90": "module calc\ncontains\n" + "  subroutine big(a)\n    integer :: a\n"
                    + "".join(f"    if (a == {i}) a = {i}\n" for i in range(16)) + "  end subroutine big\n"
                    + "  subroutine small()\n  end subroutine small\nend module calc\n",
    "test/check.f90": "program check\n  if (.true.) print *, 1\nend program check\n",
}


def panel_with(qtbot, tmp_path):
    from vibefpmgui.complexity_panel import ComplexityPanel

    root = write_files(tmp_path / "proj", SOURCES)
    files = [cx.analyze_file(root / name) for name in SOURCES]
    panel = ComplexityPanel()
    qtbot.addWidget(panel)
    panel.show_metrics(files, root, [root / "test"], [], "now")
    return panel, root


def rows(panel) -> list[tuple[str, str]]:
    t = panel.procedures
    return [(t.item(r, 1).text(), t.item(r, 2).text()) for r in range(t.rowCount())]


def test_the_panel(qtbot, tmp_path):
    panel, _root = panel_with(qtbot, tmp_path)
    assert rows(panel) == [("big", "17"), ("small", "1")]  # (the most complex first; not the tests)
    assert "2 procedures" in panel.totals.text() and "1 over a threshold" in panel.totals.text()
    assert panel.procedures.item(0, 2).foreground().color().name() == cx_over()
    assert panel.subtitle.text() == "now · 1 files"
    panel.tests_check.setChecked(True)
    assert ("check", "2") in rows(panel)
    panel.filter_edit.setText("small")
    assert rows(panel) == [("small", "1")]
    panel.filter_edit.clear()
    opened = []
    panel.open_requested.connect(lambda path, line: opened.append((Path(path).name, line)))
    big = next(r for r in range(panel.procedures.rowCount()) if panel.procedures.item(r, 1).text() == "big")
    panel._procedure_clicked(big, 0)
    assert opened == [("calc.f90", 3)]
    test_row = next(r for r in range(panel.file_table.rowCount())
                    if panel.file_table.item(r, 0).text() == "test/check.f90")
    def click(row):
        from qtpy.QtTest import QTest

        point = panel.file_table.visualItemRect(panel.file_table.item(row, 0)).center()
        QTest.mouseClick(panel.file_table.viewport(), Qt.LeftButton, Qt.NoModifier, point)

    panel.resize(900, 600)
    panel.show()
    qtbot.waitExposed(panel)
    click(test_row)  # (only that file's procedures)
    assert rows(panel) == [("check", "2")]
    click(test_row)  # (again: all of them)
    assert len(rows(panel)) == 3


def cx_over() -> str:
    from vibefpmgui.complexity_panel import OVER
    return OVER


# ------------------------------------------------------------ the window
def test_the_window_measures_the_project_and_again_on_save(qtbot, window, project, monkeypatch):
    panel = window.complexity_panel
    window.show_panel(panel)
    window.refresh_complexity()
    qtbot.waitUntil(lambda: panel.procedures.rowCount() > 0, timeout=5_000)
    assert "bump" in [panel.procedures.item(r, 1).text() for r in range(panel.procedures.rowCount())]
    read = []
    real = cx.analyze_file
    monkeypatch.setattr(cx, "analyze_file", lambda path: read.append(Path(path).name) or real(path))
    editor = open_editor(window, project / "src/foo.f90")
    editor.editor.appendPlainText("subroutine extra()\nend subroutine extra")
    editor.save()  # (measured again: only the file that changed is read)
    qtbot.waitUntil(lambda: "extra" in [panel.procedures.item(r, 1).text()
                                        for r in range(panel.procedures.rowCount())], timeout=5_000)
    assert read == ["foo.f90"]
    assert window.panel_in_front(panel) and panel.procedures.item(0, 0).data(Qt.UserRole)


def test_the_arrow_keys_choose_a_file_too(qtbot, tmp_path):
    """(Only a click on a file listed its procedures: not the arrow keys.)"""
    from qtpy.QtTest import QTest

    panel, _root = panel_with(qtbot, tmp_path)
    panel.tests_check.setChecked(True)
    table = panel.file_table
    labels = [table.item(r, 0).text() for r in range(table.rowCount())]
    table.setFocus()
    table.setCurrentCell(0, 0)
    assert panel.filter_edit.text() == labels[0]
    QTest.keyClick(table, Qt.Key_Down)
    assert panel.filter_edit.text() == labels[1]
    assert {p for p, _ccn in rows(panel)} == ({"big", "small"} if labels[1] == "src/calc.f90" else {"check"})
    point = table.visualItemRect(table.item(1, 0)).center()
    panel.resize(900, 600)
    panel.show()
    qtbot.waitExposed(panel)
    QTest.mouseClick(table.viewport(), Qt.LeftButton, Qt.NoModifier, point)  # (the file chosen: all of them)
    assert panel.filter_edit.text() == "" and len(rows(panel)) == 3
    panel.refresh_palette()  # (refilling the tables doesn't choose a file)
    assert panel.filter_edit.text() == ""


def test_the_file_comes_first_and_shrinks(qtbot, tmp_path):
    from qtpy.QtWidgets import QHeaderView

    from vibefpmgui.complexity_panel import FILE_COLUMN, NAME_COLUMN

    panel, _root = panel_with(qtbot, tmp_path)
    table = panel.procedures
    assert table.horizontalHeaderItem(0).text() == "FILE" and table.horizontalHeaderItem(1).text() == "PROCEDURE"
    assert (FILE_COLUMN, NAME_COLUMN) == (0, 1) and table.item(0, 0).text() == "src/calc.f90"
    header = table.horizontalHeader()
    assert header.sectionResizeMode(NAME_COLUMN) == QHeaderView.ResizeToContents
    panel.resize(2400, 500)
    panel.show()
    qtbot.waitExposed(panel)
    wide = header.sectionSize(FILE_COLUMN)
    panel.resize(1000, 500)
    qtbot.wait(20)
    assert header.sectionSize(FILE_COLUMN) < wide and header.sectionSize(NAME_COLUMN) > 0
    viewport = table.viewport().width()
    assert header.length() <= viewport or header.sectionSize(FILE_COLUMN) == 60  # (no scrolling while it can shrink)
    panel.resize(2400, 500)
    qtbot.wait(20)
    assert header.sectionSize(FILE_COLUMN) >= wide - 2 and header.length() <= table.viewport().width() + 1


# ------------------------------------------------------------ control flow
from vibefpmgui import control_flow as cf


def graph_of(text: str, path: str = "x.f90", index: int = 0) -> cf.Graph:
    proc = cx.analyze_text(text, path).procedures[index]
    return cf.build(text, path, proc)


def edges(graph: cf.Graph) -> set[tuple[str, str, str]]:
    """(source's first text, target's first text, label) of every edge."""
    text = {n.id: n.text[0] for n in graph.nodes}
    return {(text[e.source], text[e.target], e.label) for e in graph.edges}


FLOW = """subroutine s(a, n)
    integer, intent(inout) :: a, n   ! (declarations: not in the graph)
    integer :: i
    a = 0
    if (a > 0) then
        a = 1
    else if (a < 0) then
        a = 2
    end if
    if (n == 0) return   ! done
    do i = 1, n
        if (i == 2) cycle
        select case (i)
        case (1)
            a = 1
        case (5)
            exit
        end select
    end do
    if (a < 0) error stop 'bad: if (x) then'
end subroutine s
"""


def test_the_control_flow_graph():
    g = graph_of(FLOW)
    kinds = [n.kind for n in g.nodes]
    assert kinds.count(cf.START) == kinds.count(cf.END) == kinds.count(cf.STOP) == 1
    assert kinds.count(cf.LOOP) == 1 and kinds.count(cf.DECISION) == 6
    assert all("integer" not in line for n in g.nodes for line in n.text)  # (declarations)
    found = edges(g)
    assert ("if (a > 0) then", "a = 1", "true") in found
    assert ("if (a > 0) then", "else if (a < 0) then", "false") in found
    assert ("else if (a < 0) then", "if (n == 0)", "false") in found  # (no else: on)
    assert ("if (n == 0)", "return", "true") in found and ("return", "end", "") in found
    assert ("do i = 1, n", "if (i == 2)", "loop") in found
    assert ("cycle", "do i = 1, n", "") in found  # (cycle: back to the loop)
    assert ("select case (i)", "a = 1", "case (1)") in found
    assert ("select case (i)", "do i = 1, n", "default") in found  # (no case default: back round)
    assert ("exit", "if (a < 0)", "") in found and ("do i = 1, n", "if (a < 0)", "done") in found
    assert ("error stop 'bad: if (x) then'", "stop", "") in found
    proc = cx.analyze_text(FLOW).procedures[0]
    assert g.complexity == proc.ccn == 9


def test_legacy_control_flow():
    text = """      SUBROUTINE TP(MODE, X)
      DOUBLE PRECISION X(5)
      GOTO (1, 2, 2), MODE
      X(1) = 0
    1 DO 10 I = 1, 5
      DO 10 J = 1, 2
   10 X(I) = X(I) + J
      IF (X(1)) 20, 30, 30
   20 RETURN
   30 STOP
    2 CONTINUE
      END
"""
    g = graph_of(text, "legacy.f")
    found = edges(g)
    assert ("GOTO (1, 2, 2), MODE", "1 DO 10 I = 1, 5", "1") in found
    assert ("GOTO (1, 2, 2), MODE", "2 CONTINUE", "2, 3") in found  # (a label twice: one edge)
    assert ("GOTO (1, 2, 2), MODE", "X(1) = 0", "else") in found
    assert ("DO 10 J = 1, 2", "IF (X(1)) 20, 30, 30", "done") not in found  # (the inner loop: back to the outer)
    assert ("1 DO 10 I = 1, 5", "IF (X(1)) 20, 30, 30", "done") in found
    assert ("IF (X(1)) 20, 30, 30", "20 RETURN", "<0") in found
    assert ("IF (X(1)) 20, 30, 30", "30 STOP", "=0, >0") in found
    proc = cx.analyze_text(text, "legacy.f").procedures[0]
    assert g.complexity == proc.ccn


def test_an_endless_do_and_a_type_in_a_program():
    text = """program p
    type :: pair
        integer :: a, b
    end type pair
    integer :: n
    n = 0
    do
        n = n + 1
        if (n > 3) exit
    end do
end program p
"""
    g = graph_of(text)
    found = edges(g)
    assert not any(label == "done" for _s, _t, label in found)  # (endless: out only by its exit)
    assert ("n = 0", "do", "") in found and ("exit", "end", "") in found
    assert g.complexity == cx.analyze_text(text).procedures[0].ccn == 2


def test_the_layout():
    g = graph_of(FLOW)
    sizes = {n.id: (120.0, 30.0) for n in g.nodes}
    places = cf.layout(g, sizes)
    assert set(places.positions) == {n.id for n in g.nodes}
    back = {(g.nodes[e.source].text[0], g.nodes[e.target].text[0]) for e in g.edges if e.back}
    assert back == {("cycle", "do i = 1, n"), ("a = 1", "do i = 1, n"), ("select case (i)", "do i = 1, n")}
    end = next(n.id for n in g.nodes if n.kind == cf.END)
    assert places.positions[end][1] == max(y for _x, y in places.positions.values())  # (at the bottom)
    for index, route in places.routes.items():  # (a long edge: a point in each layer it crosses)
        e = g.edges[index]
        top, bottom = places.positions[e.source][1], places.positions[e.target][1]
        assert route and all(top < y < bottom for _x, y in route)


def test_the_graph_s_complexity_is_the_ccn_everywhere():
    """For every procedure of the demo: edges - nodes + 2 counts the same
    decisions as the CCN (which also counts .and. / .or.)."""
    root = Path(__file__).resolve().parents[1] / "examples/demo"
    checked = 0
    for path in sorted(root.rglob("*")):
        if not cx.is_fortran(path) or "build" in path.parts:
            continue
        text = path.read_text()
        for proc in cx.analyze_text(text, path).procedures:
            stmts = cf.procedure_statements(text, path, proc.start)
            logical = sum(len(cx.LOGICAL_RE.findall(st.text)) for st in stmts)
            assert cf.build(text, path, proc).complexity == proc.ccn - logical, proc.qualified
            checked += 1
    assert checked > 20


def test_the_panel_shows_the_selected_procedure_s_flow(qtbot, tmp_path):
    from qtpy.QtTest import QTest

    from vibefpmgui.complexity_panel import NAME_COLUMN

    panel, _root = panel_with(qtbot, tmp_path)
    panel.resize(1400, 600)
    panel.show()
    qtbot.waitExposed(panel)
    table = panel.procedures
    assert panel.flow_shown is None
    table.setFocus()
    table.setCurrentCell(0, NAME_COLUMN)  # ("big": the most complex first)
    assert panel.flow_shown.name == "big" and "big" in panel.flow_title.text()
    assert "16 decisions" in panel.flow_title.text() and len(panel.flow.nodes) > 20
    opened = []
    panel.open_requested.connect(lambda path, line: opened.append((Path(path).name, line)))
    node = next(item for item in panel.flow.nodes.values() if item.node.first)
    panel.flow.line_clicked.emit(node.node.first)
    assert opened == [("calc.f90", node.node.first)]
    QTest.keyClick(table, Qt.Key_Down)  # (the arrow keys too)
    assert panel.flow_shown.name == "small" and "0 decisions" in panel.flow_title.text()
    panel.filter_edit.setText("smal")  # (refilled: the same procedure stays shown)
    assert panel.flow_shown.name == "small" and table.currentRow() == 0
