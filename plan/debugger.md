# Plan: debugger support

*Drafted 2026-10-12 · Status: Phase 1 implemented (2026-10-17), with Phase 2's variables tree.*

**Done:** `wire.py` (the framing, shared with `lsp.py`), `dap.py`,
`debug_adapters.py`, `debug_session.py`, `debug_panel.py`,
`window/debugging.py`; breakpoints in the gutter (saved with the session,
moving with edits); the Debug menu and panel (call stack, variables with lazy
expansion, console with evaluate). Tests: `tests/test_debugger.py` (a fake
adapter, `tests/fake_dap.py`: runs anywhere) and `tests/test_debug_gdb.py`
(real gdb + gfortran + fpm: Linux, CI, the Docker image).

**Spike 1 (Linux + gdb 18.1 DAP + gfortran 15), findings:**
- gdb answers `launch` only after `configurationDone`: send `launch`, then
  wait for the `initialized` event to set breakpoints.
- Breakpoints set before the program loads are `pending`, then placed by
  `breakpoint` events; one on a line with no code stays `pending` for ever
  (taken as not placed at the first stop).
- Scopes: Arguments, Locals, Globals (module variables, as
  `shapes::counter`), and Registers (left out).
- Arrays expand to 1-based elements; derived types to their components;
  `evaluate` takes `a(2:3)`. Its `repl` context runs a *gdb command*: the
  `watch` context evaluates expressions.
- gdb's own messages come as the program's `stdout` output: `-q` drops the
  banner; the "pending" notes are filtered.
- gfortran's main program is `MAIN__` (shown as "main program").
- **fpm 0.13 without `--profile` passes no profile flags (no `-g`):** Start
  Debugging adds `--profile debug` when none (and no features) is chosen.

## Goal

Debug the project's programs, tests and examples from VibeFPMGUI:
- set breakpoints in the editor's gutter;
- run, pause, step and stop;
- see the call stack, the variables and watch expressions;
- have the editor follow the current line.

It should work on macOS (Apple Silicon and Intel), Linux and Windows, with the
compilers fpm users have: gfortran first, then LFortran, and others where a
debugger supports them.

**Non-goals (for now):**
- attaching to a running process;
- remote debugging;
- MPI programs (several processes);
- reverse debugging;
- the Intel compilers' own debugger;
- debugging the C code in a mixed-language package beyond what the debugger
  gives for free.

## What works today: findings

Tested on this Mac (Apple Silicon, macOS 27), with a small program built
`-g -O0`: a module procedure with an array argument, a scalar argument and a
derived-type argument, plus a local.

