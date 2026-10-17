"""A big file with many issues stays quick to edit: highlights only around
the view, spelling checked once per comment, colors applied a chunk at a
time, trimming and finding at once."""

from __future__ import annotations

import time

from conftest import open_editor, write_files
from qtpy.QtGui import QTextCursor

from vibefpmgui import highlighter as hl
from vibefpmgui import lint, spelling
from vibefpmgui.code_editor import CodeEditor

LINES = 20_000


def big_text(lines: int = LINES) -> str:
    return "".join(f"x{i} = {i}  ! a comment about the value {i}\n" for i in range(lines))


def test_highlights_only_around_the_view(qtbot):
    code = CodeEditor()
    qtbot.addWidget(code)
    code.resize(600, 400)
    code.show()
    code.setPlainText(big_text())
    issues = [lint.LintIssue(n, 1, n, 3, "C001", "an issue", "") for n in range(1, LINES + 1)]
    code.set_lint(issues)
    code.set_markers({n: [("warning", "a note")] for n in range(1, LINES + 1)})
    near = len(code.extraSelections())
    assert near < 1000  # (not 40 000: two per line of the file)
    lines = {s.cursor.blockNumber() for s in code.extraSelections()}
    assert 0 in lines and max(lines) < 500
    code.moveCursor(QTextCursor.End)  # (scrolled to the end, the cursor there: highlighted there)
    code.verticalScrollBar().setValue(code.verticalScrollBar().maximum())
    lines = {s.cursor.blockNumber() for s in code.extraSelections()}
    assert LINES - 1 in lines and min(lines) > LINES - 500
    started = time.perf_counter()
    cursor = code.textCursor()
    cursor.select(QTextCursor.Document)
    cursor.insertText(big_text())  # (with 40 000 highlights, each a cursor Qt moved along: tens of seconds)
    assert time.perf_counter() - started < 5


def test_spelling_checks_each_comment_once(monkeypatch):
    if not spelling.available():
        return
    text = big_text(2000) + "! a mispeled word\n"
    prose = [(m, len(line) - (m - start)) for start, line, m in
             ((pos, line, pos + line.index("!")) for pos, line in _lines(text))]
    checked = []
    real = spelling._unknown_words
    monkeypatch.setattr(spelling, "_unknown_words", lambda segment: checked.append(segment) or real(segment))
    first = spelling.check(text, prose)
    assert [i.word for i in first] == ["mispeled"] and len(checked) == 2001
    checked.clear()
    assert spelling.check(text, prose) == first and checked == []  # (unchanged: nothing checked again)
    spelling.user_words.add("mispeled")
    try:
        assert spelling.check(text, prose) == [] and len(checked) == 2001  # (another dictionary: all again)
    finally:
        spelling.user_words.discard("mispeled")


def _lines(text: str):
    pos = 0
    for line in text.splitlines(keepends=True):
        yield pos, line.rstrip("\n")
        pos += len(line)


def test_big_files_are_colored_a_chunk_at_a_time(qtbot, monkeypatch):
    code = CodeEditor()
    qtbot.addWidget(code)
    code.setPlainText(big_text(hl.CHUNK_LINES * 3))
    view = hl.CHUNK_LINES * 2  # (the line on view: colored first)
    chunks = []
    real = hl.PygmentsHighlighter._recolor_lines
    monkeypatch.setattr(hl.PygmentsHighlighter, "_recolor_lines",
                        lambda self, first, last: chunks.append((first, last)) or real(self, first, last))
    highlighter = hl.PygmentsHighlighter(code.document(), hl.lexer_for_path("x.f90"), "default")
    highlighter.line_on_view = lambda: view
    relexed = []
    highlighter.relexed.connect(lambda: relexed.append(True))
    qtbot.waitUntil(lambda: bool(relexed), timeout=20_000)
    assert chunks[0][0] <= view <= chunks[0][1] and len(chunks) > 1  # (on view first, then the rest)
    assert all(last - first < hl.CHUNK_LINES for first, last in chunks)
    covered = sorted(line for first, last in chunks for line in range(first, last + 1))
    assert covered == list(range(hl.CHUNK_LINES * 3 + 1))  # (every line once)
    assert code.document().firstBlock().layout().formats() and highlighter.is_current()


def test_an_edit_while_coloring_colors_everything_again(qtbot):
    code = CodeEditor()
    qtbot.addWidget(code)
    code.setPlainText(big_text(hl.CHUNK_LINES * 3))
    highlighter = hl.PygmentsHighlighter(code.document(), hl.lexer_for_path("x.f90"), "default")
    qtbot.waitUntil(lambda: bool(highlighter._pending), timeout=10_000)
    QTextCursor(code.document()).insertText("! new first line\n")
    assert not highlighter._pending and highlighter._full  # (dropped: all of it, once lexed again)
    qtbot.waitUntil(highlighter.is_current, timeout=20_000)
    assert code.document().lastBlock().previous().layout().formats()


def test_trimming_a_big_file(window, project):
    text = big_text(5000).replace("= 7  !", "= 7 \t  !").replace("comment about the value 9\n", "comment 😀 9   \n")
    write_files(project, {"src/big.f90": text})
    editor = open_editor(window, project / "src/big.f90")
    assert editor.trim_trailing_whitespace() == 1  # (the line with 😀: Qt counts it twice, lines aren't)
    assert editor.editor.toPlainText() == text.replace("comment 😀 9   \n", "comment 😀 9\n")
    assert editor.trim_trailing_whitespace() == 0


def test_the_find_bar_finds_as_qt_does(window, project):
    write_files(project, {"src/f.f90": "count = count_all + Count\n! count😀 count\n"})
    editor = open_editor(window, project / "src/f.f90")
    bar = editor.find_bar
    bar.open_bar()
    bar.find_edit.setText("count")
    for case in (False, True):
        for word in (False, True):
            bar.case_check.setChecked(case)
            bar.word_check.setChecked(word)
            editor.editor.setPlainText("count = count_all + Count\n! count count\n")
            fast = bar._all_matches()
            assert fast == bar._qt_matches(), (case, word)
    editor.editor.setPlainText("! 😀 count count\n")  # (beyond U+FFFF: Qt's own search, its positions)
    assert bar._all_matches() == bar._qt_matches() and len(bar._all_matches()) == 2
