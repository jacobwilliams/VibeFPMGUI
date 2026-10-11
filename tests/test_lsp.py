"""The language server: answers taken apart (canned, no server), the client
with the real fortls, and the editor's features that use it."""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import open_editor
from qtpy.QtGui import QTextCursor
from qtpy.QtWidgets import QDialog, QInputDialog

from vibefpmgui import lsp


def uri(path: Path) -> str:
    return lsp.to_uri(path)


def rng(line, start, end_line=None, end=None):
    return {"start": {"line": line, "character": start},
            "end": {"line": line if end_line is None else end_line, "character": start if end is None else end}}


# --------------------------------------------------------------- answers
def test_uris_and_files(tmp_path):
    path = (tmp_path / "a b.f90").resolve()
    assert lsp.from_uri(lsp.to_uri(path)) == path
    assert lsp.is_fortran(Path("x.F90")) and not lsp.is_fortran(Path("x.c"))
    assert not lsp.has_config(tmp_path)
    (tmp_path / ".fortlsrc").write_text("{}")
    assert lsp.has_config(tmp_path)


def test_server_args(project):
    from vibefpmgui import manifest as m

    args = lsp.server_args(m.resolve(project / "fpm.toml"), project)
    assert "--disable_autoupdate" in args and "--source_dirs" in args
    dirs = args[args.index("--source_dirs") + 1:]
    assert all(d.endswith("/**") for d in dirs)
    assert any(d.startswith(str(project.resolve())) for d in dirs)


def test_hover_info_for_code_and_intrinsics():
    info = lsp.hover_info({"kind": "markdown", "value": "```fortran\nINTEGER :: count\n```\n-----\nA counter"})
    assert info.kind == "variable" and "counter" in info.body_html
    assert lsp.hover_info("```fortran\nSUBROUTINE bump()\n```").kind == "subroutine"
    assert lsp.hover_info("```fortran\nINTEGER, PARAMETER :: n = 3\n```").kind == "parameter"
    assert lsp.hover_info("```fortran\nMODULE foo\n```").kind == "module"
    manual = "-----\n## sum\n\n### **Name**\n\n**sum** - \\[ARRAY:REDUCTION\\] Sum the elements\n\nMore _text_."
    assert lsp.intrinsic_name(manual) == "sum"
    info = lsp.hover_info([{"value": manual}])
    assert (info.kind, info.category, info.summary) == ("intrinsic", "ARRAY · REDUCTION", "Sum the elements")
    assert 'font-style:italic;">text</span>' in info.body_html  # (_text_: italics, not underlined)
    assert lsp.hover_info("") is None and lsp.hover_html("") == ""
    assert "Sum the elements" in lsp.hover_html(manual)


def test_code_and_markdown_html():
    assert "<pre" in lsp.code_html("x = 1") and "x" in lsp.code_html("x = 1")
    assert "<b>bold</b>" in lsp.markdown_html("**bold**") or "font-weight" in lsp.markdown_html("**bold**")


def test_first_location(tmp_path):
    path = (tmp_path / "a.f90").resolve()
    assert lsp.first_location([{"uri": uri(path), "range": rng(4, 2)}]) == (path, 5, 2)
    assert lsp.first_location({"targetUri": uri(path), "targetSelectionRange": rng(1, 0)}) == (path, 2, 0)
    assert lsp.first_location([]) is None and lsp.first_location({"uri": uri(path)}) is None


def test_outline():
    symbols = [
        {"name": "foo", "kind": 2, "range": rng(0, 0, 9, 0), "children": [
            {"name": "bump", "kind": 12, "range": rng(4, 2, 6, 2), "children": []},
            {"name": "count", "kind": 13, "range": rng(1, 2)},  # (a variable: not in the outline)
        ]},
    ]
    lines = {1: "module foo", 5: "  subroutine bump()"}
    rows = lsp.outline(symbols, lambda n: lines.get(n, ""))
    assert rows == [(0, "module", "foo", 1, 10), (1, "subroutine", "bump", 5, 7)]


