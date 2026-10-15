"""Talking with the AI: Explain, the chat, and the edits it proposes.

Findings and edit cards in the Review panel act on the editor from here.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import ai_context as ctx
from .. import ai_edits as ed
from .. import ai_review as ai
from ..edit_cards import diff_rows
from ..editor import EditorTab
from ..result_tree import Problem
from ..review_monitor import Entry


class AIChatMixin:
    """Explain, the chat, and the AI's edits (a part of `vibefpmgui.main_window.MainWindow`)."""

    EXPLAINABLE = ("build", "fortitude", "fortls")  # (Problems' sources: compiler, linter, language server)

    def explain_problem(self, editor: EditorTab, problem):
        """Explain with AI: ask the model what a problem means and how to fix
        it; the answer appears in the Review panel as it's written."""
        if not self._can_ask("Explain with AI"):
            return
        self._explaining = (editor, problem)
        self._after_model = self._run_explain
        self._add_entry(Entry("explain", problem.message, editor.path, f"{editor.path.name}:{problem.line}",
                              self.ai_model))
        self._ensure_ollama(self._check_model)

    def _run_explain(self):
        editor, problem = self._explaining
        if editor not in self.editors():
            self.review_monitor.finish(False, "The file was closed")
            return
        prompt = ai.build_explain_prompt(editor.path.name, editor.editor.toPlainText(), problem.line, problem.source,
                                         problem.message, problem.badge, self.effective_compiler()[0])
        self._explain_prompt = prompt
        self.review_monitor.set_status("Thinking")
        worker = ai.ReviewWorker(ai.DEFAULT_HOST, self.ai_model, prompt, system=ai.EXPLAIN_SYSTEM_PROMPT,
                                 **self._answer_options())
        # (Methods, not lambdas: a lambda would run in the worker's thread.)
        worker.chunk.connect(self.review_monitor.add_answer)
        worker.finished.connect(self._explain_finished)
        worker.failed.connect(self._explain_failed)
        self._start_answer(worker)

    def _explain_finished(self, _text: str, stats: dict):
        entry = self._ai_entry
        self._remember(entry, ctx.Turn(entry.question, self._explain_prompt))
        detail = f"{stats.get('seconds', 0):.1f} s · {self.ai_model}" if stats.get("seconds") else self.ai_model
        self.review_monitor.finish(True, detail)

    def _explain_failed(self, message: str):
        hint = f" (is '{self.ai_model}' downloaded? See AI → Model)" if "not found" in message.lower() else ""
        self.review_monitor.finish(False, message + hint)

    def _end_explain_setup(self, why: str):
        if self.review_monitor.running:
            self.review_monitor.finish(False, why)

    def send_chat(self, question: str, with_file: bool, kind: str = "ask"):
        """A question from the Review panel's box: "ask" (with ``with_file``,
        the open file goes along), or "edit" (a change to the open file: its
        edits are shown as cards to accept or reject). The model sees
        everything in the timeline since New Chat (see ai_context)."""
        editing = kind == "edit"
        editor = self.current_editor() if with_file or editing else None
        if editing and editor is None:
            self.statusBar().showMessage("Open the file to change in the editor first", 5000)
            return
        if not self._can_ask("AI chat"):
            return
        request, attachment = question, None
        if editor is not None:
            self._ai_editor = editor  # (the findings' lines are this file's)
            text = editor.editor.toPlainText()
            selection = None
            if editing and self._edit_selection is not None and self._edit_selection[0] is editor:
                selection = self._edit_selection[1:]
                first, last = selection
                part = "\n".join(ctx.file_lines(text)[first - 1:last])
                request += (f"\n\nChange only these lines (lines {first}–{last} of the file), unless the "
                            f"change needs more:\n```fortran\n{part}\n```")
            attachment = ctx.file_attachment(
                editor.path.name, text, numbers=not editing, context=self._chat_context(),
                ratio=self._token_ratio, selection=selection,
                cursor_line=editor.editor.textCursor().blockNumber() + 1)
        self._chat_turn = ctx.Turn(question, request, attachment)
        self._chat_kind, self._chat_editor = kind, editor
        self._edit_selection = None
        monitor = self.review_monitor
        monitor.set_selection_note("")
        monitor.chat_edit.clear()
        self._after_model = self._run_chat
        self._add_entry(Entry(kind, question, editor.path if editor is not None else None,
                              editor.path.name if editor is not None else "", self.ai_model))
        self._ensure_ollama(self._check_model)

    def edit_with_ai(self, editor: EditorTab | None = None):
        """AI → Edit with AI… (or the editor's Edit Selection with AI…): the
        box, in Edit mode, for the selection if there is one."""
        editor = editor or self.current_editor()
        if editor is None:
            self.statusBar().showMessage("Open the file to change in the editor first", 5000)
            return
        lines = editor.editor.selected_lines()
        self._edit_selection = (editor, *lines) if lines else None
        monitor = self.review_monitor
        self.show_panel(monitor)
        monitor.set_chat_kind("edit")
        if lines:
            monitor.set_selection_note(f"line {lines[0]}" if lines[0] == lines[1] else f"lines {lines[0]}–{lines[1]}")
        else:
            monitor.set_selection_note("")
        monitor.chat_edit.setFocus()

    def fix_with_ai(self, path, problem):
        """Fix with AI… (a problem, or a finding in the timeline): a request
        to address it, sent in Edit mode for its lines (shown in the box's
        history, as if typed)."""
        self.open_file(str(path), force_editor=True)
        editor = next((e for e in self.editors() if e.path == Path(path).resolve()), None)
        if editor is None:
            return
        first, last = problem.line, max(problem.line, problem.end_line)
        self._edit_selection = (editor, first, last)
        message = problem.message
        what = {"build": "compiler message", "fortitude": f"fortitude issue {problem.badge}",
                "fortls": "fortls message"}.get(problem.source, problem.source)
        if problem.source == "review":  # ("AI (bug): …")
            found = re.match(r"AI \((\w+)\):\s*(.*)", message, re.DOTALL)
            what = f"AI review finding ({found.group(1)})" if found else "AI review finding"
            message = found.group(2) if found else message
        lines = f"line {first}" if first == last else f"lines {first}–{last}"
        monitor = self.review_monitor
        self.show_panel(monitor)
        monitor.set_chat_kind("edit")
        monitor.set_selection_note(lines)
        monitor.chat_edit.setText(f"Address this {what} at {lines}: {message} "
                                  "Change only what's needed for it.")
        monitor.chat_edit.setFocus()
        monitor.send_button.click()  # (if the AI is busy, the request stays in the box)

    def new_chat(self):
        """The model forgets everything so far (it stays in the timeline)."""
        self.stop_review()
        self._forget()
        self.review_monitor.add_divider()
        self.review_monitor.chat_edit.setFocus()

    def clear_ai_history(self):
        """Empty the timeline; the model forgets it too."""
        self.stop_review()
        self._forget()
        self.review_monitor.clear()

    def _forget(self):
        """The model forgets everything so far (what's still running too)."""
        self._turns = []
        self._memory_generation += 1

    def _chat_context(self) -> int:
        """The most context a chat asks for: the model's own limit, at most
        ai.MAX_CONTEXT."""
        return min(self._model_contexts.get(self.ai_model) or ai.MAX_CONTEXT, ai.MAX_CONTEXT)

    def _run_chat(self):
        editing = self._chat_kind == "edit"
        system = ai.EDIT_SYSTEM_PROMPT if editing else ai.CHAT_SYSTEM_PROMPT
        cap = self._chat_context()
        messages, tokens, dropped = ctx.compose(self._turns, self._chat_turn, system, cap, self._token_ratio)
        options = self._answer_options(edit=editing)
        room = options["num_predict"] + (ai.REASONING_TOKENS if options["think"] else 0) \
            if options["num_predict"] else ai.REASONING_TOKENS
        num_ctx = ai.context_for(tokens, cap, room)
        self._chat_estimate = (tokens, len(self._turns) - dropped)
        monitor = self.review_monitor
        monitor.context_note = (f"context ~{tokens / 1000:.1f}k of {num_ctx // 1024}k"
                                + (f" ({dropped} earliest left out)" if dropped else ""))
        if tokens > cap * ctx.BUDGET * 0.9:
            monitor.context_note += " · nearly full: New Chat starts over"
        monitor.set_status("Thinking")
        self._chat_keys: list = []
        worker = ai.ReviewWorker(ai.DEFAULT_HOST, self.ai_model, messages[-1]["content"], system=system,
                                 history=messages[1:-1], num_ctx=num_ctx,
                                 temperature=0.1 if editing else 0.2, **options)
        # (Methods, not lambdas: a lambda would run in the worker's thread.)
        worker.chunk.connect(self._chat_chunk)
        worker.finished.connect(self._chat_finished)
        worker.failed.connect(self._chat_failed)
        self._start_answer(worker)

    def _chat_findings(self):
        """Lines of the sent file the answer points to: listed in its entry,
        and marked in the editor (as a review's)."""
        entry, editor = self._ai_entry, self._chat_editor
        if editor is None or editor not in self.editors() or self._chat_kind == "edit":
            return
        findings, _summary = ai.parse_findings(entry.answer, editor.editor.blockCount())
        keys = [(f.line, f.end_line, f.category, f.comment) for f in findings]
        if findings and keys != self._chat_keys:
            self._chat_keys = keys
            entry.findings = findings
            self.review_monitor.refresh_soon(entry)
            editor.show_review(findings)

    def _chat_new_edits(self):
        """Edit blocks the answer has finished: cards for them."""
        entry = self._ai_entry
        if self._chat_kind != "edit" or entry is None or entry.path is None:
            return
        found = ed.parse_edits(entry.answer)
        if len(found) > len(entry.edits):
            entry.edits += found[len(entry.edits):]
            self._refresh_edits(entry)

    def _chat_chunk(self, text: str):
        self.review_monitor.add_answer(text)
        self._chat_findings()
        self._chat_new_edits()

    def _chat_finished(self, _text: str, stats: dict):
        self._chat_findings()
        self._chat_new_edits()
        tokens, earlier = self._chat_estimate
        measured = stats.get("prompt_tokens") or 0
        if measured and earlier == 0 and self._chat_turn.attachment is not None:
            # (Only for a first question: on later ones Ollama reuses what it
            # read before, and counts only the new part.)
            raw = tokens / self._token_ratio
            self._token_ratio = min(2.0, max(0.5, measured / raw))
        self._remember(self._ai_entry, self._chat_turn)
        detail = f"{stats.get('seconds', 0):.1f} s · {self.ai_model}" if stats.get("seconds") else self.ai_model
        if stats.get("thinking"):
            detail += " · the model spent its answer's length on reasoning (see AI → Answer Length)"
        elif stats.get("truncated"):
            detail += " · cut off at the length limit"
        self.review_monitor.finish(True, detail)

    def _chat_failed(self, message: str):
        self._chat_new_edits()
        if self._ai_entry is not None and self._ai_entry.answer:
            self._remember(self._ai_entry, self._chat_turn)  # (what it had written: part of the conversation)
        hint = f" (is '{self.ai_model}' downloaded? See AI → Model)" if "not found" in message.lower() else ""
        self.review_monitor.finish(False, message + hint)

    def _entry_editor(self, entry: Entry, open_it: bool = False) -> EditorTab | None:
        """The tab of an entry's file (opened, with ``open_it``, if it isn't)."""
        if entry.path is None:
            return None
        file = Path(entry.path).resolve()
        editor = next((e for e in self.editors() if e.path == file), None)
        if editor is None and open_it:
            self.open_file(str(file), force_editor=True)
            editor = next((e for e in self.editors() if e.path == file), None)
        return editor

    def _entry_text(self, entry: Entry) -> str | None:
        """An entry's file as it is now: its tab's text, or the file's."""
        editor = self._entry_editor(entry)
        if editor is not None:
            return editor.editor.toPlainText()
        try:
            return Path(entry.path).read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError, TypeError):
            return None

    def go_to_finding(self, entry: Entry, finding):
        editor = self._entry_editor(entry, open_it=True)
        if editor is not None:
            self.tabs.setCurrentWidget(editor)
            editor.editor.go_to_line(finding.line, select=True)

    def fix_finding(self, entry: Entry, finding):
        """Fix with AI… on a finding in the timeline."""
        if entry.path is not None:
            self.fix_with_ai(entry.path, Problem(finding.line, 0, finding.end_line, 0, "review", "review",
                                                 f"AI ({finding.category}): {finding.comment}", "", "review"))

    def show_findings_in_editor(self, entry: Entry):
        """An entry's findings, marked in the editor again."""
        editor = self._entry_editor(entry, open_it=True)
        if editor is not None:
            self.tabs.setCurrentWidget(editor)
            editor.show_review(entry.findings)

    def _relocate(self, edit: ed.Edit, text: str | None):
        """Find a pending edit again in the file as it is now: one that was
        there and no longer is turns stale (and back, if the text returns)."""
        if edit.status in (ed.APPLIED, ed.REJECTED):
            return
        was_there = edit.located or edit.status == ed.STALE
        if text is None:
            edit.start = edit.end = -1
            edit.status = ed.STALE
            return
        ed.locate(text, edit)
        if edit.located:
            edit.status = ed.PENDING
        elif was_there:
            edit.status = ed.STALE

    def _refresh_edits(self, entry: Entry):
        """The edit cards of every entry about this entry's file, found again
        in the file as it is now."""
        for other in self.review_monitor.entries:
            if other.edits and other.path == entry.path:
                text = self._entry_text(other)
                for edit in other.edits:
                    self._relocate(edit, text)
                self.review_monitor.refresh(other, text or "")

    def accept_edit(self, entry: Entry, index: int):
        self._make_edits(entry, [entry.edits[index]])

    def accept_all_edits(self, entry: Entry):
        self._make_edits(entry, [e for e in entry.edits if e.status == ed.PENDING])

    def _make_edits(self, entry: Entry, edits: list[ed.Edit]):
        """Make edits in the editor (one step to undo; the file is opened if
        it isn't), each found again in the text as it is now; any no longer
        there turn stale."""
        editor = self._entry_editor(entry, open_it=True)
        text = editor.editor.toPlainText() if editor is not None else None
        for edit in edits:
            self._relocate(edit, text)
        if editor is None:
            self._refresh_edits(entry)
            return
        located = [e for e in edits if e.located]
        _new, made = ed.apply_all(text, located)
        for edit in made:
            edit.diff = diff_rows(text, edit)
            edit.status = ed.APPLIED
        for edit in located:
            if edit not in made:
                edit.status = ed.STALE  # (overlapped another one made just now)
        if made:
            editor.editor.replace_ranges([(e.start, e.end, e.new_text) for e in made])
            self.statusBar().showMessage(f"Made {len(made)} edit{'s' * (len(made) != 1)} "
                                         f"(Edit → Undo takes {'them' if len(made) > 1 else 'it'} back)", 5000)
        self._refresh_edits(entry)

    def reject_edit(self, entry: Entry, index: int):
        self._reject(entry, [entry.edits[index]])

    def reject_all_edits(self, entry: Entry):
        self._reject(entry, [e for e in entry.edits if e.status == ed.PENDING])

    def _reject(self, entry: Entry, edits: list[ed.Edit]):
        text = self._entry_text(entry)
        for edit in edits:
            if edit.located and text is not None:
                edit.diff = diff_rows(text, edit)
            edit.status = ed.REJECTED
        self._refresh_edits(entry)

    def show_edit(self, entry: Entry, index: int):
        """A card was clicked: show (and select) its place in the editor."""
        edit, editor = entry.edits[index], self._entry_editor(entry, open_it=True)
        if editor is None:
            return
        self._relocate(edit, editor.editor.toPlainText())
        self.tabs.setCurrentWidget(editor)
        if edit.located:
            editor.editor.select_range(edit.start, edit.end)
        elif edit.line:
            editor.editor.go_to_line(edit.line)
