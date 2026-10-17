"""A project's GUI options, in its build configuration file's "options"
(see `vibefpmgui.build_configs`): the View menu's choices, the panels'
layout, the editor's tabs and the files open in them… everything the app
keeps in its settings that's about how the project is worked on. (New
configuration's Build options are in the file too, but as a configuration:
see `vibefpmgui.window.options`.)

Not in it (kept in the app's settings, for this computer): the window's size
and place, the recent lists and the last project, the command palette's
recent commands, the spelling dictionary, the AI choices, the fpm executable,
which configuration file a project uses, and what the app has learned (tests'
durations, which build folders a build wrote).

`LayeredSettings` stands in for the app's QSettings: while a project's
options are in use (``options`` not None), an option is read from them (the
app's value if they don't have it yet) and written to them; anything else,
and every option while there are none, is the app's. An option the app
keeps per project (`PER_PROJECT`) is asked for by its name; the app's key
for it is the project's (see ``use``). In the file, the open
files and the Project files tree's folders inside the project are relative
to it (so it works wherever the project is).
"""

from __future__ import annotations

import json
from pathlib import Path

from qtpy.QtCore import QByteArray

BYTES, JSON = "bytes", "json"  # (kinds besides bool, int, float, str and list)

# Each option, by its settings key, and the kind of value it has.
OPTIONS: dict[str, object] = {
    # View
    "app_style": str, "app_theme": str, "syntax_style": str, "font_delta": int,
    "editor_font_family": str, "editor_font_size": float, "use_editor": bool, "lint": bool,
    "lsp_diagnostics": bool, "spelling": bool, "spelling_language": str, "gremlins": bool, "minimap": bool,
    "show_coverage": bool, "highlight_matches": bool, "word_wrap": bool, "indent_width": int,
    "indent_tabs": bool, "fortls": bool, "trim_on_save": bool,
    # The editor's tabs, the panels
    "tab_rows_wrap": bool, "panels": BYTES, "panels_front": list, "session": JSON,
    # Graph
    "graph_layout": str, "graph_theme": str, "graph_edges": str, "graph_dependencies_dev": bool,
    # Panels' own choices
    "complexity_tests": bool, "complexity_dependencies": bool, "coverage_tests": bool,
    "search/case": bool, "search/word": bool, "search/regex": bool, "search/dependencies": bool,
    "search/ignored": bool, "search/include": str, "search/exclude": str,
    # The configuration chosen
    "build_configuration": str,
}
PREFIXES: dict[str, object] = {"problems/": bool}  # (one key per source of problems)
# Options the app keeps per project (its settings key: "<name>/<hash of the project>").
PER_PROJECT = ("session", "build_configuration")


def kind_of(key: str):
    """The option's kind (None: not an option, always the app's)."""
    if key in OPTIONS:
        return OPTIONS[key]
    return next((kind for prefix, kind in PREFIXES.items() if key.startswith(prefix)), None)


def _truthy(value) -> bool:
    return value in (True, 1, "true", "1") if not isinstance(value, bool) else value


def to_file(value, kind):
    """A value as the file has it (JSON's types)."""
    if kind == BYTES:
        return bytes(QByteArray(value).toBase64()).decode("ascii") if value else ""
    if kind == JSON:
        return json.loads(value) if isinstance(value, str) and value else (value or {})
    if kind is bool:
        return _truthy(value)
    if kind is int:
        return int(value)
    if kind is float:
        return float(value)
    if kind is list:
        return [str(v) for v in value] if isinstance(value, (list, tuple)) else ([str(value)] if value else [])
    return "" if value is None else str(value)


def from_file(value, kind, default):
    """A value from the file, as the app's settings would give it (``default``
    if it's of the wrong type)."""
    if kind == BYTES:
        return QByteArray.fromBase64(value.encode("ascii")) if isinstance(value, str) and value else default
    if kind == JSON:
        return json.dumps(value) if isinstance(value, dict) else default
    if kind is bool:
        return value if isinstance(value, bool) else default
    if kind is int:
        return value if isinstance(value, int) and not isinstance(value, bool) else default
    if kind is float:
        return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else default
    if kind is list:
        return list(value) if isinstance(value, list) else default
    return value if isinstance(value, str) else default


