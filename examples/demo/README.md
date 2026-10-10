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
| **Test** | Four tests; `check_slow` takes a few seconds so you can watch the progress bar. Run it twice to get time estimates |
| Features → `failing-test` (or Profile → `failures`), then Test | `check_feature` fails with exit code 1; click it to see only its output |
| Features → `greeting`, then Run with Target `demo` | The program prints a greeting enabled by a preprocessor macro |
| Target `bench`, then Run | The second program (OpenMP timing loop) |
| Profile → `ci` | A feature profile from `[profiles]` (checks + warnings) |

Features defined in `fpm.toml`: `fast`, `checks`, `warnings`, `greeting`,
`failing-test`, `broken`. Profiles: `ci`, `failures`.
