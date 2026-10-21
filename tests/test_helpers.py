"""Modules without windows: manifests, ANSI colors, lint output, edits, lexers."""

from __future__ import annotations

import json

import pytest
from qtpy.QtGui import QColor

from vibefpmgui import ansi, lint
from vibefpmgui import highlighter as hl
from vibefpmgui import manifest as m
from vibefpmgui.lsp import TextEdit, apply_edits


# ------------------------------------------------------------- manifests
def test_resolve_and_conflicts(conflict_project):
    graph = m.resolve(conflict_project / "fpm.toml")
    assert graph.root == "conflictdemo"
    assert {e.child for e in graph.children("conflictdemo")} == {"libA", "libB", "test-drive"}
    assert {e.parent for e in graph.parents("test-drive")} == {"conflictdemo", "libA", "libB"}
    conflicts = m.conflicts(graph)
    assert list(conflicts) == ["test-drive"]
    declarations = conflicts["test-drive"]
    assert declarations[0].parent == "conflictdemo"  # (the one fpm uses)
    keys = {d.parent: m.spec_key(d) for d in declarations}
    assert keys["libB"] == keys["conflictdemo"] != keys["libA"]  # (".git" makes no difference)
    assert "v0.4.0" in m.describe_spec(next(d for d in declarations if d.parent == "libA").spec)


def test_no_conflicts(project):
    assert m.conflicts(m.resolve(project / "fpm.toml")) == {}


# ----------------------------------------------------------- ANSI colors
def test_ansi_strip_and_partial():
    colored = "\x1b[0;31;1msemantic error\x1b[0;0m: x"
    assert ansi.strip(colored) == "semantic error: x"
    assert ansi.split_partial("abc\x1b[0;3") == ("abc", "\x1b[0;3")
    assert ansi.split_partial("abc") == ("abc", "")


def test_ansi_writer(qtbot):
    from qtpy.QtWidgets import QPlainTextEdit

    edit = QPlainTextEdit()
    qtbot.addWidget(edit)
    writer = ansi.AnsiWriter(edit)
    writer.write("plain \x1b[31mred\x1b[0m done")
    assert edit.toPlainText() == "plain red done"
    fragments, it = {}, edit.document().firstBlock().begin()
    while not it.atEnd():
        fragment = it.fragment()
        fragments[fragment.text()] = fragment.charFormat().foreground().color()
        it += 1
    red = fragments["red"]
    assert red.red() > 2 * red.green() and red.red() > 2 * red.blue()


# ------------------------------------------------------------ lint output
def test_lint_parse():
    output = json.dumps([{
        "code": "C121", "message": "use of `count` without only",
        "location": {"line": 2, "column": 3}, "end_location": {"line": 2, "column": 10},
        "fix": {"applicability": "safe", "message": "add only",
                "edits": [{"content": ", only: x", "location": {"line": 2, "column": 10},
                           "end_location": {"line": 2, "column": 10}}]},
    }, {
        "code": "E001", "message": "syntax error",
        "location": {"line": 5, "column": 1}, "end_location": {"line": 5, "column": 4},
    }])
    fixable, error = lint.parse(output)
    assert (fixable.line, fixable.code, fixable.fixable, fixable.fix_safe) == (2, "C121", True, True)
    assert fixable.edits[0] == lint.FixEdit(1, 9, 1, 9, ", only: x")
    assert fixable.severity == "lint" and error.severity == "lint error" and not error.fixable
    assert lint.parse("not json") == []


@pytest.mark.skipif(lint.executable() is None, reason="fortitude not installed")
def test_fortitude_fix(tmp_path):
    source = "program p\n  implicit none\n  integer :: x\n  x = 1   \nend program p\n"
    path = tmp_path / "p.f90"
    path.write_text(source)
    fixed, error = lint.fix_text(source, path)
    assert error == "" and fixed is not None


# ------------------------------------------------------------------ edits
def test_apply_edits():
    text = "module foo\nend module foo\n"
    edits = [TextEdit(0, 7, 0, 10, "bar"), TextEdit(1, 11, 1, 14, "bar")]
    assert apply_edits(text, edits) == "module bar\nend module bar\n"
    assert apply_edits("abc", [TextEdit(0, 1, 0, 2, "")]) == "ac"


# ----------------------------------------------------------------- lexers
@pytest.mark.parametrize(("name", "lexer"), [
    ("a.f90", "Fortran"),
    ("a.F90", "Fortran"),
    ("a.f", "FortranFixed"),
    ("a.f08", "Fortran"),
    ("a.fypp", "Fortran (fypp)"),
    ("a.FYPP", "Fortran (fypp)"),
    ("fpm.toml", "TOML"),
    ("x.code-workspace", "JSON with Comments"),
    ("unknown.zzz", "Text only"),
])
def test_lexer_for_path(name, lexer):
    assert hl.lexer_for_path(name).name == lexer


def test_fypp_is_fortran_with_its_preprocessing():
    from pygments.token import Comment, Keyword, Name

    code = ("#:for k in KINDS\n"
            "  real(${k}$) :: x_${k}$_y  ! Fortran's comment\n"
            "  @:assert(n > 0)\n"
            "  $:mkname('a')\n"
            "  #! fypp's comment\n"
            "  call @{name}@(1)\n"
            "#:endfor\n")
    tokens = [(t, v) for t, v in hl.lexer_for_path("a.fypp").get_tokens(code) if v.strip()]
    preproc = [v for t, v in tokens if t in Comment.Preproc]
    assert preproc == ["#:for k in KINDS", "${k}$", "${k}$", "@:assert(n > 0)", "$:mkname('a')",
                       "#! fypp's comment", "@{name}@", "#:endfor"]
    assert (Keyword.Type, "real") in tokens and (Name, "x_") in tokens and (Name, "_y") in tokens
    assert any(t in Comment and t not in Comment.Preproc and "Fortran's" in v for t, v in tokens)
    assert not any(t in hl.Token.Error for t, _v in tokens)


def test_no_file_name_is_plain_text_without_a_search(monkeypatch):
    """(Pygments went through every lexer for "": the Git panel's empty diff panes, 40 ms at start-up.)"""
    monkeypatch.setattr(hl, "get_lexer_for_filename", lambda name: pytest.fail("looked up"))
    assert hl.lexer_for_path("").name == "Text only"


def test_token_format():
    from pygments.styles import get_style_by_name
    from pygments.token import Comment, Keyword

    style = get_style_by_name("monokai")
    keyword, comment = hl.token_format(style, Keyword), hl.token_format(style, Comment)
    assert keyword.foreground().color() != comment.foreground().color()
    assert keyword.foreground().color() == QColor("#" + style.style_for_token(Keyword)["color"])
