# VibeFPMGUI
A vibe-coded GUI for the [Fortran Package Manager](https://fpm.fortran-lang.org) (fpm),
written in Python with PySide6 (through the [qtpy](https://github.com/spyder-ide/qtpy) shim).

## Features

* Open any `fpm.toml` and run `fpm build`, `run`, `test`, `update` and `clean`
  from the toolbar or the *Commands* menu (Build ⌘B, Run ⌘R, Test ⌘U,
  Stop ⌘.; Ctrl on Linux/Windows), with live output and the usual options
  (profile, compiler, flags, target, extra arguments, program arguments).
  Hover an option to see the fpm flag it becomes.
  Very long output is cut short so the app stays responsive (an lfortran
  crash, for one, can print 100+ MB): after about 2 MB the Output panel
  stops, shows the last 200 lines when the command ends, and *Full Output…*
  shows the file the whole output was saved to.
  *File → Recent Manifests* reopens any of the last 10 projects (missing
  ones are greyed out); the last one is reopened at startup. Each project
  remembers the files open in the editor (with their cursor and scroll
  positions), the tab in front and the folders open in the *Project files*
  tree, and restores them when it is opened again. *File → Open fpm.toml in
  New Window…* (⇧⌘O) opens another project in its own window (a separate
  process, so each has its own builds and language server); such windows
  don't change which project is reopened at startup.
* *Features* menu: tick any of the features declared in the project's
  `[features]` table to build/run/test with `--features …` (remembered per
  project). The *Profile* list also offers the project's own `[profiles]`;
  since fpm can't combine the two, Profile is disabled while features are on.
* Build status grid next to the output: every file/target fpm compiles is
  listed live (works with parallel/OpenMP builds) with its package, status
  (compiling / done / warnings / failed / aborted) and elapsed time, plus an
  overall progress bar. Warnings are read from fpm's per-object `.o.log`
  files, since fpm only prints compiler output when a compile fails
  (gfortran, gcc/clang, Intel and lfortran message formats are understood;
  color codes in compiler output, which lfortran always writes, are shown as
  colors in the Output panel). Hover a
  status for the compiler messages; double-click a row to open the source.
* The Build, Tests and Review tables sort by clicking a column header
  (again to reverse, a third time for the original order); the order is
  kept while a build or test run updates them.
* Test progress: **Test** first builds and lists the tests (`fpm test
  --list`), then runs them through a small wrapper given to fpm's `--runner`
  so each test's start, end and exit code are known. The *Tests* tab shows
  every test (queued / running / passed / failed / not run) with its time,
  and a progress bar. Test durations are remembered per project, so later
  runs weight the bar by expected time and estimate the time left. Click a
  test to see only its output. A `--runner` you put in *Extra args* (e.g.
  `mpirun -np 4`) still wraps each test.
* *Project files* panel (below the build options): the project's files and
  those of every dependency (including git downloads in `build/dependencies`),
  with a filter box (files ignored by git and greyed-out files are left out
  of the matches unless *Ignored* is ticked). fpm's standard folders get a badge on their icon
  (library sources, include, programs, tests, examples — following any
  `source-dir`/`include-dir` set in `fpm.toml`), and `fpm.toml` gets its own
  icon. Hidden files and folders, `build/`, compiled files and nested
  packages are shown greyed out (greyed folders don't expand; double-click
  shows them in Finder). Files ignored by git (the project's `.gitignore`
  files, or each dependency's own) are shown in a medium grey, with the
  responsible `.gitignore` in the tooltip. Double-click a file to open it in
  the editor; right-click for Open
  in Default App, Show in Finder and Copy Path. Files with errors or warnings
  in the last build are shown in red / orange.
* *Run* tab: **Run** (toolbar) and the **Run** buttons in the *Build outputs*
  tab show each program running (spinner, time, exit code, output per
  program), like the Tests tab. Toolbar Run builds first (shown in the Build
  tab), then runs every program — or those matching *Target* — through the
  same marker wrapper as Test; run times are remembered for estimates.
* Built-in editor: double-clicking a file (a row in the Build tab, or a
  package in the graph for its `fpm.toml`) opens it in a tab with syntax
  highlighting chosen by file extension, line numbers, find/replace, go to
  line, undo/redo and save. Files opened from the Build tab show the last
  build's warnings and errors as markers in the gutter (hover for the
  message) and open at the first one. Saving the project's `fpm.toml`
  reloads the project. *View → Open Files in Built-in Editor* switches back
  to your default application. *View → Syntax Colors* picks any installed
  Pygments style (listed as light or dark) for the editor and `fpm.toml`;
  *Automatic*, the default, uses `default` in light mode and `monokai` in
  dark mode. *View → Editor Font…* picks the fixed-width font and size for
  the editor and `fpm.toml` (*Bigger/Smaller Text* still scale it; *Reset
  Editor Font* goes back to the system's).
* Linting: free-form Fortran files (`.f90`, `.F90`, …) open in the editor
  are checked with [fortitude](https://fortitude.readthedocs.io) when opened
  and as you type (unsaved text included). Findings are underlined and get a
  teal marker in the gutter (red for syntax errors); hover either for the
  rule code, message and suggested fix. fortitude runs in the file's folder,
  so the project's own settings (`[extra.fortitude]` in `fpm.toml`, or a
  `fortitude.toml`) apply. *View → Lint Fortran Files* turns it off.
  fortitude is part of the pixi environment; for a pip install, add it
  with `pip install fortitude-lint`.
  fortitude can fix many issues itself: hover one and click *Apply fix* (or
  right-click it → *Fix: …*) to apply just that fix, or use *Edit → Fix All
  Safe Issues* for the whole file. *Safe* fixes don't change what the code
  does; *Edit → Fix All Issues, Including Unsafe* also applies the others
  (marked "unsafe — check it"), which you should review. Either way it's a
  single Undo step and nothing is saved until you save.
* Formatting: **Format** (toolbar, or *Edit → Format with fprettify*,
  ⇧⌘I) re-indents and re-spaces the free-form Fortran file in the editor
  with [fprettify](https://github.com/fortran-lang/fprettify). Unsaved text
  is formatted too; the change is one Undo step and isn't saved until you
  save. A `.fprettify.rc` in the file's folder, a parent folder or your home
  folder sets the style. fprettify is part of the pixi environment
  (`pip install fprettify` otherwise).
* Fortran language server: for Fortran files the editor uses
  [fortls](https://fortls.fortran-lang.org), which indexes the project and
  its downloaded dependencies (unsaved edits included):
  * Hover a name for its declaration (with argument types and any doc
    comment), shown together with any fortitude message.
  * *Go to Definition* (F12, or ⌘-click a name; Ctrl-click on Linux/Windows)
    opens where it is defined, including in a dependency's sources. *Go Back*
    / *Go Forward* (⌘[ / ⌘]; Alt+←/→ elsewhere) return.
  * An outline drop-down above the editor lists the file's modules, types
    and procedures, follows the cursor, and jumps to the one you pick. A
    colored badge shows each one's kind (M module, P program, S subroutine,
    F function, T type, I interface), indented to show what is inside what.
  * *Edit → Go to Symbol in Project…* (⌘T) finds a module, type or
    procedure by name in the project and its dependencies.
  * Completion: after two letters of a name (or a `%`, for a derived
    type's components) a list of matching names appears — your procedures,
    variables and modules, those of the dependencies, and intrinsics — with
    each one's type below the list. ↑/↓ choose, Enter or Tab inserts, Esc
    closes; *Show Completions* (⌃Space; Ctrl+Space elsewhere) asks at any
    point. Nothing pops up inside comments or strings.
  * Argument hints: typing `(` or `,` in a call shows the routine's
    arguments with the current one underlined, and that argument's
    declaration and documentation.
  * *Find References* (⇧F12) lists every use of the name at the cursor in
    the *References* tab, grouped by file; click one to go there.
  * Go to Definition, Find References and Rename Symbol are also at the top
    of the editor's right-click menu, for the name you right-click.
  * *Rename Symbol…* (F2) renames a module, procedure, type or variable
    everywhere it is used, after a preview of every file and line it will
    change. Files open in the editor are changed there (unsaved; Undo
    reverts), other files are saved straight away. Files in dependencies
    are left unticked in the preview. (fortls misses a function's result
    variable — `mean = …` inside `function mean` — so the GUI renames that
    too.)

  Unless the project has its own fortls settings (`.fortlsrc`,
  `.fortls.json` or `.fortls`), fortls indexes only the fpm folders (`src`,
  `app`, `test`, `example`, `include`, any `source-dir`) of the project and of
  each dependency, so e.g. a Python environment inside the project isn't
  searched. *View → Fortran Language Server* turns it off. fortls is part of
  the pixi environment; for a pip install, `pip install .[fortran]` adds
  fortls, fortitude and fprettify.
* AI code review, fully local: *AI → Review Current File* (⌥⌘R) sends the
  file in the editor (plus its compiler messages) to a model running on
  your machine through [Ollama](https://ollama.com) — nothing leaves the
  computer. Findings are listed in the *Review* tab (click to jump to the
  line), marked in violet in the editor's gutter, and the full answer
  streams into the Output panel. *AI → Model* picks any installed Ollama
  model (default `qwen2.5-coder:7b`, downloaded on first use after asking);
  *AI → Focus* steers the review (bugs, modern Fortran style, performance,
  documentation). The GUI starts `ollama serve` itself if it isn't running.
  Small local models are helpful but fallible — treat findings as hints.
  See [AI code review](#ai-code-review) below for setup and where models
  are stored.
* *Build outputs* tab: what fpm keeps in `build/`, one card per
  configuration (fpm writes a separate set of `<compiler>_<hash>` folders for
  every combination of settings and never removes old ones). Each card shows
  the settings it was built with (recorded when the GUI runs the build;
  older folders are grouped as "built outside the app"), when it was built,
  its size, the library and module counts, and its programs, tests and
  examples with a **Run** button (runs them directly, without rebuilding).
  Delete a configuration, or all except the current one, to free space.
* *Show Compile Command* (right-click a file in the Build tab or a source in
  the Project files tree): the exact compiler command from
  `build/compile_commands.json`, with a Copy button.
* Selecting a git dependency in the graph shows the exact commit fpm checked
  out (from `build/cache.toml`), linked to it on GitHub/GitLab.
* *Commands → Check for Dependency Updates…* (only when you ask): asks
  each git dependency's repository for its tags and branches
  (`git ls-remote`, nothing downloaded) and shows, per dependency, what is
  requested, the commit you have, the newest tag / branch commit and a
  status. A dependency pinned to an older tag gets **Update to vX** (changes
  just that tag in the `fpm.toml` that declares it, then offers Update deps)
  and **Compare** (the commits in between on GitHub/GitLab); one pinned to a
  commit (`rev = "…"`) can be switched to the newest tag the same way. After
  **Update deps**, the Output panel lists which dependencies moved to another
  commit.
* Dependency diagram (drawn with [pyqtgraph](https://www.pyqtgraph.org)) built
  from the main `fpm.toml` and the `fpm.toml` of every downloaded dependency
  (git, local path, registry and metapackages; dashed edges are dev/test
  dependencies; dashed nodes haven't been downloaded yet).
  * **Graph** menu to choose the layout (layered
    top-down or left-right, radial tree, circular, force-directed), the theme
    (auto, light, dark, blueprint, neon) and curved or straight edges. Layout
    changes animate; choices are remembered.
  * Zoom with the mouse wheel, pan by dragging the background, drag packages
    around. Click a package to highlight everything it depends on and
    everything that uses it (click the background to clear); double-click to
    open its `fpm.toml`. Export to PNG or SVG.
  * *Find package…* (top left of the graph, or ⌘F with the graph in front):
    typing highlights the packages whose names match; Enter selects and
    centers the next match, Esc clears.
  * Version conflicts: when packages ask for the same dependency in
    different ways (another tag, commit, branch, repository or path), it
    gets an orange **!** badge, the legend and status bar say so, and its
    details list who asks for what and which one fpm uses (the declaration
    closest to the project; fpm doesn't warn about the others).
  * Right-click a package for its menu: open its `fpm.toml`, show it in
    *Project Files* or Finder, *Open as Project in New Window* (e.g. to work
    on a local library while keeping the project open; *Open as Project Here*
    replaces it instead), open its repository, checked-out commit or homepage, *Check for
    Updates* or *Update This Dependency* (`fpm update <name>`), *Copy
    Dependency Line* (its declaration, ready to paste into another
    `fpm.toml`), and *Why Is This Here?*, which highlights every chain of
    dependencies from the project down to it.
* The graph refreshes automatically after commands that may download
  dependencies.

## Website

[web/index.html](web/index.html) is a one-page website about the app, with
screenshots in `web/img` (taken from the real app by
[web/screenshots.py](web/screenshots.py)).

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

## AI code review

The review runs entirely on your computer: [Ollama](https://ollama.com)
serves an open-weights model locally and the GUI talks to it over
`localhost`. No code is sent anywhere, and after the model has been
downloaded once it works offline.

### What's needed

* **Ollama** — the `ollama` server and the `ollama` Python package are
  included in the pixi environment (`ollama` and `ollama-python` from
  conda-forge). For a pip install, use `pip install .[ai]` and install the
  Ollama server from [ollama.com](https://ollama.com).
* **A model** — the default is `qwen2.5-coder:7b` (about 4.7 GB on disk,
  needs roughly 6 GB of free memory). It isn't part of the pixi environment;
  it's downloaded the first time you run a review (the GUI asks first and
  shows the download progress), or ahead of time with:

  ```sh
  pixi run ollama serve &                 # if Ollama isn't already running
  pixi run ollama pull qwen2.5-coder:7b
  ```

You don't need to start Ollama yourself: if no server is running, the GUI
starts `ollama serve` when you ask for a review and stops it again when you
quit (a server you started yourself is left alone). On Apple Silicon the
model runs on the GPU (Metal); a review of a typical source file takes
around 5–15 seconds.

### Where models are stored

Models are **not** stored in this repository or in the pixi environment.
Ollama keeps them in its own folder in your home directory:

| OS | Default location |
|---|---|
| macOS / Linux | `~/.ollama/models` |
| Windows | `%USERPROFILE%\.ollama\models` |

Inside it, `blobs/` holds the model data (large files named by checksum) and
`manifests/` records which blobs make up each model. Because the folder is
outside the project, deleting `.pixi` or re-running `pixi install` doesn't
remove downloaded models, and any other Ollama installation on the same
machine shares them.

To keep models somewhere else (e.g. an external drive), set
`OLLAMA_MODELS` before Ollama starts; the server the GUI starts inherits it:

```sh
export OLLAMA_MODELS=/Volumes/Data/ollama-models
pixi run start
```

### Managing models

```sh
pixi run ollama list                    # installed models and their sizes
pixi run ollama pull qwen2.5-coder:14b  # download another model
pixi run ollama rm qwen2.5-coder:7b     # delete a model and free the space
```

These talk to the Ollama server, so one must be running: either the one the
GUI starts for a review, or `pixi run ollama serve` in another terminal
(otherwise they fail with "could not connect to ollama"). Deleting a model
removes its files from the models folder above; it can be downloaded again
at any time. Deleting the model selected in *AI → Model* just means the GUI
offers to download it again the next time you start a review.

In the GUI, **AI → Model** lists the installed models and remembers your
choice; **AI → Model → Other Model…** accepts any name from the
[Ollama library](https://ollama.com/library) and offers to download it on
first use.

### Choosing a model

Bigger models give better reviews but need more memory and are slower.
Coding models that work well for this (all free to use):

| Model | Download | Memory needed (approx.) |
|---|---|---|
| `qwen2.5-coder:7b` (default) | 4.7 GB | 6 GB |
| `qwen2.5-coder:14b` | 9 GB | 11 GB |
| `qwen3-coder:30b` / `gpt-oss:20b` | 13–19 GB | 16–20 GB |

On a Mac, "memory" is the unified memory shared with the GPU. Even the
larger models make mistakes, especially with older Fortran — read the
findings as suggestions to check, not verdicts.
