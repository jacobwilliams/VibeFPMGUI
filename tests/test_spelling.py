"""Spell checking the editor's comments and strings."""

from __future__ import annotations

import pytest
from conftest import open_editor, write_files
from pygments import lex
from pygments.lexers import FortranLexer, MarkdownLexer
from qtpy.QtCore import QEvent, QPoint
from qtpy.QtGui import QHelpEvent, QTextCursor

from vibefpmgui import spelling
from vibefpmgui.highlighter import PROSE_LEXERS, token_role

pytestmark = pytest.mark.skipif(not spelling.available(), reason="pyspellchecker not installed")

SOURCE = """\
!> Teh solver for the namelist (see e.g. notes.md)
module solver_mod
  implicit none
  real :: f_star  ! f_star is the tolerence
contains
  subroutine report()
    !$omp parallel do
    print *, "Convergnce reached"
  end subroutine report
end module solver_mod
"""


@pytest.fixture(autouse=True)
def no_user_words(monkeypatch):
    monkeypatch.setattr(spelling, "user_words", set())
    yield
    spelling.set_language("en")  # (a test may switch: back to the default)


def words(text: str, prose=None, names=frozenset()) -> list[str]:
    return [i.word for i in spelling.check(text, prose or [(0, len(text))], names)]


def test_only_real_words_are_checked():
    text = ("Teh x1 f_star e.g. foo.f90 x=1 ALLCAPS camelCase http://exmaple.org "
            "well-knwon solver's (paralel) don't enddo Recieve")
    assert words(text) == ["Teh", "knwon", "paralel", "Recieve"]
    assert words("the tolerence of wombats", names={"tolerence"}) == []  # (a name in the code)
    assert words("!$omp paralell do\n!DIR$ IVDEPP") == []  # (directives)


def test_positions_and_suggestions():
    issue = spelling.check("ok\n  ! a recieve\n", [(5, 11)])[0]
    assert (issue.line, issue.column, issue.end_column, issue.word) == (2, 7, 14, "recieve")
    assert issue.severity == "spelling" and not issue.fixable and "recieve" in issue.describe()
    assert spelling.suggestions("recieve")[0] == "receive"
    assert spelling.suggestions("Recieve")[0] == "Receive"  # (in the word's capitalization)
    spelling.user_words.add("recieve")
    assert spelling.check("a recieve", [(0, 9)]) == []


def test_comments_and_strings_are_prose():
    roles = {value.strip(): token_role(ttype, False) for ttype, value in lex(SOURCE, FortranLexer())}
    assert roles["!> Teh solver for the namelist (see e.g. notes.md)"] == "prose"
    assert roles['"Convergnce reached"'] == "prose"
    assert roles["f_star"] == roles["subroutine"] == "name"
    assert "Markdown" in PROSE_LEXERS
    md = {v: token_role(t, True) for t, v in lex("Some txet `cdoe`\n", MarkdownLexer())}
    assert md["txet"] == "prose" and md["`cdoe`"] is None  # (inline code isn't)


@pytest.fixture
def editor(qtbot, window, project):
    write_files(project, {"src/solver.f90": SOURCE})
    editor = open_editor(window, project / "src/solver.f90")
    qtbot.waitUntil(lambda: len(editor._spelling_issues) == 3, timeout=5_000)
    return editor


def spot(editor, word: str) -> QPoint:
    code = editor.editor
    cursor = code.document().find(word)
    cursor.setPosition(cursor.selectionStart() + 1)
    return code.cursorRect(cursor).center()


def test_misspelled_words_are_underlined(editor):
    assert [i.word for i in editor._spelling_issues] == ["Teh", "tolerence", "Convergnce"]
    assert {i.word for i in editor.editor.lint_issues if i.severity == "spelling"} == {
        "Teh", "tolerence", "Convergnce"}
    assert "3 misspelled words" in editor.status.text()
    assert 1 not in editor.editor.markers  # (no gutter marks: the underline is enough)


def test_choosing_a_suggestion(qtbot, editor):
    code = editor.editor
    menu = code.context_menu(spot(editor, "tolerence"))
    texts = [a.text() for a in menu.actions()]
    assert texts[0] == "tolerance" and "Add “tolerence” to Dictionary" in texts
    menu.actions()[0].trigger()
    assert "f_star is the tolerance" in code.toPlainText()
    code.undo()  # (one step)
    assert "f_star is the tolerence" in code.toPlainText()


