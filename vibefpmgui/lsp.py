"""A small client for the Fortran language server, fortls.

`fortls <https://fortls.fortran-lang.org>`_ indexes the project's Fortran
sources and answers questions about them: what a name is (hover), where it
is defined, the outline of a file and the symbols in the whole project. It
runs as a background process speaking the Language Server Protocol (JSON
messages over stdin/stdout); `LspClient` handles that with a QProcess,
so the editor never waits for it.

Open editor tabs send their text to the server (``didOpen``/``didChange``),
so unsaved edits are seen. Unless the project has its own fortls settings
(``.fortlsrc``, ``.fortls.json`` or ``.fortls``), fortls is told to index
just the fpm folders (``src``, ``app``, ``test``, ``example``, ``include`` and
any ``source-dir``) of the project and each downloaded dependency, rather
than every folder under the project (a Python environment, say).
"""

from __future__ import annotations

import html
import json
import re
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from qtpy.QtCore import QObject, QProcess, QUrl, Signal
from qtpy.QtGui import QPalette, QTextDocument

from . import manifest as m
from .project_tree import fpm_folders

FORTRAN_EXTS = {".f", ".for", ".ftn", ".f77", ".f90", ".f95", ".f03", ".f08", ".f18", ".f23", ".fpp"}
CONFIG_FILES = (".fortlsrc", ".fortls.json", ".fortls")

# LSP SymbolKind numbers -> words (the ones fortls uses for Fortran).
SYMBOL_KINDS = {2: "module", 5: "type", 6: "subroutine", 8: "component", 9: "constructor",
                10: "enum", 11: "interface", 12: "function", 13: "variable", 14: "constant",
                22: "enumerator", 23: "type", 26: "type parameter"}
OUTLINE_KINDS = {2, 5, 6, 9, 10, 11, 12, 23}  # what the outline lists (no variables)
# fortls's outline calls subroutines "functions" and programs "modules"; the
# keyword on the symbol's first line says which it is.
KEYWORD_RE = re.compile(r"^(?:[^!'\"]*?\s)?(program|submodule|module|subroutine|function)\b", re.IGNORECASE)


def executable() -> str | None:
    return shutil.which("fortls")


def is_fortran(path: Path) -> bool:
    return path.suffix.lower() in FORTRAN_EXTS


def to_uri(path: Path) -> str:
    return QUrl.fromLocalFile(str(path)).toString()


def from_uri(uri: str) -> Path:
    return Path(QUrl(uri).toLocalFile()).resolve()


def has_config(project_dir: Path) -> bool:
    return any((project_dir / name).is_file() for name in CONFIG_FILES)


def source_dirs(graph: m.DependencyGraph | None, project_dir: Path) -> list[str]:
    """``folder/**`` globs for the fpm folders of the project and of every
    dependency on disk (fortls doesn't look into subfolders otherwise)."""
    packages = [project_dir]
    if graph is not None:
        packages += [p.directory for p in graph.packages.values()
                     if p.name != graph.root and p.kind not in (m.META, m.MISSING)
                     and p.directory and p.directory.is_dir()]
    folders: set[str] = set()
    for directory in packages:
        manifest = directory / "fpm.toml"
        found = fpm_folders(manifest) if manifest.is_file() else {}
        folders.update(found or {str(directory.resolve()): ""})
    # A folder inside another listed one is already covered by its "/**".
    kept = [f for f in sorted(folders)
            if not any(other != f and Path(other) in Path(f).parents for other in folders)]
    return [f"{folder}/**" for folder in kept]


def _packages(graph: m.DependencyGraph | None, project_dir: Path) -> list[tuple[Path, str]]:
    """(folder, version) of the project and of every dependency on disk."""
    out = [(project_dir, "")]
    if graph is not None:
        root = graph.packages.get(graph.root)
        out[0] = (project_dir, (root.version if root else "") or "")
        out += [(p.directory, p.version or "") for p in graph.packages.values()
                if p.name != graph.root and p.kind not in (m.META, m.MISSING)
                and p.directory and p.directory.is_dir()]
    return out


