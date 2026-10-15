"""AI code review with a local model served by Ollama (https://ollama.com).

Everything runs on this machine: the GUI talks to an ``ollama serve``
process on localhost (starting one if needed), so no code leaves the
computer. Models are downloaded once with ``ollama pull`` (the GUI can do
that too) and are then available offline.

The model is asked to answer with one finding per line::

    LINE 12: bug: the loop never updates i
    LINE 30-34: style: use an assumed-shape array instead of (*)

followed by ``SUMMARY: ...``. `parse_findings` reads those lines back
(tolerating the small variations models produce), so findings can be listed
and marked in the editor's gutter.

The work happens in a `ReviewWorker` on a background thread; the
``ollama`` Python package is optional and imported lazily.
"""

from __future__ import annotations

import contextlib
import re
import shutil
import threading
import time
from dataclasses import dataclass

from qtpy.QtCore import QObject, QThread, Signal, Slot

from .ai_edits import END_RE as EDIT_END_RE
from .ai_edits import START_RE as EDIT_START_RE

DEFAULT_MODEL = "qwen2.5-coder:7b"
MAX_ANSWER_TOKENS = 1500  # the default answer length: plenty for a list of findings; stops runaway answers
ANSWER_LENGTHS = (1500, 4000, 8000, 16000, 0)  # AI → Answer Length (0: no limit)
REASONING_TOKENS = 8192  # room for a thinking model's reasoning (AI → Think Before Answering), on top
DEFAULT_HOST = None  # the ollama library's default: $OLLAMA_HOST or localhost:11434
CATEGORIES = ("bug", "warning", "performance", "style", "suggestion")

FOCUS = {
    "general": "Give a balanced review: correctness first, then clarity, performance and style.",
    "bugs": "Concentrate on real bugs: wrong results, out-of-bounds access, uninitialised "
            "variables, integer overflow, wrong intent, aliasing, precision loss, race conditions.",
    "modern": "Concentrate on modernising the code: implicit none, intent, modules instead of "
              "common blocks/include, assumed-shape arrays, allocatable instead of pointers, "
              "do concurrent where safe, removing obsolescent features (goto, arithmetic if, "
              "fixed form, equivalence).",
    "performance": "Concentrate on performance: memory access order (Fortran is column-major), "
                   "unnecessary temporaries and copies, repeated allocation in loops, "
                   "vectorisation and OpenMP opportunities.",
    "docs": "Concentrate on documentation and readability: missing or misleading comments, "
            "unclear names, FORD-style (!>) doc comments for public procedures.",
}
FOCUS_LABELS = {"general": "General", "bugs": "Bugs", "modern": "Modern Fortran style",
                "performance": "Performance", "docs": "Documentation"}

SYSTEM_PROMPT = """You are an experienced reviewer of scientific and numerical code, \
expert in modern Fortran (2008/2018), and also C and Python. You review one file at a time.

Rules for your answer:
- Write one line per finding, in exactly this form:
  LINE <n>: <category>: <comment>
  or, for a range of lines,
  LINE <start>-<end>: <category>: <comment>
- <category> is one of: bug, warning, performance, style, suggestion.
- Use the line numbers shown at the start of each source line.
- Keep each comment to one or two sentences and say how to fix it.
- Only report real, specific issues. Do not invent problems; if the code is fine, say so.
- After the findings, write a line starting with "SUMMARY:" with a one to three
  sentence overall assessment.
- Do not repeat the code and do not use Markdown headings or code blocks."""


@dataclass
class Finding:
    line: int
    end_line: int
    category: str
    comment: str


FINDING_RE = re.compile(
    r"""^\s*(?:[-*•]\s*|\d+[.)]\s*)?      # optional bullet / numbering
        \**\s*L(?:INE)?S?\s*\#?\s*(\d+)        # LINE 12 / Line 12 / L12
        (?:\s*(?:-|–|to)\s*(\d+))?        # optional range
        \s*\**\s*[:\])-]\s*\**\s*              # separator
        (?:(bug|error|warning|performance|style|suggestion|info|note)s?\**\s*[:\-]\s*)?
        (.+?)\s*$""",
    re.IGNORECASE | re.VERBOSE)
SUMMARY_RE = re.compile(r"^\s*\**\s*SUMMARY\s*\**\s*:\s*(.*)$", re.IGNORECASE)


