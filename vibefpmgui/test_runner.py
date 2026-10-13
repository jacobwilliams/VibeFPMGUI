"""Runner wrapper passed to ``fpm test --runner``.

fpm calls ``<runner> <test executable> [args...]`` for each test, one after
another.  This script prints a marker before and after running the test so
the GUI can tell where each test starts and ends and what it returned::

    @@vibefpm:start:check_fast@@
    ...test output...
    @@vibefpm:end:check_fast:0:0.012@@

With ``--collect`` first, the test isn't run: its command line is printed
instead (``@@vibefpm:exe:check_fast@@["/abs/path/check_fast", "arg"]``), for
the GUI to run the tests itself, in parallel (see
`vibefpmgui.parallel_tests`).

A runner the user gave fpm themselves (e.g. ``mpirun -np 4``) is passed in
the ``VIBEFPM_INNER_RUNNER`` environment variable and still wraps the test.

Only the standard library is used, so this runs with ``python -I``.
"""

import json
import os
import shlex
import subprocess
import sys
import time

MARKER = "@@vibefpm:{}@@"


def executable_name(executable: str) -> str:
    name = os.path.basename(executable)
    return name[:-4] if name.lower().endswith(".exe") else name


def split_runner(runner: str) -> list[str]:
    """A runner command (e.g. "mpirun -np 4") as a list."""
    if os.name == "nt":  # backslashes are path separators there, not escapes
        parts = shlex.split(runner, posix=False)
        return [p[1:-1] if len(p) >= 2 and p[0] == p[-1] and p[0] in "\"'" else p for p in parts]
    return shlex.split(runner)


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: test_runner.py EXECUTABLE [ARGS...]", file=sys.stderr)
        return 2
    if argv[0] == "--collect":
        if len(argv) < 2:
            return 2
        name = executable_name(argv[1])
        print(MARKER.format(f"exe:{name}") + json.dumps([os.path.abspath(argv[1]), *argv[2:]]), flush=True)
        return 0
    name = executable_name(argv[0])
    command = split_runner(os.environ.get("VIBEFPM_INNER_RUNNER", "")) + argv

    # The test writes straight to our stdout, so flush our own text first.
    # The leading newline keeps the marker off a line the previous test left
    # unfinished.
    print("\n" + MARKER.format(f"start:{name}"), flush=True)
    started = time.monotonic()
    try:
        code = subprocess.call(command)
    except OSError as exc:
        print(f"could not run {command[0]}: {exc}", flush=True)
        code = 127
    seconds = time.monotonic() - started
    print("\n" + MARKER.format(f"end:{name}:{code}:{seconds:.3f}"), flush=True)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