def test_completions_and_snippets():
    result = {"items": [
        {"label": "bump", "kind": 3, "insertText": "bump(${1:n}, $2)$0", "insertTextFormat": 2,
         "detail": "SUBROUTINE", "documentation": {"value": "Adds one"}},
        {"label": "count", "kind": 6, "textEdit": {"newText": "count", "range": rng(0, 0)}},
        {"kind": 6},  # (no label: skipped)
    ]}
    items = lsp.completions(result)
    assert [(c.label, c.insert, c.kind, c.doc) for c in items] == [
        ("bump", "bump(n, )", "function", "Adds one"), ("count", "count", "variable", "")]
    assert lsp.snippet_text("a\\$b ${1} $3") == "a$b  "


def test_comments_strings_and_arguments():
    assert lsp.in_comment_or_string("x = 1 ! note", 10)
    assert lsp.in_comment_or_string("print *, 'it''s here", 15)
    assert not lsp.in_comment_or_string("x = 'a' + b", 10)
    assert lsp.argument_index("call foo(a, b(1, 2), ") == 2
    assert lsp.argument_index("call foo(a, 'x,y', ") == 2
    assert lsp.argument_index("x = y") is None
    assert lsp.argument_index("x = arr[1, ") is None


def test_signature_html():
    result = {"signatures": [{"label": "SUBROUTINE move(x, dx)", "parameters": [
        {"label": "x"}, {"label": "dx", "documentation": "```fortran\nREAL :: dx\n```"}]}],
        "activeParameter": 1}
    shown = lsp.signature_html(result)
    assert "<b><u>dx</u></b>" in shown and "REAL" in shown
    offsets = {"signatures": [{"label": "f(a, b)", "parameters": [{"label": [2, 3]}, {"label": [5, 6]}]}]}
    assert "<b><u>a</u></b>" in lsp.signature_html(offsets, active_hint=0)
    assert lsp.signature_html({}) == ""


def test_locations_and_workspace_edits(tmp_path):
    a, b = (tmp_path / "a.f90").resolve(), (tmp_path / "b.f90").resolve()
    refs = [{"uri": uri(b), "range": rng(2, 4, 2, 9)}, {"uri": uri(a), "range": rng(0, 1, 0, 3)},
            {"uri": uri(a), "range": rng(0, 1, 0, 3)}, {"bad": 1}]
    assert lsp.locations(refs) == [(a, 1, 1, 3), (b, 3, 4, 9)]
    edit = {"changes": {uri(a): [{"range": rng(0, 1, 0, 3), "newText": "zz"}]},
            "documentChanges": [{"textDocument": {"uri": uri(b)}, "edits": [{"range": rng(2, 4, 2, 9), "newText": "q"}]}]}
    edits = lsp.workspace_edits(edit)
    assert edits[a] == [lsp.TextEdit(0, 1, 0, 3, "zz")] and edits[b] == [lsp.TextEdit(2, 4, 2, 9, "q")]
    assert lsp.workspace_edits(None) == {}


def test_result_variable_edits(tmp_path):
    path = tmp_path / "f.f90"
    text = ("module m\ncontains\n  integer function twice(x)\n    integer :: x\n"
            "    twice = 2 * x ! twice\n  end function\n  integer function other(x) result(r)\n"
            "    r = x\n  end function\nend module\n")
    renamed = {path: [lsp.TextEdit(2, 19, 2, 24, "double")]}
    out = lsp.add_result_variable_edits(renamed, "twice", "double", lambda p: text)
    assert lsp.TextEdit(4, 4, 4, 9, "double") in out[path]  # (the result variable)
    assert len(out[path]) == 2  # (not the comment, not the other function)


# ---------------------------------------------------- the real fortls
needs_fortls = pytest.mark.skipif(lsp.executable() is None, reason="fortls not installed")


@pytest.fixture
def client(qtbot, project):
    from vibefpmgui import manifest as m

    client = lsp.LspClient()
    assert client.start(project, lsp.server_args(m.resolve(project / "fpm.toml"), project))
    yield client
    client.stop()


def ask(qtbot, method, *args):
    """Call a client request; wait for and return its answer."""
    answers = []
    assert method(*args, answers.append)
    qtbot.waitUntil(lambda: bool(answers), timeout=20_000)
    return answers[0]