def test_hover_card_links(qtbot, editor):
    code = editor.editor
    issue = next(i for i in code.lint_issues if i.word == "Convergnce")
    card = code.lint_card_html([issue])
    n = code.lint_issues.index(issue)
    assert "SPELLING" in card and f"spell:{n}:Convergence" in card and f"addword:{n}" in card
    from qtpy.QtCore import QUrl
    code.hover_popup._link_clicked(QUrl(f"spell:{n}:Convergence"))
    assert '"Convergence reached"' in code.toPlainText()
    qtbot.waitUntil(lambda: len(editor._spelling_issues) == 2, timeout=5_000)


def test_add_to_dictionary(qtbot, window, editor):
    menu = editor.editor.context_menu(spot(editor, "Teh"))
    next(a for a in menu.actions() if a.text().startswith("Add “Teh”")).trigger()
    assert [i.word for i in editor._spelling_issues] == ["tolerence", "Convergnce"]
    assert "teh" in window.settings.value("spelling_words").split()


def test_turned_off_from_the_menu(qtbot, window, editor):
    window.spelling_action.setChecked(False)
    assert not editor._spelling_issues and "misspelled" not in editor.status.text()
    assert not any(i.severity == "spelling" for i in editor.editor.lint_issues)
    assert window.settings.value("spelling") in (False, "false")
    window.spelling_action.setChecked(True)
    assert len(editor._spelling_issues) == 3


def test_rechecked_after_typing(qtbot, editor):
    code = editor.editor
    cursor = QTextCursor(code.document())
    cursor.movePosition(QTextCursor.End)
    cursor.insertText("! one more mistaek\n")
    qtbot.waitUntil(lambda: "mistaek" in [i.word for i in editor._spelling_issues], timeout=5_000)


def test_gutter_card_leaves_out_spelling(editor):
    code = editor.editor
    y = code.cursorRect(QTextCursor(code.document().findBlockByNumber(0))).center().y()
    point = QPoint(4, y)
    code.gutter.event(QHelpEvent(QEvent.ToolTip, point, code.gutter.mapToGlobal(point)))
    assert not code.hover_popup.isVisible()


def test_easy_to_see_on_a_dark_background(editor):
    from qtpy.QtGui import QColor, QPalette

    from vibefpmgui.editor import DARK_UNDERLINE_COLORS, MARKER_COLORS

    code = editor.editor
    word = code.document().find("tolerence")
    # (drawn by the editor, not a Qt underline or highlight)
    assert not any(s.cursor.selectionStart() == word.selectionStart() for s in code.extraSelections())
    spans = code.squiggle_spans(code.viewport().rect())
    assert len(spans) == 3 and {s[3] for s in spans} == {MARKER_COLORS["spelling"]}
    start = QTextCursor(code.document())
    start.setPosition(word.selectionStart())
    rect = code.cursorRect(start)
    left, right, y, _ = next(s for s in spans if abs(s[0] - rect.left()) < 3)
    assert right > left and rect.top() < y <= rect.bottom() + 2  # (under the word)
    palette = code.palette()
    palette.setColor(QPalette.Base, QColor("#0d1117"))  # (e.g. github-dark)
    code.setPalette(palette)
    assert {s[3] for s in code.squiggle_spans(code.viewport().rect())} == {DARK_UNDERLINE_COLORS["spelling"]}
    assert not code.viewport().grab().isNull()  # (painted)


def test_another_language():
    assert {"en", "fr", "de"} <= set(spelling.languages())
    assert "pomme" in words("le garçon mange une pomme")  # (not an English word)
    spelling.set_language("fr")
    assert spelling.language == "fr"
    assert words("le garçon mange une pomme") == []  # (accented letters are letters too)
    assert words("le garcon") == ["garcon"] and spelling.suggestions("garcon")[0] == "garçon"
    spelling.set_language("xx")  # (no such dictionary: unchanged)
    assert spelling.language == "fr"


def test_language_menu(qtbot, window, editor):
    menu = window.spelling_language_menu
    names = [a.text() for a in menu.actions()]
    assert "English (en)" in names and "French (fr)" in names
    assert next(a for a in menu.actions() if a.data() == "en").isChecked()
    french = next(a for a in menu.actions() if a.data() == "fr")
    french.trigger()
    assert french.isChecked() and window.settings.value("spelling_language") == "fr"
    assert "solver" in [i.word for i in editor._spelling_issues]  # (checked again, in French)


def test_spelling_items_are_grouped_in_the_view_menu(window):
    view = next(o for o in window.spelling_action.associatedObjects() if hasattr(o, "actions"))
    actions = view.actions()
    i = actions.index(window.spelling_action)
    assert actions[i - 1].isSeparator()
    assert actions[i + 1].text() == "Spelling Language"
    assert actions[i + 2].isSeparator()