| | gfortran 15.3 (conda-forge) | LFortran 0.67 |
|---|---|---|
| **Apple lldb** (Command Line Tools, `lldb-2103`) | Breakpoints by file:line ✅, call stack with source lines ✅, stepping ✅. **No variables at all**: "no plugin for the language fortran95" | Breakpoints ✅, call stack ✅. Locals ✅, but **arguments missing**; module and program variables show "no location"; a derived type shows as `void` |
| **conda-forge lldb 22.1.8** | Same as Apple lldb (launches fine using the system's debug server) | (same LLVM: expected the same) |
| **gdb** | **Not available on Apple Silicon.** The conda-forge `gdb` package for osx-arm64 installs only code-signing scripts, no `gdb`: GDB doesn't support native debugging on arm64 macOS | — |

What that means:
- **Your suspicion is right: gdb is out on Apple Silicon.** lldb is the only
  native debugger there.
- **lldb has no Fortran support, and on macOS that hurts gfortran most.**
  lldb shows nothing at all for a gfortran compile unit, because it only builds
  variables for the C-family languages. Breakpoints, stepping and the call
  stack still work, since they only need the line table.
- **LFortran + lldb is the better pairing on macOS today, but it's
  immature.** LFortran's debug info is partly usable: locals show, but
  arguments, module variables and derived types don't yet. LFortran's debug
  info is actively developed, so this should improve; the gaps found here are
  worth reporting upstream.
- **On Linux, gdb + gfortran is the gold standard** (not tested here). gdb
  understands Fortran: arrays with their bounds, derived types, module
  variables, `print a(2:3)`, case-insensitive names.
- **Both debuggers speak the Debug Adapter Protocol (DAP), the protocol
  VS Code uses.**
  - `lldb-dap` comes with LLVM. It's in Apple's Command Line Tools
    (`xcrun lldb-dap`, present here) and in the conda-forge `lldb` package
    on every platform (checked: osx-arm64, osx-64, linux-64, win-64 all have
    `lldb` 22.1.8, which includes `lldb-dap`).
  - gdb has DAP built in since gdb 14 (`gdb --interpreter=dap`). conda-forge
    has gdb 18.1 for linux-64 and osx-64; there is no win-64 build.

Availability on conda-forge, checked with `pixi search`:

| | osx-arm64 | osx-64 | linux-64 | win-64 |
|---|---|---|---|---|
| `lldb` (includes `lldb-dap`) | 22.1.8 | 22.1.8 | 22.1.8 | 22.1.8 |
| `gdb` (DAP since 14) | 17.2, but no `gdb` program in it | 18.1 (needs code signing on macOS) | 18.1 | — |

## Approach

**Speak DAP, and use whichever debugger fits the platform and compiler.** The
app gets one debugger client, much like the existing fortls client (`lsp.py`).
That one client then works with:

| Platform | Debugger (adapter) | Notes |
|---|---|---|
| Linux | **gdb ≥ 14** (`gdb --interpreter=dap`), from conda-forge | Best Fortran support. `lldb-dap` as a fallback (e.g. for LFortran). |
| macOS arm64 | **lldb-dap**: Apple's (`xcrun lldb-dap`), else conda-forge's | Apple's uses Apple's signed `debugserver`: no code-signing setup. Variables: see the limits above. |
| macOS x86_64 | lldb-dap, or gdb if the user has code-signed it | gdb on macOS needs a signed binary (conda-forge ships `macos-codesign-gdb.sh`); offer it as an option, not the default. |
| Windows | gdb ≥ 14 from MSYS2/MinGW (with MinGW gfortran); lldb-dap otherwise | No conda-forge gdb for win-64. Phase 4: see *Windows* below. |

Why DAP rather than driving gdb/MI or lldb's Python API directly:
- **One client for every backend.** Adding another adapter later (e.g. a
  Fortran-aware lldb if one appears) costs nothing.
- **The adapters handle the hard parts:** process launch, threads, stepping
  granularity, variable references and lazy expansion.
- **The same wire format as LSP** (`Content-Length` headers + JSON), so the
  framing code in `lsp.py` can be shared (see *Architecture*).
- **It's what VS Code, Neovim and Zed use**, so adapter bugs get found and
  fixed by others.

### Choosing the adapter
A *Debugger* setting (View or Build options) chooses **Automatic**, **gdb**
or **lldb**, plus an optional path.

*Automatic* picks:
- **Linux:** gdb if `gdb --version` ≥ 14, else lldb-dap.
- **macOS:** `xcrun -f lldb-dap`, else `lldb-dap` on the PATH. gdb is never
  picked automatically on arm64.
- **Windows:** gdb ≥ 14 on the PATH, else lldb-dap.

The choice and its version show in the Debug panel's header, as the Terminal
shows its shell.

**pixi:** add `lldb` for all platforms and `gdb` for `linux-64` (as
`[target.linux-64.dependencies]`). On macOS the app prefers Apple's lldb-dap
when present (signed debug server, no prompts). It falls back to the
environment's lldb-dap otherwise; that also launched fine here.

## What gets debugged, and how it's built

**The executable.** fpm decides where programs go (`build/<compiler>_<hash>/app/…`).
The app already has two ways to find them:
1. **Build outputs** (`build_outputs.py`) lists the programs of each
   configuration.
2. **The `--runner` trick** (as `test_runner.py` does): `fpm run <name>
   --runner <script>` builds with the current options, then calls `<script>
   <exe> <args>` instead of running the program. A tiny script that prints
   its arguments tells us exactly which executable fpm would run, with which
   arguments, for programs, tests (`fpm test`) and examples (`fpm run
   --example`).

Use (2): it always matches the current Build options, builds first (the Build
tab shows it), and covers tests and examples the same way.

**Debug info.**
- **Profiles:** fpm's `debug` profile (the default) compiles with `-g` for
  gfortran. The `release` profile doesn't: debugging with *release* selected
  should warn ("no debug info: switch to debug, or debug anyway?").
- **Extra flags:** for gfortran, `-O0 -g` plus `-fcheck=all` and
  `-ffpe-trap=invalid,zero,overflow` are useful when debugging. Offer them as
  a *Debug with runtime checks* option. They go in `--flag`, which gives a
  separate build folder, so normal builds aren't disturbed.
- **LFortran:** needs `-g` (check what fpm's debug profile passes it; see
  *Spikes*).

**macOS specifics.**
- gfortran leaves the debug info in the `.o` files with a debug map in the
  executable. That works while `build/` is intact, as it was in the test. No
  `dsymutil` is needed, but running it after the build would make the
  executable self-contained.
- The first debug session may trigger the "Developer Tools Access" prompt for
  an admin password. Document `DevToolsSecurity -enable` for people who want
  to avoid it.

**Linux.** Launching under a debugger is always allowed. The kernel's
`ptrace_scope` setting only restricts *attaching* (a non-goal). In a
container, ptrace needs `--cap-add=SYS_PTRACE`.

**Program input and output.**
- **Output:** the program's output goes to the debug console (DAP `output`
  events).
