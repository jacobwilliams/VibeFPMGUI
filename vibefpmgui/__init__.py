"""A desktop GUI for the Fortran Package Manager (fpm).

> [!NOTE]
> These are the **API docs** for the `vibefpmgui` Python package, generated
> with [pdoc](https://pdoc.dev) from the docstrings in the source code. They
> describe the code; for what the app does and how to use it, see the
> [website](https://jacobwilliams.github.io/VibeFPMGUI/) or the
> [README](https://github.com/jacobwilliams/VibeFPMGUI#readme).

VibeFPMGUI opens an `fpm.toml` and runs fpm's commands (build, run, test,
update, clean) with live progress, shows the project's dependencies as an
interactive graph, and has a built-in Fortran editor with a language server,
linting, formatting and an AI code reviewer that runs on your own computer.
It is written in Python with Qt (PySide6, through qtpy).

*VibeFPMGUI is an experimental, vibe-coded project: expect rough edges.*

## Links

* **Website:** <https://jacobwilliams.github.io/VibeFPMGUI/>
* **Source code and issues:** <https://github.com/jacobwilliams/VibeFPMGUI>
* **fpm:** <https://fpm.fortran-lang.org>
* Tools it uses: [fortls](https://fortls.fortran-lang.org) (language server),
  [fortitude](https://fortitude.readthedocs.io) (linter),
  [fprettify](https://github.com/fortran-lang/fprettify) (formatter),
  [Ollama](https://ollama.com) (local AI models),
  [pyqtgraph](https://www.pyqtgraph.org) (graph) and
  [Pygments](https://pygments.org) (syntax highlighting).

## Running it

```sh
pixi run start                      # reopens the last project
pixi run start path/to/fpm.toml     # or a project folder
pixi run demo                       # the demo project in examples/demo
```

With pip (Python 3.11+): `pip install .` (add `.[fortran]` for fortls,
fortitude and fprettify, `.[ai]` for the Ollama client), then
`vibefpmgui [path/to/fpm.toml]`. fpm and a Fortran compiler must be on the
`PATH`. The program starts in `vibefpmgui.main_window.main`.

The editor tools are optional. Without fortls or fortitude their *View*
menu item says "not found" and the feature is off; without fprettify the
*Format* button is greyed out; AI review needs Ollama only when you start one.

## Project structure

**Main window**

| Module | What it does |
|---|---|
| `vibefpmgui.main_window` | The window: starting it, building it in order, closing it, and `main` |
| `vibefpmgui.window` | The window's parts, one mixin per area: panels, View menu, project, graph, editor tabs, Edit menu, language server, search, running fpm, Output, Test and Run, coverage, AI |
| `vibefpmgui.dock_snap` | Dragging a panel's edge past its smallest size collapses it, as in VS Code |
| `vibefpmgui.manifest` | Reading `fpm.toml` files and resolving the dependency tree (no Qt) |
| `vibefpmgui.graph_view` | The interactive dependency graph (pyqtgraph) |
| `vibefpmgui.project_tree` | The *Project files* tree, with fpm folder badges and `.gitignore` handling |
| `vibefpmgui.build_outputs` | The *Build outputs* tab: what fpm keeps in `build/` |
| `vibefpmgui.dep_updates` | *Check for Dependency Updates* (`git ls-remote`) |
| `vibefpmgui.about` | The About window |
| `vibefpmgui.packages` | *About → Packages…*: the environment's installed packages (`pixi list` / `conda list`) |

**Build, test and run panels**

| Module | What it does |
|---|---|
| `vibefpmgui.build_monitor` | The *Build* tab: a live table of the files fpm compiles, and compiler-message parsing |
| `vibefpmgui.test_monitor` | The *Tests* tab: each test's progress, time and output |
| `vibefpmgui.run_monitor` | The *Run* tab (the Tests tab with different wording) |
| `vibefpmgui.test_runner` | The small wrapper given to `fpm test --runner` to mark where each test starts and ends |
| `vibefpmgui.parallel_tests` | Running tests several at a time, each with its own output |
| `vibefpmgui.fpm_args` | fpm's command line: link flags, `--runner`, the test wrapper's command |
| `vibefpmgui.processes` | Ending a process and everything it started |
| `vibefpmgui.ansi` | Terminal color codes in program output, shown as colors |
| `vibefpmgui.output_links` | File locations in the output (runtime errors, backtraces, compiler messages) that open the file on a click |
| `vibefpmgui.terminal` | The *Terminal* tab: your shell in the project's folder, with exactly the app's environment (pyte, drawn by our own widget) |

**Editor**

| Module | What it does |
|---|---|
| `vibefpmgui.editor` | The editor tab: gutter markers, find/replace, hover popup, outline, saving |
| `vibefpmgui.highlighter` | Pygments syntax highlighting and the *Syntax Colors* styles |
| `vibefpmgui.themes` | *View → Theme*: the whole window's colors (System, Light, Dark and colorful ones) |
| `vibefpmgui.lsp` | The client for the fortls language server (hover, definition, completion, references, rename) |
| `vibefpmgui.completion` | The completion list and argument-hint popups |
| `vibefpmgui.hover_card` | The HTML of the hover card |
| `vibefpmgui.symbol_search` | *Go to Symbol in Project* |
| `vibefpmgui.references_panel` | The *References* tab |
| `vibefpmgui.result_tree` | The list of places in files shared by the *Search*, *References* and *Problems* tabs (icons, syntax-colored lines) |
| `vibefpmgui.problems_panel` | The *Problems* tab: everything flagged in the file in the editor |
| `vibefpmgui.project_search` | *Find in Files*: search and replace across the project and its dependencies (*Search* tab) |
| `vibefpmgui.rename_dialog` | The *Rename Symbol* preview |
| `vibefpmgui.lint` | Linting with fortitude |
| `vibefpmgui.formatter` | Formatting with fprettify |
| `vibefpmgui.spelling` | Spell checking comments and strings (pyspellchecker) |
| `vibefpmgui.gremlins` | Finding non-ASCII and invisible characters, and drawing the gutter's gremlin |

**AI review**

| Module | What it does |
|---|---|
| `vibefpmgui.ai_review` | Prompting a local model through Ollama and parsing its findings |
| `vibefpmgui.review_monitor` | The *Review* tab: a timeline of every review, explanation, question and edit |
| `vibefpmgui.ai_edits` | The AI's proposed edits (search/replace blocks): finding and applying them |
| `vibefpmgui.ai_context` | What a chat sends the model, so it fits its context window |
| `vibefpmgui.edit_cards` | The proposed edits as cards to accept or reject |

**Shared helpers**

| Module | What it does |
|---|---|
| `vibefpmgui.icons` | Toolbar icons, symbol badges and the app icon, drawn with QPainter |
| `vibefpmgui.coverage` | Test coverage: gcov's counts collected and read |
| `vibefpmgui.coverage_panel` | The *Coverage* tab: totals, each file's coverage, its missed lines and functions |
| `vibefpmgui.widgets` | Small shared widgets (a status line that never widens the window), and running a dialog |
| `vibefpmgui.background` | Blocking calls (Format, Fix All, asking Ollama) on a pool thread, the result back in the GUI thread |
| `vibefpmgui.compat` | Things that differ between macOS, Linux and Windows |

## How it fits together

* **fpm runs as a child process** (`QProcess`). Its output goes to the
  *Output* panel and is parsed line by line for the *Build* tab. fpm only
  prints compiler output when a compile fails, so warnings are read from
  its per-file `.o.log` files.
* **Tests and programs run through `vibefpmgui.test_runner`**, which fpm
  calls for each one (`--runner`). The markers it prints tell the *Tests*
  and *Run* tabs where each starts and ends and how it exited. For tests
  run in parallel, it only reports each test's command line
  (``--collect``), and `vibefpmgui.parallel_tests.ParallelTests` runs them.
* **The editor's Fortran features use separate programs:** fortls (a
  long-running language server, see `vibefpmgui.lsp.LspClient`), fortitude
  and fprettify (run on the editor's text through stdin), and Ollama (over
  HTTP, on a worker thread). None of them blocks the window.
* **Settings** are stored with `QSettings`, some per project (keyed by the
  manifest's path). Set the `VIBEFPMGUI_SETTINGS` environment variable to an
  `.ini` path to use a separate settings file, e.g. for testing.

## Development

```sh
pixi run ruff check .     # lint the code
pixi run test             # the tests (pytest + pytest-qt; no windows appear)
pixi run coverage         # the same, with a code coverage report in web/coverage
pixi run docs             # these API docs, into web/api
pixi run docs-serve       # the same, live in the browser
```

The tests in `tests/` drive the real windows with
[pytest-qt](https://pytest-qt.readthedocs.io), using Qt's offscreen platform
(so they also run on CI machines without a display) and a settings file of
their own (your settings are never touched). To watch them run, set
`QT_QPA_PLATFORM=cocoa` (macOS), `xcb` (Linux) or `windows`. CI runs them on
every push and publishes the [test coverage report](../coverage/index.html)
(which lines of the code the tests run) next to these docs.

Docstrings are Markdown with Google-style sections (`Args:`, `Returns:`,
`Raises:`); a name in backticks, such as `vibefpmgui.lsp.LspClient`,
becomes a link here.

MIT License. The fpm logo is © 2020 fpm contributors (MIT License). The
Fortran file icon is adapted from [vscode-icons](https://github.com/vscode-icons/vscode-icons)
(MIT License).
"""

__version__ = "0.0.5"
__docformat__ = "google"  # docstrings: Markdown with Google-style sections (for pdoc)