def preprocessor_args(graph: m.DependencyGraph | None, project_dir: Path) -> list[str]:
    """fortls options for the C preprocessor settings in the fpm.toml files
    (``[preprocess.cpp]``: macros, suffixes) and the packages' include
    folders, so #ifdef branches are read as fpm compiles them. [] when no
    package has preprocessor settings (fortls's own defaults then)."""
    macros: dict[str, str] = {}
    suffixes: list[str] = []
    includes: list[str] = []
    found = False
    for directory, version in reversed(_packages(graph, project_dir)):  # (the project's own win)
        manifest = directory / "fpm.toml"
        try:
            data = m.load_toml(manifest) if manifest.is_file() else {}
        except Exception:  # noqa: BLE001 - an unreadable manifest has no settings
            data = {}
        cpp = (data.get("preprocess") or {}).get("cpp") if isinstance(data.get("preprocess"), dict) else None
        if not isinstance(cpp, dict):
            continue
        found = True
        for macro in cpp.get("macros") or []:
            name, _, value = str(macro).partition("=")
            if name.strip():
                macros[name.strip()] = value.strip().replace("{version}", version)
        for suffix in cpp.get("suffixes") or []:
            suffix = "." + str(suffix).lstrip(".")
            if suffix not in suffixes:
                suffixes.append(suffix)
        includes += [f for f, role in fpm_folders(manifest).items() if role == "include" and f not in includes]
    if not found:
        return []
    args = []
    if macros:
        args += ["--pp_defs", json.dumps(macros)]
    if suffixes:
        args += ["--pp_suffixes", *suffixes]
    if includes:
        args += ["--include_dirs", *includes]
    return args


def server_args(graph: m.DependencyGraph | None, project_dir: Path) -> list[str]:
    args = ["--disable_autoupdate",  # it would otherwise ask PyPI for updates
            "--hover_signature",
            "--use_signature_help",    # complete names only; arguments are hinted on "("
            "--lowercase_intrinsics"]  # complete "size", not "SIZE"
    if not has_config(project_dir):  # (a .fortlsrc says it all)
        args += ["--source_dirs", *source_dirs(graph, project_dir)]
        args += preprocessor_args(graph, project_dir)
    return args


# --------------------------------------------------------- diagnostics
class LspIssue(NamedTuple):
    """A problem fortls found in a file (it checks a file when it's opened
    or saved: the saved text), shown like fortitude's issues."""
    line: int        # 1-based
    column: int      # 1-based
    end_line: int
    end_column: int
    message: str
    level: int = 2   # LSP: 1 error, 2 warning, 3 information, 4 hint
    code: str = "fortls"
    fix: str = ""
    fix_safe: bool = False
    edits: tuple = ()
    source: str = "fortls"

    @property
    def fixable(self) -> bool:
        return False

    @property
    def severity(self) -> str:
        return "fortls error" if self.level == 1 else "fortls"

    def describe(self) -> str:
        return f"fortls: {self.message}"


def diagnostic_issues(diagnostics) -> list[LspIssue]:
    """publishDiagnostics' list -> issues (anything malformed is skipped)."""
    out = []
    for d in diagnostics or []:
        try:
            r = d["range"]
            out.append(LspIssue(r["start"]["line"] + 1, r["start"]["character"] + 1,
                                r["end"]["line"] + 1, r["end"]["character"] + 1,
                                " ".join(str(d.get("message", "")).split()), int(d.get("severity") or 2)))
        except (KeyError, TypeError, ValueError):
            continue
    return out


# fortls's hover text for an intrinsic is a manual page in markdown that
# starts with a rule and the name as a heading ("-----\n## sum\n\n### **Name**");
# for the project's own names it starts with a ```fortran block.
INTRINSIC_RE = re.compile(r"^\s*-{3,}[ \t]*\n\s*##[ \t]+(\w+)[ \t]*\n(?:\s*###[ \t]*\**Name\**[ \t]*\n)?")


def hover_text(contents) -> str:
    """The markdown text of a hover answer (MarkupContent, string or list)."""
    if isinstance(contents, dict):
        return contents.get("value", "")
    if isinstance(contents, list):
        return "\n\n".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in contents)
    return str(contents or "")