- **Input:** a program that reads from the keyboard needs a terminal. DAP's
  `runInTerminal` request asks the client to start the program in one:
  implement it with the Terminal panel. lldb-dap supports it; gdb's DAP
  support for it needs checking (see *Spikes*).
- **Phase 1** can run without a terminal and say so: "input isn't available
  yet; use Run in Terminal".

## User interface

- **Debug menu and toolbar**, with the usual keys:

  | Action | Key |
  |---|---|
  | Start Debugging / Continue | F5 |
  | Stop | ⇧F5 |
  | Restart | ⇧⌘F5 |
  | Pause | F6 |
  | Step Over | F10 |
  | Step Into | F11 |
  | Step Out | ⇧F11 |
  | Toggle Breakpoint | F9 |

  On macOS the F-keys need Fn unless set as standard function keys; the menu
  items and toolbar buttons are always there. *Start Debugging* uses the
  *Target* field (or asks which program, test or example when there are
  several), and the *Program args*.
- **Breakpoints in the gutter.**
  - **Setting them:** click left of a line number for a red dot. ⌥-click
    gives a conditional breakpoint (`i > 100`) or a hit count; ⌘-click
    disables it.
  - **Layout:** the gutter already has the markers and the gremlins column,
    so breakpoints get their own column next to the numbers.
  - **Keeping them:** they're saved per project, like the open files.
  - **When lines move:** breakpoints move with editing, using `QTextCursor`s
    like the multi-cursors do.
  - **Feedback:** a breakpoint the debugger couldn't place (no code on that
    line) shows hollow, with the adapter's reason in its tooltip.
- **Current line.** While paused, the line gets a yellow arrow in the gutter
  and a highlight, and the editor opens the file and scrolls to it, as Go to
  Definition does.
- **Debug panel** (a new dock, stacked with the bottom panels):
  - **Call stack:** frames with function and file:line; click one to go there
    and to see its variables.
  - **Variables:** a tree of Locals, Arguments and (gdb) module variables.
    Arrays and derived types expand lazily, through DAP's
    `variablesReference`. Big arrays are paged: first 100 elements, then
    "more…".
  - **Watch:** expressions, re-evaluated at each stop.
  - **Breakpoints:** list, enable/disable, remove all.
