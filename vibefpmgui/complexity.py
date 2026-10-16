"""Code complexity of Fortran: lizard's metrics (https://github.com/terryyin/lizard),
measured by a reader of Fortran's own (no Qt).

Per procedure (subroutines, functions, module procedures, a main program):

- **CCN**, the cyclomatic complexity: 1, plus 1 for each decision: ``if``
  (a one-line one too), ``else if``, each loop (``do`` with a loop control,
  ``do while``, ``do concurrent``, ``forall``: not an endless ``do``, whose
  way out is an ``exit`` under an ``if``), each ``case (…)`` (not ``case default``),
  each ``type is`` / ``class is`` / ``rank (…)`` guard, each masked ``where``
  / ``elsewhere``, and each ``.and.`` / ``.or.``. An arithmetic ``if`` counts
  one less than its different labels (up to 2), a computed ``goto`` as many
  as its different labels (it can also go on).
- **NLOC**: its lines with code (not blank, not only a comment).
- **Tokens**: its code's tokens (names, numbers, strings, operators).
- **Parameters**: its dummy arguments.
- **Length**: its lines, first to last (with comments and blank lines).
- **Depth**: how deeply its block constructs nest (``do``, ``if … then``,
  ``select``, ``where``, ``associate``, ``block``…).

An internal procedure (after ``contains``) is measured on its own: its lines,
tokens and decisions aren't its host's (its lines are in the host's length).
Interface bodies are declarations, not procedures.

The reader works a statement at a time: free and fixed form, comments,
continuation lines, ``;``, labels and construct names, and strings (whose
text is never read as code). Preprocessor lines (``#ifdef``…) aren't code
lines, as in lizard: they choose the code, they aren't part of it. Included
files (``#include "…"``, ``include '…'``) are read where they're included,
as the compiler reads them (lizard doesn't): their code is the including
procedure's (a procedure's body can be all in one). Their lines are counted
for the file that includes them; a procedure's length is its own file's lines.

Where lizard's Fortran reader (1.24.1, with the fixes in its Fortran issue)
counts differently: it counts ``select case`` and ``case default`` too, and
the preprocessor's ``#if`` family; it doesn't count ``else if`` written with
a space, ``where``, ``forall`` or ``type is``, and counts an endless ``do``. It reports interface bodies
(declarations) as functions but not a main program, ends a host procedure
where its internal procedures start, and misses procedures ended by a bare
``END`` after another one (fixed form).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .source_text import decode

FIXED_FORM = {".f", ".for", ".ftn", ".f77"}
FORTRAN = FIXED_FORM | {".f90", ".f95", ".f03", ".f08", ".f18", ".f23", ".fpp"}
# lizard's warning thresholds (its -C, -L, -a), and a nesting depth's (its -ND default).
THRESHOLDS = {"ccn": 15, "length": 1000, "params": 100, "depth": 5}


def is_fortran(path: Path) -> bool:
    return path.suffix.lower() in FORTRAN


@dataclass
class Procedure:
    name: str
    kind: str                 # "subroutine", "function", "procedure" (a module procedure), "program"
    container: str            # the module (or host procedure) it's in: "calc", "calc > outer"; "" if none
    path: Path
    start: int                # 1-based lines
    end: int = 0
    nloc: int = 0
    ccn: int = 1
    tokens: int = 0
    params: int = 0
    depth: int = 0

    @property
    def length(self) -> int:
        return self.end - self.start + 1

    @property
    def qualified(self) -> str:
        return f"{self.container}::{self.name}" if self.container else self.name

    def warnings(self, thresholds: dict[str, int] = THRESHOLDS) -> list[str]:
        """The metrics over their thresholds ("ccn", "length"…)."""
        values = {"ccn": self.ccn, "length": self.length, "params": self.params, "depth": self.depth}
        return [key for key, limit in thresholds.items() if values[key] > limit]


@dataclass
class FileMetrics:
    path: Path
    nloc: int = 0             # every line with code in the file (also outside procedures; with its included files')
    tokens: int = 0
    procedures: list[Procedure] = field(default_factory=list)
    includes: list[Path] = field(default_factory=list)  # the files it includes (theirs too)

    @property
    def average_ccn(self) -> float:
        return sum(p.ccn for p in self.procedures) / len(self.procedures) if self.procedures else 0.0

    @property
    def average_nloc(self) -> float:
        return sum(p.nloc for p in self.procedures) / len(self.procedures) if self.procedures else 0.0


# ------------------------------------------------------------------ included files
INCLUDE_RE = re.compile(r"""^\s*(?:#\s*include\s*["<]([^">]+)[">]|include\s*(['"])(.+?)\2\s*(?:!.*)?$)""",
                        re.IGNORECASE)