def _accent(dark: bool) -> str:
    """The highlight color, made lighter on a dark background so it reads."""
    from qtpy.QtWidgets import QApplication
    color = QApplication.palette().color(QPalette.Active, QPalette.Highlight)
    if dark:
        color = color.lighter(170)
    elif color.lightness() > 150:
        color = color.darker(140)
    return color.name()


def code_html(code: str, language: str = "fortran", style: str | None = None) -> str:
    """A block of code, syntax-highlighted with the editor's Pygments style,
    on a panel in that style's background color."""
    from pygments import highlight
    from pygments.formatters import HtmlFormatter
    from pygments.lexers import FortranLexer, TextLexer
    from pygments.styles import get_style_by_name

    style = style or _current_style()
    lexer = FortranLexer() if language.lower().startswith("fortran") or not language else TextLexer()
    spans = highlight(code.rstrip("\n"), lexer, HtmlFormatter(nowrap=True, noclasses=True, style=style))
    # Synopses aren't always valid Fortran ("[,dim] | [mask]"): no error boxes.
    spans = re.sub(r"(background-color|border):\s*[^;\"]+;?\s*", "", spans)
    background = get_style_by_name(style).background_color
    return (f"<table width='100%' cellspacing='0' cellpadding='6' style='margin:3px 0; "
            f"background-color:{background}'><tr><td><pre style='margin:0'>{spans.rstrip()}</pre>"
            "</td></tr></table>")


def _current_style() -> str:
    from qtpy.QtWidgets import QApplication

    from . import highlighter as hl
    return hl.style_name(QApplication.palette())


def markdown_html(text: str, style: str | None = None) -> str:
    """Markdown as HTML for a rich-text label (rendered by Qt): code blocks
    syntax-highlighted, headings as small colored labels."""
    out = []
    for i, part in enumerate(re.split(r"```([\w+-]*)[ \t]*\n(.*?)```", text, flags=re.DOTALL)):
        kind = i % 3
        if kind == 1:
            language = part
        elif kind == 2:
            out.append(code_html(part, language, style))
        elif part.strip():
            doc = QTextDocument()
            doc.setMarkdown(part, QTextDocument.MarkdownDialectGitHub)
            body = re.search(r"<body[^>]*>(.*)</body>", doc.toHtml(), re.DOTALL)
            out.append(body.group(1).strip() if body else "")
    result = "".join(out)
    from . import highlighter as hl
    accent = _accent(hl.style_is_dark(style or _current_style()))

    def heading(match):
        title = html.unescape(re.sub(r"<[^>]+>", "", match.group(2))).strip()
        return (f"<p style='margin:9px 0 2px 0'><span style='color:{accent}; font-weight:600; "
                f"font-size:small'>{html.escape(title.upper())}</span></p>")

    return re.sub(r"<h(\d)[^>]*>(.*?)</h\1>", heading, result, flags=re.DOTALL)


class HoverInfo(NamedTuple):
    kind: str        # "intrinsic", "function", "subroutine", "variable", "parameter", … or ""
    category: str    # an intrinsic's group, e.g. "ARRAY · REDUCTION"
    summary: str     # an intrinsic's one-line description
    body_html: str   # the rest, rendered


def _clean(text: str) -> str:
    # "-----" between the declaration and a doc comment: just a gap (the code
    # panel already sets them apart; markdown would make the line above a heading).
    text = re.sub(r"^[ \t]*-{3,}[ \t]*$", "\n", text, flags=re.MULTILINE)
    # Qt shows _word_ underlined (like a link); the manual pages mean italics.
    parts = re.split(r"(```.*?```|`[^`]*`)", text, flags=re.DOTALL)
    return "".join(part if i % 2 else re.sub(r"(?<![\w\\*])_([^_\n]+?)_(?!\w)", r"*\1*", part)
                   for i, part in enumerate(parts))


DECL_KINDS = [("subroutine", r"\bSUBROUTINE\b"), ("function", r"\bFUNCTION\b"),
              ("program", r"^\s*PROGRAM\b"), ("submodule", r"^\s*SUBMODULE\b"),
              ("module", r"^\s*MODULE\b(?!\s+PROCEDURE)"), ("interface", r"\bINTERFACE\b"),
              ("type", r"^\s*TYPE\b(?!\s*\()")]