def parse_findings(text: str, max_line: int | None = None) -> tuple[list[Finding], str]:
    """Findings and the summary from a model's answer."""
    findings: list[Finding] = []
    summary_lines: list[str] = []
    in_summary = False
    for raw in text.splitlines():
        if in_summary:
            if raw.strip():
                summary_lines.append(raw.strip())
            continue
        match = SUMMARY_RE.match(raw)
        if match:
            in_summary = True
            if match.group(1).strip():
                summary_lines.append(match.group(1).strip())
            continue
        match = FINDING_RE.match(raw)
        if not match:
            # Models often explain a finding on the following line(s).
            extra = raw.strip().lstrip("-*\u2022 ").strip()
            if extra and findings and not extra.startswith("```"):
                last = findings[-1]
                joiner = " " if last.comment.rstrip("\x00").endswith((".", "!", "?", ":")) else " — "
                last.comment = f"{last.comment}{joiner}{extra}"
            elif not extra and findings:
                findings[-1].comment += "\x00"  # a blank line ends the explanation
            continue
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start:
            start, end = end, start
        if max_line is not None and (start < 1 or start > max_line):
            continue  # a hallucinated line number
        category = (match.group(3) or "suggestion").lower()
        category = {"error": "bug", "info": "suggestion", "note": "suggestion"}.get(category, category)
        comment = match.group(4).strip().strip("*").strip()
        if comment:
            findings.append(Finding(start, min(end, max_line or end), category, comment))
    unique: list[Finding] = []
    seen = set()
    for f in findings:
        # Text after a blank line wasn't part of the finding.
        f.comment = f.comment.split("\x00")[0].strip()
        key = (f.line, f.end_line, f.category, " ".join(f.comment.lower().split()))
        if key not in seen:  # small models sometimes repeat themselves
            seen.add(key)
            unique.append(f)
    return unique, " ".join(summary_lines)


class RepeatGuard:
    """Spots a model stuck in a loop (writing a line it already wrote).

    Text is passed on a line at a time, so a repeated line can be held back:
    `feed` returns the text that's safe to show and whether to stop.

    Lines in code (``` blocks) and in edits (SEARCH/REPLACE blocks) aren't
    looked at: there, repeating is normal (an edit's new text repeats the
    unchanged lines of the old one). A loop there still ends at the answer's
    length limit.
    """

    MIN_LENGTH = 12  # short lines ("", "```", "SUMMARY:") may legitimately repeat

    def __init__(self):
        self._partial = ""
        self._seen: set[str] = set()
        self.repeated = False
        self._in_code = False   # in a ``` block
        self._in_edit = False   # in a SEARCH/REPLACE block

    @staticmethod
    def _key(line: str) -> str:
        return " ".join(line.lower().split())

    def feed(self, text: str) -> tuple[str, bool]:
        self._partial += text
        out = []
        while "\n" in self._partial:
            line, self._partial = self._partial.split("\n", 1)
            key = self._key(line)
            if EDIT_START_RE.match(line):
                self._in_edit = True
            elif EDIT_END_RE.match(line):
                self._in_edit = False
            elif line.lstrip().startswith("```"):
                self._in_code = not self._in_code
            elif len(key) >= self.MIN_LENGTH and not (self._in_code or self._in_edit):
                if key in self._seen:
                    self.repeated = True
                    self._partial = ""
                    return "".join(out), True
                self._seen.add(key)
            out.append(line + "\n")
        return "".join(out), False

    def flush(self) -> str:
        """Whatever is left at the end (a last line without a newline)."""
        rest, self._partial = self._partial, ""
        if rest and len(self._key(rest)) >= self.MIN_LENGTH and self._key(rest) in self._seen \
                and not (self._in_code or self._in_edit):
            self.repeated = True
            return ""
        return rest


