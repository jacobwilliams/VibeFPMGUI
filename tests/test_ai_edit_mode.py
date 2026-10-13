"""The Review panel's Edit mode: the AI proposes edits, shown as cards to
accept or reject; and what a conversation sends the model (ai_context).
No real model: FakeOllama gives canned answers."""

from __future__ import annotations

import pytest
from conftest import open_editor
from test_ai_review import FakeOllama, ask

from vibefpmgui import ai_context as ctx
from vibefpmgui import ai_edits as ed
from vibefpmgui import ai_review as ai


def block(search: str, replace: str) -> str:
    return f"<<<<<<< SEARCH\n{search}\n=======\n{replace}\n>>>>>>> REPLACE\n"


TWO_EDITS = ("I'll rename the counter and add intent.\n\n"
             + block("  integer :: count_all", "  integer :: count_total")
             + "\nand\n```fortran\n" + block("    count = count + 1 ! count", "    count = count + 2 ! count")
             + "```\nDone.\n")


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama(answer=TWO_EDITS)
    monkeypatch.setattr(ai, "make_client", lambda host=None, timeout=None: fake)
    monkeypatch.setattr(ai, "ollama_available", lambda: True)
    return fake


def ask_edit(qtbot, window, request):
    window.review_monitor.set_chat_kind("edit")
    ask(qtbot, window, request)


