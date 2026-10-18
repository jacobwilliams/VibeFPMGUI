"""A fake debug adapter for the tests: speaks DAP over stdin/stdout as gdb's
does (``launch`` answered only once the configuration is done, breakpoints
"pending" first, then placed), with a made-up program: it stops at its
breakpoints, steps a line at a time, and has a few variables. No debugger
needed, so the tests run anywhere.

The program's lines are the breakpoints' file's; it ends (exit code 0) after
its last breakpoint. ``FAKE_DAP_NO_CODE``: lines (comma-separated) where a
breakpoint can't be placed. ``FAKE_DAP_SILENT``: requests it never answers
(to test timeouts). ``FAKE_DAP_NEVER_PLACED``: lines whose breakpoints stay
"pending" (as gdb leaves one on a line with no code).

Only the standard library, so it runs with ``python -I``.
"""

import json
import os
import sys

NO_CODE = {int(n) for n in os.environ.get("FAKE_DAP_NO_CODE", "").split(",") if n}
SILENT = set(filter(None, os.environ.get("FAKE_DAP_SILENT", "").split(",")))
NEVER_PLACED = {int(n) for n in os.environ.get("FAKE_DAP_NEVER_PLACED", "").split(",") if n}  # (pending for ever)

out = sys.stdout.buffer
seq = 0


def send(message: dict):
    global seq
    seq += 1
    body = json.dumps({"seq": seq, **message}).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    out.flush()


def event(name: str, body: dict | None = None):
    send({"type": "event", "event": name, **({"body": body} if body is not None else {})})


def respond(request: dict, body: dict | None = None, success: bool = True, message: str = ""):
    send({"type": "response", "request_seq": request["seq"], "command": request["command"], "success": success,
          **({"body": body} if body is not None else {}), **({"message": message} if message else {})})


def read() -> dict | None:
    header = b""
    while not header.endswith(b"\r\n\r\n"):
        ch = sys.stdin.buffer.read(1)
        if not ch:
            return None
        header += ch
    length = int(header.split(b":")[1])
    return json.loads(sys.stdin.buffer.read(length))


class Program:
    def __init__(self):
        self.source = ""  # the breakpoints' file (the program's source)
        self.breakpoints: list[int] = []
        self.line = 0
        self.launch = None
        self.ended = False

    def stop(self, line: int, reason: str):
        self.line = line
        event("stopped", {"threadId": 1, "allThreadsStopped": True, "reason": reason,
                          **({"hitBreakpointIds": [self.breakpoints.index(line) + 1]} if reason == "breakpoint" else {})})

    def run_from(self, line: int):
        """Run on from ``line``: to the next breakpoint, or the end."""
        ahead = [n for n in self.breakpoints if n >= line]
        if ahead:
            self.stop(ahead[0], "breakpoint")
        else:
            event("output", {"category": "stdout", "output": "done\n"})
            event("exited", {"exitCode": 0})
            event("terminated")
            self.ended = True


def main():
    program = Program()
    while True:
        request = read()
        if request is None:
            return
        command, args = request["command"], request.get("arguments") or {}
        if command in SILENT:
            continue
        if command == "initialize":
            respond(request, {"supportsConfigurationDoneRequest": True})
            event("initialized")
        elif command == "launch":
            program.launch = request  # (answered once the configuration is done, as gdb does)
            event("output", {"category": "stdout", "output": "No source file named x.f90.\n"})  # (gdb's noise)
        elif command == "setBreakpoints":
            program.source = args["source"]["path"]
            lines = [b["line"] for b in args.get("breakpoints", [])]
            program.breakpoints = sorted(n for n in lines if n not in NO_CODE and n not in NEVER_PLACED)
            respond(request, {"breakpoints": [
                {"id": i + 1, "verified": False, "reason": "pending"} if n not in NO_CODE
                else {"id": i + 1, "verified": False, "message": "No code at that line"}
                for i, n in enumerate(lines)]})
            for i, n in enumerate(lines):  # (placed, once the program is loaded)
                if n not in NO_CODE and n not in NEVER_PLACED:
                    event("breakpoint", {"reason": "changed", "breakpoint": {"id": i + 1, "verified": True,
                                                                             "line": n}})
        elif command == "configurationDone":
            respond(request)
            if program.launch is not None:
                respond(program.launch)
                event("output", {"category": "stdout", "output": "program started\n"})
                program.run_from(1)
        elif command == "threads":
            respond(request, {"threads": [{"id": 1, "name": "demo"}]})
        elif command == "stackTrace":
            respond(request, {"stackFrames": [
                {"id": 0, "name": "demo", "line": program.line, "column": 1,
                 "source": {"name": os.path.basename(program.source), "path": program.source}},
                {"id": 1, "name": "__libc_start_main", "line": 0, "column": 0}]})
        elif command == "scopes":
            respond(request, {"scopes": [
                {"name": "Locals", "variablesReference": 1, "expensive": False},
                {"name": "Registers", "variablesReference": 9, "presentationHint": "registers"}]})
        elif command == "variables":
            reference = args["variablesReference"]
            if reference == 1:
                variables = [{"name": "n", "value": str(program.line), "variablesReference": 0},
                             {"name": "a", "value": "", "variablesReference": 2, "indexedVariables": 3},
                             {"name": "p", "value": "", "variablesReference": 3, "namedVariables": 2}]
            elif reference == 2:
                variables = [{"name": str(i), "value": f"{i}.5", "variablesReference": 0} for i in (1, 2, 3)]
            elif reference == 3:
                variables = [{"name": "x", "value": "2", "variablesReference": 0},
                             {"name": "y", "value": "3", "variablesReference": 0}]
            else:
                variables = [{"name": "x0", "value": "0", "variablesReference": 0}]
            respond(request, {"variables": variables})
        elif command == "evaluate":
            if args.get("expression") == "n * 2":
                respond(request, {"result": str(program.line * 2), "variablesReference": 0})
            else:
                respond(request, success=False, message=f"No symbol \"{args.get('expression')}\" in current context.")
        elif command in ("next", "stepIn", "stepOut"):
            respond(request)
            event("continued", {"threadId": 1})
            program.stop(program.line + 1, "step")
        elif command == "continue":
            respond(request, {"allThreadsContinued": True})
            program.run_from(program.line + 1)
        elif command == "pause":
            respond(request)
            program.stop(program.line or 1, "pause")
        elif command == "disconnect":
            respond(request)
            return
        else:
            respond(request, success=False, message=f"{command}: not supported by the fake")


if __name__ == "__main__":
    main()
