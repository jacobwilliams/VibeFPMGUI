"""AI review, with Ollama mocked: no server, no model downloads (CI too)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from conftest import open_editor
from qtpy.QtCore import Qt
from qtpy.QtWidgets import QMessageBox

from vibefpmgui import ai_review as ai

ANSWER = """\
LINE 6: bug: count is incremented but never used afterwards.
It is also a module variable, so this changes shared state.

LINES 2-3: style: declare these variables with an explicit kind.
- L5: performance: the loop could be vectorised
SUMMARY: Mostly fine; one real problem with the counter.
"""


def part(text="", done=False, tokens=0):
    """A streamed piece of an Ollama chat answer."""
    return SimpleNamespace(message=SimpleNamespace(content=text), done=done, eval_count=tokens,
                           prompt_eval_count=900 if done else 0, total_duration=1_500_000_000 if done else 0)


class FakeOllama:
    """Stands in for ollama.Client: canned models, answers and downloads."""

    def __init__(self, answer=ANSWER, models=("qwen2.5-coder:7b",), endless=False, error=None):
        self.answer, self.models, self.endless, self.error = answer, list(models), endless, error
        self.chats: list[dict] = []
        self.pulled: list[str] = []
        self._client = SimpleNamespace(close=lambda: None)

    def list(self):
        if self.error:
            raise ConnectionError(self.error)
        return {"models": [{"model": name} for name in self.models]}

    refuses_think = False  # (a model that can't turn its thinking off)
    reasons = False        # (a model that reasons first when asked to think)
    thinks = False         # (a model that reasons before it answers, whatever it's asked)

    def chat(self, model, stream, options, messages, think=None):
        self.chats.append({"model": model, "options": options, "messages": messages, "think": think})
        if self.error:
            raise RuntimeError(self.error)
        if self.refuses_think and think is not None:
            return self._refuse()
        if self.reasons and think:
            return self._reasoned()
        return self._stream()

    def _reasoned(self):
        for thought in ("The loop ", "looks fine. "):
            yield SimpleNamespace(message=SimpleNamespace(content="", thinking=thought), done=False)
        yield from self._stream()

    def _refuse(self):
        raise RuntimeError(f'"{self.chats[-1]["model"]}" does not support thinking (status code: 400)')
        yield  # (a generator: the error comes when it's read, as Ollama's does)

    def _thinking(self, budget):
        for _ in range(budget):
            yield SimpleNamespace(message=SimpleNamespace(content="", thinking="hmm, let me think. "),
                                  done=False)
        yield SimpleNamespace(message=SimpleNamespace(content="", thinking=""), done=True, eval_count=budget,
                              prompt_eval_count=900, total_duration=1_500_000_000)

    def _stream(self):
        if self.thinks:
            yield from self._thinking(ai.MAX_ANSWER_TOKENS)
            return
        if self.endless:  # (a long answer, until stopped)
            for i in range(10_000):
                time.sleep(0.01)
                yield part(f"LINE 1: suggestion: idea number {i}\n")
        for line in self.answer.splitlines(keepends=True):
            yield part(line)
        yield part(done=True, tokens=42)

    def pull(self, model, stream):
        self.pulled.append(model)
        yield SimpleNamespace(status="pulling manifest", total=0, completed=0)
        yield SimpleNamespace(status="downloading", total=100, completed=50)
        yield SimpleNamespace(status="success", total=100, completed=100)
        self.models.append(model)


@pytest.fixture
def ollama(monkeypatch):
    fake = FakeOllama()
    monkeypatch.setattr(ai, "make_client", lambda host=None, timeout=None: fake)
    monkeypatch.setattr(ai, "ollama_available", lambda: True)
    return fake


# ------------------------------------------------------------ the pieces
def test_parse_findings():
    findings, summary = ai.parse_findings(ANSWER, max_line=10)
    assert [(f.line, f.end_line, f.category) for f in findings] == [(6, 6, "bug"), (2, 3, "style"),
                                                                      (5, 5, "performance")]
    assert findings[0].comment.startswith("count is incremented") and "shared state" in findings[0].comment
    assert "Mostly fine" in summary


def test_parse_findings_drops_hallucinated_lines_and_repeats():
    text = "LINE 99: bug: no such line\nLINE 2: note: same\nLINE 2: note: same\n1. Line 3 - error: broken\n"
    findings, _ = ai.parse_findings(text, max_line=5)
    assert [(f.line, f.category, f.comment) for f in findings] == [(2, "suggestion", "same"),
                                                                    (3, "bug", "broken")]


def test_repeat_guard():
    guard = ai.RepeatGuard()
    shown, stop = guard.feed("LINE 1: a long enough line\nshort\nshort\n")
    assert not stop and shown.count("short") == 2  # (short lines may repeat)
    shown, stop = guard.feed("LINE 1: a long enough line\nmore")
    assert stop and shown == "" and guard.repeated
    guard = ai.RepeatGuard()
    guard.feed("the last line, no newline")
    assert guard.flush() == "the last line, no newline"


def test_repeat_guard_lets_edits_and_code_repeat_lines():
    guard = ai.RepeatGuard()
    edit = ("<<<<<<< SEARCH\n    do i = 1, n\n      a(i) = 0.0\n=======\n    do i = 1, n\n"
            "      a(i) = 1.0\n>>>>>>> REPLACE\n")
    code = "```fortran\n    do i = 1, n\n```\n"
    shown, stop = guard.feed(edit + edit + code + code)  # (the new text repeats the old's lines)
    assert not stop and shown.count("do i = 1, n") == 6
    _shown, stop = guard.feed("Some explanation of the change.\nSome explanation of the change.\n")
    assert stop  # (a loop in the prose: still caught)


def test_build_prompt_and_context():
    prompt = ai.build_prompt("a.f90", "Fortran", "line one\nline two\n", "bugs", [(2, "warning", "unused")])
    assert "Review this Fortran file: a.f90" in prompt and ai.FOCUS["bugs"] in prompt
    assert "1| line one" in prompt and "2| line two" in prompt and "3|" not in prompt
    assert "line 2: warning: unused" in prompt
    assert ai.context_for(10) == 4096
    assert ai.context_for(100_000) == 32768
    assert ai.context_for(5000, answer=8000) == 16384  # (room for a longer answer)


def test_models_and_server(ollama):
    assert ai.installed_models() == ["qwen2.5-coder:7b"]
    assert ai.server_running()
    ollama.error = "connection refused"
    assert ai.installed_models() == [] and not ai.server_running()


# ---------------------------------------------------------------- worker
def run_worker(worker) -> dict:
    """Run a worker in this thread; what it emitted."""
    out = {"chunks": [], "finished": None, "failed": None, "progress": []}
    worker.chunk.connect(out["chunks"].append)
    worker.finished.connect(lambda text, stats: out.update(finished=(text, stats)))
    worker.failed.connect(lambda message: out.update(failed=message))
    worker.progress.connect(lambda status, fraction: out["progress"].append((status, fraction)))
    worker.run()
    return out


def test_worker_streams_a_review(ollama):
    out = run_worker(ai.ReviewWorker(None, "m", "the prompt"))
    text, stats = out["finished"]
    assert text == ANSWER and "".join(out["chunks"]) == ANSWER
    assert stats == {"tokens": 42, "prompt_tokens": 900, "seconds": 1.5}
    chat = ollama.chats[0]
    assert chat["messages"][1]["content"] == "the prompt" and chat["options"]["num_ctx"] == 4096


def test_thinking_is_turned_off(ollama):
    out = run_worker(ai.ReviewWorker(None, "m", "the prompt"))
    assert ollama.chats[0]["think"] is False and out["finished"][0] == ANSWER


def test_a_model_that_cannot_stop_thinking_is_asked_as_it_is(ollama):
    ollama.refuses_think = True
    out = run_worker(ai.ReviewWorker(None, "m", "the prompt"))
    assert [c["think"] for c in ollama.chats] == [False, None]  # (asked again, without it)
    assert out["finished"][0] == ANSWER


def test_reasoning_that_uses_up_the_answer_is_said_so(ollama):
    ollama.thinks = True
    out = run_worker(ai.ReviewWorker(None, "m", "the prompt"))
    text, stats = out["finished"]
    assert text == "" and stats["truncated"] and stats["thinking"]


def test_worker_stops_a_repetition_loop(ollama):
    ollama.answer = "LINE 1: bug: the same thing again\n" * 5
    out = run_worker(ai.ReviewWorker(None, "m", "p"))
    text, stats = out["finished"]
    assert text.count("the same thing") == 1 and stats.get("repeated")
    assert "repeating itself" in out["chunks"][-1]


def test_worker_reports_errors(ollama):
    ollama.error = "model 'm' not found"
    out = run_worker(ai.ReviewWorker(None, "m", "p"))
    assert out["failed"] == "model 'm' not found" and out["finished"] is None


def test_worker_downloads_a_model(ollama):
    out = run_worker(ai.ReviewWorker(None, "new-model", pull_only=True))
    assert ollama.pulled == ["new-model"] and out["finished"] == ("", {})
    assert out["progress"] == [("pulling manifest", -1.0), ("downloading", 0.5), ("success", 1.0)]


def test_worker_cancel(ollama):
    ollama.endless = True
    worker = ai.ReviewWorker(None, "m", "p")
    out = {}
    worker.failed.connect(lambda message: out.update(failed=message))
    threading.Timer(0.1, worker.cancel).start()
    worker.run()
    assert out["failed"] == "Stopped"


# ----------------------------------------------------- in the main window
def wait_for_review(qtbot, window):
    qtbot.waitUntil(lambda: window._ai_thread is None and window._ai_entry is not None
                    and window._ai_entry.status != "running", timeout=10_000)


def entries(window, kind=None):
    return [e for e in window.review_monitor.entries if kind is None or e.kind == kind]


def test_review_in_the_window(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    window.review_action.trigger()
    wait_for_review(qtbot, window)
    (entry,) = entries(window)
    assert entry.kind == "review" and entry.status == "done" and entry.where == "foo.f90"
    assert sorted((f.line, f.category) for f in entry.findings) == [(2, "style"), (5, "performance"), (6, "bug")]
    assert entry.summary and window.review_monitor.widget_for(entry).findings.isVisibleTo(window.review_monitor)
    assert set(editor._review_markers) == {2, 5, 6}  # (a range is marked on its first line)
    assert editor._review_markers[6][0][1].startswith("AI (bug): count is incremented")
    assert window.panel_in_front(window.review_monitor)
    prompt = ollama.chats[0]["messages"][1]["content"]
    assert "foo.f90" in prompt and "module foo" in prompt
    window.go_to_finding(entry, next(f for f in entry.findings if f.line == 6))
    assert editor.editor.textCursor().blockNumber() == 5
    window.clear_review_action.trigger()
    assert not editor._review_markers
    window.show_findings_in_editor(entry)  # (the entry puts them back)
    assert set(editor._review_markers) == {2, 5, 6}


def test_review_downloads_a_missing_model(qtbot, window, project, ollama, monkeypatch):
    ollama.models = []
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.Yes)
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    wait_for_review(qtbot, window)
    assert "isn't downloaded yet" in asked[0]
    assert ollama.pulled == [window.ai_model] and ollama.chats


def test_review_declined_download_does_nothing(qtbot, window, project, ollama, monkeypatch):
    ollama.models = []
    asked = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    qtbot.waitUntil(lambda: bool(asked), timeout=5_000)
    assert not ollama.pulled and not ollama.chats and window._ai_thread is None
    assert window.review_action.isEnabled()  # (ready for another try)


def test_stop_review(qtbot, window, project, ollama):
    ollama.endless = True
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    qtbot.waitUntil(lambda: window.stop_review_action.isEnabled(), timeout=5_000)
    qtbot.waitUntil(lambda: bool(ollama.chats), timeout=5_000)
    window.stop_review_action.trigger()
    qtbot.waitUntil(lambda: window._ai_thread is None, timeout=10_000)
    assert not window.stop_review_action.isEnabled()


def test_review_without_ollama(qtbot, window, project, monkeypatch):
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    monkeypatch.setattr(QMessageBox, "information", lambda *a, **k: warnings.append(a[2]))
    window.start_review()  # (no file open)
    assert "Open a file" in warnings[-1]
    open_editor(window, project / "src/foo.f90")
    monkeypatch.setattr(ai, "ollama_available", lambda: False)
    window.start_review()
    assert "isn't installed" in warnings[-1]
    monkeypatch.setattr(ai, "ollama_available", lambda: True)
    monkeypatch.setattr(ai, "server_running", lambda host=None: False)
    monkeypatch.setattr(ai, "ollama_executable", lambda: None)
    window.start_review()
    qtbot.waitUntil(lambda: "wasn't found" in warnings[-1], timeout=5_000)
    assert window.review_action.isEnabled()


def test_review_failure_is_shown(qtbot, window, project, ollama):
    ollama.models = [window.ai_model]
    open_editor(window, project / "src/foo.f90")
    ollama.error = None
    real_chat = ollama.chat

    def failing_chat(**kwargs):
        ollama.error = f"model '{window.ai_model}' not found"
        return real_chat(**kwargs)

    ollama.chat = failing_chat
    window.start_review()
    qtbot.waitUntil(lambda: window._ai_thread is None and "not found" in window.statusBar().currentMessage(),
                    timeout=10_000)
    assert "See AI → Model" in window.statusBar().currentMessage()


def model_menu_labels(qtbot, window) -> list[str]:
    """The Model menu once Ollama has answered (it's asked on a pool thread)."""
    window._fill_model_menu()
    qtbot.waitUntil(lambda: not window._asking_models, timeout=5_000)
    window._show_model_menu()  # (the open menu is refilled by itself; this one isn't shown)
    return [a.text() for a in window.model_menu.actions()]


def test_model_and_focus_menus(qtbot, window, ollama, monkeypatch):
    window._fill_model_menu()
    assert "(Checking for installed models…)" in [a.text() for a in window.model_menu.actions()]
    labels = model_menu_labels(qtbot, window)
    assert "qwen2.5-coder:7b" in labels and "Other Model…" in labels
    monkeypatch.setattr("vibefpmgui.main_window.QInputDialog.getText", lambda *a, **k: ("gpt-oss:20b", True))
    window._choose_other_model()
    assert window.ai_model == "gpt-oss:20b" and window.settings.value("ai_model") == "gpt-oss:20b"
    assert "gpt-oss:20b (not downloaded yet)" in model_menu_labels(qtbot, window)
    window._set_ai_focus("bugs")
    assert window.settings.value("ai_focus") == "bugs"
    ollama.error = "down"
    assert any("isn't running" in label for label in model_menu_labels(qtbot, window))


def test_the_window_never_waits_for_ollama(qtbot, window, project, ollama, monkeypatch):
    """Asking Ollama can take seconds (a model loading): it's done on a pool
    thread, and AI Review can't be clicked again meanwhile (a second server)."""
    import time

    real = ai.server_running
    monkeypatch.setattr(ai, "server_running", lambda host=None: time.sleep(0.8) or real(host))
    open_editor(window, project / "src/foo.f90")
    started = time.monotonic()
    window.start_review()
    assert time.monotonic() - started < 0.3  # (didn't wait for the answer)
    assert not window.review_action.isEnabled()  # (getting ready)
    window.start_review()  # (clicked again: ignored)
    wait_for_review(qtbot, window)
    assert len(ollama.chats) == 1
    started = time.monotonic()
    monkeypatch.setattr(ai, "installed_models", lambda host=None: time.sleep(0.8) or ["m"])
    window._fill_model_menu()  # (the Model menu opening)
    assert time.monotonic() - started < 0.3


# ----------------------------------------------------- explain with AI
def test_explain_prompt():
    code = "\n".join(f"line {i}" for i in range(1, 41))
    prompt = ai.build_explain_prompt("foo.f90", code, 20, "build", "Symbol 'x' has no IMPLICIT type", "error",
                                     "gfortran")
    assert "line 20, the compiler (gfortran) reports" in prompt and "error: Symbol 'x'" in prompt
    assert ">> 20| line 20" in prompt and "   5| line 5" in prompt and "   35| line 35" in prompt
    assert "line 4\n" not in prompt and "line 36" not in prompt  # (15 lines each side)
    assert "the fortitude linter" in ai.build_explain_prompt("f.f90", code, 1, "fortitude", "m", "S061")


def explain_from_the_problems(qtbot, window, editor):

    window.show_panel(window.problems_panel)
    qtbot.waitUntil(lambda: window.problems_panel.tree.topLevelItemCount() > 0, timeout=5_000)
    tree = window.problems_panel.tree
    row = tree.topLevelItem(0).child(0)
    menu = window.problems_panel.context_menu(tree.visualItemRect(row).center())
    action = next(a for a in menu.actions() if a.text() == "Explain with AI")
    assert action.isEnabled()
    action.trigger()


def wait_for_explanation(qtbot, window):
    qtbot.waitUntil(lambda: not window.review_monitor.running and window._ai_thread is None, timeout=10_000)


def test_explain_a_compiler_error(qtbot, window, project, ollama):
    from types import SimpleNamespace

    editor = open_editor(window, project / "src/foo.f90")
    editor.show_diagnostics([SimpleNamespace(line=2, severity="error", message="Symbol 'count' has no type")])
    explain_from_the_problems(qtbot, window, editor)
    monitor = window.review_monitor
    (entry,) = entries(window)
    assert entry.kind == "explain" and monitor.running and monitor.stop_button.isVisibleTo(monitor)
    wait_for_explanation(qtbot, window)
    messages = ollama.chats[-1]["messages"]
    assert messages[0]["content"] == ai.EXPLAIN_SYSTEM_PROMPT  # (not the review's instructions)
    assert "Symbol 'count' has no type" in messages[1]["content"] and ">> 2|" in messages[1]["content"]
    widget = monitor.widget_for(entry)
    assert entry.answer == ollama.answer and "Symbol 'count'" in widget.question.text()
    assert "count is incremented" in widget.answer.toPlainText()
    assert not monitor.stop_button.isVisibleTo(monitor) and entry.status == "done"
    assert not editor._review_markers  # (an explanation doesn't touch the review's markers)
    window.start_review()  # (a review afterwards: a new entry; the explanation stays)
    wait_for_review(qtbot, window)
    assert [e.kind for e in entries(window)] == ["explain", "review"]
    assert not widget.expanded and widget.isVisibleTo(monitor) and entry.answer == ollama.answer


def test_explain_from_the_editor_menu(qtbot, window, project, ollama):
    from types import SimpleNamespace

    editor = open_editor(window, project / "src/foo.f90")
    editor.show_diagnostics([SimpleNamespace(line=2, severity="warning", message="Unused variable"),
                             SimpleNamespace(line=2, severity="error", message="Bad thing")])
    code = editor.editor
    from qtpy.QtGui import QTextCursor
    cursor = QTextCursor(code.document().findBlockByNumber(1))
    menu = code.context_menu(code.cursorRect(cursor).center())
    next(a for a in menu.actions() if a.text() == "Explain Error with AI").trigger()
    wait_for_explanation(qtbot, window)
    assert "Bad thing" in ollama.chats[-1]["messages"][1]["content"]  # (the line's most serious problem)
    cursor = QTextCursor(code.document().findBlockByNumber(0))  # (a line without problems: not offered)
    assert "Explain Error with AI" not in [a.text() for a in code.context_menu(code.cursorRect(cursor).center()).actions()]


def test_explain_only_what_can_be_explained_and_one_at_a_time(qtbot, window, project, ollama):
    from vibefpmgui.result_tree import Problem

    open_editor(window, project / "src/foo.f90")
    spelling = Problem(1, 0, 1, 3, "spelling", "spelling", "“teh” isn't in the dictionary", "#ae3ec9", "spelling")
    window.problems_panel.show_problems(project / "src/foo.f90", [spelling])
    tree = window.problems_panel.tree
    menu = window.problems_panel.context_menu(tree.visualItemRect(tree.topLevelItem(0).child(0)).center())
    assert not next(a for a in menu.actions() if a.text() == "Explain with AI").isEnabled()
    window._ai_starting = True  # (a review getting ready)
    window.explain_problem(window.current_editor(), spelling._replace(source="build"))
    assert "busy" in window.statusBar().currentMessage() and not ollama.chats
    assert not entries(window)  # (nothing added)
    window._ai_starting = False


def test_stop_an_explanation(qtbot, window, project, ollama):
    from types import SimpleNamespace

    ollama.endless = True
    editor = open_editor(window, project / "src/foo.f90")
    editor.show_diagnostics([SimpleNamespace(line=2, severity="error", message="Oops")])
    explain_from_the_problems(qtbot, window, editor)
    monitor = window.review_monitor
    qtbot.waitUntil(lambda: bool(ollama.chats) and monitor.stop_button.isEnabled(), timeout=10_000)
    monitor.stop_button.click()
    wait_for_explanation(qtbot, window)
    (entry,) = entries(window)
    assert entry.status == "failed" and "Stopped" in entry.note and not monitor.stop_button.isVisibleTo(monitor)
    assert "stopped" in monitor.widget_for(entry).title.text()


def test_explaining_with_ollama_starting_and_a_download(qtbot, window, project, ollama, monkeypatch):
    """Getting ready (starting Ollama, downloading the model) shows as the
    entry's status, and the entry ends with the explanation."""
    from types import SimpleNamespace

    calls = []
    real = ai.server_running
    monkeypatch.setattr(ai, "server_running", lambda host=None: (calls.append(1) or len(calls) > 1) and real(host))  # (down, then up)
    monkeypatch.setattr(ai, "ollama_executable", lambda: "/bin/sleep")  # (a stand-in "ollama serve")
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    ollama.models = []
    editor = open_editor(window, project / "src/foo.f90")
    editor.show_diagnostics([SimpleNamespace(line=2, severity="error", message="Oops")])
    explain_from_the_problems(qtbot, window, editor)
    monitor = window.review_monitor
    qtbot.waitUntil(lambda: "Starting Ollama" in monitor.progress_label.text(), timeout=5_000)
    wait_for_explanation(qtbot, window)
    (entry,) = entries(window)  # (no entries of their own for getting ready)
    assert ollama.pulled and entry.kind == "explain" and entry.answer == ollama.answer and entry.status == "done"


def test_explain_declined_download_ends(qtbot, window, project, ollama, monkeypatch):
    from types import SimpleNamespace

    ollama.models = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    editor = open_editor(window, project / "src/foo.f90")
    editor.show_diagnostics([SimpleNamespace(line=2, severity="error", message="Oops")])
    explain_from_the_problems(qtbot, window, editor)
    qtbot.waitUntil(lambda: not window.review_monitor.running, timeout=5_000)
    (entry,) = entries(window)
    assert entry.status == "failed" and "isn't downloaded" in entry.note


# ------------------------------------------------------------------ chat
def ask(qtbot, window, question, with_file=True):
    monitor = window.review_monitor
    monitor.chat_file_check.setChecked(with_file)
    monitor.chat_edit.setText(question)
    monitor.chat_edit.returnPressed.emit()
    qtbot.waitUntil(lambda: not monitor.running and window._ai_thread is None, timeout=10_000)


def test_chat_about_the_open_file(qtbot, window, project, ollama):
    ollama.answer = "Mostly fine.\n\nLINE 2: bug: `x` is never set.\n\n```fortran\nx = 0\n```\n"
    editor = open_editor(window, project / "src/foo.f90")
    monitor = window.review_monitor
    ask(qtbot, window, "Is this file OK?")
    messages = ollama.chats[-1]["messages"]
    assert messages[0]["content"] == ai.CHAT_SYSTEM_PROMPT
    assert messages[-1]["content"].startswith("Is this file OK?") and "foo.f90" in messages[-1]["content"]
    assert "\n1| module foo\n" in messages[-1]["content"]  # (the file, with numbered lines)
    (first,) = entries(window)
    assert first.kind == "ask" and first.question == "Is this file OK?" and first.answer == ollama.answer
    assert not monitor.chat_edit.text()
    assert [(f.line, f.category) for f in first.findings] == [(2, "bug")]  # (in the entry…)
    assert 2 in editor._review_markers  # (…and the editor)
    assert "1 finding" in monitor.widget_for(first).title.text()
    assert "**You:** Is this file OK?" in first.markdown()

    ollama.answer = "Yes: `implicit none` is there.\n"
    ask(qtbot, window, "And implicit none?", with_file=False)
    messages = ollama.chats[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]  # (the conversation)
    assert messages[-1]["content"] == "And implicit none?" and messages[2]["content"].startswith("Mostly fine")
    assert len(entries(window)) == 2 and first.findings  # (the earlier one, as it was)

    monitor.new_chat_button.click()  # (a line in the timeline: forgotten, but still there)
    assert [e.kind for e in monitor.entries] == ["ask", "ask", "divider"]
    ask(qtbot, window, "Hello", with_file=False)
    assert [m["role"] for m in ollama.chats[-1]["messages"]] == ["system", "user"]  # (forgotten)
    monitor.clear_button.click()
    assert not monitor.entries and not window._turns


def test_chat_without_a_file_has_no_findings(qtbot, window, ollama):
    ollama.answer = "LINE 3: bug: no file was sent, so this isn't about any line.\n"
    ask(qtbot, window, "What is fpm?")
    assert ollama.chats[-1]["messages"][-1]["content"] == "What is fpm?"  # (no editor open: nothing sent)
    (entry,) = entries(window)
    assert not entry.findings and entry.path is None


def test_reviews_and_explanations_are_part_of_the_conversation(qtbot, window, project, ollama):
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    wait_for_review(qtbot, window)
    review_answer = ollama.answer
    ollama.answer = "Because it's incremented twice.\n"
    ask(qtbot, window, "Why is the line 6 finding a bug?")
    messages = ollama.chats[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"].startswith("Review my file foo.f90")
    assert "1| module foo" in messages[1]["content"]  # (the file came with the review…)
    assert messages[2]["content"] == review_answer
    assert "is as I sent it above" in messages[-1]["content"]  # (…so it isn't sent again)
    assert [e.kind for e in entries(window)] == ["review", "ask"]
    assert entries(window)[0].findings  # (the review's findings: still there)


def test_filters(qtbot, window, project, ollama):
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    wait_for_review(qtbot, window)
    ask(qtbot, window, "Something?", with_file=False)
    monitor = window.review_monitor
    review, question = monitor.widgets
    monitor.filter_buttons["review"].click()
    assert review.isVisibleTo(monitor) and not question.isVisibleTo(monitor)
    monitor.filter_buttons["chat"].click()
    assert not review.isVisibleTo(monitor) and question.isVisibleTo(monitor)
    monitor.filter_buttons["pending"].click()
    assert not review.isVisibleTo(monitor) and not question.isVisibleTo(monitor)
    monitor.filter_buttons["all"].click()
    assert review.isVisibleTo(monitor) and question.isVisibleTo(monitor)
    review.header.mousePressEvent(SimpleNamespace(button=lambda: Qt.LeftButton))  # (opened again)
    assert review.expanded and review.body.isVisibleTo(monitor)


def test_stop_a_chat_answer(qtbot, window, ollama):
    ollama.endless = True
    monitor = window.review_monitor
    monitor.chat_edit.setText("Tell me everything")
    monitor.send_button.click()
    qtbot.waitUntil(lambda: bool(window._ai_entry and window._ai_entry.answer)
                    and monitor.stop_button.isEnabled(), timeout=10_000)
    monitor.stop_button.click()
    qtbot.waitUntil(lambda: not monitor.running and window._ai_thread is None, timeout=10_000)
    assert "Stopped" in monitor.progress_label.text() and window._ai_entry.status == "failed"
    assert window._turns[-1].answer  # (what it wrote is kept)


def test_chat_while_busy_waits(qtbot, window, ollama):
    window._ai_starting = True
    window.send_chat("Hi", False)
    assert "busy" in window.statusBar().currentMessage() and not ollama.chats
    window._ai_starting = False


def test_up_and_down_recall_earlier_questions(qtbot, window, ollama):
    from qtpy.QtCore import Qt
    from qtpy.QtTest import QTest

    monitor = window.review_monitor
    edit = monitor.chat_edit
    for question in ("first", "second", "second", "third"):  # (a repeat right after: kept once)
        ask(qtbot, window, question, with_file=False)
    assert monitor.asked == ["first", "second", "third"]
    edit.setText("half-typed")
    QTest.keyClick(edit, Qt.Key_Up)
    assert edit.text() == "third"
    QTest.keyClick(edit, Qt.Key_Up)
    QTest.keyClick(edit, Qt.Key_Up)
    assert edit.text() == "first"
    QTest.keyClick(edit, Qt.Key_Up)  # (the oldest: stays)
    assert edit.text() == "first"
    QTest.keyClick(edit, Qt.Key_Down)
    assert edit.text() == "second"
    QTest.keyClick(edit, Qt.Key_Down)
    QTest.keyClick(edit, Qt.Key_Down)  # (past the newest: what was being typed)
    assert edit.text() == "half-typed"
    QTest.keyClick(edit, Qt.Key_Up, Qt.KeypadModifier)  # (as macOS sends arrow keys)
    assert edit.text() == "third"
    monitor.chat_edit.returnPressed.emit()  # (sent again: the newest, not added twice)
    qtbot.waitUntil(lambda: not monitor.running and window._ai_thread is None, timeout=10_000)
    assert monitor.asked == ["first", "second", "third"] and not edit.text()
    QTest.keyClick(edit, Qt.Key_Up)
    assert edit.text() == "third"


def test_no_sending_while_the_ai_is_busy(qtbot, window, ollama):
    ollama.endless = True
    monitor = window.review_monitor
    ask_text = "First"
    monitor.chat_edit.setText(ask_text)
    monitor.send_button.click()
    qtbot.waitUntil(lambda: window._ai_thread is not None, timeout=10_000)
    assert not monitor.send_button.isEnabled()
    monitor.chat_edit.setText("Second, too soon")
    monitor.chat_edit.returnPressed.emit()
    assert monitor.chat_edit.text() == "Second, too soon" and len(monitor.entries) == 1  # (kept, not lost)
    monitor.stop_button.click()
    qtbot.waitUntil(lambda: window._ai_thread is None, timeout=10_000)
    assert monitor.send_button.isEnabled()


def test_code_in_answers_is_highlighted(qtbot):
    from qtpy.QtGui import QTextCursor

    from vibefpmgui.review_monitor import MarkdownView

    view = MarkdownView()
    qtbot.addWidget(view)
    view.set_markdown("Like this:\n\n```fortran\ninteger :: n  ! a count\n```\n\n```\nplain text\n```\n")
    doc = view.document()

    def color_at(word: str) -> str:
        cursor = doc.find(word)
        assert not cursor.isNull(), word
        probe = QTextCursor(doc)
        probe.setPosition(cursor.selectionStart() + 1)
        return probe.charFormat().foreground().color().name()

    assert color_at("integer") != color_at("a count")  # (a keyword and a comment: colored apart)
    assert doc.find("integer").charFormat().fontFamilies()  # (and in the editor's font)
    assert color_at("plain text") == color_at("Like this")  # (no language: not colored)


def test_stop_the_ollama_server_the_app_started(qtbot, window, project, ollama, monkeypatch, tmp_path):
    import os
    import stat

    # A stand-in "ollama serve" that starts a "model runner" of its own, as Ollama does.
    runner_pid = tmp_path / "runner.pid"
    serve = tmp_path / "ollama"
    serve.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {runner_pid}\nwait\n")
    serve.chmod(serve.stat().st_mode | stat.S_IEXEC)
    calls = []
    real = ai.server_running
    monkeypatch.setattr(ai, "server_running", lambda host=None: (calls.append(1) or len(calls) > 1) and real(host))
    monkeypatch.setattr(ai, "ollama_executable", lambda: str(serve))
    window._update_ollama_action()
    assert not window.stop_ollama_action.isEnabled()  # (no server of ours)
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    wait_for_review(qtbot, window)
    qtbot.waitUntil(runner_pid.exists, timeout=5_000)
    pid = int(runner_pid.read_text())
    window._update_ollama_action()
    assert window.stop_ollama_action.isEnabled()
    window.stop_ollama_action.trigger()
    assert window._ollama_process is None and not window.stop_ollama_action.isEnabled()
    assert "Stopped the Ollama server" in window.statusBar().currentMessage()

    def gone() -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        return False

    qtbot.waitUntil(gone, timeout=5_000)  # (its runner too: not left holding the model's memory)


def test_think_before_answering_and_answer_length(qtbot, window, ollama):
    ollama.reasons = True
    monitor = window.review_monitor
    assert not window.think_action.isChecked() and window.ai_answer_tokens == ai.MAX_ANSWER_TOKENS
    ask(qtbot, window, "Quick one?", with_file=False)
    chat = ollama.chats[-1]
    assert chat["think"] is False and chat["options"]["num_predict"] == ai.MAX_ANSWER_TOKENS
    assert not window._ai_entry.reasoning

    window.think_action.trigger()  # (on)
    ask(qtbot, window, "Think about it?", with_file=False)
    chat = ollama.chats[-1]
    assert chat["think"] is True  # (with room of its own for the reasoning)
    assert chat["options"]["num_predict"] == ai.MAX_ANSWER_TOKENS + ai.REASONING_TOKENS
    entry = window._ai_entry
    assert entry.reasoning == "The loop looks fine. " and entry.answer == ollama.answer
    widget = monitor.widget_for(entry)
    assert widget.reasoning_button.isVisibleTo(monitor) and not widget.reasoning.isVisibleTo(monitor)
    widget.reasoning_button.click()
    assert widget.reasoning.isVisibleTo(monitor) and widget.reasoning.text() == "The loop looks fine."

    length_menu = window.answer_length_menu
    labels = [a.text() for a in length_menu.actions()]
    assert labels[0] == "1,500 tokens (default)" and labels[-1] == "No limit"
    length_menu.actions()[-1].trigger()  # (no limit)
    window.think_action.trigger()  # (off again)
    ask(qtbot, window, "Long one?", with_file=False)
    assert ollama.chats[-1]["options"]["num_predict"] == -1 and ollama.chats[-1]["think"] is False
    length_menu.actions()[1].trigger()  # (4,000)
    window.review_monitor.set_chat_kind("edit")
    assert window._answer_options(edit=True)["num_predict"] == max(4000, ai.EDIT_ANSWER_TOKENS)
    assert window.settings.value("ai_answer_tokens", type=int) == 4000
    assert window.settings.value("ai_think", type=bool) is False


def test_a_long_answer_is_shown_in_steps_not_on_every_piece(qtbot):
    from vibefpmgui.review_monitor import Entry, ReviewMonitor

    monitor = ReviewMonitor()
    qtbot.addWidget(monitor)
    entry = monitor.add_entry(Entry("ask", "q"))
    view = monitor.widget_for(entry).answer
    renders = []
    real = view.set_markdown
    view.set_markdown = lambda text: (renders.append(len(text)), real(text))
    for _ in range(200):
        monitor.add_answer("word ")
    assert len(renders) <= 1  # (not once a piece: the timer shows it)
    qtbot.waitUntil(lambda: bool(renders) and renders[-1] >= len("word " * 200) - 1, timeout=2_000)
    monitor.add_answer("the end")
    monitor.finish(True, "done")
    assert "the end" in view.toPlainText()  # (all of it, at the end)


def big_module(procedures: int) -> str:
    parts = ["module big", "  implicit none", "contains"]
    for k in range(procedures):
        parts += [f"  subroutine s{k}(x)", "    real, intent(inout) :: x"]
        parts += [f"    x = x + {i}.0" for i in range(12)]
        parts += [f"  end subroutine s{k}"]
    return "\n".join(parts + ["end module big"]) + "\n"


def test_a_file_too_long_is_reviewed_in_part(qtbot, window, project, ollama, monkeypatch):
    path = project / "src/big.f90"
    path.write_text(big_module(80))
    editor = open_editor(window, path)
    monkeypatch.setattr(window, "_chat_context", lambda: 4096)  # (a small model's context)
    editor.editor.go_to_line(ctx_line(editor, "  subroutine s40(x)") + 3)
    window.start_review()
    wait_for_review(qtbot, window)
    prompt = ollama.chats[-1]["messages"][1]["content"]
    assert "Only lines" in prompt and "subroutine s40(x)" in prompt and "subroutine s5(x)" not in prompt
    first = ctx_line(editor, "  subroutine s40(x)")
    assert f"{first}|   subroutine s40(x)" in prompt  # (numbered as in the file)
    entry = window._ai_entry
    assert entry.where.startswith("big.f90:") and "too long" in entry.question
    assert ollama.chats[-1]["options"]["num_ctx"] <= 4096


def ctx_line(editor, text: str) -> int:
    return editor.editor.toPlainText().split("\n").index(text) + 1


def test_a_selection_is_reviewed_on_its_own(qtbot, window, project, ollama):
    from qtpy.QtGui import QTextCursor

    editor = open_editor(window, project / "src/foo.f90")
    code = editor.editor
    code.go_to_line(5)
    cursor = code.textCursor()
    cursor.movePosition(QTextCursor.Down, QTextCursor.KeepAnchor, 2)
    cursor.movePosition(QTextCursor.EndOfBlock, QTextCursor.KeepAnchor)  # (lines 5–7)
    code.setTextCursor(cursor)
    window.start_review()
    wait_for_review(qtbot, window)
    prompt = ollama.chats[-1]["messages"][1]["content"]
    assert "Only lines 5–7 of the file's 8" in prompt and "5|   subroutine bump()" in prompt
    assert "module foo" not in prompt.split("Source")[1]
    assert window._ai_entry.where == "foo.f90:5–7" and "selection" in window._ai_entry.question


@pytest.mark.parametrize("reset", ["new_chat", "clear"])
def test_an_answer_stopped_by_a_reset_is_forgotten(qtbot, window, ollama, reset):
    ollama.endless = True
    monitor = window.review_monitor
    monitor.chat_edit.setText("Tell me everything")
    monitor.send_button.click()
    qtbot.waitUntil(lambda: bool(window._ai_entry and window._ai_entry.answer), timeout=10_000)
    (monitor.new_chat_button if reset == "new_chat" else monitor.clear_button).click()
    qtbot.waitUntil(lambda: window._ai_thread is None, timeout=10_000)
    assert window._turns == []  # (the partial answer came in after: not remembered)
    ollama.endless = False
    ask(qtbot, window, "Hello", with_file=False)
    assert [m["role"] for m in ollama.chats[-1]["messages"]] == ["system", "user"]
