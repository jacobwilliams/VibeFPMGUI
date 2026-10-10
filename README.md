# VibeFPMGUI
A vibe-coded GUI for the [Fortran Package Manager](https://fpm.fortran-lang.org) (fpm),
written in Python with PySide6 (through the [qtpy](https://github.com/spyder-ide/qtpy) shim).

## Features

* Open any `fpm.toml` and run `fpm build`, `run`, `test`, `update` and `clean`
  with live output, a Stop button and the usual options (profile, compiler,
  flags, target, extra arguments, program arguments).
* Build status grid next to the output: every file/target fpm compiles is
  listed live (works with parallel/OpenMP builds) with its package, status
  (compiling / done / warnings / failed / aborted) and elapsed time, plus an
  overall progress bar. Warnings are read from fpm's per-object `.o.log`
  files, since fpm only prints compiler output when a compile fails. Hover a
  status for the compiler messages; double-click a row to open the source.
* Test progress: **Test** first builds and lists the tests (`fpm test
  --list`), then runs them through a small wrapper given to fpm's `--runner`
  so each test's start, end and exit code are known. The *Tests* tab shows
  every test (queued / running / passed / failed / not run) with its time,
  and a progress bar. Test durations are remembered per project, so later
  runs weight the bar by expected time and estimate the time left. Click a
  test to see only its output. A `--runner` you put in *Extra args* (e.g.
  `mpirun -np 4`) still wraps each test.
* Dependency diagram (drawn with [pyqtgraph](https://www.pyqtgraph.org)) built
  from the main `fpm.toml` and the `fpm.toml` of every downloaded dependency
  (git, local path, registry and metapackages; dashed edges are dev/test
  dependencies; dashed nodes haven't been downloaded yet).
  * **Graph** menu (also the *Style* button) to choose the layout (layered
    top-down or left-right, radial tree, circular, force-directed), the theme
    (auto, light, dark, blueprint, neon) and curved or straight edges. Layout
    changes animate; choices are remembered.
  * Zoom with the mouse wheel, pan by dragging the background, drag packages
    around. Click a package to highlight everything it depends on and
    everything that uses it (click the background to clear); double-click to
    open its `fpm.toml`. Export to PNG or SVG.
* The graph refreshes automatically after commands that may download
  dependencies.

## Running

```sh
pixi run start                      # reopens the last project
pixi run start path/to/fpm.toml     # or a project directory
```
