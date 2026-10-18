"""Which debugger (DAP adapter) to use, per platform (see plan/debugger.md):

- **Linux:** gdb ≥ 14 (``gdb --interpreter=dap``): it knows Fortran (arrays
  with their bounds, derived types, module variables). Else lldb-dap.
- **macOS:** lldb-dap: Apple's (``xcrun -f lldb-dap``, its signed debug
  server: no code-signing prompts), else one on the PATH. (gdb doesn't run on
  Apple Silicon; lldb shows no variables of gfortran's programs.)
- **Windows:** gdb ≥ 14 on the PATH, else lldb-dap.

Finding them runs programs (``gdb --version``): done once, then remembered.
"""

from __future__ import annotations

import re
import shutil
import sys
from dataclasses import dataclass

from . import console

GDB_MINIMUM = 14  # the first gdb with DAP


@dataclass(frozen=True)
class Adapter:
    kind: str  # "gdb" or "lldb"
    command: list[str]  # the adapter: its program and arguments
    version: str = ""

    @property
    def title(self) -> str:
        """As shown (the Debug panel's header): "gdb 18.1"."""
        return f"{self.kind} {self.version}".strip()

    def launch_arguments(self, program: str, args: list[str], cwd: str) -> dict:
        """The ``launch`` request's arguments, as this adapter names them."""
        arguments = {"program": program, "args": list(args), "cwd": cwd}
        if self.kind == "lldb":
            arguments["stopOnEntry"] = False
        return arguments


def _gdb_version(gdb: str) -> tuple[int, int] | None:
    code, output, _err = console.run([gdb, "--version"], timeout=10)
    if code != 0 or not output:
        return None
    match = re.search(rb"GNU gdb[^\n]*?(\d+)\.(\d+)", output)
    return (int(match.group(1)), int(match.group(2))) if match else None


def gdb_adapter() -> Adapter | None:
    """gdb on the PATH, if it's new enough to speak DAP."""
    gdb = shutil.which("gdb")
    if gdb is None:
        return None
    version = _gdb_version(gdb)
    if version is None or version[0] < GDB_MINIMUM:
        return None
    # (-q: no banner, which would be shown as the program's output.)
    return Adapter("gdb", [gdb, "-q", "--interpreter=dap"], f"{version[0]}.{version[1]}")


def lldb_adapter() -> Adapter | None:
    """lldb-dap: Apple's on macOS, else one on the PATH."""
    if sys.platform == "darwin":
        code, output, _err = console.run(["xcrun", "-f", "lldb-dap"], timeout=10)
        if code == 0 and output and output.strip():
            return Adapter("lldb", [output.decode().strip()])
    program = shutil.which("lldb-dap")
    return Adapter("lldb", [program]) if program else None


_found: dict[str, Adapter | None] = {}


def find_adapter() -> Adapter | None:
    """The debugger for this platform (None: none found), looked for once."""
    if "adapter" not in _found:
        finders = (lldb_adapter, gdb_adapter) if sys.platform == "darwin" else (gdb_adapter, lldb_adapter)
        _found["adapter"] = next((a for a in (f() for f in finders) if a is not None), None)
    return _found["adapter"]


def forget():
    """Look for the debugger again next time (a new environment)."""
    _found.clear()


NOT_FOUND = ("No debugger was found. On Linux, install gdb 14 or newer (the project's pixi environment has it); "
             "on macOS, lldb-dap comes with Xcode's Command Line Tools (xcode-select --install).")