def test_edits_become_cards_to_accept_or_reject(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    original = editor.editor.toPlainText()
    monitor = window.review_monitor
    ask_edit(qtbot, window, "rename count_all, and count by 2")
    sent = ollama.chats[-1]
    assert sent["messages"][0]["content"] == ai.EDIT_SYSTEM_PROMPT and sent["options"]["temperature"] == 0.1
    assert "  integer :: count_all\n" in sent["messages"][-1]["content"]  # (the file as it is: no numbers)
    assert sent["options"]["num_predict"] == ai.EDIT_ANSWER_TOKENS
    cards = monitor.edit_list.cards
    assert len(cards) == 2 and monitor.edit_list.isVisibleTo(monitor)
    assert [(e.line, e.status) for e in window._edits] == [(3, ed.PENDING), (6, ed.PENDING)]
    assert "✎ Edit 1" in monitor.explanation.toPlainText() and "SEARCH" not in monitor.explanation.toPlainText()
    assert "- " in cards[0].diff.text() or "count_all" in cards[0].diff.text()

    cards[0].accept_button.click()
    assert "count_total" in editor.editor.toPlainText() and window._edits[0].status == ed.APPLIED
    assert editor.is_dirty() and not cards[0].accept_button.isVisibleTo(cards[0])
    cards[1].reject_button.click()
    assert window._edits[1].status == ed.REJECTED and "count + 2" not in editor.editor.toPlainText()
    editor.editor.undo()
    assert editor.editor.toPlainText() == original


def test_accept_all_is_one_undo_step(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    original = editor.editor.toPlainText()
    ask_edit(qtbot, window, "both")
    window.review_monitor.edit_list.accept_all.click()
    text = editor.editor.toPlainText()
    assert "count_total" in text and "count + 2" in text
    assert all(e.status == ed.APPLIED for e in window._edits)
    assert not window.review_monitor.edit_list.accept_all.isEnabled()
    editor.editor.undo()
    assert editor.editor.toPlainText() == original


def test_an_edit_whose_code_changed_turns_stale(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    ask_edit(qtbot, window, "both")
    code = editor.editor
    code.selectAll()
    code.insertPlainText(code.toPlainText().replace("count_all", "count_every"))  # (the user, meanwhile)
    window.accept_edit(0)
    assert window._edits[0].status == ed.STALE and "count_total" not in code.toPlainText()
    assert "changed since" in window.review_monitor.edit_list.cards[0].state.text()
    window.accept_edit(1)  # (its lines are as they were: still fine)
    assert window._edits[1].status == ed.APPLIED


def test_an_edit_that_does_not_match_says_why(qtbot, window, project, ollama):
    ollama.answer = block("  real :: nowhere", "  real :: here") + block("integer", "logical")
    open_editor(window, project / "src/foo.f90")
    ask_edit(qtbot, window, "something")
    first, second = window._edits
    assert first.status == ed.FAILED and first.problem.startswith("not found")
    assert second.status == ed.FAILED and "matches 2 places" in second.problem
    card = window.review_monitor.edit_list.cards[0]
    assert card.show_button.isVisibleTo(card) and not card.diff.isVisibleTo(card)
    card.show_button.click()
    assert card.diff.isVisibleTo(card) and "nowhere" in card.diff.text()


def test_clicking_a_card_selects_its_lines(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    ask_edit(qtbot, window, "both")
    window.review_monitor.edit_list.cards[1].clicked.emit()
    assert editor.editor.textCursor().selectedText() == "    count = count + 1 ! count"


def test_edit_needs_a_file(qtbot, window, ollama):
    window.send_chat("change it", True, "edit")
    assert "Open the file" in window.statusBar().currentMessage() and not ollama.chats


def test_edit_selection_with_ai(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    code.go_to_line(5)
    cursor = code.textCursor()
    cursor.movePosition(cursor.MoveOperation.Down, cursor.MoveMode.KeepAnchor, 2)  # (lines 5–6)
    code.setTextCursor(cursor)
    assert "Edit Selection with AI…" in [a.text() for a in code.context_menu(code.cursorRect().center())
                                          .actions()]
    code.edit_selection_requested.emit()
    monitor = window.review_monitor
    assert monitor.chat_kind == "edit" and monitor.selection_note.text().startswith("lines 5–6")
    assert not monitor.chat_file_check.isEnabled() and monitor.chat_file_check.isChecked()
    ask(qtbot, window, "count by 2")
    request = ollama.chats[-1]["messages"][-1]["content"]
    assert "Change only these lines (lines 5–6" in request and "  subroutine bump()\n    count =" in request
    assert not monitor.selection_note.isVisibleTo(monitor)  # (used once)
    monitor.set_chat_kind("ask")
    assert monitor.chat_file_check.isEnabled()


def test_follow_ups_send_the_file_once_per_version(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    ask_edit(qtbot, window, "first")
    window.accept_edit(0)  # (a new version of the file)
    ollama.answer = "Sure.\n"
    ask(qtbot, window, "anything else?")
    messages = ollama.chats[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert ctx.placeholder(ctx.Attachment("foo.f90", "", "")) in messages[1]["content"]  # (the old copy)
    assert "count_total" in messages[-1]["content"]  # (the new version, sent again)
    assert messages[2]["content"] == TWO_EDITS  # (the answer as the model wrote it)
    assert messages[3]["content"].startswith("(About your previous answer's edits: edit 1, at line 3, was "
                                             "applied; edit 2, at line 6, is not applied yet.)")
    ask(qtbot, window, "and now?")  # (unchanged since: not sent again)
    last = ollama.chats[-1]["messages"][-1]["content"]
    assert "count_total" not in last and "is as I sent it above" in last
    assert "context ~" in window.review_monitor.progress_label.text()
    assert editor is window.current_editor()


# ------------------------------------------------------- ai_context
def turn(question: str, answer: str = "", attachment=None) -> ctx.Turn:
    return ctx.Turn(question, question, attachment, answer)


def test_compose_leaves_out_the_oldest_turns_first():
    past = [turn(f"question {i}", "x" * 3000) for i in range(10)]
    messages, tokens, dropped = ctx.compose(past, turn("now"), "system", context=8192)
    assert dropped > 0 and tokens <= 8192 * ctx.BUDGET
    assert messages[0]["content"] == "system" and messages[-1]["content"] == "now"
    assert messages[1]["content"] == f"question {dropped}"  # (the oldest ones went)


def test_compose_keeps_the_file_when_its_turn_goes():
    file = ctx.Attachment("f.f90", "text", "THE FILE")
    past = [turn("q0", "a", file)] + [turn(f"q{i}", "x" * 3000) for i in range(1, 8)]
    messages, _tokens, dropped = ctx.compose(past, turn("now", attachment=file), "system", context=8192)
    assert dropped > 0
    assert "THE FILE" in messages[-1]["content"]  # (its turn left out: sent with the new one)


def big_file() -> str:
    parts = ["module big", "  use iso_fortran_env", "  implicit none", "contains"]
    for k in range(60):
        parts += [f"  subroutine s{k}(x)", "    real, intent(inout) :: x"]
        parts += [f"    x = x + {i}" for i in range(10)]
        parts += [f"  end subroutine s{k}"]
    return "\n".join(parts + ["end module big"]) + "\n"


def test_a_big_file_is_sent_in_part():
    text = big_file()
    lines = ctx.file_lines(text)
    line = lines.index("  subroutine s30(x)") + 5  # (inside s30)
    a = ctx.file_attachment("big.f90", text, numbers=False, context=32768, cursor_line=line)
    assert a.partial and "too long to send all of it" in a.shown
    assert "  subroutine s30(x)" in a.shown.split("```fortran")[1]  # (the procedure, in full)
    assert "line 1: module big" in a.shown and "subroutine s59(x)" in a.shown  # (the outline)
    assert "x = x + 3" in a.shown and a.shown.count("x = x + 3") < 5
    small = ctx.file_attachment("small.f90", "program p\nend program p\n", numbers=True, context=32768)
    assert not small.partial and "1| program p" in small.shown


def test_enclosing_procedure():
    lines = ctx.file_lines(big_file())
    start = lines.index("  subroutine s2(x)") + 1
    assert ctx.enclosing_procedure(lines, start + 4) == (start, start + 12)
    assert ctx.enclosing_procedure(lines, 2) == (2, 2)  # (not in one)


def test_fix_a_review_finding_with_ai(qtbot, window, project, ollama):
    from vibefpmgui.ai_review import Finding
    from vibefpmgui.result_tree import RESULT_ROLE

    editor = open_editor(window, project / "src/foo.f90")
    editor.show_review([Finding(6, 6, "bug", "count is never reset.")])
    window.show_panel(window.problems_panel)
    tree = window.problems_panel.tree
    qtbot.waitUntil(lambda: tree.topLevelItemCount() > 0, timeout=5_000)
    file_item = tree.topLevelItem(0)
    row = next(file_item.child(i) for i in range(file_item.childCount())
               if file_item.child(i).data(0, RESULT_ROLE)[1].source == "review")
    menu = window.problems_panel.context_menu(tree.visualItemRect(row).center())
    action = next(a for a in menu.actions() if a.text() == "Fix with AI…")
    assert action.isEnabled()
    action.trigger()
    monitor = window.review_monitor
    assert monitor.chat_kind == "edit"
    qtbot.waitUntil(lambda: not monitor.running and window._ai_thread is None, timeout=10_000)  # (sent at once)
    assert monitor.turns[0][0] == ("Address this AI review finding (bug) at line 6: count is never reset. "
                                   "Change only what's needed for it.")
    assert monitor.asked[-1] == monitor.turns[0][0]  # (Up brings it back, as if typed)
    request = ollama.chats[-1]["messages"][-1]["content"]
    assert request.startswith("Address this AI review finding") and "Change only these lines (lines 6–6" in request
    assert len(window._edits) == 2  # (the answer's edits, as cards)
