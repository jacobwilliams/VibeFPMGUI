"""Runner wrapper passed to ``fpm test --runner``.

fpm calls ``<runner> <test executable> [args...]`` for each test, one after
another.  This script prints a marker before and after running the test so
the GUI can tell where each test starts and ends and what it returned::

    @@vibefpm:start:check_fast@@
    ...test output...
    @@vibefpm:end:check_fast:0:0.012@@

A runner the user gave fpm themselves (e.g. ``mpirun -np 4``) is passed in
the ``VIBEFPM_INNER_RUNNER`` environment variable and still wraps the test.

Only the standard library is used, so this runs with ``python -I``.
"""

import os
import shlex
import subprocess
import sys
import time

MARKER = "@@vibefpm:{}@@"


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: test_runner.py EXECUTABLE [ARGS...]", file=sys.stderr)
        return 2
    name = os.path.basename(argv[0])
    if name.lower().endswith(".exe"):
        name = name[:-4]
    inner = os.environ.get("VIBEFPM_INNER_RUNNER", "")
    command = shlex.split(inner, posix=os.name != "nt") + argv

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
