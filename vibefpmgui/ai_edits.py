"""Edits proposed by the AI (the Review panel's *Edit* mode), as
search/replace blocks::

    <<<<<<< SEARCH
        do i = 1, n
    =======
        do j = 1, n
    >>>>>>> REPLACE

A block is found in the file by its text, not by line numbers (which small
models get wrong, and which every earlier edit would shift). When the exact
text isn't there, it's looked for again ignoring trailing spaces, then
indentation (the replacement is then re-indented to match), then with line
numbers the model copied from the prompt taken out. An edit that matches
nothing, or more than one place, isn't applied: it never lands anywhere but
where it says.

Pure text functions (no Qt), used by `vibefpmgui.main_window` and shown by
`vibefpmgui.edit_cards`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

START_RE = re.compile(r"^\s*<{5,9}\s*SEARCH\s*$")
DIVIDER_RE = re.compile(r"^\s*={5,9}\s*$")
END_RE = re.compile(r"^\s*>{5,9}\s*REPLACE\s*$")
FENCE_RE = re.compile(r"^\s*```")
NUMBER_RE = re.compile(r"^\s*\d+\s?\| ?")  # "  12| " (how the chat numbers a file's lines)

PENDING, APPLIED, REJECTED, STALE, FAILED = "pending", "applied", "rejected", "stale", "failed"


@dataclass
class Edit:
    search: str            # as the model wrote it
    replace: str
    start: int = -1        # where it applies in the text it was located in (offsets), -1: nowhere
    end: int = -1
    new_text: str = ""     # what goes there (the replacement, re-indented if needed)
    line: int = 0          # the first and last lines it replaces (1-based)
    end_line: int = 0
    how: str = ""          # "", "spacing", "indentation", "line numbers": what had to be ignored
    status: str = PENDING
    problem: str = ""      # why it can't be applied
    diff: list[tuple[str, str]] | None = None  # the change as shown when it was made or turned down

    @property
    def located(self) -> bool:
        return self.start >= 0

    def block(self) -> str:
        """The block as the model wrote it."""
        return f"<<<<<<< SEARCH\n{self.search}\n=======\n{self.replace}\n>>>>>>> REPLACE"


def parse_edits(answer: str) -> list[Edit]:
    """The complete search/replace blocks in an answer (also one still being
    written: a block counts once its REPLACE line has come)."""
    edits: list[Edit] = []
    state, search, replace = "", [], []
    for line in answer.replace("\r\n", "\n").split("\n"):
        if state == "":
            if START_RE.match(line):
                state, search, replace = "search", [], []
        elif state == "search":
            if DIVIDER_RE.match(line):
                state = "replace"
            elif START_RE.match(line):  # (a block left unfinished: start again)
                search = []
            else:
                search.append(line)
        elif END_RE.match(line):
            edits.append(Edit(_unfence(search), _unfence(replace)))
            state = ""
        else:
            replace.append(line)
    return edits


def _unfence(lines: list[str]) -> str:
    """A side of a block, without a ``` fence the model put around it."""
    if lines and FENCE_RE.match(lines[0]):
        lines = lines[1:]
    if lines and FENCE_RE.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines)


def locate(text: str, edit: Edit, tab_width: int = 4) -> Edit:
    """Find where ``edit`` applies in ``text``: sets start/end, new_text,
    line/end_line and how; or problem (and status FAILED) when it can't."""
    edit.start = edit.end = -1
    edit.problem, edit.how = "", ""
    if edit.status == FAILED:
        edit.status = PENDING
    if not edit.search.strip():  # (nothing to find: add at the end)
        joiner = "" if not text or text.endswith("\n") else "\n"
        _found(text, edit, len(text), len(text), joiner + edit.replace.strip("\n") + "\n", "")
        return edit
    count = text.count(edit.search)
    if count == 1:
        start = text.index(edit.search)
        _found(text, edit, start, start + len(edit.search), edit.replace, "")
        return edit
    if count > 1:
        return _fail(edit, f"matches {count} places: ask for a more specific change")
    search, replace = _trim_blank_ends(edit.search.split("\n"), edit.replace.split("\n"))
    lines = text.split("\n")
    for numbers in (False, True):
        if numbers:  # (the model copied the prompt's line numbers)
            if not all(NUMBER_RE.match(s) for s in search if s.strip()):
                break
            search = [NUMBER_RE.sub("", s, count=1) for s in search]
            replace = [NUMBER_RE.sub("", r, count=1) for r in replace]
        for how, same in (("spacing", lambda a, b: a.rstrip() == b.rstrip()),
                          ("indentation", lambda a, b: a.strip() == b.strip())):
            places = _places(lines, search, same, tab_width)
            if len(places) > 1:
                return _fail(edit, f"matches {len(places)} places: ask for a more specific change")
            if places:
                first = places[0]
                new = replace
                if how == "indentation":
                    new = _reindent(replace, search, lines[first:first + len(search)], tab_width)
                start = sum(len(line) + 1 for line in lines[:first])
                end = start + len("\n".join(lines[first:first + len(search)]))
                _found(text, edit, start, end, "\n".join(new), "line numbers" if numbers else how)
                return edit
    return _fail(edit, "not found: the model's text doesn't match the file")


def _places(lines: list[str], search: list[str], same, tab_width: int) -> list[int]:
    n = len(search)
    if n == 0:
        return []
    wanted = [s.expandtabs(tab_width) for s in search]
    return [i for i in range(len(lines) - n + 1)
            if all(same(lines[i + k].expandtabs(tab_width), wanted[k]) for k in range(n))]


def _trim_blank_ends(search: list[str], replace: list[str]) -> tuple[list[str], list[str]]:
    """Blank lines around SEARCH (models add them) don't have to match; the
    same number goes from around REPLACE, when it has them too."""
    while search and not search[0].strip():
        search = search[1:]
        if replace and not replace[0].strip():
            replace = replace[1:]
    while search and not search[-1].strip():
        search = search[:-1]
        if replace and not replace[-1].strip():
            replace = replace[:-1]
    return search, replace


def _indent(line: str, tab_width: int) -> int:
    expanded = line.expandtabs(tab_width)
    return len(expanded) - len(expanded.lstrip(" "))


def _reindent(replace: list[str], search: list[str], found: list[str], tab_width: int) -> list[str]:
    """Shift the replacement by what the model's indentation was off by."""
    pair = next(((s, f) for s, f in zip(search, found, strict=False) if s.strip()), None)
    if pair is None:
        return replace
    shift = _indent(pair[1], tab_width) - _indent(pair[0], tab_width)
    if shift == 0:
        return replace
    shifted = []
    for line in replace:
        if not line.strip():
            shifted.append(line)
        elif shift > 0:
            shifted.append(" " * shift + line)
        else:
            line = line.expandtabs(tab_width)
            shifted.append(line[min(-shift, _indent(line, tab_width)):])
    return shifted


def _found(text: str, edit: Edit, start: int, end: int, new_text: str, how: str):
    # Removing whole lines: take the line break too, not to leave a blank line.
    if not new_text and (start == 0 or text[start - 1] == "\n"):
        if end < len(text) and text[end] == "\n":
            end += 1
        elif start > 0 and end == len(text):
            start -= 1
    edit.start, edit.end, edit.new_text, edit.how = start, end, new_text, how
    edit.line = text.count("\n", 0, start) + 1
    edit.end_line = edit.line + text.count("\n", start, max(start, end - 1))


def _fail(edit: Edit, problem: str) -> Edit:
    edit.problem = problem
    edit.status = FAILED
    return edit


def apply(text: str, edit: Edit) -> str:
    """``text`` with a located edit made."""
    if not edit.located:
        raise ValueError("the edit wasn't located")
    return text[:edit.start] + edit.new_text + text[edit.end:]


def apply_all(text: str, edits: list[Edit]) -> tuple[str, list[Edit]]:
    """``text`` with every located edit made (from the end of the text, so
    the offsets stay right); edits overlapping one already made are left
    out. Returns the new text and the edits made."""
    made: list[Edit] = []
    for edit in sorted((e for e in edits if e.located), key=lambda e: e.start, reverse=True):
        if made and edit.end > made[-1].start:
            continue  # (overlaps the next one down)
        text = apply(text, edit)
        made.append(edit)
    return text, made[::-1]


def report(edits: list[Edit]) -> str:
    """What became of an answer's edits, said in the next question ("edit 1,
    at line 30, was applied; edit 2 was rejected"), so the model knows. (In
    the user's words, not in the model's answer: small models copied notes
    put there, or a placeholder for the edit, instead of writing an edit.)"""
    words = {APPLIED: "was applied", PENDING: "is not applied yet", REJECTED: "was rejected by the user",
             FAILED: "wasn't found in the file", STALE: "no longer matches the file"}
    parts = [f"edit {i}{f', at line {e.line},' if e.line else ''} {words.get(e.status, 'was not used')}"
             for i, e in enumerate(edits, 1)]
    return f"(About your previous answer's edits: {'; '.join(parts)}.)" if parts else ""