@needs_fortls
def test_client_answers(qtbot, client, project):
    foo = (project / "src/foo.f90").resolve()
    client.open_document(foo, foo.read_text())
    hover = ask(qtbot, client.hover, foo, 6, 5)  # "count" in "count = count + 1"
    assert "INTEGER" in lsp.hover_text(hover["contents"]).upper()
    main = (project / "app/main.f90").resolve()
    client.open_document(main, main.read_text())
    definition = ask(qtbot, client.definition, main, 3, 8)  # "bump" in "call bump()"
    assert lsp.first_location(definition)[:2] == (foo, 5)
    rows = lsp.outline(ask(qtbot, client.document_symbols, foo))
    assert [(r[2], r[3]) for r in rows] == [("foo", 1), ("bump", 5)]
    found = ask(qtbot, client.workspace_symbols, "bump")
    assert any(s.get("name") == "bump" for s in found)
    refs = lsp.locations(ask(qtbot, client.references, foo, 5, 15))
    assert {(p.name, line) for p, line, _s, _e in refs} >= {("foo.f90", 5), ("main.f90", 3)}
    edits = lsp.workspace_edits(ask(qtbot, client.rename, foo, 5, 15, "step"))
    assert {p.name for p in edits} == {"foo.f90", "main.f90"}
    # Unsaved changes are what the server sees.
    client.change_document(main, main.read_text().replace("call bump()", "call bump()\n  print *, cou"))
    items = lsp.completions(ask(qtbot, client.completion, main, 4, 14))
    assert any(c.label.lower() == "count" for c in items)
    client.close_document(main)
    assert client.is_running()


@needs_fortls
def test_client_reports_when_the_server_stops(qtbot, client):
    stopped = []
    client.stopped.connect(stopped.append)
    client.process.kill()
    qtbot.waitUntil(lambda: bool(stopped), timeout=10_000)
    assert "fortls stopped" in stopped[0] and not client.is_running()


# ------------------------------------------------ the editor with fortls
@pytest.fixture
def lsp_window(make_window, project, settings_file):
    settings_file.write_text("[General]\nfortls=true\nlint=false\n")
    window = make_window(project / "fpm.toml")
    return window


def cursor_at(editor, line: int, column: int):
    cursor = editor.editor.textCursor()
    block = editor.editor.document().findBlockByNumber(line - 1)
    cursor.setPosition(block.position() + column)
    editor.editor.setTextCursor(cursor)


@needs_fortls
def test_editor_outline_and_definition(qtbot, lsp_window, project):
    foo = open_editor(lsp_window, project / "src/foo.f90")
    qtbot.waitUntil(lambda: foo.outline_box.count() == 2, timeout=20_000)
    main = open_editor(lsp_window, project / "app/main.f90")
    cursor_at(main, 3, 8)
    lsp_window.definition_action.trigger()
    qtbot.waitUntil(lambda: lsp_window.current_editor() is foo
                    and foo.editor.textCursor().blockNumber() == 4, timeout=20_000)
    lsp_window.back_action.trigger()  # (Go Back: to the call)
    assert lsp_window.current_editor() is main


@needs_fortls
def test_editor_references_and_hover(qtbot, lsp_window, project):
    foo = open_editor(lsp_window, project / "src/foo.f90")
    qtbot.waitUntil(lambda: foo.outline_box.count() == 2, timeout=20_000)
    cursor_at(foo, 5, 15)  # bump
    lsp_window.references_action.trigger()
    panel = lsp_window.references_panel
    qtbot.waitUntil(lambda: panel.tree.topLevelItemCount() == 2, timeout=20_000)
    assert "bump" in panel.title.text()
    # The card for "count" (shown only while the real mouse is on the name:
    # here, take the card the editor makes, and show it).
    cards = []
    foo.editor.show_hover = lambda line, column, pos, card: cards.append((line, column, card))
    pos = foo.editor.viewport().mapToGlobal(foo.editor.cursorRect().center())
    foo._request_hover(6, 4, pos, "")  # ("count": columns are 0-based)
    qtbot.waitUntil(lambda: bool(cards), timeout=20_000)
    line, column, card = cards[0]
    assert (line, column) == (6, 4) and "count" in card and "INTEGER" in card.upper()
    foo.editor.hover_popup.show_at(pos, card, (line, column))
    assert foo.editor.hover_popup.isVisible()