def hover_info(contents, style: str | None = None) -> HoverInfo | None:
    """fortls's hover answer, taken apart for the hover card (None if empty)."""
    text = hover_text(contents)
    if not text.strip():
        return None
    match = INTRINSIC_RE.match(text)
    if match:
        rest = text[match.end():]
        category = summary = ""
        line = re.match(r"\s*\*\*\w+\*\*\s*-\s*(?:\\?\[([^\]\\]*)\\?\])?\s*(.*)\n", rest)
        if line:
            category = " · ".join(p.strip() for p in (line.group(1) or "").split(":") if p.strip())
            summary = line.group(2).strip()
            rest = rest[line.end():]
        return HoverInfo("intrinsic", category, summary, markdown_html(_clean(rest), style))
    kind = ""
    code = re.match(r"\s*```[\w+-]*[ \t]*\n(.*?)```", text, re.DOTALL)
    if code:
        first = code.group(1).strip().splitlines()[0] if code.group(1).strip() else ""
        if "::" in first and not re.match(r"^\s*TYPE\b\s*(,|::)", first, re.IGNORECASE):
            kind = "parameter" if re.search(r"\bPARAMETER\b", first, re.IGNORECASE) else "variable"
        else:
            kind = next((k for k, pattern in DECL_KINDS if re.search(pattern, first, re.IGNORECASE)), "")
            if not kind and re.match(r"^\s*TYPE\b", first, re.IGNORECASE):
                kind = "type"
    return HoverInfo(kind, "", "", markdown_html(_clean(text), style))


def hover_html(contents) -> str:
    """fortls's hover text (markdown) as HTML, e.g. for an argument's
    documentation in the argument hints; "" if there is nothing."""
    info = hover_info(contents)
    if info is None:
        return ""
    lead = f"<p style='margin:0 0 4px 0'>{html.escape(info.summary)}</p>" if info.summary else ""
    return lead + info.body_html


def first_location(result) -> tuple[Path, int, int] | None:
    """(file, 1-based line, 0-based column) from a definition answer
    (a Location, a list of them, or LocationLinks)."""
    if isinstance(result, list):
        result = result[0] if result else None
    if not isinstance(result, dict):
        return None
    uri = result.get("uri") or result.get("targetUri")
    rng = result.get("targetSelectionRange") or result.get("range") or result.get("targetRange")
    if not uri or not rng:
        return None
    return from_uri(uri), rng["start"]["line"] + 1, rng["start"]["character"]


