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
* Dependency diagram built from the main `fpm.toml` and the `fpm.toml` of every
  downloaded dependency (git, local path, registry and metapackages; dashed
  edges are dev/test dependencies; grey dashed nodes haven't been downloaded
  yet). Zoom with the mouse wheel, pan by dragging the background, drag nodes
  around, click a node for details, double-click to open its `fpm.toml`, and
  export the diagram to PNG or SVG.
* The graph refreshes automatically after commands that may download
  dependencies.

## Running

```sh
pixi run start                      # reopens the last project
pixi run start path/to/fpm.toml     # or a project directory
```
