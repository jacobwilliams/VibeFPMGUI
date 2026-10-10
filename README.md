# VibeFPMGUI
A vibe-coded GUI for the [Fortran Package Manager](https://fpm.fortran-lang.org) (fpm),
written in Python with PySide6 (through the [qtpy](https://github.com/spyder-ide/qtpy) shim).

## Features

* Open any `fpm.toml` and run `fpm build`, `run`, `test`, `update` and `clean`
  from the toolbar or the *Commands* menu (Build ⌘B, Run ⌘R, Test ⌘U,
  Stop ⌘.; Ctrl on Linux/Windows), with live output and the usual options
  (profile, compiler, flags, target, extra arguments, program arguments).
  Hover an option to see the fpm flag it becomes.
* *Features* menu: tick any of the features declared in the project's
  `[features]` table to build/run/test with `--features …` (remembered per
  project). The *Profile* list also offers the project's own `[profiles]`;
  since fpm can't combine the two, Profile is disabled while features are on.
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
* Built-in editor: double-clicking a file (a row in the Build tab, or a
  package in the graph for its `fpm.toml`) opens it in a tab with syntax
  highlighting chosen by file extension, line numbers, find/replace, go to
  line, undo/redo and save. Files opened from the Build tab show the last
  build's warnings and errors as markers in the gutter (hover for the
  message) and open at the first one. Saving the project's `fpm.toml`
  reloads the project. *View → Open Files in Built-in Editor* switches back
  to your default application.
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
pixi run demo                       # open the demo project in examples/demo
```

Or install it as a Python package (Python 3.11+); `fpm` and a Fortran compiler
must also be on your `PATH`:

```sh
pip install .        # from a checkout
vibefpmgui [path/to/fpm.toml]
```

The demo project ([examples/demo](examples/demo)) uses features, profiles,
git/path/metapackage/dev dependencies, warnings, parallel compiles and tests,
so every part of the GUI can be tried; its README lists what to click.