MAX_INCLUDE_DEPTH = 10


@dataclass
class Source:
    """Code with its included files read in where they're included."""
    text: str
    origins: list[tuple[Path, int]] | None = None  # each line's file and line there (None: no includes)
    includes: list[Path] = field(default_factory=list)

    def origin(self, line: int, path: Path) -> tuple[Path, int]:
        """Where ``line`` (1-based, of ``text``) is: its file and line there."""
        if self.origins is None or not 0 < line <= len(self.origins):
            return path, line
        return self.origins[line - 1]


def expand_includes(text: str, path: Path | str) -> Source:
    """``text`` (``path``'s) with each file it includes in place of the line
    that includes it (looked for beside the including file, then beside
    ``path``); a file that isn't found stays an include line."""
    path = Path(path)
    if "include" not in text.lower():  # (most files: nothing to do)
        return Source(text)
    lines: list[str] = []
    origins: list[tuple[Path, int]] = []
    includes: list[Path] = []

    def read(code: str, where: Path, depth: int, reading: tuple[Path, ...]):
        for number, line in enumerate(code.split("\n"), start=1):
            found = INCLUDE_RE.match(line) if "include" in line.lower() else None
            name = (found.group(1) or found.group(3)) if found else None
            target = _find_include(name, where, path) if name else None
            if target is None or depth >= MAX_INCLUDE_DEPTH or target in reading:
                lines.append(line)
                origins.append((where, number))
                continue
            try:
                included = decode(target.read_bytes())[0]
            except OSError:
                lines.append(line)
                origins.append((where, number))
                continue
            if target not in includes:
                includes.append(target)
            read(included.replace("\r\n", "\n"), target, depth + 1, (*reading, target))

    read(text, path, 0, (path,))
    if not includes:
        return Source(text)
    return Source("\n".join(lines), origins, includes)


def _find_include(name: str, where: Path, top: Path) -> Path | None:
    for folder in dict.fromkeys((where.parent, top.parent)):
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


# ------------------------------------------------------------------ statements
@dataclass
class Statement:
    text: str                 # lowercase code: strings blanked (''), no comments, label or construct name
    label: str                # its statement label ("" if none)
    lines: list[int]          # the lines it's on (with code)
    tokens: int = 0


def _strip_code(line: str, quote: str | None) -> tuple[str, str | None]:
    """(a line's code with strings' text blanked and its comment removed, the
    quote still open at its end: a string continued on the next line)."""
    out = []
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            if ch == quote:
                if i + 1 < len(line) and line[i + 1] == quote:  # ('it''s': a quote in the string)
                    i += 2
                    continue
                out.append(ch)
                quote = None
            elif ch == "&" and line[i + 1:].strip() == "":  # (the string goes on, on the next line)
                out.append("&")
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "!":
            break
        else:
            out.append(ch)
        i += 1
    return "".join(out), quote


TOKEN_RE = re.compile(r"'[^']*'|\"[^\"]*\"|\.[a-z]+\.|\d+(?:\.\d*)?(?:[ed][+-]?\d+)?(?:_\w+)?|\w+"
                      r"|::|=>|\*\*|//|==|/=|<=|>=|\S")
LABEL_RE = re.compile(r"^(\d{1,5})\s+")
CONSTRUCT_NAME_RE = re.compile(r"^[a-z_]\w*\s*:(?!:)\s*")