@needs_fortls
def test_editor_rename(qtbot, lsp_window, project, monkeypatch):
    foo = open_editor(lsp_window, project / "src/foo.f90")
    qtbot.waitUntil(lambda: foo.outline_box.count() == 2, timeout=20_000)
    cursor_at(foo, 5, 15)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("step", True))
    monkeypatch.setattr("vibefpmgui.main_window.RenamePreviewDialog.exec", lambda self: QDialog.Accepted)
    lsp_window.rename_action.trigger()
    qtbot.waitUntil(lambda: "subroutine step()" in foo.editor.toPlainText(), timeout=20_000)
    assert "call step()" in (project / "app/main.f90").read_text()  # (not open: saved)
    assert foo.is_dirty()  # (open: changed in the editor)


@needs_fortls
def test_editor_completion(qtbot, lsp_window, project):
    main = open_editor(lsp_window, project / "app/main.f90")
    qtbot.waitUntil(lambda: lsp_window.lsp_client.is_running(), timeout=20_000)
    cursor = main.editor.textCursor()
    cursor.movePosition(QTextCursor.End)
    main.editor.setTextCursor(cursor)
    main.editor.insertPlainText("subroutine t\n  use foo\n  call bu")
    main.request_completion(manual=True)
    popup = main.editor.completion_popup
    qtbot.waitUntil(lambda: popup.isVisible(), timeout=20_000)
    assert popup.current().label.lower() == "bump"


@needs_fortls
def test_editor_context_menu_commands(qtbot, lsp_window, project):
    foo = open_editor(lsp_window, project / "src/foo.f90")
    main = open_editor(lsp_window, project / "app/main.f90")
    qtbot.waitUntil(lambda: main.editor.hover_provider is not None, timeout=20_000)
    cursor_at(main, 3, 8)  # bump
    menu = main.editor.context_menu(main.editor.cursorRect().center())
    actions = {a.text(): a for a in menu.actions()}
    assert actions["Go to Definition"].isEnabled() and "Rename Symbol…" in actions
    actions["Go to Definition"].trigger()
    qtbot.waitUntil(lambda: lsp_window.current_editor() is foo, timeout=20_000)
    blank = main.editor.context_menu(main.editor.cursorRect().center() + main.editor.cursorRect().bottomRight() * 40)
    assert not {a.text(): a for a in blank.actions()}["Go to Definition"].isEnabled()  # (not on a name)


# ------------------------------------- preprocessor, diagnostics, implementation
SHAPES = """\
module shapes
  implicit none
  type :: circle
    real :: r
  contains
    procedure :: area => circle_area
  end type circle
contains
  real function circle_area(self)
    class(circle), intent(in) :: self
    circle_area = 3.14 * self%r**2
  end function circle_area
end module shapes
"""
BAD = "module bad\n  implicit none\n  use shapes\n  integer :: x\n  integer :: x\nend module bad\n"
PP = ("module pp\n  implicit none\ncontains\n#ifdef USE_FAST\n  subroutine fast_path()\n  end subroutine fast_path\n"
      "#else\n  subroutine slow_path()\n  end subroutine slow_path\n#endif\nend module pp\n")


@pytest.fixture
def oo_project(tmp_path):
    from conftest import write_files

    return write_files(tmp_path / "oo", {
        "fpm.toml": 'name = "oo"\nversion = "1.2.0"\n[preprocess]\n[preprocess.cpp]\n'
                    'macros = ["USE_FAST", "N=3", "VER={version}"]\nsuffixes = ["F90"]\n',
        "src/shapes.f90": SHAPES, "src/bad.f90": BAD, "src/pp.F90": PP,
        "include/defs.h": "#define X 1\n",
        "app/main.f90": "program main\n  use shapes\n  type(circle) :: c\n  c%r = 1.0\n  print *, c%area()\n"
                        "end program main\n",
    })


