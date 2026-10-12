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
                           total_duration=1_500_000_000 if done else 0)


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
    assert stats == {"tokens": 42, "seconds": 1.5}
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
    assert out["failed"] == "Review stopped"


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


def test_review_declined_download_does_nothing(window, project, ollama, monkeypatch):
    ollama.models = []
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    assert not ollama.pulled and not ollama.chats and window._ai_thread is None


def test_stop_review(qtbot, window, project, ollama):
    ollama.endless = True
    open_editor(window, project / "src/foo.f90")
    window.start_review()
    qtbot.waitUntil(lambda: window.stop_review_action.isEnabled(), timeout=5_000)
    qtbot.waitUntil(lambda: bool(ollama.chats), timeout=5_000)
    window.stop_review_action.trigger()
    qtbot.waitUntil(lambda: window._ai_thread is None, timeout=10_000)
    assert not window.stop_review_action.isEnabled()


def test_review_without_ollama(window, project, monkeypatch):
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
    assert "wasn't found" in warnings[-1]


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


def test_model_and_focus_menus(window, ollama, monkeypatch):
    window._fill_model_menu()
    labels = [a.text() for a in window.model_menu.actions()]
    assert "qwen2.5-coder:7b" in labels and "Other Model…" in labels
    monkeypatch.setattr("vibefpmgui.main_window.QInputDialog.getText", lambda *a, **k: ("gpt-oss:20b", True))
    window._choose_other_model()
    assert window.ai_model == "gpt-oss:20b" and window.settings.value("ai_model") == "gpt-oss:20b"
    window._fill_model_menu()
    assert "gpt-oss:20b (not downloaded yet)" in [a.text() for a in window.model_menu.actions()]
    window._set_ai_focus("bugs")
    assert window.settings.value("ai_focus") == "bugs"
    ollama.error = "down"
    window._fill_model_menu()
    assert any("isn't running" in a.text() for a in window.model_menu.actions())