class LspClient(QObject):
    """One fortls process for the open project."""

    stopped = Signal(str)  # the server exited on its own (with why)
    message = Signal(str)  # something the server wants the user to know
    diagnostics = Signal(object, object)  # file (Path), [LspIssue]: fortls's check of a file

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process: QProcess | None = None
        self.root: Path | None = None
        self.args: list[str] = []
        self._buffer = b""
        self._next_id = 0
        self._callbacks: dict[int, Callable] = {}
        self._ready = False
        self._queue: list[dict] = []        # sent once initialize has answered
        self._versions: dict[str, int] = {}  # open document uri -> version

    # ---------------------------------------------------------- lifecycle
    def is_running(self) -> bool:
        return self.process is not None

    def start(self, root: Path, args: list[str]) -> bool:
        self.stop()
        program = executable()
        if program is None:
            return False
        self.root, self.args = root, list(args)
        process = QProcess(self)
        process.setWorkingDirectory(str(root))
        process.setStandardErrorFile(QProcess.nullDevice())
        process.readyReadStandardOutput.connect(self._read)
        # (Methods, not lambdas holding the process: when Qt deletes the
        # process, PySide drops those lambdas, and with them possibly the last
        # reference to the process's Python wrapper, mid-delete: a crash.)
        process.finished.connect(self._process_finished)
        process.errorOccurred.connect(self._process_error)
        self.process = process
        process.start(program, self.args)
        self._send({"jsonrpc": "2.0", "id": self._new_id(self._initialized), "method": "initialize",
                    "params": {"processId": None, "rootUri": to_uri(root), "capabilities": {}}})
        return True

    def stop(self):
        process, self.process = self.process, None
        self._reset()
        if process is not None:
            process.kill()
            process.waitForFinished(1000)
            process.deleteLater()

    def _reset(self):
        self._buffer = b""
        self._callbacks.clear()
        self._queue.clear()
        self._versions.clear()
        self._ready = False

    def _process_finished(self, *_):
        if isinstance(self.sender(), QProcess):
            self._finished(self.sender())

    def _process_error(self, error):
        if error == QProcess.FailedToStart and isinstance(self.sender(), QProcess):
            self._finished(self.sender())

    def _finished(self, process: QProcess):
        if process is not self.process:
            return  # one we stopped
        self.process = None
        self._reset()
        try:
            process.deleteLater()
        except RuntimeError:
            return  # the app is shutting down (Qt has already deleted it)
        self.stopped.emit("fortls could not be started" if process.error() == QProcess.FailedToStart
                          else f"fortls stopped (exit code {process.exitCode()})")

    def _initialized(self, _result):
        self._ready = True
        self._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        queue, self._queue = self._queue, []
        for message in queue:
            self._send(message)

    # ---------------------------------------------------------- messages
    def _new_id(self, callback: Callable) -> int:
        self._next_id += 1
        self._callbacks[self._next_id] = callback
        return self._next_id

    def _send(self, message: dict):
        if self.process is None:
            return
        body = json.dumps(message).encode("utf-8")
        self.process.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)

    def _post(self, message: dict):
        if self._ready:
            self._send(message)
        else:
            self._queue.append(message)

    def request(self, method: str, params: dict, callback: Callable) -> bool:
        """Ask the server something; ``callback(result)`` gets the answer
        (None on an error). False if no server is running."""
        if self.process is None:
            return False
        self._post({"jsonrpc": "2.0", "id": self._new_id(callback), "method": method, "params": params})
        return True

    def notify(self, method: str, params: dict):
        if self.process is not None:
            self._post({"jsonrpc": "2.0", "method": method, "params": params})

    def _read(self):
        if self.process is None:
            return
        self._buffer += bytes(self.process.readAllStandardOutput())
        while True:
            end = self._buffer.find(b"\r\n\r\n")
            if end < 0:
                return
            match = re.search(rb"Content-Length:\s*(\d+)", self._buffer[:end], re.IGNORECASE)
            if not match:  # not a header: skip it
                self._buffer = self._buffer[end + 4:]
                continue
            length = int(match.group(1))
            if len(self._buffer) < end + 4 + length:
                return
            body, self._buffer = self._buffer[end + 4:end + 4 + length], self._buffer[end + 4 + length:]
            try:
                message = json.loads(body)
            except ValueError:
                continue
            self._dispatch(message)

    def _dispatch(self, message: dict):
        if "method" in message:
            if "id" in message:  # a request from the server: we don't support any
                self._send({"jsonrpc": "2.0", "id": message["id"], "result": None})
            elif message["method"] == "window/showMessage":  # e.g. why a rename failed
                text = (message.get("params") or {}).get("message")
                if text:
                    self.message.emit(str(text))
            elif message["method"] == "textDocument/publishDiagnostics":
                params = message.get("params") or {}
                if params.get("uri"):
                    self.diagnostics.emit(from_uri(params["uri"]), diagnostic_issues(params.get("diagnostics")))
            return  # other notifications (logs) aren't used
        callback = self._callbacks.pop(message.get("id"), None)
        if callback is not None:
            try:
                callback(message.get("result") if "error" not in message else None)
            except RuntimeError:
                pass  # the editor that asked has been closed

    # ---------------------------------------------------------- documents
    def open_document(self, path: Path, text: str):
        uri = to_uri(path)
        if self.process is None:
            return
        if uri in self._versions:
            self.change_document(path, text)
            return
        self._versions[uri] = 1
        self.notify("textDocument/didOpen", {"textDocument": {
            "uri": uri, "languageId": "fortran", "version": 1, "text": text}})
        # fortls reads the file from disk on didOpen; send the editor's text
        # too, in case it has unsaved changes.
        self.change_document(path, text)

    def change_document(self, path: Path, text: str):
        uri = to_uri(path)
        if uri not in self._versions:
            self.open_document(path, text)
            return
        self._versions[uri] += 1
        self.notify("textDocument/didChange", {
            "textDocument": {"uri": uri, "version": self._versions[uri]},
            "contentChanges": [{"text": text}]})

    def save_document(self, path: Path):
        self.notify("textDocument/didSave", {"textDocument": {"uri": to_uri(path)}})

    def file_changed_on_disk(self, path: Path):
        """A file that isn't open was rewritten (e.g. by Rename): fortls
        re-reads a file from disk when told it was saved."""
        if to_uri(path) not in self._versions:
            self.save_document(path)

    def close_document(self, path: Path):
        uri = to_uri(path)
        if self._versions.pop(uri, None) is not None:
            self.notify("textDocument/didClose", {"textDocument": {"uri": uri}})

    # ---------------------------------------------------------- questions
    @staticmethod
    def _at(path: Path, line: int, column: int) -> dict:
        """Position parameters; ``line`` is 1-based, ``column`` 0-based."""
        return {"textDocument": {"uri": to_uri(path)},
                "position": {"line": line - 1, "character": column}}

    def hover(self, path: Path, line: int, column: int, callback: Callable) -> bool:
        return self.request("textDocument/hover", self._at(path, line, column), callback)

    def definition(self, path: Path, line: int, column: int, callback: Callable) -> bool:
        return self.request("textDocument/definition", self._at(path, line, column), callback)

    def implementation(self, path: Path, line: int, column: int, callback: Callable) -> bool:
        """For a type-bound procedure, the procedure it binds; for a generic
        interface, its first module procedure."""
        return self.request("textDocument/implementation", self._at(path, line, column), callback)

    def document_symbols(self, path: Path, callback: Callable) -> bool:
        return self.request("textDocument/documentSymbol",
                            {"textDocument": {"uri": to_uri(path)}}, callback)

    def workspace_symbols(self, query: str, callback: Callable) -> bool:
        return self.request("workspace/symbol", {"query": query}, callback)

    def completion(self, path: Path, line: int, column: int, callback: Callable) -> bool:
        return self.request("textDocument/completion", self._at(path, line, column), callback)

    def signature_help(self, path: Path, line: int, column: int, callback: Callable) -> bool:
        return self.request("textDocument/signatureHelp", self._at(path, line, column), callback)

    def references(self, path: Path, line: int, column: int, callback: Callable) -> bool:
        return self.request("textDocument/references",
                            {**self._at(path, line, column), "context": {"includeDeclaration": True}},
                            callback)

    def rename(self, path: Path, line: int, column: int, new_name: str, callback: Callable) -> bool:
        return self.request("textDocument/rename",
                            {**self._at(path, line, column), "newName": new_name}, callback)


