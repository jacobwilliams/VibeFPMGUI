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


def test_a_big_lint_answer_is_read_on_a_thread_and_a_late_one_dropped(qtbot, window, project, monkeypatch):
    from vibefpmgui import editor_checks

    write_files(project, {"src/big.f90": big_text(200)})
    editor = open_editor(window, project / "src/big.f90")
    issue = '{"code": "C001", "message": "m", "location": {"line": %d, "column": 1}, ' \
            '"end_location": {"line": %d, "column": 2}, "fix": null}'
    output = "[" + ", ".join(issue % (n, n) for n in range(1, 101)) + "]"
    monkeypatch.setattr(editor_checks, "PARSE_APART", 10)  # (any answer: "big")
    monkeypatch.setattr(type(editor), "can_lint", lambda self: True)
    later = []
    monkeypatch.setattr(editor_checks, "call_in_background", lambda work, then, parent: later.append((work, then)))

    class Answer:
        def readAllStandardOutput(self):
            return output.encode()

        def deleteLater(self):
            pass

    editor._lint_sent_text = editor.editor.toPlainText()  # (as a run sets it)
    for _ in range(2):  # (two answers, the first one's reading done last)
        editor._lint_process = answer = Answer()
        editor._lint_finished(answer)
    assert editor._fortitude_issues == [] and len(later) == 2  # (not read here)
    work, then = later[1]
    then(work())
    assert len(editor._fortitude_issues) == 100
    editor._fortitude_issues = []
    work, then = later[0]
    then(work())  # (the older answer, late: dropped)
    assert editor._fortitude_issues == []


def test_a_big_text_is_spell_checked_on_a_thread(qtbot, window, project, monkeypatch):
    from vibefpmgui import editor_checks

    if not spelling.available():
        return
    write_files(project, {"src/big.f90": big_text(300) + "! a mispeled word\n"})
    editor = open_editor(window, project / "src/big.f90")
    qtbot.waitUntil(editor.highlighter.is_current, timeout=10_000)
    monkeypatch.setattr(editor_checks, "PARSE_APART", 10)
    later = []
    monkeypatch.setattr(editor_checks, "call_in_background", lambda work, then, parent: later.append((work, then)))
    editor._spelling_issues = []
    editor.check_spelling()
    assert later and editor._spelling_issues == []  # (on a thread)
    work, then = later[-1]
    then(work())
    assert [i.word for i in editor._spelling_issues] == ["mispeled"]
    editor._spelling_issues = []
    editor.check_spelling()
    work, then = later[-1]
    editor.highlighter._text = editor.highlighter._text + "x"  # (edited meanwhile: the answer is for another text)
    then(work())
    assert editor._spelling_issues == []
