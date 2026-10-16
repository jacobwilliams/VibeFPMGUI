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


def test_a_chosen_file_s_procedures_and_flow(qtbot, tmp_path):
    from vibefpmgui.complexity_panel import FILE_COLUMN, NAME_COLUMN

    panel, _root = panel_with(qtbot, tmp_path)
    panel.tests_check.setChecked(True)
    assert panel.splitter.widget(0) is panel.file_table and panel.splitter.widget(1) is panel.procedures
    procs, files = panel.procedures, panel.file_table
    procs.setCurrentCell(next(r for r in range(procs.rowCount()) if procs.item(r, 1).text() == "small"),
                         NAME_COLUMN)
    assert panel.flow_shown.name == "small"
    row_of = {files.item(r, 0).text(): r for r in range(files.rowCount())}
    files.setCurrentCell(row_of["test/check.f90"], 0)  # (another file: its first procedure, and its graph)
    assert rows(panel) == [("check", "2")] and procs.isColumnHidden(FILE_COLUMN)
    assert procs.currentRow() == 0 and panel.flow_shown.name == "check"
    files.setCurrentCell(row_of["src/calc.f90"], 0)
    assert panel.flow_shown.name == "big" and panel.chosen_file() == "src/calc.f90"
    panel.refresh_palette()  # (refilled, as after a save: still that file, still that procedure)
    assert panel.chosen_file() == "src/calc.f90" and procs.isColumnHidden(FILE_COLUMN)
    assert panel.flow_shown.name == "big" and procs.item(procs.currentRow(), NAME_COLUMN).text() == "big"
    panel.filter_edit.setText("c")  # (typed: a filter, not a file; the file column back)
    assert panel.chosen_file() is None and not procs.isColumnHidden(FILE_COLUMN)
    assert panel.flow_shown.name == "big"  # (still listed: still shown)


def test_the_columns_fit_their_contents_and_scroll(qtbot, tmp_path):
    from vibefpmgui.complexity_panel import FILE_COLUMN, NAME_COLUMN

    panel, _root = panel_with(qtbot, tmp_path)
    table = panel.procedures
    assert table.horizontalHeaderItem(0).text() == "FILE" and table.horizontalHeaderItem(1).text() == "PROCEDURE"
    assert (FILE_COLUMN, NAME_COLUMN) == (0, 1) and table.item(0, 0).text() == "src/calc.f90"
    header = table.horizontalHeader()
    file_width = table.fontMetrics().horizontalAdvance("src/calc.f90")
    panel.resize(1600, 500)
    panel.show()
    qtbot.waitExposed(panel)
    widths = [header.sectionSize(i) for i in range(header.count())]
    assert widths[FILE_COLUMN] > file_width  # (the whole path)
    panel.resize(500, 500)  # (narrow: the same widths, scrolling sideways; the file not squeezed out)
    qtbot.wait(20)
    assert [header.sectionSize(i) for i in range(header.count())] == widths
    assert header.length() > table.viewport().width() and table.horizontalScrollBar().maximum() > 0
    # Room for the sort arrow only in the sorted column ("CCN", then "NLOC").
    ccn, nloc = 2, 3
    sorted_ccn = header.sectionSize(ccn)
    header.setSortIndicator(nloc, Qt.DescendingOrder)
    assert header.sectionSize(ccn) < sorted_ccn and header.sectionSize(nloc) > widths[nloc]


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
    panel.flow.open_node(node.node)
    assert opened == [("calc.f90", node.node.first)]
    QTest.keyClick(table, Qt.Key_Down)  # (the arrow keys too)
    assert panel.flow_shown.name == "small" and "0 decisions" in panel.flow_title.text()
    panel.filter_edit.setText("smal")  # (refilled: the same procedure stays shown)
    assert panel.flow_shown.name == "small" and table.currentRow() == 0


