"""fpm's command line: link flags, and the test runner the app puts in."""

from __future__ import annotations

import os
import sys

from .compat import join_args

# The wrapper each test or program runs in (marks where it starts and ends).
TEST_RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_runner.py")


def with_link_flag(args: list[str], flag: str) -> list[str]:
    """fpm arguments with ``flag`` added to the link flags (to the user's own
    --link-flag, if there's one: fpm would take only one of two)."""
    out = list(args)
    for i, arg in enumerate(out):
        if arg == "--link-flag" and i + 1 < len(out):
            out[i + 1] = f"{out[i + 1]} {flag}"
            return out
        if arg.startswith("--link-flag="):
            out[i] = f"{arg} {flag}"
            return out
    return out + ["--link-flag", flag]


def split_runner(args: list[str]) -> tuple[list[str], str]:
    """Take "--runner CMD" / "--runner=CMD" out of an fpm argument list."""
    rest, runner = [], ""
    i = 0
    while i < len(args):
        if args[i] == "--runner" and i + 1 < len(args):
            runner = args[i + 1]
            i += 2
            continue
        if args[i].startswith("--runner="):
            runner = args[i].split("=", 1)[1]
        else:
            rest.append(args[i])
        i += 1
    return rest, runner


def runner_command(collect: bool = False) -> str:
    """The command fpm should put in front of each test executable (with
    ``collect``: one that only prints the test's command line)."""
    parts = [sys.executable, "-I", TEST_RUNNER] + (["--collect"] if collect else [])
    return join_args(parts)  # quoted for the platform's shell


# Valgrind's memory check, as fpm's runner: every leak, and exit code 1 when it finds an error.
VALGRIND_RUNNER = "valgrind --leak-check=full --show-leak-kinds=all --error-exitcode=1"


def with_runner(args: list[str], runner: str) -> list[str]:
    """fpm's ``args`` with ``--runner runner`` (before any program arguments)."""
    cut = args.index("--") if "--" in args else len(args)
    return args[:cut] + ["--runner", runner] + args[cut:]
