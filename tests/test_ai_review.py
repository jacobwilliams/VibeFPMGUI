"""AI review, with Ollama mocked: no server, no model downloads (CI too)."""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest
from conftest import open_editor
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

    def chat(self, model, stream, options, messages):
        self.chats.append({"model": model, "options": options, "messages": messages})
        if self.error:
            raise RuntimeError(self.error)
        return self._stream()

    def _stream(self):
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
    assert ai.context_size("x") == 4096
    assert ai.context_size("x" * 100_000) == 32768


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
    qtbot.waitUntil(lambda: window._ai_thread is None and bool(window.review_monitor.rows()), timeout=10_000)


def test_review_in_the_window(qtbot, window, project, ollama):
    editor = open_editor(window, project / "src/foo.f90")
    window.review_action.trigger()
    wait_for_review(qtbot, window)
    rows = window.review_monitor.rows()
    assert sorted((f.line, f.category) for f in rows) == [(2, "style"), (5, "performance"), (6, "bug")]
    assert set(editor._review_markers) == {2, 5, 6}  # (a range is marked on its first line)
    assert editor._review_markers[6][0][1].startswith("AI (bug): count is incremented")
    assert window.panel_in_front(window.review_monitor)
    prompt = ollama.chats[0]["messages"][1]["content"]
    assert "foo.f90" in prompt and "module foo" in prompt
    window._go_to_finding(6)
    assert editor.editor.textCursor().blockNumber() == 5
    window.clear_review_action.trigger()
    assert not editor._review_markers


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
    assert monitor.mode == "explain" and monitor.running and monitor.stop_button.isVisibleTo(monitor)
    wait_for_explanation(qtbot, window)
    messages = ollama.chats[-1]["messages"]
    assert messages[0]["content"] == ai.EXPLAIN_SYSTEM_PROMPT  # (not the review's instructions)
    assert "Symbol 'count' has no type" in messages[1]["content"] and ">> 2|" in messages[1]["content"]
    assert monitor.answer == ollama.answer and "Symbol 'count'" in monitor.question.text()
    assert monitor.explanation.isVisibleTo(monitor) and not monitor.table.isVisibleTo(monitor)
    assert not monitor.stop_button.isVisibleTo(monitor) and "Explanation" in monitor.progress_label.text()
    assert not editor._review_markers  # (an explanation doesn't touch the review's markers)
    window.start_review()  # (a review afterwards: the findings again)
    qtbot.waitUntil(lambda: len(ollama.chats) == 2 and window._ai_thread is None and not monitor.running,
                    timeout=10_000)
    assert monitor.mode == "review" and monitor.table.isVisibleTo(monitor) and not monitor.explanation.isVisibleTo(monitor)


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
    assert window.review_monitor.mode == "review"  # (the panel is left alone)
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
    assert "Stopped" in monitor.progress_label.text() and not monitor.stop_button.isVisibleTo(monitor)


def test_explaining_with_ollama_starting_and_a_download(qtbot, window, project, ollama, monkeypatch):
    """Getting ready (starting Ollama, downloading the model) shows in the
    explanation's status, and the panel ends with the explanation."""
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
    assert monitor.mode == "explain"
    wait_for_explanation(qtbot, window)
    assert ollama.pulled and monitor.mode == "explain" and monitor.answer == ollama.answer
    assert "Explanation" in monitor.progress_label.text()


def test_explain_declined_download_ends(qtbot, window, project, ollama, monkeypatch):
    from types import SimpleNamespace

    ollama.models = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    editor = open_editor(window, project / "src/foo.f90")
    editor.show_diagnostics([SimpleNamespace(line=2, severity="error", message="Oops")])
    explain_from_the_problems(qtbot, window, editor)
    qtbot.waitUntil(lambda: not window.review_monitor.running, timeout=5_000)
    assert "isn't downloaded" in window.review_monitor.progress_label.text()


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
    assert monitor.mode == "chat" and monitor.title.text() == "AI chat"
    assert monitor.turns == [("Is this file OK?", ollama.answer)] and not monitor.chat_edit.text()
    assert [(f.line, f.category) for f in monitor.findings] == [(2, "bug")]  # (in the list…)
    assert monitor.table.isVisibleTo(monitor) and 2 in editor._review_markers  # (…and the editor)
    assert "1 finding" in monitor.progress_label.text() and monitor.new_chat_button.isVisibleTo(monitor)
    assert "**You:** Is this file OK?" in monitor.copy_text()

    ollama.answer = "Yes: `implicit none` is there.\n"
    ask(qtbot, window, "And implicit none?", with_file=False)
    messages = ollama.chats[-1]["messages"]
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "user"]  # (the conversation)
    assert messages[-1]["content"] == "And implicit none?" and messages[2]["content"].startswith("Mostly fine")
    assert len(monitor.turns) == 2 and "And implicit none?" in monitor.explanation.toPlainText()
    assert "Mostly fine" in monitor.explanation.toPlainText()  # (the earlier turn still shown)
    assert [f.line for f in monitor.findings] == [2]  # (an answer without findings: the earlier ones stay)

    monitor.new_chat_button.click()
    assert monitor.turns == [] and not monitor.findings and not monitor.table.isVisibleTo(monitor)
    ask(qtbot, window, "Hello", with_file=False)
    assert [m["role"] for m in ollama.chats[-1]["messages"]] == ["system", "user"]  # (forgotten)


def test_chat_without_a_file_has_no_findings(qtbot, window, ollama):
    ollama.answer = "LINE 3: bug: no file was sent, so this isn't about any line.\n"
    ask(qtbot, window, "What is fpm?")
    monitor = window.review_monitor
    assert ollama.chats[-1]["messages"][-1]["content"] == "What is fpm?"  # (no editor open: nothing sent)
    assert not monitor.findings and not monitor.table.isVisibleTo(monitor)


def test_a_review_or_explanation_starts_a_new_chat(qtbot, window, project, ollama):
    open_editor(window, project / "src/foo.f90")
    ask(qtbot, window, "First question", with_file=False)
    window.start_review()
    qtbot.waitUntil(lambda: len(ollama.chats) == 2 and window._ai_thread is None
                    and not window.review_monitor.running, timeout=10_000)
    assert window.review_monitor.mode == "review"
    ask(qtbot, window, "Second question", with_file=False)
    assert [m["role"] for m in ollama.chats[-1]["messages"]] == ["system", "user"]
    assert window.review_monitor.turns == [("Second question", ollama.answer)]


def test_stop_a_chat_answer(qtbot, window, ollama):
    ollama.endless = True
    monitor = window.review_monitor
    monitor.chat_edit.setText("Tell me everything")
    monitor.send_button.click()
    qtbot.waitUntil(lambda: bool(monitor.answer) and monitor.stop_button.isEnabled(), timeout=10_000)
    monitor.stop_button.click()
    qtbot.waitUntil(lambda: not monitor.running and window._ai_thread is None, timeout=10_000)
    assert "Stopped" in monitor.progress_label.text()
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