def _relative(path: str, root: Path) -> str:
    try:
        return Path(path).relative_to(root).as_posix() or "."
    except ValueError:
        return path


def _absolute(path: str, root: Path) -> str:
    return path if Path(path).is_absolute() else str(root / path)


def _session_paths(state: dict, root: Path, change) -> dict:
    """The session's paths (open files, the one in front, the tree's open
    folders and selection) changed with ``change`` (to relative or absolute)."""
    state = json.loads(json.dumps(state))  # (a copy)
    for f in state.get("files") or []:
        if isinstance(f, dict) and isinstance(f.get("path"), str):
            f["path"] = change(f["path"], root)
    if isinstance(state.get("current"), str):
        state["current"] = change(state["current"], root)
    tree = state.get("tree")
    if isinstance(tree, dict):
        tree["expanded"] = [p if not isinstance(p, str) or p.startswith("group:") else change(p, root)
                            for p in tree.get("expanded") or []]
        if isinstance(tree.get("selected"), str):
            tree["selected"] = change(tree["selected"], root)
    return state


class LayeredSettings:
    """The app's settings (``app``), with a project's options over them while
    ``options`` isn't None (see the module's description)."""

    def __init__(self, app):
        self.app = app
        self.options: dict | None = None
        self.root: Path | None = None  # the project's folder (the session's paths are relative to it)
        self.project_keys: dict[str, str] = {}  # the app's key of each PER_PROJECT option, for this project

    def use(self, options: dict | None, root: Path | None = None, project_keys: dict[str, str] | None = None):
        """Use a project's options from now on (None: the app's settings
        only); ``project_keys``: the app's key of each PER_PROJECT option for
        the project (none: there's no project)."""
        self.options = None if options is None else dict(options)
        self.root = root
        self.project_keys = dict(project_keys or {})

    def _app_key(self, key: str) -> str | None:
        """The app's key for ``key`` (None: a per-project one, with no project)."""
        return self.project_keys.get(key) if key in PER_PROJECT else key

    def _in_options(self, key: str) -> bool:
        return self.options is not None and kind_of(key) is not None

    def value(self, key: str, default=None, type=None):
        kind = kind_of(key)
        if self._in_options(key) and key in self.options:
            value = self.options[key]
            if kind == JSON and isinstance(value, dict) and self.root is not None:
                value = _session_paths(value, self.root, _absolute)
            fallback = default if default is not None else self._default(kind)
            return from_file(value, kind, fallback)
        source = self._app_key(key)
        if source is None:
            return default
        return self.app.value(source, default) if type is None else self.app.value(source, default, type=type)

    def setValue(self, key: str, value):
        if self._in_options(key):
            kind = kind_of(key)
            value = to_file(value, kind)
            if kind == JSON and isinstance(value, dict) and self.root is not None:
                value = _session_paths(value, self.root, _relative)
            self.options[key] = value
        elif (source := self._app_key(key)) is not None:
            self.app.setValue(source, value)

    def contains(self, key: str) -> bool:
        if self._in_options(key) and key in self.options:
            return True
        source = self._app_key(key)
        return source is not None and self.app.contains(source)

    def remove(self, key: str):
        if self._in_options(key):
            self.options.pop(key, None)
        elif (source := self._app_key(key)) is not None:
            self.app.remove(source)

    @staticmethod
    def _default(kind):
        return {bool: False, int: 0, float: 0.0, list: [], str: ""}.get(kind)

    def complete_options(self) -> dict:
        """The options to write: those set, and the app's values for the
        others it has (so the file has them all)."""
        out = {}
        prefixed = {k for k in [*self.app.allKeys(), *(self.options or {})] if any(k.startswith(p) for p in PREFIXES)}
        names = set(OPTIONS) | prefixed
        for name in sorted(names):
            if name in (self.options or {}):
                out[name] = self.options[name]
                continue
            source = self._app_key(name)
            if source is None or not self.app.contains(source):
                continue
            kind = kind_of(name)
            raw = self.app.value(source)
            try:
                value = to_file(raw if kind is not bool else _truthy(raw), kind)
            except (TypeError, ValueError):
                continue
            if kind == JSON and isinstance(value, dict) and self.root is not None:
                value = _session_paths(value, self.root, _relative)
            out[name] = value
        return out

    def __getattr__(self, name):
        return getattr(self.app, name)