def test_the_graph_s_menu(qtbot, tmp_path):
    from qtpy.QtWidgets import QApplication

    from vibefpmgui.cfg_view import CfgView

    view = CfgView()
    qtbot.addWidget(view)
    view.resize(500, 700)
    view.show()
    qtbot.waitExposed(view)
    text = "\n" * 2 + FLOW  # (the procedure from line 3)
    view.show_graph(graph_of(text), text)
    loop = next(item for item in view.nodes.values() if item.node.kind == cf.LOOP)
    menu = view.context_menu(view.vb.mapViewToScene(loop.pos()))
    names = [a.text() for a in menu.actions() if a.text()]
    assert names == ["Open Line 13", "Copy Code", "Center on This", "Fit to View", "Zoom In", "Zoom Out",
                     "Actual Size", "Show Edge Labels", "Copy Image", "Save Image…"]
    clicked = []
    view.line_clicked.connect(lambda path, line: clicked.append((path, line)))
    action = {a.text(): a for a in menu.actions()}
    action["Open Line 13"].trigger()
    assert clicked == [("x.f90", 13)]
    block = next(item.node for item in view.nodes.values() if item.node.text[0].startswith("a = 0"))
    assert view.node_code(block) == "a = 0"  # (as in the file, its indent off)
    action["Copy Code"].trigger()
    assert QApplication.clipboard().text() == "do i = 1, n"
    action["Show Edge Labels"].trigger()
    assert not view.show_labels and not any(label.isVisible() for label in view.labels)
    # The background's menu: the view's actions only.
    corner = view.vb.mapViewToScene(view.bounds().topLeft())
    assert next(a.text() for a in view.context_menu(corner).actions() if a.text()) == "Fit to View"
    # Zooming; actual size; fitting again.
    fitted = view.vb.viewPixelSize()[0]
    action["Zoom In"].trigger()
    assert view.vb.viewPixelSize()[0] < fitted
    action["Actual Size"].trigger()
    assert abs(view.vb.viewPixelSize()[0] - 1) < 1e-6
    action["Fit to View"].trigger()
    assert abs(view.vb.viewPixelSize()[0] - fitted) < 1e-6
    # Images: the whole graph, whatever is in view; the view kept.
    view.zoom(4)
    shown = view.vb.viewRange()
    image = view.image()
    assert image.width() > view.bounds().width() and view.vb.viewRange() == shown
    assert view.export_image(str(tmp_path / "flow.png")) and view.export_image(str(tmp_path / "flow.svg"))
    assert "<svg" in (tmp_path / "flow.svg").read_text()


def test_back_edges_pass_below_their_layer():
    from qtpy.QtCore import QRectF

    from vibefpmgui.cfg_view import _back_edge

    source, target = QRectF(0, 100, 80, 30), QRectF(0, 0, 80, 30)
    points, _ = _back_edge(source, target, 200, 10)
    assert points[1][1] == points[2][1] == 140  # (across in the gap under the source)
    assert points[-1].tolist() == [80, 15]  # (into the target's right side)


def test_continued_lines_are_shown_joined():
    text = """subroutine s(me, n)
    initialized = allocated(me%row_ptr) .and. &
                      allocated(me%col_ind) .and. &   ! (a comment)
                      allocated(me%values)
    if (initialized .and. &
        n > 0) x = 1
    call f(a, &
           & b)
end subroutine s
"""
    shown = [n.text for n in graph_of(text).nodes if n.first]
    assert shown == [["initialized = allocated(me%row_ptr) .and. allocated(me%col_…"],  # (too long: "…")
                     ["if (initialized .and. n > 0)"], ["x = 1"], ["call f(a, b)"]]
    fixed = """      SUBROUTINE T(X)
      X = 1 +
C     (a comment)
     &    2
      IF (X .GT. 0 .AND.
     &    X .LT. 9) X = 0
      END
"""
    shown = [n.text for n in graph_of(fixed, "t.f").nodes if n.first]
    assert shown == [["X = 1 + 2"], ["IF (X .GT. 0 .AND. X .LT. 9)"], ["X = 0"]]


def test_a_long_one_line_if_is_split_before_it_s_shortened():
    """(Cut to 60 characters first, its statement lost its end: "deallocate(…".)"""
    text = """subroutine s(json, vec)
    if (json%exception_thrown .and. allocated(vec)) deallocate(vec)
    if (json%exception_thrown .and. allocated(vec) .and. size(vec) > 1000000) call clear(vec, 'a long message here')
end subroutine s
"""
    shown = [n.text[0] for n in graph_of(text).nodes if n.first]
    assert shown[:2] == ["if (json%exception_thrown .and. allocated(vec))", "deallocate(vec)"]
    assert shown[2] == "if (json%exception_thrown .and. allocated(vec) .and. size(v…"  # (each part: 60 at most)
    assert shown[3] == "call clear(vec, 'a long message here')"