def outline(symbols, line_text: Callable[[int], str] | None = None) -> list[tuple[int, str, str, int, int]]:
    """documentSymbol answer -> [(depth, kind word, name, first line, last
    line)] for modules, types and procedures, in file order (lines 1-based).
    ``line_text(line)`` (the file's text) refines the kind words."""
    rows = []

    def kind_word(s, first):
        word = SYMBOL_KINDS.get(s["kind"], "")
        if line_text is not None and s["kind"] in (2, 6, 12):
            match = KEYWORD_RE.match(line_text(first).strip())
            if match:
                word = match.group(1).lower()
        return word

    def visit(items, depth, container_depth):
        for s in items or []:
            if not isinstance(s, dict) or s.get("kind") not in OUTLINE_KINDS:
                continue
            rng = s.get("range") or s.get("location", {}).get("range")
            if not rng:
                continue
            level = depth if "children" in s else container_depth.get(s.get("containerName"), -1) + 1
            rows.append((level, kind_word(s, rng["start"]["line"] + 1), s.get("name", ""),
                         rng["start"]["line"] + 1, rng["end"]["line"] + 1))
            container_depth[s.get("name")] = level
            visit(s.get("children"), depth + 1, container_depth)

    visit(symbols, 0, {})
    rows.sort(key=lambda r: (r[3], r[0]))
    return rows