def build_prompt(path: str, language: str, code: str, focus: str,
                 diagnostics: list[tuple[int, str, str]], first_line: int = 1, total_lines: int = 0) -> str:
    """The user message: instructions, compiler messages and numbered code.
    ``code`` can be part of the file: its lines from ``first_line`` (of
    ``total_lines``), numbered as in the file."""
    # Split only at newlines, like the editor: str.splitlines() would also
    # split at form feeds, U+2028 etc. and shift every later line number.
    lines = code.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()  # a final newline doesn't start another line
    last_line = first_line + len(lines) - 1
    width = len(str(last_line))
    numbered = "\n".join(f"{i:>{width}}| {line}" for i, line in enumerate(lines, first_line))
    parts = [f"Review this {language} file: {path}", FOCUS.get(focus, FOCUS["general"])]
    if total_lines and (first_line > 1 or last_line < total_lines):
        parts.append(f"Only lines {first_line}–{last_line} of the file's {total_lines} are shown: review "
                     "only these. Don't report problems that may come from code that isn't shown "
                     "(declarations elsewhere in the file, other procedures).")
        diagnostics = [d for d in diagnostics if first_line <= d[0] <= last_line]
    if diagnostics:
        notes = "\n".join(f"line {ln}: {sev}: {msg}" for ln, sev, msg in diagnostics[:40])
        parts.append("The compiler already reported these messages in the last build, and they "
                     "are shown to the user separately. Do NOT list them again; look for other "
                     "problems the compiler can't see:\n" + notes)
    parts.append("Source (line numbers on the left):\n" + numbered)
    return "\n\n".join(parts)


EXPLAIN_SYSTEM_PROMPT = """You are an expert in modern Fortran, its compilers (gfortran, \
LFortran, Intel) and linters, helping a programmer understand one message about their code. \
Answer in Markdown, briefly and concretely, for this code (not in general):
1. **What it means**: the message in plain words.
2. **Why here**: what in the marked line (and around it) causes it.
3. **How to fix it**: the change to make, with the corrected lines in a ```fortran block \
(code always in fenced blocks labeled with its language, so it can be highlighted).
If the message is harmless or a style preference, say so. Don't invent code that isn't shown."""

EXPLAIN_CONTEXT_LINES = 15  # lines of code shown before and after the problem


def build_explain_prompt(path: str, code: str, line: int, kind: str, message: str,
                         rule: str = "", compiler: str = "") -> str:
    """The question for one problem: the message, where, and the code around
    it (numbered, the problem's line marked with >>)."""
    lines = code.split("\n")
    first = max(1, line - EXPLAIN_CONTEXT_LINES)
    last = min(len(lines), line + EXPLAIN_CONTEXT_LINES)
    width = len(str(last))
    shown = "\n".join(f"{'>>' if n == line else '  '} {n:>{width}}| {lines[n - 1]}" for n in range(first, last + 1))
    source = {"build": f"the compiler ({compiler})" if compiler else "the compiler",
              "fortitude": "the fortitude linter", "fortls": "the fortls language server"}.get(kind, kind)
    what = f"{rule}: {message}" if rule and rule != kind else message
    return (f"In {path}, line {line}, {source} reports:\n\n    {what}\n\n"
            f"The code around it (line numbers on the left; the line is marked >>):\n\n{shown}\n\n"
            "Explain this message for this code, and how to fix it.")


CHAT_SYSTEM_PROMPT = """You are a helpful assistant for a programmer working on a Fortran \
project built with fpm (the Fortran Package Manager). You are an expert in modern Fortran \
(2008/2018), its compilers (gfortran, LFortran, Intel), fpm, and numerical methods. \
Answer in Markdown, concisely. Put code in fenced blocks labeled with its language \
(```fortran, ```toml for fpm.toml, ```bash for commands), so it can be highlighted. \
If you are not sure, say so.
When the user's file is attached (its lines are numbered on the left) and you point out a \
problem or a change at specific lines, also write each one on a line of its own, exactly as
  LINE <n>: <category>: <comment>
(or LINE <start>-<end>: ...), where <category> is one of: bug, warning, performance, style, \
suggestion. Those lines are listed for the user and marked in the editor."""


EDIT_SYSTEM_PROMPT = """You change a programmer's Fortran file as they ask. Reply with:
1. One or two sentences on what you'll change.
2. One SEARCH/REPLACE block per change, in exactly this form:
<<<<<<< SEARCH
(lines copied exactly from the file)
=======
(the new lines)
>>>>>>> REPLACE
Rules: change only what was asked; keep everything else exactly as it is (names, \
comments, formatting, free or fixed form). Copy the SEARCH lines exactly, enough of them \
(usually 2 to 6) to be found in only one place in the file. Never write "..." or \
"rest unchanged" inside a block. To delete lines, leave REPLACE empty. If the request is \
unclear or unsafe, ask instead of editing. Earlier edits in the conversation are marked \
applied, not applied yet, or rejected: answer the latest request, and don't propose a \
rejected edit again."""

