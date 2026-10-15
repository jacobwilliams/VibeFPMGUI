"""The AI menu, the local Ollama server and model, AI reviews, and the AI's worker threads.

The Review panel (`vibefpmgui.review_monitor`) shows what comes back.
"""

from __future__ import annotations

import time

from qtpy.QtCore import QProcess, QTimer
from qtpy.QtGui import QActionGroup, QKeySequence, QTextCursor
from qtpy.QtWidgets import QInputDialog, QMessageBox, QSizePolicy, QWidget

from .. import ai_context as ctx
from .. import ai_review as ai
from ..background import call_in_background
from ..editor import EditorTab
from ..processes import kill_process_tree
from ..review_monitor import Entry, ReviewMonitor


class AIMixin:
    """The AI menu, Ollama and AI reviews (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_ai_menu(self):
        self.ai_model = self.settings.value("ai_model", ai.DEFAULT_MODEL)
        self.ai_focus = self.settings.value("ai_focus", "general")
        self._ai_thread = None
        self._ai_worker = None
        self._ai_editor: EditorTab | None = None
        self._ollama_process: QProcess | None = None
        # Ollama is asked things on a pool thread (see background.call_in_background):
        self._ai_starting = False  # a review is getting ready (server, model): AI Review waits
        self._known_models: list[str] | None = None  # installed models, as last heard (None: not yet)
        self._after_model = lambda: None  # what runs once the model is there: a review, an explanation, a chat
        self._turns: list[ctx.Turn] = []  # what the model remembers: every entry since New Chat (see ai_context)
        self._token_ratio = 1.0  # measured tokens over estimated ones (see _chat_finished)
        self._model_contexts: dict[str, int | None] = {}  # each model's longest context, as Ollama says
        self._ai_entry: Entry | None = None  # the Review panel's entry for what's running (or last ran)
        # What the model remembers starts over with New Chat, Clear or another
        # project: a request started before (stopped, but still ending) mustn't
        # add to it.
        self._memory_generation = 0
        self._request_generation = 0
        self._edit_selection: tuple[EditorTab, int, int] | None = None  # Edit Selection with AI: the lines
        self._asking_models = False

        menu = self.menuBar().addMenu("&AI")
        self.review_action = menu.addAction("Review Current File")
        self.review_action.setShortcut(QKeySequence("Ctrl+Alt+R"))
        self.review_action.setStatusTip("Ask a local AI model (via Ollama) to review the file in the editor")
        self.review_action.triggered.connect(self.start_review)
        # Also on the toolbar, at the far right.
        self.review_action.setIconText("AI Review")
        keys = self.review_action.shortcut().toString(QKeySequence.NativeText)
        self.review_action.setToolTip(f"AI Review ({keys}) — review the file in the editor "
                                      "with a local AI model")
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.command_toolbar.addWidget(spacer)
        self.command_toolbar.addAction(self.format_action)
        self.command_icons[self.format_action] = "format"
        self.command_toolbar.addAction(self.review_action)
        self.command_icons[self.review_action] = "robot"
        self._update_command_icons()
        self.stop_review_action = menu.addAction("Stop")
        self.stop_review_action.setEnabled(False)
        self.stop_review_action.triggered.connect(self.stop_review)
        self.edit_with_ai_action = menu.addAction("Edit with AI…")
        self.edit_with_ai_action.setShortcut(QKeySequence("Ctrl+Alt+E"))
        self.edit_with_ai_action.setStatusTip("Ask the AI to change the file in the editor (the selection, if "
                                              "there is one); you accept or reject each edit")
        self.edit_with_ai_action.triggered.connect(self.edit_with_ai)
        self.stop_ollama_action = menu.addAction("Stop Ollama Server")
        self.stop_ollama_action.setStatusTip("Stop the Ollama server the app started (and the model it has "
                                             "loaded, freeing its memory); it starts again when the AI is next used")
        self.stop_ollama_action.triggered.connect(self.stop_ollama)
        menu.aboutToShow.connect(self._update_ollama_action)
        self._update_ollama_action()
        self.clear_review_action = menu.addAction("Clear Review Markers")
        self.clear_review_action.triggered.connect(
            lambda: self._with_editor(lambda e: e.clear_review()))
        menu.addSeparator()
        self.model_menu = menu.addMenu("Model")
        self.model_menu.aboutToShow.connect(self._fill_model_menu)
        self.ai_think = self.settings.value("ai_think", False, type=bool)
        self.ai_answer_tokens = self.settings.value("ai_answer_tokens", ai.MAX_ANSWER_TOKENS, type=int)
        self.think_action = menu.addAction("Think Before Answering")
        self.think_action.setCheckable(True)
        self.think_action.setChecked(self.ai_think)
        self.think_action.setStatusTip(
            "Let a thinking model (qwen3, deepseek-r1…) reason before it answers: slower, sometimes better. "
            f"The reasoning gets {ai.REASONING_TOKENS:,} tokens of its own, on top of the answer's length")
        self.think_action.toggled.connect(self._set_ai_think)
        length_menu = self.answer_length_menu = menu.addMenu("Answer Length")
        lengths = QActionGroup(self)
        for tokens in ai.ANSWER_LENGTHS:
            action = length_menu.addAction(f"{tokens:,} tokens" + (" (default)" if tokens == ai.MAX_ANSWER_TOKENS
                                                                     else "") if tokens else "No limit")
            action.setCheckable(True)
            action.setChecked(tokens == self.ai_answer_tokens)
            action.setStatusTip("How much the model may write in an answer (edits get at least "
                                f"{ai.EDIT_ANSWER_TOKENS:,}); a longer answer needs more of its context")
            action.triggered.connect(lambda _=False, t=tokens: self._set_ai_answer_tokens(t))
            lengths.addAction(action)
        focus_menu = menu.addMenu("Focus")
        group = QActionGroup(self)
        for key, label in ai.FOCUS_LABELS.items():
            action = focus_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(key == self.ai_focus)
            action.setStatusTip(ai.FOCUS[key])
            action.triggered.connect(lambda _=False, k=key: self._set_ai_focus(k))
            group.addAction(action)

    def _build_review_panel(self):
        """The Review panel: the AI's timeline."""
        self.review_monitor = ReviewMonitor()
        monitor = self.review_monitor
        monitor.stop_requested.connect(self.stop_review)
        monitor.chat_requested.connect(self.send_chat)
        monitor.new_chat_requested.connect(self.new_chat)
        monitor.clear_requested.connect(self.clear_ai_history)
        monitor.selection_cleared.connect(lambda: setattr(self, "_edit_selection", None))
        monitor.finding_selected.connect(self.go_to_finding)
        monitor.fix_requested.connect(self.fix_finding)
        monitor.show_in_editor_requested.connect(self.show_findings_in_editor)
        monitor.accept_requested.connect(self.accept_edit)
        monitor.reject_requested.connect(self.reject_edit)
        monitor.accept_all_requested.connect(self.accept_all_edits)
        monitor.reject_all_requested.connect(self.reject_all_edits)
        monitor.edit_clicked.connect(self.show_edit)

    def _update_review_actions(self):
        """AI review needs an editor tab in front (and no review running)."""
        has_editor = self.current_editor() is not None
        busy = self._ai_thread is not None or getattr(self, "_ai_starting", False)
        self.review_action.setEnabled(has_editor and not busy)
        self.review_monitor.set_sending(not busy)  # (a question while it's answering: wait for it)
        self.clear_review_action.setEnabled(has_editor)

    def _set_ai_think(self, on: bool):
        self.ai_think = on
        self.settings.setValue("ai_think", on)

    def _set_ai_answer_tokens(self, tokens: int):
        self.ai_answer_tokens = tokens
        self.settings.setValue("ai_answer_tokens", tokens)

    def _answer_options(self, edit: bool = False) -> dict:
        """The worker's settings from AI → Think Before Answering and Answer
        Length (an edit's answer, with its old and new text: at least
        EDIT_ANSWER_TOKENS)."""
        tokens = self.ai_answer_tokens
        if edit and tokens:
            tokens = max(tokens, ai.EDIT_ANSWER_TOKENS)
        return {"num_predict": tokens, "think": self.ai_think}

    def _start_answer(self, worker) -> None:
        """Start a worker whose reasoning (if the model thinks) goes to the
        running entry."""
        worker.reasoning.connect(self.review_monitor.add_reasoning)
        self._start_ai_worker(worker)

    def _set_ai_focus(self, key: str):
        self.ai_focus = key
        self.settings.setValue("ai_focus", key)

    def _set_ai_model(self, name: str):
        self.ai_model = name
        self.settings.setValue("ai_model", name)
        self.statusBar().showMessage(f"AI model: {name}", 4000)

    def _fill_model_menu(self):
        """Installed models (if Ollama is running), plus the current choice:
        the list as last heard, at once, while Ollama is asked again."""
        self._show_model_menu()
        if ai.ollama_available() and not self._asking_models:
            self._asking_models = True
            call_in_background(ai.installed_models, self._models_heard, self)

    def _models_heard(self, models: list[str]):
        self._asking_models = False
        self._known_models = models
        if self.model_menu.isVisible():
            self._show_model_menu()

    def _show_model_menu(self):
        menu = self.model_menu
        menu.clear()
        models = self._known_models or []
        names = sorted(set(models) | {self.ai_model})
        group = QActionGroup(menu)
        for name in names:
            label = name if name in models else f"{name} (not downloaded yet)"
            action = menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(name == self.ai_model)
            action.triggered.connect(lambda _=False, n=name: self._set_ai_model(n))
            group.addAction(action)
        if self._known_models is None and ai.ollama_available():
            note = menu.addAction("(Checking for installed models…)")
            note.setEnabled(False)
        elif not models:
            note = menu.addAction("(Ollama isn't running — installed models appear once it is)")
            note.setEnabled(False)
        menu.addSeparator()
        other = menu.addAction("Other Model…")
        other.triggered.connect(self._choose_other_model)

    def _choose_other_model(self):
        name, ok = QInputDialog.getText(
            self, "AI model", "Ollama model name (e.g. qwen2.5-coder:14b, qwen3-coder:30b, "
            "gpt-oss:20b). It's downloaded the first time it's used:", text=self.ai_model)
        if ok and name.strip():
            self._set_ai_model(name.strip())

    def start_review(self):
        editor = self.current_editor()
        if editor is None:
            QMessageBox.information(self, "AI review", "Open a file in the editor first "
                                    "(double-click a file, or File → Open File in Editor).")
            return
        if not self._can_ask("AI review"):
            return
        self._ai_editor = editor
        self._after_model = self._run_review
        focus = ai.FOCUS_LABELS.get(self.ai_focus, self.ai_focus)
        self._add_entry(Entry("review", focus, editor.path, editor.path.name, self.ai_model))
        self._ensure_ollama(self._check_model)

    def _can_ask(self, title: str) -> bool:
        """The AI is free, and Ollama's Python package is there (if not: why)."""
        if self._ai_thread is not None or self._ai_starting:
            self.statusBar().showMessage("The AI is busy: try again when it's done", 5000)
            return False
        if not ai.ollama_available():
            QMessageBox.warning(self, title, "The 'ollama' Python package isn't installed.\n"
                                "Run 'pixi install' (or 'pip install ollama').")
            return False
        return True

    def _add_entry(self, entry: Entry) -> Entry:
        """A new entry in the Review panel's timeline, for what's starting."""
        self._ai_entry = entry
        self._request_generation = self._memory_generation
        self.show_panel(self.review_monitor)
        if entry.kind not in ("ask", "edit"):
            self.review_monitor.context_note = ""
        return self.review_monitor.add_entry(entry)

    def _remember(self, entry: Entry, turn: ctx.Turn):
        """What the model was asked and answered: it's part of what later
        questions send (see ai_context)."""
        if self._request_generation != self._memory_generation:
            return  # (asked before New Chat, Clear or another project: forgotten)
        turn.answer, turn.edits = entry.answer, entry.edits
        entry.turn = turn
        self._turns.append(turn)

    def _set_ai_starting(self, on: bool):
        """A review is getting ready (asking Ollama, starting it): AI Review
        waits, so a second click doesn't start a second server."""
        self._ai_starting = on
        self._update_review_actions()

    def _ensure_ollama(self, then):
        """Start "ollama serve" if no server answers, then call ``then``
        (Ollama is asked on a pool thread: the window never waits for it)."""
        self._set_ai_starting(True)
        call_in_background(ai.server_running, lambda up: self._server_checked(up, then), self)

    def _server_checked(self, up: bool, then):
        if up:
            then()
            return
        exe = ai.ollama_executable()
        if exe is None:
            self._set_ai_starting(False)
            self._end_explain_setup("Ollama isn't running")
            QMessageBox.warning(self, "AI review", "Ollama isn't running and the 'ollama' program "
                                "wasn't found. Run 'pixi install', or install Ollama from ollama.com.")
            return
        self._ai_status("starting the local Ollama server", "Starting Ollama")
        self._ollama_process = QProcess(self)
        self._ollama_process.setProcessChannelMode(QProcess.MergedChannels)
        self._ollama_process.start(exe, ["serve"])
        deadline = time.monotonic() + 20

        def poll():
            call_in_background(ai.server_running, polled, self)

        def polled(up: bool):
            if up:
                then()
            elif time.monotonic() > deadline or self._ollama_process.state() == QProcess.NotRunning:
                self._set_ai_starting(False)
                self.review_monitor.finish(False, "Could not start the Ollama server.")
            else:
                QTimer.singleShot(300, self, poll)

        QTimer.singleShot(300, self, poll)

    def _check_model(self):
        """Is the model downloaded? (Asked on a pool thread.)"""
        call_in_background(ai.installed_models, self._model_checked, self)

    def _model_checked(self, installed: list[str]):
        self._known_models = installed
        self._set_ai_starting(False)  # (from here a worker thread, or nothing, follows)
        model = self.ai_model if ":" in self.ai_model else self.ai_model + ":latest"
        if model in installed or self.ai_model in installed:
            if self.ai_model not in self._model_contexts:  # (for the next chat question: asked once)
                self._model_contexts[self.ai_model] = None
                call_in_background(lambda m=self.ai_model: ai.model_context_length(m),
                                   lambda n, m=self.ai_model: self._model_contexts.__setitem__(m, n), self)
            self._after_model()
            return
        answer = QMessageBox.question(
            self, "Download AI model",
            f"The model '{self.ai_model}' isn't downloaded yet. Download it now?\n\n"
            "Models are a few GB; this happens once, and everything runs locally afterwards.")
        if answer != QMessageBox.Yes:
            self._end_explain_setup(f"{self.ai_model} isn't downloaded")
            return
        self._ai_status(f"downloading {self.ai_model}", "Downloading")
        worker = ai.ReviewWorker(ai.DEFAULT_HOST, self.ai_model, pull_only=True)
        worker.progress.connect(self._pull_progress)
        # (A method of the window, not a lambda: a lambda would run in the
        # worker's thread, which is ending, and the review would never start.)
        worker.finished.connect(self._pull_finished)
        worker.failed.connect(self._review_failed)
        self._start_ai_worker(worker)

    def _ai_status(self, description: str, status: str):
        """Getting ready (starting Ollama, downloading the model): the status
        of the entry that's waiting for it."""
        self.show_panel(self.review_monitor)
        if self.review_monitor.running:
            self.review_monitor.set_status(status)

    def _pull_finished(self, *_):
        self._ai_done(then=self._after_model)

    def _pull_progress(self, status: str, fraction: float):
        if fraction >= 0:
            self.review_monitor.set_status(f"Downloading {self.ai_model} {fraction * 100:.0f}%", fraction)
        else:
            self.review_monitor.set_status(status.capitalize() or "Downloading")

    def _run_review(self):
        editor = self._ai_editor
        if editor is None or editor not in self.editors():
            self.review_monitor.finish(False, "The file was closed")
            return
        code = editor.editor.toPlainText()
        diagnostics = editor.compiler_diagnostics()
        lines = ctx.file_lines(code)
        options = self._answer_options()
        cap = self._chat_context()
        room = options["num_predict"] + (ai.REASONING_TOKENS if options["think"] else 0) \
            if options["num_predict"] else ai.REASONING_TOKENS

        def prompt_for(first: int, last: int) -> str:
            return ai.build_prompt(editor.path.name, editor.highlighter.lexer.name, "\n".join(lines[first - 1:last]),
                                   self.ai_focus, diagnostics, first, len(lines))

        # The selection (two lines or more), or the whole file if it fits the
        # model's context with room for the answer; else the procedure at the
        # cursor. (Sent anyway, a file too long loses its start, instructions
        # included: Ollama doesn't refuse, it drops what doesn't fit.)
        first, last, why = 1, len(lines), ""
        selection = editor.editor.selected_lines()
        if selection is not None and selection[1] > selection[0]:
            (first, last), why = selection, "the selection"
        elif ctx.estimate_tokens(prompt_for(1, len(lines)) + ai.SYSTEM_PROMPT, self._token_ratio) + room \
                > cap * 0.9:
            first, last = ctx.window(lines, None, editor.editor.textCursor().blockNumber() + 1)
            why = "the procedure at the cursor: the file is too long to review at once"
        prompt = prompt_for(first, last)
        entry = self._ai_entry
        if why:
            entry.where = f"{editor.path.name}:{first}–{last}"
            entry.question = f"{entry.question} · {why}"
            self.review_monitor.refresh(entry)
        num_ctx = ai.context_for(ctx.estimate_tokens(prompt + ai.SYSTEM_PROMPT, self._token_ratio), cap, room)
        self.review_monitor.set_status("Thinking")
        self._show_single_output(f"AI review of {editor.path.name} ({self.ai_model})", "")
        self._review_text = ""
        self._review_keys = []
        self._review_code = code
        editor.clear_review()  # markers from an earlier review would be misleading
        worker = ai.ReviewWorker(ai.DEFAULT_HOST, self.ai_model, prompt, num_ctx=num_ctx, **options)
        worker.chunk.connect(self._review_chunk)
        worker.finished.connect(self._review_finished)
        worker.failed.connect(self._review_failed)
        self._start_answer(worker)

    def _start_ai_worker(self, worker):
        self._ai_worker = worker
        self._ai_thread = ai.start_worker(worker, self)
        self._ai_thread.finished.connect(self._ai_thread_done)
        self._update_review_actions()
        self.stop_review_action.setEnabled(True)
        self.review_monitor.set_stoppable(True)

    def _ai_thread_done(self):
        self._ai_thread = None
        self._ai_worker = None
        self._update_review_actions()
        self.stop_review_action.setEnabled(False)
        self.review_monitor.set_stoppable(False)

    def _ai_done(self, then=None):
        # Let the download thread finish before starting the review thread.
        if then is not None:
            QTimer.singleShot(100, self, lambda: self._after_ai_thread(then))

    def _after_ai_thread(self, then):
        if self._ai_thread is not None:
            QTimer.singleShot(100, self, lambda: self._after_ai_thread(then))
        else:
            then()

    def _review_chunk(self, text: str):
        cursor = self.test_output.textCursor()
        cursor.movePosition(QTextCursor.End)
        cursor.insertText(text)
        self.test_output.setTextCursor(cursor)
        self.test_output.ensureCursorVisible()
        self.review_monitor.add_answer(text)
        # Text arrives a line at a time: re-read the answer so far and show
        # the findings found so far (the entry's list and the editor's markers).
        self._review_text += text
        editor, entry = self._ai_editor, self._ai_entry
        max_line = editor.editor.blockCount() if editor in self.editors() else None
        findings, _summary = ai.parse_findings(self._review_text, max_line)
        keys = [(f.line, f.end_line, f.category, f.comment) for f in findings]
        if keys != self._review_keys:
            self._review_keys = keys
            entry.findings = findings
            self.review_monitor.refresh_soon(entry)  # (with the answer's next showing)
            if editor in self.editors():
                editor.show_review(findings)

    def _review_finished(self, text: str, stats: dict):
        editor, entry = self._ai_editor, self._ai_entry
        max_line = editor.editor.blockCount() if editor in self.editors() else None
        findings, summary = ai.parse_findings(text, max_line)
        entry.findings, entry.summary = findings, summary
        name = editor.path.name if editor is not None else entry.where.split(":")[0]
        self._remember(entry, ctx.Turn(
            f"Review {entry.where}", f"Review my file {entry.where} ({entry.question.lower()}): list the issues "
            "you find.",
            ctx.file_attachment(name, self._review_code, numbers=True, context=self._chat_context(),
                                ratio=self._token_ratio)))
        detail = f"{stats.get('seconds', 0):.1f} s · {self.ai_model}" if stats else self.ai_model
        if stats.get("repeated"):
            detail += " · stopped a repetition loop"
        elif stats.get("thinking"):
            detail += " · the model spent its answer's length on reasoning (see AI → Answer Length)"
        elif stats.get("truncated"):
            detail += " · answer cut off at the length limit"
        self.review_monitor.finish(True, detail)
        if editor in self.editors():
            editor.show_review(findings)
        self.statusBar().showMessage(f"AI review: {len(findings)} findings", 5000)

    def _review_failed(self, message: str):
        hint = ""
        if "not found" in message.lower():
            hint = f" (is '{self.ai_model}' downloaded? See AI → Model)"
        self.review_monitor.finish(False, message + hint)
        self.statusBar().showMessage(f"AI: {message}{hint}", 6000)

    def stop_review(self):
        if self._ai_worker is not None:
            self._ai_worker.cancel()

    def _ollama_running(self) -> bool:
        """A server the app started is running (one started otherwise isn't ours to stop)."""
        return self._ollama_process is not None and self._ollama_process.state() != QProcess.NotRunning

    def _update_ollama_action(self):
        self.stop_ollama_action.setEnabled(self._ollama_running())

    def stop_ollama(self, quitting: bool = False):
        """AI → Stop Ollama Server: the server the app started, with the
        model runner it started (killing only the server would leave that
        running, holding the model's memory)."""
        if not self._ollama_running():
            return
        if not quitting:
            self.stop_review()
        kill_process_tree(self._ollama_process)
        self._ollama_process.waitForFinished(3000)
        self._ollama_process = None
        self._update_ollama_action()
        if not quitting:
            self.statusBar().showMessage("Stopped the Ollama server (it starts again when the AI is next used)", 5000)

    def _shutdown_ai(self):
        if self._ai_worker is not None:
            self._ai_worker.cancel()
        if self._ai_thread is not None:
            self._ai_thread.quit()
            self._ai_thread.wait(3000)
        self.stop_ollama(quitting=True)