# ------------------------------------------------------------- completion
# LSP CompletionItemKind numbers -> words.
COMPLETION_KINDS = {1: "text", 2: "method", 3: "function", 4: "constructor", 5: "component",
                    6: "variable", 7: "type", 8: "interface", 9: "module", 10: "property",
                    13: "enum", 14: "keyword", 15: "snippet", 20: "enumerator", 21: "constant",
                    22: "type", 25: "type parameter"}


class Completion(NamedTuple):
    label: str
    insert: str   # the text to insert (snippet placeholders removed)
    kind: str
    detail: str
    doc: str      # markdown (fortls: the declaration and doc comment)


def snippet_text(text: str) -> str:
    """A snippet ("foo(${1:x}, $2)$0") as plain text ("foo(x, )")."""
    text = re.sub(r"\$\{\d+:([^}]*)\}", r"\1", text)
    text = re.sub(r"\$\{\d+\}|\$\d+", "", text)
    return text.replace("\\$", "$").replace("\\}", "}")


def completions(result) -> list[Completion]:
    items = result.get("items", []) if isinstance(result, dict) else result or []
    out = []
    for item in items:
        if not isinstance(item, dict) or not item.get("label"):
            continue
        insert = item.get("insertText") or item.get("label")
        edit = item.get("textEdit")
        if isinstance(edit, dict) and edit.get("newText"):
            insert = edit["newText"]
        if item.get("insertTextFormat") == 2:
            insert = snippet_text(insert)
        doc = item.get("documentation") or ""
        if isinstance(doc, dict):
            doc = doc.get("value", "")
        out.append(Completion(str(item["label"]), insert, COMPLETION_KINDS.get(item.get("kind"), ""),
                              str(item.get("detail") or ""), str(doc)))
    return out


def in_comment_or_string(line_text: str, column: int) -> bool:
    """Is ``column`` of a line inside a "!" comment or a quoted string?"""
    quote = None
    for ch in line_text[:column]:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "!":
            return True
    return quote is not None


# --------------------------------------------------------- signature help
def signature_html(result, active_hint: int | None = None) -> str:
    """signatureHelp answer -> tooltip HTML: the call with the current
    argument in bold, then that argument's declaration and doc comment."""
    if not isinstance(result, dict) or not result.get("signatures"):
        return ""
    signatures = result["signatures"]
    sig = signatures[min(int(result.get("activeSignature") or 0), len(signatures) - 1)]
    params = sig.get("parameters") or []
    active = sig.get("activeParameter", result.get("activeParameter"))
    if active is None:
        active = active_hint
    label = str(sig.get("label", ""))
    shown = html.escape(label)
    doc = ""
    if active is not None and 0 <= int(active) < len(params):
        param = params[int(active)]
        name = param.get("label")
        if isinstance(name, list) and len(name) == 2:  # [start, end] offsets
            start, end = name
            shown = (html.escape(label[:start]) + "<b><u>" + html.escape(label[start:end]) + "</u></b>"
                     + html.escape(label[end:]))
        elif isinstance(name, str):
            # The argument's name inside the parentheses (not the routine's name).
            open_paren = label.find("(")
            match = re.search(rf"\b{re.escape(name)}\b", label[open_paren + 1:]) if open_paren >= 0 else None
            if match:
                start, end = open_paren + 1 + match.start(), open_paren + 1 + match.end()
                shown = (html.escape(label[:start]) + "<b><u>" + html.escape(label[start:end]) + "</u></b>"
                         + html.escape(label[end:]))
        doc = hover_html(param.get("documentation") or "")
    return f"<pre style='margin:0'>{shown}</pre>" + doc


def argument_index(text_before: str) -> int | None:
    """Which argument (0-based) the cursor is in, from the text before it
    on the line: the commas at the level of the innermost open "(" (None if
    the cursor isn't inside parentheses)."""
    depth, commas, quote = 0, 0, None
    for ch in reversed(text_before):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
        elif ch in ")]":
            depth += 1
        elif ch in "([":
            if depth == 0:
                return commas if ch == "(" else None
            depth -= 1
        elif ch == "," and depth == 0:
            commas += 1
    return None


# ------------------------------------------------------- references / rename
class TextEdit(NamedTuple):
    line: int        # 0-based, as in LSP
    column: int
    end_line: int
    end_column: int
    new_text: str