EDIT_ANSWER_TOKENS = 4096  # (several blocks, each with the old and new text)
MAX_CONTEXT = 32768  # the most asked of Ollama (memory: a 7B model at 32k already needs ~6 GB)


def context_for(tokens: int, cap: int = MAX_CONTEXT, answer: int = MAX_ANSWER_TOKENS) -> int:
    """A context window for ``tokens`` of conversation plus the answer: a
    power of two from 4096, at most ``cap``."""
    size = 4096
    while size < tokens + answer and size < cap:
        size *= 2
    return min(size, cap)


def model_context_length(model: str, host: str | None = DEFAULT_HOST) -> int | None:
    """The longest context ``model`` was trained for, as Ollama reports it
    (None if it can't be asked)."""
    try:
        info = make_client(host, timeout=5).show(model)
        data = getattr(info, "modelinfo", None)
        if data is None and isinstance(info, dict):
            data = info.get("model_info") or info.get("modelinfo")
        for key, value in dict(data or {}).items():
            if str(key).endswith(".context_length"):
                return int(value)
    except Exception:  # noqa: BLE001 - not known, then
        return None
    return None


def ollama_available() -> bool:
    try:
        import ollama  # noqa: F401
    except ImportError:
        return False
    return True


def ollama_executable() -> str | None:
    return shutil.which("ollama")


def make_client(host: str | None = DEFAULT_HOST, timeout: float | None = None):
    import ollama
    return ollama.Client(host=host, timeout=timeout)


def server_running(host: str | None = DEFAULT_HOST) -> bool:
    try:
        make_client(host, timeout=2).list()
        return True
    except Exception:  # noqa: BLE001 - any failure means "not reachable"
        return False


def installed_models(host: str | None = DEFAULT_HOST) -> list[str]:
    try:
        response = make_client(host, timeout=5).list()
    except Exception:  # noqa: BLE001
        return []
    models = getattr(response, "models", None) or response.get("models", [])
    names = []
    for m in models:
        name = getattr(m, "model", None) or (m.get("model") if isinstance(m, dict) else None) \
            or (m.get("name") if isinstance(m, dict) else None)
        if name:
            names.append(name)
    return sorted(names)


