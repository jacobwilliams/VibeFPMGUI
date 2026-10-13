"""What a chat with the AI sends the model each time, so a conversation
fits in its context window (Ollama doesn't fail when it doesn't: it drops
the start, system prompt included).

- **The file once per version.** A question carries the open file only if
  it changed since it was last sent; earlier copies in the history are
  replaced by a placeholder.
- **What became of the edits** proposed in an answer is said at the start
  of the next question (see `vibefpmgui.ai_edits.report`); answers are sent
  back as the model wrote them.
- **The oldest turns go first** when the estimate is over the budget (75%
  of the context, the rest left for the answer); the system prompt, the
  current file and the new question always stay.
- **Big files are sent in part:** the selection, or the procedure around
  the cursor, in full, plus an outline of the rest (`file_attachment`).

Pure functions (no Qt).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .ai_edits import Edit, report

BUDGET = 0.75           # of the context window, for what's sent (the rest: the answer)
BIG_FILE_LINES = 600    # bigger files are sent in part
BIG_FILE_SHARE = 0.4    # …and so are files taking more than this share of the budget
WINDOW_PADDING = 20     # lines shown around the selection or procedure
WINDOW_MAX_LINES = 400

PROCEDURE_RE = re.compile(r"^\s*(?!end\b)(?:[\w(),=*\s]*?\s)?(subroutine|function)\s+\w+", re.IGNORECASE)
PROCEDURE_END_RE = re.compile(r"^\s*end\s*(subroutine|function)\b|^\s*end\s*(!.*)?$", re.IGNORECASE)
OUTLINE_RE = re.compile(
    r"^\s*(module|submodule|program|use|implicit|contains|interface|abstract\s+interface|"
    r"end\s*(module|submodule|program|interface|type|subroutine|function)\b|"
    r"type\s*(,|::|\s\w)|(?!end\b)(?:[\w(),=*\s]*?\s)?(subroutine|function)\s+\w+)",
    re.IGNORECASE)


def estimate_tokens(text: str, ratio: float = 1.0) -> int:
    """A rough token count (about 3 characters each for code), scaled by
    ``ratio``: measured counts over estimated ones, from earlier answers."""
    return int(len(text) / 3 * ratio) + 4


@dataclass
class Attachment:
    """The open file as sent with a question: all of it, or part of it."""
    name: str
    file_text: str  # the whole file when it was sent
    shown: str      # what the prompt contains
    partial: bool = False


@dataclass
class Turn:
    question: str               # as the user typed it
    request: str                # what's sent besides the file (the question, plus notes)
    attachment: Attachment | None = None  # the file wanted with it (sent if it changed)
    answer: str = ""
    edits: list[Edit] = field(default_factory=list)


def placeholder(attachment: Attachment) -> str:
    return f"[{attachment.name} as it was then: replaced by the newer version below]"


def compose(past: list[Turn], turn: Turn, system: str, context: int,
            ratio: float = 1.0) -> tuple[list[dict], int, int]:
    """The messages for ``turn`` after the ``past`` ones: (messages, the
    estimated tokens, how many of the oldest turns were left out)."""
    kept = list(past)
    budget = int(context * BUDGET)
    while True:
        messages = _render(kept, turn, system)
        tokens = sum(estimate_tokens(m["content"], ratio) for m in messages)
        if tokens <= budget or not kept:
            return messages, tokens, len(past) - len(kept)
        kept.pop(0)


def _render(kept: list[Turn], turn: Turn, system: str) -> list[dict]:
    last = next((t.attachment for t in reversed(kept) if t.attachment is not None), None)
    send = turn.attachment is not None and turn.attachment != last
    full = None if send else next((i for i in range(len(kept) - 1, -1, -1) if kept[i].attachment), None)
    messages = [{"role": "system", "content": system}]
    previous: Turn | None = None
    for i, t in enumerate(kept):
        user = _with_report(previous, t.request)
        if t.attachment is not None:
            user += "\n\n" + (t.attachment.shown if i == full else placeholder(t.attachment))
        messages.append({"role": "user", "content": user})
        messages.append({"role": "assistant", "content": t.answer})
        previous = t
    request = _with_report(previous, turn.request)
    if send:
        request += "\n\n" + turn.attachment.shown
    elif turn.attachment is not None:
        request += f"\n\n(My file, {turn.attachment.name}, is as I sent it above.)"
    messages.append({"role": "user", "content": request})
    return messages


def _with_report(previous: Turn | None, request: str) -> str:
    note = report(previous.edits) if previous is not None else ""
    return f"{note}\n\n{request}" if note else request


# ------------------------------------------------------------- the file
def numbered(lines: list[str], first: int = 1) -> str:
    width = len(str(first + len(lines) - 1))
    return "\n".join(f"{n:>{width}}| {line}" for n, line in enumerate(lines, first))


def file_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()  # (a final newline doesn't start another line)
    return lines


def file_attachment(name: str, text: str, numbers: bool, context: int, ratio: float = 1.0,
                    selection: tuple[int, int] | None = None, cursor_line: int = 1) -> Attachment:
    """The file as sent: with line numbers (for answers that point at lines)
    or as it is (for edits, whose old text must be copied exactly). A big
    file is sent in part: the ``selection`` (first, last line) or the
    procedure around ``cursor_line``, plus an outline of the rest."""
    lines = file_lines(text)
    whole = numbered(lines) if numbers else "\n".join(lines)
    big = len(lines) > BIG_FILE_LINES or estimate_tokens(whole, ratio) > context * BUDGET * BIG_FILE_SHARE
    if not big:
        how = "line numbers on the left" if numbers else "exactly as it is"
        return Attachment(name, text, f"My file, {name} ({how}):\n\n```fortran\n{whole}\n```")
    first, last = window(lines, selection, cursor_line)
    part = lines[first - 1:last]
    shown = numbered(part, first) if numbers else "\n".join(part)
    outline = [(n, line) for n, line in enumerate(lines, 1)
               if not first <= n <= last and OUTLINE_RE.match(line)]
    outline_text = "\n".join(f"line {n}: {line.strip()}" for n, line in outline) or "(nothing)"
    return Attachment(name, text, (
        f"My file, {name}, has {len(lines)} lines: too long to send all of it. Only lines "
        f"{first}–{last} are shown in full; for the rest, only an outline. Base changes on the "
        f"lines shown in full.\n\nOutline of the rest:\n{outline_text}\n\n"
        f"Lines {first}–{last}{' (line numbers on the left)' if numbers else ''}:\n\n"
        f"```fortran\n{shown}\n```"), partial=True)


def window(lines: list[str], selection: tuple[int, int] | None, cursor_line: int) -> tuple[int, int]:
    """The lines to send in full (1-based, inclusive): the selection, or the
    procedure around the cursor, with some lines around it."""
    if selection is not None:
        first, last = selection
    else:
        first, last = enclosing_procedure(lines, cursor_line)
    first, last = max(1, first - WINDOW_PADDING), min(len(lines), last + WINDOW_PADDING)
    if last - first + 1 > WINDOW_MAX_LINES:  # (a huge procedure: around the cursor in it)
        center = cursor_line if selection is None else selection[0]
        first = max(1, min(center - WINDOW_MAX_LINES // 2, len(lines) - WINDOW_MAX_LINES + 1))
        last = min(len(lines), first + WINDOW_MAX_LINES - 1)
    return first, last


def enclosing_procedure(lines: list[str], line: int) -> tuple[int, int]:
    """The subroutine or function around ``line`` (first, last), or the line
    itself when it's in none."""
    line = min(max(1, line), max(1, len(lines)))
    start = next((n for n in range(line, 0, -1) if PROCEDURE_RE.match(lines[n - 1])), None)
    if start is None or any(PROCEDURE_END_RE.match(lines[n - 1]) for n in range(start + 1, line)):
        return line, line  # (in none, or between two)
    end = next((n for n in range(max(line, start + 1), len(lines) + 1) if PROCEDURE_END_RE.match(lines[n - 1])),
               None)
    if end is None or (end < line):
        return line, line
    return start, end