def statements(text: str, fixed: bool) -> list[Statement]:
    """The code's statements, in order (preprocessor lines are left out)."""
    out: list[Statement] = []
    pending: list[str] = []
    pending_lines: list[int] = []
    quote: str | None = None
    label = ""

    def finish():
        nonlocal pending, pending_lines, label
        if pending_lines:
            joined = " ".join(pending)
            first = True
            for part in _split_semicolons(joined):
                part = part.strip()
                if not part:
                    continue
                this_label = label if first else ""
                found = LABEL_RE.match(part)
                if found:
                    this_label, part = found.group(1), part[found.end():]
                part = CONSTRUCT_NAME_RE.sub("", part, count=1)
                out.append(Statement(part.lower(), this_label, list(pending_lines),
                                     len(TOKEN_RE.findall(part.lower()))))
                first = False
        pending, pending_lines, label = [], [], ""

    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.rstrip("\r")
        if line.startswith("#"):
            continue
        if fixed:
            if line[:1] in ("c", "C", "*", "!", "d", "D") or not line.strip():
                continue
            continued = len(line) > 5 and line[5] not in (" ", "0", "\t") and not line[:5].strip()
            body = line[6:] if len(line) > 6 else ""
            if not continued:
                finish()
                label = line[:5].strip() if line[:5].strip().isdigit() else ""
            code, quote = _strip_code(body, quote if continued else None)
            if code.strip():
                pending.append(code)
                pending_lines.append(number)
            continue
        if quote:  # (a string going on from the line before: up to its "&")
            stripped = line.lstrip()
            code, quote = _strip_code(stripped.removeprefix("&"), quote)
        else:
            stripped = line.lstrip()
            if pending and stripped.startswith("&"):
                stripped = stripped[1:]
            code, quote = _strip_code(stripped, None)
        if not code.strip():
            continue
        body = code.rstrip()
        continues = body.endswith("&")
        pending.append(body[:-1] if continues else body)
        pending_lines.append(number)
        if not continues and not quote:
            finish()
    finish()
    return out


def _split_semicolons(code: str) -> list[str]:
    parts, start, quote = [], 0, None
    for i, ch in enumerate(code):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            parts.append(code[start:i])
            start = i + 1
    parts.append(code[start:])
    return parts


def _close_paren(text: str, start: int) -> int:
    """The index just past the ")" matching the "(" at ``start`` (len(text) if none)."""
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
    return len(text)


def _is_assignment(text: str) -> bool:
    """An assignment ("x = …", "a(i)%b = …", "p => …"): a variable (with
    subscripts and components) before the "=". Not "if (c) x = 1", nor
    "do i = 1, n" (a keyword, then something other than "(", "%" or "=")."""
    found = re.match(r"[a-z_]\w*", text)
    if not found:
        return False
    i = found.end()
    while True:
        while i < len(text) and text[i] == " ":
            i += 1
        if i < len(text) and text[i] == "(":
            i = _close_paren(text, i)
        elif i < len(text) and text[i] == "%":
            name = re.match(r"%\s*[a-z_]\w*", text[i:])
            if not name:
                return False
            i += name.end()
        else:
            break
    rest = text[i:]
    return rest.startswith("=>") or (rest.startswith("=") and not rest.startswith("=="))


# ------------------------------------------------------------------ structure
END_RE = re.compile(r"^end\s*(subroutine|function|program|module|submodule|interface|type|select|where|forall|"
                    r"associate|block\s*data|block|critical|procedure|enum|team|do|if)?\b\s*\w*\s*$")
PROCEDURE_RE = re.compile(r"^(?:([\w\s(),=*:]*?)\s)?(subroutine|function)\s+(\w+)\s*(\(.*)?$")
MODULE_PROCEDURE_RE = re.compile(r"^module\s+procedure\s+(\w+)\s*$")
PROGRAM_RE = re.compile(r"^program\s+(\w+)")
MODULE_RE = re.compile(r"^module\s+(\w+)\s*$")
SUBMODULE_RE = re.compile(r"^submodule\s*\(.*\)\s*(\w+)")
BLOCK_DATA_RE = re.compile(r"^block\s*data\b")
INTERFACE_RE = re.compile(r"^(abstract\s+)?interface\b")
TYPE_DEF_RE = re.compile(r"^type\s*(?:,[^:]*)?::\s*\w+|^type\s+(?!is\b)\w+\s*$")
ENUM_RE = re.compile(r"^enum\s*,")
DO_RE = re.compile(r"^do\b\s*(\d+)?")
SELECT_RE = re.compile(r"^select\s*(case|type|rank)\s*\(")
GUARD_RE = re.compile(r"^(case\s*\(|type\s+is\s*\(|class\s+is\s*\(|rank\s*\()")
LOGICAL_RE = re.compile(r"\.(and|or)\.")
UNIT_KINDS = {"subroutine", "function", "procedure", "program"}
# Scopes that a bare "end" closes (in a program unit or an interface body).
BARE_END = UNIT_KINDS | {"module", "submodule", "blockdata", "body"}


