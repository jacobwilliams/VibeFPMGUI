"""Selecting text highlights its other occurrences in the file (View →
Highlight Other Occurrences of the Selection), matched as the find bar's
Match case / Whole words are set."""

from __future__ import annotations

from conftest import open_editor
from qtpy.QtGui import QTextCursor


def select(editor, line: int, column: int, length: int):
    code = editor.editor
    block = code.document().findBlockByNumber(line - 1)
    cursor = QTextCursor(block)
    cursor.setPosition(block.position() + column)
    cursor.setPosition(block.position() + column + length, QTextCursor.KeepAnchor)
    code.setTextCursor(cursor)
    code.update_occurrences()  # (what the timer does once the selection rests)


def highlighted(editor) -> list[tuple[int, str]]:
    return [(s.cursor.block().blockNumber() + 1, s.cursor.selectedText()) for s in editor.editor.occurrence_selections]


def test_other_occurrences_are_highlighted(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    # "    count = count + 1 ! count" is line 6; select its first "count".
    select(editor, 6, 4, 5)
    assert highlighted(editor) == [(2, "Count"), (3, "count"), (6, "count"), (6, "count")]  # (not itself)
    editor.find_bar.word_check.setChecked(True)  # (count_all is a different word)
    assert highlighted(editor) == [(2, "Count"), (6, "count"), (6, "count")]
    editor.find_bar.case_check.setChecked(True)
    assert highlighted(editor) == [(6, "count"), (6, "count")]


def test_the_timer_follows_the_selection(qtbot, window, project):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    block = code.document().findBlockByNumber(5)
    cursor = QTextCursor(block)
    cursor.setPosition(block.position() + 4)
    cursor.setPosition(block.position() + 9, QTextCursor.KeepAnchor)
    code.setTextCursor(cursor)
    qtbot.waitUntil(lambda: len(code.occurrence_selections) == 4, timeout=2_000)
    cursor.clearSelection()
    code.setTextCursor(cursor)
    qtbot.waitUntil(lambda: not code.occurrence_selections, timeout=2_000)


def test_what_isnt_highlighted(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    select(editor, 6, 4, 1)  # (one character)
    assert highlighted(editor) == []
    code = editor.editor
    cursor = code.textCursor()
    cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor)  # (across lines)
    code.setTextCursor(cursor)
    code.update_occurrences()
    assert highlighted(editor) == []
    select(editor, 6, 0, 4)  # (only spaces)
    assert highlighted(editor) == []


def test_the_setting(window, project):
    editor = open_editor(window, project / "src/foo.f90")
    select(editor, 6, 4, 5)
    assert highlighted(editor)
    window.matches_action.trigger()  # (off)
    assert highlighted(editor) == [] and window.settings.value("highlight_matches", type=bool) is False
    select(editor, 6, 4, 5)
    assert highlighted(editor) == []
    window.matches_action.trigger()  # (on again)
    assert len(highlighted(editor)) == 4


def test_the_highlight_shows_on_every_syntax_style():
    """(The app's selection color was all but invisible on github-dark.)"""
    from pygments.styles import get_all_styles, get_style_by_name
    from qtpy.QtGui import QColor

    from vibefpmgui.editor_shared import OCCURRENCE_CONTRAST, occurrence_color

    for name in get_all_styles():
        background = QColor(get_style_by_name(name).background_color)
        dark = background.lightness() < 128
        change = abs(occurrence_color(background).lightness() - background.lightness())
        assert change >= OCCURRENCE_CONTRAST[dark], (name, background.name(), change)