def test_preprocessor_args(oo_project, project):
    import json

    from vibefpmgui import manifest as m

    args = lsp.preprocessor_args(m.resolve(oo_project / "fpm.toml"), oo_project)
    assert json.loads(args[args.index("--pp_defs") + 1]) == {"USE_FAST": "", "N": "3", "VER": "1.2.0"}
    assert args[args.index("--pp_suffixes") + 1] == ".F90"
    assert args[args.index("--include_dirs") + 1] == str((oo_project / "include").resolve())
    assert "--pp_defs" in lsp.server_args(m.resolve(oo_project / "fpm.toml"), oo_project)
    # No preprocessor settings: fortls's defaults, as before.
    assert lsp.preprocessor_args(m.resolve(project / "fpm.toml"), project) == []
    # A .fortlsrc says it all.
    (oo_project / ".fortlsrc").write_text("{}")
    assert "--pp_defs" not in lsp.server_args(m.resolve(oo_project / "fpm.toml"), oo_project)


def test_diagnostic_issues():
    issues = lsp.diagnostic_issues([
        {"range": rng(2, 2, 2, 12), "message": "USE statements after\n IMPLICIT statement", "severity": 1},
        {"range": rng(4, 0), "message": "a warning"},
        {"message": "no range"},
    ])
    assert [(i.line, i.column, i.severity, i.message) for i in issues] == [
        (3, 3, "fortls error", "USE statements after IMPLICIT statement"), (5, 1, "fortls", "a warning")]
    assert not issues[0].fixable and issues[0].describe().startswith("fortls: ")


@pytest.fixture
def oo_window(make_window, oo_project, settings_file):
    settings_file.write_text("[General]\nfortls=true\nlint=false\n")
    return make_window(oo_project / "fpm.toml")


@needs_fortls
def test_diagnostics_in_the_editor(qtbot, oo_window, oo_project):
    editor = open_editor(oo_window, oo_project / "src/bad.f90")
    qtbot.waitUntil(lambda: len(editor._lsp_issues) == 2, timeout=20_000)
    # (fortls marks the IMPLICIT line for "USE after IMPLICIT")
    assert {(i.line, i.severity) for i in editor._lsp_issues} == {(2, "fortls error"), (5, "fortls error")}
    assert {2, 5} <= set(editor.editor.markers) and "2 fortls issues" in editor.status.text()
    card = editor.editor.lint_card_html([i for i in editor.editor.lint_issues if i.line == 2])
    assert "FORTLS (checks the saved file)" in card and "USE statements after IMPLICIT" in card
    oo_window.lsp_diagnostics_action.setChecked(False)
    assert not editor._lsp_issues and 2 not in editor.editor.markers
    oo_window.lsp_diagnostics_action.setChecked(True)  # (the latest come back at once)
    assert len(editor._lsp_issues) == 2
    assert oo_window.settings.value("lsp_diagnostics") in (True, "true")


@needs_fortls
def test_go_to_implementation(qtbot, oo_window, oo_project):
    shapes = open_editor(oo_window, oo_project / "src/shapes.f90")
    main = open_editor(oo_window, oo_project / "app/main.f90")
    qtbot.waitUntil(lambda: main.editor.hover_provider is not None, timeout=20_000)
    cursor_at(main, 5, 15)  # area, in c%area()
    oo_window.definition_action.trigger()  # (the binding)
    qtbot.waitUntil(lambda: oo_window.current_editor() is shapes
                    and shapes.editor.textCursor().blockNumber() == 5, timeout=20_000)
    oo_window.tabs.setCurrentWidget(main)
    cursor_at(main, 5, 15)
    actions = {a.text(): a for a in main.editor.context_menu(main.editor.cursorRect().center()).actions()}
    actions["Go to Implementation"].trigger()  # (the procedure it binds)
    qtbot.waitUntil(lambda: oo_window.current_editor() is shapes
                    and shapes.editor.textCursor().blockNumber() == 8, timeout=20_000)
    assert oo_window.implementation_action.shortcut().toString() == "Ctrl+F12"
    cursor_at(shapes, 4, 4)  # (real: no implementation)
    shapes.go_to_implementation()
    qtbot.waitUntil(lambda: "no implementation found" in shapes.status.text(), timeout=20_000)


@needs_fortls
def test_preprocessor_branches_follow_fpm_toml(qtbot, oo_window, oo_project):
    pp = open_editor(oo_window, oo_project / "src/pp.F90")
    qtbot.waitUntil(lambda: pp.outline_box.count() == 2, timeout=20_000)
    assert pp.outline_box.itemText(1) == "fast_path"  # (USE_FAST is defined in fpm.toml)