class ReviewWorker(QObject):
    """Runs a review (or a model download) on a background thread."""

    chunk = Signal(str)                 # streamed text of the answer
    reasoning = Signal(str)             # streamed reasoning of a thinking model
    progress = Signal(str, float)       # download status, fraction (-1 = unknown)
    finished = Signal(str, dict)        # full answer, stats
    failed = Signal(str)

    def __init__(self, host: str | None, model: str, prompt: str = "", pull_only: bool = False,
                 system: str = SYSTEM_PROMPT, history: list[dict] | None = None,
                 num_ctx: int | None = None, temperature: float = 0.2,
                 num_predict: int = MAX_ANSWER_TOKENS, think: bool = False):
        """``system``: the model's instructions (a review's, by default;
        EXPLAIN_SYSTEM_PROMPT to explain one problem, CHAT_SYSTEM_PROMPT to
        chat, EDIT_SYSTEM_PROMPT for edits); ``history``: the conversation so
        far, as chat messages (``{"role": "user" | "assistant", "content":
        ...}``); ``num_ctx``: the context window (by default, from the
        prompt's and answer's lengths); ``num_predict``: the answer's length
        (0: no limit); ``think``: let a thinking model reason first (it gets
        REASONING_TOKENS more room for that)."""
        super().__init__()
        self.host, self.model, self.prompt, self.pull_only = host, model, prompt, pull_only
        self.system = system
        self.history = list(history or [])
        self.num_ctx, self.temperature, self.num_predict, self.think = num_ctx, temperature, num_predict, think
        self._cancel = threading.Event()
        self._client = None

    @property
    def limit(self) -> int:
        """The tokens the model may write (reasoning included); 0: no limit."""
        if self.num_predict <= 0:
            return 0
        return self.num_predict + (REASONING_TOKENS if self.think else 0)

    def cancel(self):
        """Stop (from the GUI thread). Closing the connection also stops a
        request that is still waiting for its first answer: the model can
        take a long time to load and read the prompt, and only then would
        the stopped flag be looked at. Ollama stops generating when the
        connection goes away."""
        self._cancel.set()
        client = self._client
        if client is not None:
            with contextlib.suppress(Exception):  # (already closed: fine)
                client.close()  # (its connection)

    def _stopped_message(self) -> str:
        return "Download cancelled" if self.pull_only else "Stopped"

    @Slot()
    def run(self):
        try:
            self._client = make_client(self.host)
            if self.pull_only:
                self._pull(self._client)
            else:
                self._review(self._client)
        except Exception as exc:  # noqa: BLE001 - shown to the user
            if self._cancel.is_set():  # the connection was closed to stop it
                self.failed.emit(self._stopped_message())
            else:
                self.failed.emit(str(exc) or type(exc).__name__)

    def _pull(self, client):
        for part in client.pull(self.model, stream=True):
            if self._cancel.is_set():
                self.failed.emit(self._stopped_message())
                return
            status = getattr(part, "status", "") or ""
            total = getattr(part, "total", None) or 0
            completed = getattr(part, "completed", None) or 0
            self.progress.emit(status, completed / total if total else -1.0)
        self.finished.emit("", {})

    def _review(self, client):
        asked = self.prompt + self.system + "".join(m["content"] for m in self.history)
        options = {
            "num_ctx": self.num_ctx or context_for(len(asked) // 3, answer=self.limit or REASONING_TOKENS),
            "temperature": self.temperature,
            # Discourage the repetition loops small models fall into, and
            # cap the answer so a runaway one always ends.
            "repeat_penalty": 1.1,
            "repeat_last_n": 512,
            "num_predict": self.limit or -1,  # (-1: no limit)
        }
        started = time.monotonic()
        messages = [{"role": "system", "content": self.system}, *self.history,
                    {"role": "user", "content": self.prompt}]
        # Thinking models (qwen3, deepseek-r1…) reason at length before they
        # answer, and that counts against num_predict: unless it's asked for
        # (with room of its own, see limit), thinking is turned off, which is
        # also much faster. A model that can't think, or can't stop, says so:
        # it's asked again as it is.
        stream = client.chat(model=self.model, stream=True, options=options, messages=messages, think=self.think)
        parts_iter = iter(stream)
        try:
            first = next(parts_iter, None)
        except Exception as exc:
            if "think" not in str(exc).lower():
                raise
            stream = client.chat(model=self.model, stream=True, options=options, messages=messages)
            parts_iter = iter(stream)
            first = next(parts_iter, None)
        guard = RepeatGuard()
        parts: list[str] = []
        stats: dict = {}
        thought = 0  # characters of reasoning (a model that thinks anyway)
        try:
            for part in _chain(first, parts_iter):
                if self._cancel.is_set():
                    self.failed.emit(self._stopped_message())
                    return
                message = getattr(part, "message", None)
                text = getattr(message, "content", "") if message is not None else ""
                thinking = (getattr(message, "thinking", "") or "") if message is not None else ""
                if thinking:
                    thought += len(thinking)
                    self.reasoning.emit(thinking)
                if text:
                    shown, stop = guard.feed(text)
                    if shown:
                        parts.append(shown)
                        self.chunk.emit(shown)
                    if stop:
                        break  # closing the stream makes Ollama stop generating
                if getattr(part, "done", False):
                    stats = {"tokens": getattr(part, "eval_count", 0) or 0,
                             "prompt_tokens": getattr(part, "prompt_eval_count", 0) or 0,
                             "seconds": (getattr(part, "total_duration", 0) or 0) / 1e9}
                    if self.limit and (stats["tokens"] or 0) >= self.limit:
                        stats["truncated"] = True
                        if thought and thought > 3 * sum(map(len, parts)):
                            stats["thinking"] = True  # (the limit went on reasoning, not the answer)
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                with contextlib.suppress(Exception):  # the connection may be gone (stopped)
                    close()
        rest = guard.flush()
        if rest:
            parts.append(rest)
            self.chunk.emit(rest)
        if guard.repeated:
            stats = {"seconds": time.monotonic() - started, "repeated": True}
            self.chunk.emit("\n[The model started repeating itself, so the review was stopped here.]\n")
        self.finished.emit("".join(parts), stats)


def _chain(first, rest):
    """``first`` (if any), then the rest of an iterator."""
    if first is not None:
        yield first
    yield from rest


def start_worker(worker: ReviewWorker, parent: QObject) -> QThread:
    """Run ``worker`` on a new thread that cleans itself up when done."""
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
