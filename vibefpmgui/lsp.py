"""A small client for the Fortran language server, fortls.

`fortls <https://fortls.fortran-lang.org>`_ indexes the project's Fortran
sources and answers questions about them: what a name is (hover), where it
is defined, the outline of a file and the symbols in the whole project. It
runs as a background process speaking the Language Server Protocol (JSON
messages over stdin/stdout); :class:`LspClient` handles that with a QProcess,
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

from qtpy.QtCore import QObject, QProcess, QUrl, Signal

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


def server_args(graph: m.DependencyGraph | None, project_dir: Path) -> list[str]:
    args = ["--disable_autoupdate",  # it would otherwise ask PyPI for updates
            "--hover_signature"]
    if not has_config(project_dir):
        args += ["--source_dirs", *source_dirs(graph, project_dir)]
    return args


def hover_html(contents) -> str:
    """fortls's hover answer (markdown: a ```fortran block, maybe followed by
    "-----" and a doc comment) as tooltip HTML; "" if there is nothing."""
    if isinstance(contents, dict):
        text = contents.get("value", "")
    elif isinstance(contents, list):
        text = "\n\n".join(c.get("value", "") if isinstance(c, dict) else str(c) for c in contents)
    else:
        text = str(contents or "")
    out = []
    for i, part in enumerate(re.split(r"```[\w+-]*\n?", text)):
        if not part.strip():
            continue
        if i % 2:  # inside a code fence
            out.append(f"<pre style='margin:0'>{html.escape(part.rstrip())}</pre>")
        else:
            for chunk in re.split(r"^\s*-{3,}\s*$", part.strip(), flags=re.MULTILINE):
                if chunk.strip():
                    prose = html.escape(chunk.strip()).replace("\n", "<br>")
                    prose = re.sub(r"`([^`]+)`", r"<code>\1</code>", prose)  # inline code
                    out.append(f"<p style='margin:4px 0 0 0'>{prose}</p>")
    return "".join(out)


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
        self._stopping = False

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
        process.finished.connect(lambda *_: self._finished(process))
        process.errorOccurred.connect(lambda err: err == QProcess.FailedToStart and
                                      self._finished(process))
        self.process = process
        self._stopping = False
        process.start(program, self.args)
        self._send({"jsonrpc": "2.0", "id": self._new_id(self._initialized), "method": "initialize",
                    "params": {"processId": None, "rootUri": to_uri(root), "capabilities": {}}})
        return True

    def stop(self):
        process, self.process = self.process, None
        self._reset()
        if process is not None:
            self._stopping = True
            process.kill()
            process.waitForFinished(1000)
            process.deleteLater()

    def _reset(self):
        self._buffer = b""
        self._callbacks.clear()
        self._queue.clear()
        self._versions.clear()
        self._ready = False

    def _finished(self, process: QProcess):
        if process is not self.process:
            return  # one we stopped
        self.process = None
        self._reset()
        process.deleteLater()
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
            return  # notifications (diagnostics, logs) aren't used
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
        if to_uri(path) in self._versions:
            self.notify("textDocument/didSave", {"textDocument": {"uri": to_uri(path)}})

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

    def document_symbols(self, path: Path, callback: Callable) -> bool:
        return self.request("textDocument/documentSymbol",
                            {"textDocument": {"uri": to_uri(path)}}, callback)

    def workspace_symbols(self, query: str, callback: Callable) -> bool:
        return self.request("workspace/symbol", {"query": query}, callback)


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