- **Debug console** (a tab in the Output panel, or a dock):
  - the program's output and the adapter's messages;
  - an input line for debugger commands or expressions (DAP `evaluate`, with
    `context: "repl"`).
- **Hover while paused:** hovering a name shows its value (DAP `evaluate`
  with `context: "hover"`), added to the existing hover card.
- **Problems / Run panels:** a program that stops on a runtime error
  (`-fcheck`, a floating-point trap) shows where it stopped, with the
  message.
- **Being honest about limits:** when the adapter is lldb and the frame's
  language is Fortran with no variables, the Variables section says why
  ("lldb can't show gfortran's variables; on Linux use gdb, or try
  LFortran"), instead of looking broken.

## Architecture

Following the code review's advice (§3.1), none of this goes into
`main_window.py` beyond wiring:

| Module | What it does |
|---|---|
| `jsonrpc.py` (new, shared) | `Content-Length` framing over a `QProcess`, request IDs and callbacks, incremental buffer: extracted from `LspClient`, which then uses it too |
| `dap.py` | `DapClient`: `initialize`, `launch`, `setBreakpoints`, `configurationDone`, `threads`, `stackTrace`, `scopes`, `variables`, `evaluate`, `continue`, `next`, `stepIn`, `stepOut`, `pause`, `disconnect`. Events `stopped`, `continued`, `output`, `exited`, `terminated`, `breakpoint`. Reverse request `runInTerminal`. |
| `debug_adapters.py` | Finding and checking the adapters per platform (`xcrun -f lldb-dap`, `gdb --version` ≥ 14, user's path), and each one's `launch` arguments (`program`, `args`, `cwd`, `env`, `stopOnEntry`; gdb's and lldb-dap's names differ slightly) |
| `debug_session.py` | `DebugController`: the state machine (idle → building → starting → running ⇄ stopped → ended), the fpm `--runner` trick to find the executable, breakpoints per file, mapping frames to editor tabs |
| `debug_panel.py` | The Debug panel (call stack, variables, watch, breakpoints) and the console |
| `editor.py` additions | Breakpoint column and current-line arrow in the gutter (`set_breakpoints`, `set_execution_line`), and a hover hook for values |

The process and threading pattern stays as for fortls:
- **No threads:** one `QProcess` per adapter, read in the GUI thread.
- **Methods, not lambdas, on the process** (the PySide crash noted in
  `lsp.py`).
- **Every request has a timeout**, so a hung adapter can't leave a session
  stuck. That also closes the review's §1.6-style "thread never ends"
  concerns.

## Phases

**Phase 0: spikes.** About a day each; they decide details, not direction.
1. **Linux + gdb DAP + gfortran.**
   - Breakpoints, stepping, variables and arrays through DAP.
   - Module variables (`__mod_MOD_var`): does gdb's DAP `scopes` list them, or
     do we add a "Module variables" scope ourselves via `evaluate`?
   - Does gdb's DAP support `runInTerminal`?
2. **macOS + lldb-dap + gfortran.** Confirm through DAP what the batch tests
   showed: breakpoints, stepping and the stack work; the Variables scopes come
   back empty. Is there any lldb setting or DWARF option (`-gdwarf-4`,
   `-gstrict-dwarf`) that makes lldb show gfortran's variables, even
   C-style? Worth an hour; probably no.
3. **LFortran through fpm.**
   - What does `fpm build --compiler lfortran --profile debug` pass? Does it
     include `-g`?
   - Retest the gaps (arguments, module variables, derived types) with the
     newest LFortran, and report what's missing upstream with the small
     program above.
4. **Windows.**
   - The conda-forge gfortran toolchain on win-64: MinGW-w64 (DWARF) or
     something else?
   - Does MSYS2's gdb (≥ 14, with DAP) debug its executables?
   - What does lldb-dap do with them (DWARF on Windows: limited)?

**Phase 1: run, stop and step.** The essentials, all platforms where an
adapter is found.
- `jsonrpc.py` extracted from `lsp.py` (fortls keeps working: its tests
  cover it).
- `dap.py`, `debug_adapters.py`, `debug_session.py`.
- Breakpoints in the gutter: toggle, persist, move with edits.
- Debug menu and toolbar; current line marker; call stack panel; console
  with program output.
- Debugging programs and examples via `--runner`; the release-profile
  warning.
- Tests: `dap.py` against a fake adapter (like `FakeOllama`), so CI needs
  no debugger. Plus real-debugger tests marked like the fortls ones, run
  where gdb/lldb-dap exist: gdb on the Linux CI runner, lldb-dap on the macOS
  runner.

**Phase 2: seeing the data.**
- Variables tree with lazy expansion and paging; watch expressions; hover
  values.
- Conditional breakpoints, hit counts, disabled breakpoints, the Breakpoints
  list.
- The "why no variables" message for lldb + gfortran.
- Fortran presentation:
  - 1-based indices as gdb reports them;
  - array bounds in the type column (`real(4) a(1:10, 0:5)`);
  - derived types by component;
  - `character(len=…)` strings trimmed;
  - logicals as `.true.`/`.false.`.

**Phase 3: Fortran-specific help.**
- Debug a test: from the Tests panel, a test's right-click → *Debug*.
- Stop on runtime errors:
  - **gfortran:** function breakpoints on `_gfortran_runtime_error_at`,
    `_gfortran_os_error_at`, `_gfortran_stop_numeric` (for `error stop`) and
    `_gfortran_error_stop_string`;
  - **floating-point traps:** stop on `SIGFPE`;
  - either way, then select the user's frame, not the library's.
- Program input through the Terminal panel (`runInTerminal`).
- *Debug with runtime checks* (the extra flags above).

**Phase 4: Windows, and polish.**
- Windows per the spike's results; ifx/ifort: say plainly that their
  debugger isn't supported.
- Data breakpoints (watchpoints) where the adapter supports them.
- "Run to cursor", "Set next statement" (gdb's `jump`, lldb's
  `thread jump`), memory view if asked for.

## Testing

- **A fake DAP adapter** (a small Python script speaking DAP over stdio,
  scripted per test) for every client and UI test: CI needs no debugger, and
  error cases (timeouts, an adapter that crashes, unverified breakpoints) are
  easy to produce.
- **Real-debugger tests**, skipped when the adapter isn't found:
  - **Linux CI:** gdb with gfortran; the important one for variables.
  - **macOS CI:** lldb-dap with gfortran (breakpoints, stepping, stack); and
    with LFortran (locals).
- **The demo project** (`examples/demo`) gets a small program with a loop,
  an array and a derived type to debug, and its README a "try debugging"
  section.

## Risks and open questions

- **macOS + gfortran shows no variables, and we can't fix that ourselves.**
  It's lldb's missing Fortran support. Mitigations:
  - say so clearly in the UI;
  - recommend LFortran on macOS as its debug info matures;
  - suggest Linux (or a Linux VM/container) for heavy debugging.

  Worth watching: upstream LLVM discussions on Fortran support in lldb, which
  Flang may drive.
- **gdb's DAP support is newer than lldb-dap's.** Some requests may be
  missing or differ in older gdb versions; require ≥ 14 and test with the
  conda-forge version (18.1).
- **Code signing on macOS** if anyone wants gdb on Intel Macs: documented
  opt-in only.
- **Optimised builds** (`release`, or user flags with `-O2`) show "optimized
  out" values and jumpy stepping: the release-profile warning covers the
  common case.
- **MPI and coarrays:** out of scope; a program run through a `--runner`
  like `mpirun` can't be debugged this way. Say so when the Extra args
  contain `--runner`.
- **Effort:** Phase 1 ≈ 1–2 weeks of focused work (most of it the session
  state machine and the gutter UI); Phase 2 ≈ 1 week; Phases 3–4 depend on
  the spikes.