def locations(result) -> list[tuple[Path, int, int, int]]:
    """references answer -> [(file, 1-based line, start column, end column)]."""
    out = []
    for loc in result or []:
        try:
            rng = loc["range"]
            out.append((from_uri(loc["uri"]), rng["start"]["line"] + 1, rng["start"]["character"],
                        rng["end"]["character"] if rng["end"]["line"] == rng["start"]["line"]
                        else rng["start"]["character"]))
        except (KeyError, TypeError):
            continue
    return sorted(set(out), key=lambda r: (str(r[0]), r[1], r[2]))


def workspace_edits(result) -> dict[Path, list[TextEdit]]:
    """rename answer (a WorkspaceEdit) -> {file: [edits]}."""
    if not isinstance(result, dict):
        return {}
    pairs = list((result.get("changes") or {}).items())
    for change in result.get("documentChanges") or []:
        if isinstance(change, dict) and "textDocument" in change:
            pairs.append((change["textDocument"]["uri"], change.get("edits", [])))
    out: dict[Path, list[TextEdit]] = {}
    for uri, edits in pairs:
        for e in edits or []:
            try:
                r = e["range"]
                out.setdefault(from_uri(uri), []).append(TextEdit(
                    r["start"]["line"], r["start"]["character"], r["end"]["line"], r["end"]["character"],
                    e["newText"]))
            except (KeyError, TypeError):
                continue
    return out


def apply_edits(text: str, edits: list[TextEdit]) -> str:
    """Apply LSP text edits to a text ("\\n" line ends), last first so the
    earlier positions stay valid."""
    lines = text.split("\n")
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)

    def offset(line: int, column: int) -> int:
        line = min(max(line, 0), len(lines) - 1)
        return starts[line] + min(column, len(lines[line]))

    for e in sorted(edits, key=lambda e: (e.line, e.column), reverse=True):
        text = text[:offset(e.line, e.column)] + e.new_text + text[offset(e.end_line, e.end_column):]
    return text


def _code_spans(line: str) -> list[tuple[int, int]]:
    """The parts of a line that are code: not in a string or a "!" comment."""
    spans, start, quote = [], 0, None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote, start = None, i + 1
        elif ch in "'\"":
            spans.append((start, i))
            quote = ch
        elif ch == "!":
            spans.append((start, i))
            return spans
    if quote is None:
        spans.append((start, len(line)))
    return spans


def add_result_variable_edits(edits: dict[Path, list[TextEdit]], old: str, new: str,
                              file_text: Callable[[Path], str]) -> dict[Path, list[TextEdit]]:
    """fortls's rename misses a function's result variable: in a function
    without a ``result(...)`` clause, ``old = ...`` inside the body also
    names the function and must be renamed too. Adds those edits."""
    header = re.compile(rf"\bfunction\s+{re.escape(old)}\s*\(", re.IGNORECASE)
    word = re.compile(rf"\b{re.escape(old)}\b", re.IGNORECASE)
    end = re.compile(r"^\s*(end\s*(function\b|$)|contains\b)", re.IGNORECASE)
    out = {}
    for path, file_edits in edits.items():
        try:
            lines = file_text(path).split("\n")
        except OSError:
            out[path] = file_edits
            continue
        have = {(e.line, e.column) for e in file_edits}
        added = list(file_edits)
        for i, line in enumerate(lines):
            code = "".join(line[a:b] for a, b in _code_spans(line))
            if not header.search(code):
                continue
            # The whole statement (it may continue over "&" lines).
            statement, j = code, i
            while statement.rstrip().endswith("&") and j + 1 < len(lines):
                j += 1
                statement += "".join(lines[j][a:b] for a, b in _code_spans(lines[j]))
            if re.search(r"\bresult\s*\(", statement, re.IGNORECASE):
                continue  # the result has its own name
            for k in range(j + 1, len(lines)):
                if end.match(lines[k]):
                    break
                for a, b in _code_spans(lines[k]):
                    for match in word.finditer(lines[k], a, b):
                        if (k, match.start()) not in have:
                            have.add((k, match.start()))
                            added.append(TextEdit(k, match.start(), k, match.end(), new))
        out[path] = added
    return out