# ------------------------------------------------------------ included files
INCLUDING = """module m
contains
subroutine by_path(x, found)
    integer :: x
    logical, optional :: found
#include "body.inc"
end subroutine by_path
subroutine plain(x)
    integer :: x
    include 'plain.inc'
end subroutine plain
end module m
"""
INCLUDES = {
    "src/m.F90": INCLUDING,
    "src/body.inc": "integer :: p\nif (x > 0) then\n    x = 1\n    return\nend if\n#include \"inner.inc\"\n",
    "src/inner.inc": "if (present(found) .or. x < 0) found = .true.\n",
    "src/plain.inc": "do while (x > 0)\n    x = x - 1\nend do\ninclude 'missing.inc'\n",
}


def test_included_files_are_read_where_they_re_included(tmp_path):
    root = write_files(tmp_path, INCLUDES)
    source = cx.expand_includes(INCLUDING, root / "src/m.F90")
    assert [p.name for p in source.includes] == ["body.inc", "inner.inc", "plain.inc"]
    lines = source.text.split("\n")
    at = lines.index("if (present(found) .or. x < 0) found = .true.")
    assert source.origin(at + 1, root / "src/m.F90") == (root / "src/inner.inc", 1)
    assert "include 'missing.inc'" in lines  # (not found: left as it is)
    assert cx.expand_includes("x = 1\n", "a.f90").origins is None  # (nothing included)


def test_an_include_loop_stops(tmp_path):
    root = write_files(tmp_path, {"a.inc": "x = 1\ninclude 'b.inc'\n", "b.inc": "y = 2\ninclude 'a.inc'\n"})
    source = cx.expand_includes("include 'a.inc'\n", root / "main.f90")
    assert source.text.split("\n")[:4] == ["x = 1", "y = 2", "include 'a.inc'", ""]


def test_included_code_is_measured_with_its_procedure(tmp_path):
    root = write_files(tmp_path, INCLUDES)
    metrics = cx.analyze_file(root / "src/m.F90")
    procs = {p.name: p for p in metrics.procedures}
    by_path, plain = procs["by_path"], procs["plain"]
    assert by_path.ccn == 4 and plain.ccn == 2  # (if, one-line if, .or.; do while)
    assert (by_path.path, by_path.start, by_path.end) == (root / "src/m.F90", 3, 7)  # (its own file's lines)
    assert by_path.nloc > by_path.length - 1  # (the included lines too)
    assert len(metrics.includes) == 3


def test_the_graph_of_an_included_body(qtbot, tmp_path):
    from vibefpmgui.cfg_view import CfgView

    root = write_files(tmp_path, INCLUDES)
    path = root / "src/m.F90"
    proc = next(p for p in cx.analyze_file(path).procedures if p.name == "by_path")
    graph = cf.build(INCLUDING, path, proc)
    assert graph.complexity == proc.ccn - 1  # (less the .or.)
    nodes = {n.text[0]: n for n in graph.nodes if n.first}
    assert (nodes["if (x > 0) then"].path.name, nodes["if (x > 0) then"].first) == ("body.inc", 2)
    assert nodes["x = 1"].path.name == "body.inc" and (nodes["x = 1"].first, nodes["x = 1"].last) == (3, 4)
    assert nodes["if (present(found) .or. x < 0)"].path.name == "inner.inc"
    view = CfgView()
    qtbot.addWidget(view)
    view.show_graph(graph, INCLUDING)
    item = next(i for i in view.nodes.values() if i.node is nodes["x = 1"])
    assert item.included and "body.inc (included), lines 3–4" in item.toolTip()
    clicked = []
    view.line_clicked.connect(lambda file, line: clicked.append((Path(file).name, line)))
    view.open_node(item.node)
    assert clicked == [("body.inc", 3)]
    assert view.node_code(item.node) == "x = 1\nreturn"  # (from the included file)


def test_a_changed_include_is_measured_again(tmp_path, monkeypatch):
    import os

    from vibefpmgui.project_search import Root
    from vibefpmgui.window.complexity import measure

    root = write_files(tmp_path, INCLUDES)
    roots = [Root("m", root, False)]
    files, cache = measure(roots, False, {})
    assert next(p for p in files[0].procedures if p.name == "plain").ccn == 2
    read = []
    real = cx.analyze_file
    monkeypatch.setattr(cx, "analyze_file", lambda path: read.append(path.name) or real(path))
    measure(roots, False, cache)
    assert read == []  # (nothing changed)
    inc = root / "src/plain.inc"
    inc.write_text("if (x > 0) x = 0\n" + inc.read_text())
    stat = inc.stat()
    os.utime(inc, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    files, cache = measure(roots, False, cache)
    assert read == ["m.F90"] and next(p for p in files[0].procedures if p.name == "plain").ccn == 3
