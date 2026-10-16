"""No console window flashing up on Windows when the app runs a program."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

from vibefpmgui import console

PACKAGE = Path(__file__).resolve().parents[1] / "vibefpmgui"
LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}
# (Calls that need no flag: POSIX or macOS only, a windowed program, or
# the test runner, whose programs share fpm's hidden console.)
EXEMPT = {("processes.py", "_descendants"), ("project_tree.py", "reveal_in_file_manager"), ("test_runner.py", "main")}


def launches(tree: ast.AST):
    """(function, call) for each subprocess.<launcher>(…) call."""
    for function in ast.walk(tree):
        if isinstance(function, ast.FunctionDef):
            for node in ast.walk(function):
                if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Name) and node.func.value.id == "subprocess"
                        and node.func.attr in LAUNCHERS):
                    yield function.name, node


def test_every_program_is_started_without_a_console_window():
    missing = []
    for path in sorted(PACKAGE.rglob("*.py")):
        for function, call in launches(ast.parse(path.read_text(encoding="utf-8"))):
            if (path.name, function) in EXEMPT:
                continue
            if not any(k.arg is None and isinstance(k.value, ast.Name) and k.value.id == "NO_WINDOW"
                       for k in call.keywords):
                missing.append(f"{path.relative_to(PACKAGE)}:{call.lineno} ({function})")
    assert missing == [], "subprocess calls without **NO_WINDOW: " + ", ".join(missing)


def test_another_window_starts_with_pythonw_on_windows(tmp_path, monkeypatch):
    assert console.gui_python() == sys.executable or sys.platform == "win32"
    python = tmp_path / "python.exe"
    python.write_text("")
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.setattr(sys, "platform", "win32")
    assert console.gui_python() == str(python)  # (no pythonw.exe beside it: python.exe)
    (tmp_path / "pythonw.exe").write_text("")
    assert console.gui_python() == str(tmp_path / "pythonw.exe")