@dataclass
class _Scope:
    kind: str                 # a UNIT_KINDS, "module", "submodule", "blockdata", "interface", "body", "type", "enum"
    name: str
    unit: Procedure | None = None
    constructs: list = field(default_factory=list)  # (kind, label) of the open block constructs


def analyze_text(text: str, path: Path | str = "x.f90") -> FileMetrics:
    """Fortran source's metrics (see the module's docstring)."""
    path = Path(path)
    found = FileMetrics(path)
    source = expand_includes(text, path)
    found.includes = source.includes
    stmts = statements(source.text, path.suffix.lower() in FIXED_FORM)
    stack: list[_Scope] = []

    def unit() -> _Scope | None:
        return next((s for s in reversed(stack) if s.kind in UNIT_KINDS), None)

    def container() -> str:
        names = [s.name for s in stack if s.kind in UNIT_KINDS | {"module", "submodule"}]
        return " > ".join(names)

    def open_unit(kind: str, name: str, statement: Statement, header: str = ""):
        proc = Procedure(name, kind, container(), path, statement.lines[0])
        proc.params = _count_params(header)
        found.procedures.append(proc)
        stack.append(_Scope(kind, name, proc))

    def close(kinds: set[str], statement: Statement):
        while stack:
            scope = stack.pop()
            if scope.unit is not None:
                scope.unit.end = statement.lines[-1]
            if scope.kind in kinds:
                return

    def in_interface() -> bool:
        return any(scope.kind == "interface" for scope in stack)

    line_owner: dict[int, Procedure | None] = {}  # (a line's code is counted once, for its innermost unit)
    for st in stmts:
        s = st.text
        decisions = len(LOGICAL_RE.findall(s))
        ended = END_RE.match(s)
        # Closing the do constructs that end at this statement's label ("do 10 …" / "10 continue").
        if st.label:
            scope = unit()
            while scope and scope.constructs and scope.constructs[-1] == ("do", st.label):
                scope.constructs.pop()
        if ended:
            word = (ended.group(1) or "").replace(" ", "")
            counted = unit()
            _count(counted, st, decisions, line_owner)
            if word in ("do", "if", "select", "where", "forall", "associate", "block", "critical", "team"):
                if counted and counted.constructs:
                    counted.constructs.pop()
            elif word in ("subroutine", "function", "procedure"):
                close({"subroutine", "function", "procedure", "body"}, st)
            elif word in ("program", "module", "submodule", "interface", "type", "enum"):
                close({word}, st)
            elif word == "blockdata":
                close({"blockdata"}, st)
            elif word == "":
                close(BARE_END, st)
            continue
        assignment = _is_assignment(s)
        if not assignment and _open_scope(s, st, stack, open_unit, in_interface()):
            _count(unit(), st, decisions, line_owner)
            continue
        scope = unit()
        if scope is not None and not assignment:
            decisions += _decisions(s, st, scope)
        _count(scope, st, decisions, line_owner)

    for scope in stack:  # (a file that ends without its "end"s)
        if scope.unit is not None and not scope.unit.end:
            scope.unit.end = stmts[-1].lines[-1] if stmts else scope.unit.start
    for owner in line_owner.values():
        if owner is not None:
            owner.nloc += 1
    found.nloc = len(line_owner)
    found.tokens = sum(st.tokens for st in stmts)
    if source.origins is not None:  # (the lines of the code as read: back to their own files')
        for proc in found.procedures:
            proc.path, start = source.origin(proc.start, path)
            end_path, end = source.origin(proc.end, path)
            proc.start, proc.end = start, end if end_path == proc.path else start
    return found


def _count(scope: _Scope | None, st: Statement, decisions: int, line_owner: dict):
    """A statement's tokens, lines and decisions, for the unit it's in."""
    proc = scope.unit if scope is not None else None
    for line in st.lines:
        line_owner[line] = proc
    if proc is not None:
        proc.tokens += st.tokens
        proc.ccn += decisions


