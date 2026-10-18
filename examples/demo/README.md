# Demo project

A small fpm project for trying out every part of VibeFPMGUI. Open
`examples/demo/fpm.toml` in the GUI (or `pixi run demo` from the repo root).

| Try this | What you should see |
|---|---|
| Open the project before building | Dependency graph with `fmin` and `test-drive` shown as *not downloaded* |
| **Update deps** | They download and the graph fills in (git, local path, metapackage, dashed dev-dependency) |
| **Build** | The Build tab lists each file; independent modules compile in parallel. `utils.f90` exists in both `demo` and `mathutils`, so the Package column tells them apart |
| Features → `warnings`, then Build | Files with warnings get orange badges (hover for the messages) |
| Features → `broken`, then Build | `demo_broken.F90` fails; the rest are aborted or done |
| **Test** | Six tests; `check_slow` takes a few seconds so you can watch the progress bar. Run it twice to get time estimates. `check_traceback` prints a traceback like ifx's for a runtime error (it passes): click a row's line number and file in the Output panel to open the file there |
| Features → `failing-test` (or Profile → `failures`), then Test | `check_feature` fails with exit code 1; click it to see only its output |
| *Debug → Debug a Test…* → `check_debugger` (Linux, gdb), or its **Debug** button in Build outputs | A test written for the debugger: integers, reals, complex, logicals and strings of several kinds, fixed, 2-D, allocatable and pointer arrays, derived types (nested, with allocatable components and type-bound procedures), a module variable, loops (`exit`, `cycle`, `do while`, `select case`), recursion, and contained procedures. The breakpoints its first comment suggests: `factorial` (the call stack grows with each call), `call p%move(…)` (a derived type's components) |
| **Valgrind** button beside `check_debugger` (Linux) | No errors: it frees what it allocates |
| Features → `greeting`, then Run with Target `demo` | The program prints a greeting enabled by a preprocessor macro |
| Target `bench`, then Run | The second program (OpenMP timing loop) |
| Profile → `ci` | A feature profile from `[profiles]` (checks + warnings) |

Features defined in `fpm.toml`: `fast`, `checks`, `warnings`, `greeting`,
`failing-test`, `broken`. Profiles: `ci`, `failures`.