def _open_scope(s: str, st: Statement, stack: list[_Scope], open_unit, in_interface: bool) -> bool:
    """A statement that opens a program unit, a procedure or another scope
    (pushed on ``stack``); False for any other statement."""
    if INTERFACE_RE.match(s):
        stack.append(_Scope("interface", ""))
        return True
    if in_interface:
        if PROCEDURE_RE.match(s) and not s.startswith("module procedure"):
            stack.append(_Scope("body", ""))  # (an interface body: a declaration, not a procedure)
            return True
        return False
    if TYPE_DEF_RE.match(s):
        stack.append(_Scope("type", ""))
        return True
    if ENUM_RE.match(s):
        stack.append(_Scope("enum", ""))
        return True
    if any(scope.kind in ("type", "enum") for scope in stack):
        return False  # (a type's procedure bindings are declarations)
    found = MODULE_PROCEDURE_RE.match(s)
    if found:  # (a submodule's separate module procedure)
        open_unit("procedure", found.group(1), st)
        return True
    found = PROCEDURE_RE.match(s)
    if found and not re.search(r"\bend\b", found.group(1) or ""):
        open_unit(found.group(2), found.group(3), st, found.group(4) or "")
        return True
    found = PROGRAM_RE.match(s)
    if found:
        open_unit("program", found.group(1), st)
        return True
    found = SUBMODULE_RE.match(s)
    if found:
        stack.append(_Scope("submodule", found.group(1)))
        return True
    found = MODULE_RE.match(s)
    if found:
        stack.append(_Scope("module", found.group(1)))
        return True
    if BLOCK_DATA_RE.match(s):
        stack.append(_Scope("blockdata", ""))
        return True
    return False


def _decisions(s: str, st: Statement, scope: _Scope) -> int:
    """A statement's decisions (beyond .and./.or.), and the block constructs
    it opens or closes (for the nesting depth)."""
    proc = scope.unit

    def opened(kind: str, label: str = ""):
        scope.constructs.append((kind, label))
        proc.depth = max(proc.depth, len(scope.constructs))

    if s.startswith("if") and re.match(r"^if\s*\(", s):
        after = s[_close_paren(s, s.index("(")):].strip()
        if after == "then":
            opened("if")
            return 1
        if re.match(r"^\d+\s*,\s*\d+\s*,\s*\d+$", after):
            return len(set(re.findall(r"\d+", after))) - 1  # (an arithmetic if: up to three ways)
        return 1 + (_decisions(after, st, scope) if after else 0)  # (a one-line if, and its statement)
    if re.match(r"^else\s*if\s*\(", s):
        return 1
    found = re.match(r"^go\s*to\s*\(([\d\s,]+)\)", s)
    if found:  # (a computed goto: to each label, or on: one way more than its labels)
        return len(set(re.findall(r"\d+", found.group(1))))
    found = DO_RE.match(s)
    if found:
        opened("do", found.group(1) or "")
        return 0 if s.strip() == "do" else 1  # (an endless "do" decides nothing: its exits' ifs do)
    if re.match(r"^(where|forall)\s*\(", s):
        rest = s[_close_paren(s, s.index("(")):].strip()
        if not rest:
            opened(s[:s.index("(")].strip())  # (a block: "end where" / "end forall" closes it)
        return 1
    if re.match(r"^else\s*where\s*\(", s):
        return 1
    if SELECT_RE.match(s):
        opened("select")
        return 0
    if GUARD_RE.match(s):
        return 1
    if re.match(r"^(associate\s*\(|block\s*$|critical\b|change\s+team\s*\()", s):
        opened(s.split("(")[0].strip())
    return 0


def _count_params(header: str) -> int:
    """The dummy arguments in a procedure statement's "(a, b, c) result(r)"."""
    if not header.startswith("("):
        return 0
    inside = header[1:_close_paren(header, 0) - 1].strip()
    return len([a for a in inside.split(",") if a.strip()]) if inside else 0


def analyze_file(path: Path) -> FileMetrics:
    """A file's metrics (an unreadable one: none)."""
    try:
        text = decode(path.read_bytes())[0]
    except OSError:
        return FileMetrics(path)
    return analyze_text(text, path)
