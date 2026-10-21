# VibeFPMGUI
A vibe-coded GUI for the [Fortran Package Manager](https://fpm.fortran-lang.org) (fpm),
written in Python with PySide6 (through the [qtpy](https://github.com/spyder-ide/qtpy) shim).

## Features

* A window of panels around the editor: *Build options* and *Project files*
  on the left, the *Dependency graph* and *Build outputs* on the right,
  *Build*, *Tests*, *Run*, *Problems*, *Review*, *References*, *Search* and
  *Terminal* at the bottom with *Output* beside them, the files open in the editor in
  the middle. Drag a panel by its title (or its tab) to the left, right or
  bottom, onto another to stack them as tabs, or out of the window to float
  it; double-click the title to float it or dock it again. Drag a panel's
  edge past its smallest size to collapse it (at the edge of a side, that
  whole side collapses, as in VS Code) to a thin grip; drag the grip back
  out (or double-click it) to open it.
  *View → Panels* shows or hides each one, and *Reset Layout* puts them all
  back. The
  layout is remembered.
* *File → Clear Settings and Quit…* forgets everything the app has saved
  (recent projects, open files, layout, options, View menu choices, your
  spelling dictionary) and quits, so the next start is like the first.
* Open any `fpm.toml` and run `fpm build`, `run`, `test`, `update` and `clean`
  from the toolbar or the *Commands* menu (Build ⌘B, Run ⌘R, Test ⌘U,
  Stop ⌘.; Ctrl on Linux/Windows), with live output and the usual options
  (profile, compiler, flags, target, extra arguments, program arguments).
  Hover an option to see the fpm flag it becomes. Build leaves the tests out
  (fpm builds them only for Test); **Build Tests** (⇧⌘B) builds them too
  (`fpm build --tests`), without running them.
* Build configurations: named sets of Build options, saved in `vibefpm.json`
  beside the project's `fpm.toml` (so they can be committed with it). The
  list at the top of *Build options* chooses one: its options are shown
  read-only (the fpm executable, a path on this computer, isn't part of
  one). *New configuration* is the options as you set them, editable;
  *Save…* adds what's shown to the file under a name, as a configuration
  from then on. *Edit* makes a saved configuration's options editable;
  *Save* puts the changes back in the file under its name, and *Cancel*
  drops them. Leaving it with changes not saved (choosing another, opening
  another project or configuration file, quitting) asks to save them first.
  *⋯* saves under another name (*Save As…*), edits a copy as New
  configuration, deletes one, shows the file in the editor, or opens
  another configuration file (any `.json` of them beside the `fpm.toml`). A
  project opened with *File → Open Project…* comes with its
  `vibefpm.json`. The configuration chosen is remembered for each project.
  The configurations are read again when the project reloads (after a
  build, say); saving the file in the editor applies it, taking the options
  changed there.

  The file's `options` are the GUI's settings for the project, one set for
  every configuration in it: the View menu's choices (word wrap, linting,
  minimap, indentation…), the editor's tabs (the files open, the one in
  front, where each is scrolled to; one row or several), the panels' layout
  and their own choices (Search, Problems, Complexity, Coverage, the graph's
  layout, theme and edges), and the configuration chosen. New
  configuration's Build options and features (those not saved as a
  configuration) aren't among the options but beside the configurations, as
  one without a name: `new_configuration`. While a project has a configuration file,
  these are read from it (the app's own settings for any it doesn't have
  yet) and written back when the window closes or another project is opened
  (only if they changed); open files inside the project are written relative
  to it, so the file works wherever the project is. Opening another project
  in the same window applies its options. Kept out of the file, for this
  computer: how the app looks (theme, window style, syntax colors, editor
  font, text size: yours, in every project), the window's size and place, the recent lists, the spelling
  dictionary, the AI choices and the fpm executable. A project without a
  configuration file uses the app's settings as before (nothing is written
  in it); saving its first configuration starts its options.

  ```json
  {"vibefpm": 1, "configurations": [
    {"name": "release (ifx)", "profile": "release", "features": [], "compiler": "ifx",
     "flags": "-O3", "target": "", "extra_args": "", "program_args": "",
     "parallel_tests": 4, "coverage": false, "verbose": false}],
   "new_configuration": {"profile": "", "features": [], "compiler": "", "flags": "-g", "…": "…"},
   "options": {"word_wrap": true, "minimap": false, "indent_width": 4, "…": "…",
     "session": {"files": [{"path": "src/main.f90", "line": 12, "column": 0, "scroll": 0}],
                 "current": "src/main.f90"}}}
  ```
  Very long output is cut short so the app stays responsive (an lfortran
  crash, for one, can print 100+ MB): after about 2 MB the Output panel
  stops, shows the last 200 lines when the command ends, and *Full Output…*
  shows the file the whole output was saved to.
  File locations in the output are links: click a runtime error's "At line
  1753 of file src/x.f90", a backtrace's "… at src/x.f90:12" or a compiler
  message's "src/x.f90:12:5:" to open the file at that line; in an ifx
  traceback, a row's line number and file (ifx gives only the file's name: the
  project's file of that name opens, a dependency's if it's only there).
  *File → New fpm Project…* makes a new project with `fpm new` and opens it:
  its name and folder, and its kind (*Full*, the default: an annotated
  `fpm.toml` and every folder, `--full`; only the folders chosen; or
  *Bare*), with *Backfill* to add only what an existing folder is missing;
  the dialog shows the command it will run.
  *File → Open Project…* (⌘O) opens a project's folder: with its
  `vibefpm.json` (its build configurations and options) if it has one, else
  its `fpm.toml`; a folder without an `fpm.toml` isn't a project, and says
  so. *File → Recent Projects* reopens any of the last 10 project folders
  (missing ones are greyed out); the last one is reopened at startup. Each project
  remembers the files open in the editor (with their cursor and scroll
  positions), the tab in front and the folders open in the *Project files*
  tree, and restores them when it is opened again. *File → Open Project in
  New Window…* (⇧⌘N) opens another project in its own window (a separate
  process, so each has its own builds and language server); such windows
  don't change which project is reopened at startup.
* *Build options → Features*: tick any of the features declared in the
  project's `[features]` table (the menu stays open to tick several) to
  build/run/test with `--features …` (remembered per project). The *Profile* list also offers the project's own `[profiles]`;
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
* *Complexity* panel: each procedure's complexity, as
  [lizard](https://github.com/terryyin/lizard) measures it, with a Fortran
  reader of its own: CCN (cyclomatic complexity), lines of code, tokens,
  parameters, length and nesting depth, most complex first, the values over
  lizard's thresholds (CCN 15, length 1000, parameters 100, depth 5) in red;
  and, above them, each file's totals. Filter by name or file, click a
  procedure to open it, click a file (or move to it with the arrow keys) to
  list only its procedures, its most complex one selected (click it again
  for all of them). Included files (`#include "…"`, `include '…'`) are
  read where they're included, so a procedure whose body is in one is
  measured and drawn whole. *Include tests* and *Include
  dependencies* widen it. It's measured again when a Fortran file is saved
  (only the files that changed are read again). Selecting a procedure
  draws its control-flow graph beside the tables: blocks of code,
  decisions (`if`, `select case`, `where`, computed and arithmetic `goto`
  …), loops with their way back, and `return` / `stop` exits, each arrow
  labelled with the way taken. Wheel to zoom, drag to pan, click a node to
  open its lines (a dashed node's are in an included file), double-click
  the background to fit it. Right-click for
  more: open or copy a node's code, center on it, fit, zoom, actual size,
  edge labels on or off, copy or save the graph as an image (PNG or SVG).
* *Git* panel: the project's repository's branch (with its upstream and
  how many commits ahead or behind), and its changed files: staged, not
  staged, untracked, in conflict. Select a file to see its diff, side by side
  (or *Unified*), syntax-colored, with unchanged stretches folded;
  double-click a line to open the file there. Right-click a file to show it
  in *Project files*, or *Discard Changes…* (asked first, saying what's
  lost): a change goes back to its staged version or the last commit's; a
  staged one to the last commit's; an untracked file (*Delete File…*), or a
  new one that was staged, is moved to the Trash. It refreshes when a file is
  saved, the project is reloaded, or the app is used again after another
  (git in a terminal), and with *Refresh*. Read with the
  [git](https://git-scm.com) program, which must be installed (on the
  PATH); without it the panel says so.
* Debugging, with gdb (14 or newer: Linux, and the [Docker image](docker/README.md)
  on a Mac) or lldb-dap, through their Debug Adapter Protocol. Click left of
  a line number (or F9) for a breakpoint; ⌘-click it (Ctrl-click on Linux
  and Windows, or Ctrl+F9) to disable it (kept, drawn hollow: the debugger
  doesn't stop there), and *Debug → Disable All Breakpoints* does it for
  every one (*Enable All Breakpoints* then). Grey: the debugger couldn't
  place it (e.g. no code on that line). Breakpoints move with your edits and
  are kept with the project. *Debug → Start Debugging* (F5) builds with the
  Build options (with the debug profile, for debug information, unless a
  profile or features are chosen) and runs the program under the debugger
  (several programs and no *Target*: it asks which). *Debug a Test…* and
  *Debug an Example…* do the same for the tests and examples, and a test's
  (or program's) right-click in the *Tests* (or *Run*) panel debugs that
  one; F5 then repeats what was debugged last. Where it stops, the
  file opens at the line, marked with an arrow; the *Debug* panel shows the
  call stack (click a frame to go there), every breakpoint in the project
  (its dot as in the gutter, its file and line, and its code; click one to
  go there; right-click to disable, enable or remove it, or all of them;
  the one the program stopped at is selected), the variables (arrays and
  derived types expand), and a console with the program's output and a field to
  evaluate an expression (`a(2:3)`, `p%x`). Continue (F5), Step Over (F10),
  Step Into (F11), Step Out (⇧F11), Pause, Stop (⇧F5). On macOS, lldb shows
  no variables of gfortran's programs: debug in the Linux container for those.
* Valgrind (Linux; in the project's pixi environment there): the
  **Valgrind** button beside each program, test and example in *Build
  outputs* (or *Run with Valgrind* on its right-click) builds it with the
  Build options (the debug profile, for files and lines, unless a profile or
  features are chosen) and has fpm run it under Valgrind's memory check:
  `fpm test NAME --runner "valgrind --leak-check=full
  --show-leak-kinds=definite,possible --track-origins=yes
  --expensive-definedness-checks=yes --num-callers=30 --error-exitcode=1"`
  (`fpm run`, `fpm run --example` for the others, with the Program args):
  the leaks that are lost (not the blocks libraries such as OpenMP keep to
  the end, "still reachable"), where an uninitialised value came from, and
  fewer false alarms from optimised code. The report (leaks, invalid reads
  and writes, each with its file and line) goes to the *Output* panel, and
  the status bar says how many errors it found. Where Valgrind isn't found (macOS, Windows) the
  button is disabled; on arm64 Linux Valgrind also needs the C library's
  debug symbols (`libc6-dbg` on Debian and Ubuntu; the Docker image has it).
* Test coverage: tick *Build options → Coverage* (gfortran) and run Test.
  fpm builds with `--coverage` (a build of its own), the tests' counts are
  read with gcov (which comes with gfortran) after the run, and the
  *Coverage* panel shows the totals
  (lines, functions, branches), each file's coverage and missed lines, and a
  selected file's functions with how often they were called; click a file
  to open it at its first missed line. The editor's gutter shows green bars
  for the lines the tests ran, red ones for those they didn't, and amber
  ones for lines that ran without taking every branch (hover for the
  counts; *View → Show Coverage in the Editor* hides them). Missed lines
  are also marked on the minimap; *Edit → Go To → Next / Previous Missed
  Line* (F6 / ⇧F6) jumps between them. Editing a file drops its bars until
  the next run. The project's own files are
  listed; *Include dependencies* adds the others.
* Parallel tests (off by default): *Build options → Parallel tests* sets how
  many tests run at once. fpm runs tests one after another, so then fpm only
  builds them and reports each test's command line, and the app runs them
  itself, each in its own process with its own output. The *Tests* tab
  shows every running test; the *Output* panel gets each test's output in
  one piece when it finishes (headed by its name, result, time and
  command), and clicking a test shows its output live while it runs. The
  longest tests (by their last time) start first.
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
  responsible `.gitignore` in the tooltip. Click a file (or move to it with
  the arrow keys) to preview it in the editor: its tab's name is in italics, and the next file clicked takes its
  place (as in VS Code); editing it, or double-clicking the file or the tab,
  keeps it open. Double-click a file to open it in the editor straight away;
  right-click for Open in Default App, Show in Finder, Copy
  Path, Rename… and Move to Trash… (a tab the file is open in follows the
  rename, unsaved changes included; moving a file with unsaved changes to
  the trash asks whether to save them first). Files with errors or warnings
  in the last build are shown in red / orange.
* *Run* tab: **Run** (toolbar) and the **Run** buttons in the *Build outputs*
  tab show each program running (spinner, time, exit code, output per
  program), like the Tests tab. Toolbar Run builds first (shown in the Build
  tab), then runs every program — or those matching *Target* — through the
  same marker wrapper as Test; run times are remembered for estimates.
* Terminal (*View → Terminal*, ⌃\` as in VS Code): your shell in the
  *Terminal* panel, started in the project's folder with exactly the
  environment VibeFPMGUI was started with (e.g. the activated pixi or conda
  environment), so `fpm` and the compilers are the ones the app uses. Your
  shell's startup files aren't read, as they would change that environment
  (so your prompt and aliases don't load). Colors, full-screen programs,
  Ctrl+C and Tab completion work; ⌘C/⌘V (macOS) or Ctrl+Shift+C/V copy and
  paste. *Restart* starts a new shell. The terminal emulation is
  [pyte](https://github.com/selectel/pyte)'s.
* Find in Files (⌘/Ctrl+Shift+F, or *Edit → Find → Replace in Files…*,
  ⌘/Ctrl+Shift+H): searches every file of the project and of its
  dependencies, in the *Search* tab, with options for match case, whole
  words and regular expressions, files to include or exclude (`*.f90`,
  `src/`), dependencies, and files ignored by git (left out by default, with
  fpm's `build` folder and compiled files). Files open in the editor are
  searched as they are, unsaved changes included, and the results follow
  your edits. Results are grouped by file, each line in the editor's syntax
  colors; click one to open the file with
  the match selected. *Replace All* (or *Replace* on one match or file, from
  its right-click menu) previews each change in the list, asks first, then
  changes open files in the editor (not saved; Undo reverts) and saves the
  others. With a regular expression, `\1` or `\g<name>` in the replacement
  is a group of the match.
* Built-in editor: double-clicking a file (a row in the Build tab, or a
  package in the graph for its `fpm.toml`) opens it in a tab with syntax
  highlighting chosen by file extension, line numbers, find/replace, go to
  line, undo/redo and save. *File → New File* (⌘N or Ctrl+N) opens an empty
  tab for a file not saved yet ("Untitled-1"): plain text, without syntax
  colors or checks, until saving it (Save As, in the project's folder by
  default) gives it a name and so a type; closing it with something typed
  asks whether to save it. With many files open the tabs scroll on one row
  (the arrow at its end lists them all), or, with *View → Editor Tabs →
  Several Rows (Wrap)*, wrap onto as many rows as they need. Files opened from the Build tab show the last
  build's warnings and errors as markers in the gutter (hover for the
  message) and open at the first one. A bar at the gutter's edge marks
  changed lines: amber for changes not saved yet, green for lines changed
  since the file was opened but saved; a small triangle marks where lines
  were deleted (hover them for details). A minimap at the right shows the
  whole file in miniature, in its syntax colors, with the change bars,
  warnings, errors, lint and AI-review markers and find matches; click it
  to jump, or drag the box (the part in view) to scroll. *View → Show
  Minimap* turns it off. Selecting text highlights its other occurrences in
  the file, matched as the find bar's *Match case* and *Whole words* are set
  (*View → Highlight Other Occurrences of the Selection* turns it off).
  *View → Word Wrap* (⌥/Alt+Z) wraps long lines at
  the window's edge instead of scrolling sideways (in the editor, the
  Terminal and the Output panel). Tab indents to the next tab stop (with
  several lines selected, it indents them; ⇧Tab takes an indent level off,
  spaces or a tab), and Backspace in leading spaces goes back one stop.
  *View → Indentation* chooses spaces or tab characters and the width
  (2, 3, 4 or 8; tabs in a file are shown that wide); the status line shows
  the setting. Files keep their line endings (LF or CRLF, shown in the
  status line; a file with both says "mixed" and is saved with its most
  common one); *Edit → Convert Line Endings* changes the whole file to Unix
  (LF) or Windows (CRLF) ones, saved with the next Save. Trailing spaces
  and tabs are trimmed when a file is saved (*Edit → Trim Trailing
  Whitespace on Save*, on by default; not Markdown or patch files, where
  they mean something); *Edit → Trim Trailing Whitespace* trims them now
  (Undo reverts). *Edit → Toggle Line Comment* (⌘/ or Ctrl+/) comments the
  selected lines out or back (`!` in Fortran, in the first column in fixed
  form; `#` in `fpm.toml`); *Edit → Lines* moves them up or down (⌥↑/⌥↓ or
  Alt+↑/↓) and sorts them; *Edit → Change Case* makes the selection (or the
  word at the cursor) UPPER, lower or Title Case. Multiple cursors: ⌥/Alt-click adds a
  cursor (again to remove it); ⌘⇧/Ctrl+Shift-click adds one on every line
  from the cursor to the clicked line: in the same column, or, clicked in
  another column, with each line selected between the two columns (a box
  selection). Typing, deleting,
  Enter, Tab, the arrow keys (with ⇧ to select), copy, cut and paste then
  work at all of them (pasting as many lines as there are cursors puts one
  at each); *Edit → Lines → Align Cursors* lines them up in the column of
  the right-most one, with spaces before the others (several on a line: the
  first of each line together, then the second…); Esc or a click goes back to
  one cursor. Saving the project's `fpm.toml`
  reloads the project. *View → Open Files in Built-in Editor* switches back
  to your default application. *View → Syntax Colors* picks any installed
  Pygments style (listed as light or dark) for the editor and `fpm.toml`;
  *Automatic*, the default, uses the theme's matching style, else `default`
  in light mode and `monokai` in dark mode. *View → Theme* sets the colors
  of the whole window: *System* (following macOS/Windows light or dark
  mode), *Light* or *Dark* in the platform's own look, or a colorful one
  (Nord, Dracula, Solarized Dark and Light, Gruvbox Dark and Light, One
  Dark, Ocean, Mint), which also brings its syntax colors when those are on
  *Automatic*. *View → Command Palette…* (⌘⇧P or Ctrl+Shift+P) finds any
  command in the menus by typing part of its name (letters in order:
  "mvup" finds *Move Line Up*), most recently used first. *View → Window Style* picks how buttons, tabs and the
  panels' dividers are drawn: *Automatic* (the platform's own style for
  System, Light and Dark; Fusion for the colorful themes, which need it for
  their colors), or any of Qt's styles on your platform (Fusion everywhere,
  with a dotted grip on the dividers; macOS on a Mac; windows11 or
  windowsvista on Windows…). *View → Editor Font…* picks the fixed-width font and size for
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
  right-click it → *Fix: …*, or right-click it in the *Problems* panel →
  *Apply Fix*) to apply just that fix, or use *Edit → Fix Issues (fortitude) →
  Fix All Safe Issues* for the whole file. *Safe* fixes don't change what the code
  does; *Edit → Fix Issues (fortitude) → Fix All Issues, Including Unsafe* also applies the others
  (marked "unsafe — check it"), which you should review. Either way it's a
  single Undo step and nothing is saved until you save.
* *Problems* panel: everything flagged in the file in the editor — the last
  build's compiler messages, fortitude's and fortls's issues, misspelled
  words, gremlins and AI review findings — in line order, each with its line number,
  a colored badge (the rule code or kind) and the message. Click one to go
  to it. The buttons at the top (with counts) show or hide each tool's
  problems. It follows the editor's current tab and updates as you type.
* Spell checking: misspelled words in comments and strings (all the text of
  plain text and Markdown files) get a violet wavy underline, checked with
  [pyspellchecker](https://github.com/barrust/pyspellchecker). Words that
  look like code (with digits, underscores or dots, ALL CAPS, camelCase),
  names used in the file's code, compiler directives (`!$omp …`) and Fortran
  keywords (`enddo`, `namelist`, …) aren't flagged. Hover a word or right-click it for
  suggestions and *Add to Dictionary* (your words are kept in the app's
  settings). *View → Check Spelling in Comments and Strings* turns it off;
  *View → Spelling Language* picks the dictionary (English by default, or
  Spanish, French, Italian, Portuguese, German, Russian, Arabic, Latvian,
  Basque or Dutch).
* Gremlins: characters that aren't plain printable ASCII — accented
  letters, `°`, smart quotes, non-breaking and zero-width spaces, control
  characters — get a little gremlin in the gutter of their line (hover it
  for what they are) and a tint on the character itself, so even invisible
  ones can be found; they're listed in the *Problems* panel too. Compilers
  often reject them outside comments and strings. *View → Show Gremlins*
  turns it off.
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
  * *Go to Implementation* (⌃F12) goes one step further for type-bound
    procedures: from `call shape%area()` to the procedure the binding names
    (Go to Definition stops at `procedure :: area => circle_area`); for a
    generic interface, to its first module procedure.
  * An outline drop-down above the editor lists the file's modules, types
    and procedures, follows the cursor, and jumps to the one you pick. A
    colored badge shows each one's kind (M module, P program, S subroutine,
    F function, T type, I interface), indented to show what is inside what.
  * *Edit → Go To → Go to Symbol in Project…* (⌘T) finds a module, type or
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
  * Go to Definition and Implementation, Find References and Rename Symbol
    are also at the top of the editor's right-click menu, for the name you
    right-click.
  * fortls's own checks of a file (when it's opened or saved), e.g. USE
    after IMPLICIT, a variable declared twice, a misplaced CONTAINS, are
    marked like fortitude's issues (in their own color, under *FORTLS* in the
    hover card). *View → Language Server Diagnostics* turns them off.
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
  searched. When an `fpm.toml` has a `[preprocess.cpp]` section, fortls is
  also given its `macros` (`{version}` filled in), `suffixes` and the
  packages' include folders, so `#ifdef` branches read as fpm compiles them;
  without one, fortls's defaults are used. *View → Fortran Language
  Server* turns it off. fortls is part of
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
  documentation). With text selected, only the selection is reviewed; a
  file too long for the model's context is reviewed a procedure at a time
  (the one at the cursor, which the entry says). *AI → Answer Length* sets
  how much the model may write
  (1,500 tokens by default, up to no limit; edits get at least 4,096), and
  *AI → Think Before Answering* lets a thinking model (qwen3, deepseek-r1…)
  reason first: slower, with its reasoning kept in the entry (*Show
  reasoning*) and given room of its own. It's off by default: a thinking
  model then answers straight away. The GUI starts `ollama serve` itself if it isn't running.
  Small local models are helpful but fallible — treat findings as hints.
  *Explain with AI* (right-click a compiler error, fortitude or fortls
  problem in the Problems panel, or a marked line in the editor) asks the
  same model what the message means, why it applies to that line and how to
  fix it, while you keep editing.
  The *Review* tab is a **timeline of everything the AI does**: each review,
  explanation, question and edit is an entry, newest at the bottom, and
  nothing replaces anything. Older entries fold to a one-line header (click
  to open one); filters show only reviews, explanations, chat or entries
  with edits still to accept. Everything in an entry stays usable: findings
  jump to their line or get *Fix with AI…*, *Show in Editor* puts a
  review's markers back, edits can still be accepted. The model remembers
  the timeline too, so you can ask about a review's findings or an
  explanation. *New Chat* draws a line (the model forgets what's above it;
  it stays in view), *Clear* empties it; it's not saved when you quit.
  The box at the bottom asks the model anything (Return to send; Up/Down
  bring back earlier questions), with the open file sent along unless you
  untick *Send the open file*; lines of the file an answer points to are
  listed and marked in the editor, as a review's findings are.
  Switch the box to **Edit** (or use *AI → Edit with AI…*, ⌥⌘E, or
  *Edit Selection with AI…* in the editor's right-click menu, or *Fix with
  AI…* on a problem or finding, which sends a request to address it) to ask
  for a change to the open file: the model proposes edits, each shown as a
  card with a diff and **Accept** / **Reject** (and *Accept All*). Accepting
  changes the editor's text, one step to undo; nothing is saved until you
  save. An edit is found by its text, not line numbers, so it still applies
  after other changes, and one that no longer matches says so instead of
  landing in the wrong place. Long conversations stay within the model's
  context: the file is sent again only when it changed, the oldest entries
  are left out first, and big files are sent in part (the selection or the
  procedure at the cursor, plus an outline).
  See [AI code review](#ai-code-review) below for setup and where models
  are stored.
* *Build outputs* panel: what fpm keeps in `build/`, one card per
  configuration (fpm writes a separate set of `<compiler>_<hash>` folders for
  every combination of settings and never removes old ones). Each card shows
  the settings it was built with (recorded when the GUI runs the build;
  older folders are grouped as "built outside the app"), when it was built,
  its size, the library and module counts, and its programs, tests and
  examples with a **Run** button (runs them directly, without rebuilding),
  a **Debug** button (as *Start Debugging*, for that one) and a
  **Valgrind** button (see Valgrind above).
  Right-click a program for *Run in Terminal* (types the command in the
  Terminal panel, for programs that read input), *Debug*, *Run with
  Valgrind*, *Show in Finder/Explorer* and *Copy Path*; right-click the card to show its build folder, copy its
  flags or delete it.
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
  dependencies, and a package only a dev-dependency has a **dev** tag;
  dashed nodes haven't been downloaded yet).
  * **Graph** menu to choose the layout (layered
    top-down or left-right, radial tree, circular, force-directed), the theme
    (auto, light, dark, blueprint, neon) and curved or straight edges. Layout
    changes animate; choices are remembered.
  * **Graph → Show Dependencies' Dev-Dependencies** also draws what each
    dependency lists as its dev-dependencies. fpm downloads only the
    project's own, so the ones nothing else uses are shown as *not
    downloaded* (a pale dashed box): their details say what's asked for, but
    there is no `fpm.toml` to open and nothing to update.
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

## Dependencies

`pixi install` (or any `pixi run …`) installs all of these; a pip install
gets the Python ones (`pip install ".[ai,fortran]"` for the optional ones),
and the others come from your system or conda.

| Dependency | Needed | Used for |
|---|---|---|
| [fpm](https://fpm.fortran-lang.org) | yes | Building, running, testing, updating and cleaning projects; reading their dependency tree |
| [gfortran](https://gcc.gnu.org/fortran/) | yes (or another compiler) | The default Fortran compiler fpm builds with |
| [Python](https://www.python.org) 3.11+ | yes | The app itself |
| [PySide6](https://doc.qt.io/qtforpython-6/) | yes | The Qt windows, widgets and processes |
| [qtpy](https://github.com/spyder-ide/qtpy) | yes | The Qt imports (one API over the Qt bindings; PySide6 is the one tested) |
| [pyqtgraph](https://www.pyqtgraph.org) | yes | Drawing the dependency graph |
| [NumPy](https://numpy.org) | yes | The dependency graph's force-directed layout (also needed by pyqtgraph) |
| [Pygments](https://pygments.org) | yes | Syntax colors in the editor, Search, References and Problems lists, and the style choices in *View → Syntax Colors* |
| [pathspec](https://github.com/cpburnz/python-pathspec) | yes | `.gitignore` matching: ignored files grayed in *Project files* and skipped by Find in Files |
| [pyte](https://github.com/selectel/pyte) | yes | The Terminal panel's terminal emulation |
| [pywinpty](https://github.com/andfoy/pywinpty) | Windows only | The Terminal panel's shell on Windows (a pseudo-console) |
| [pyspellchecker](https://github.com/barrust/pyspellchecker) | yes | Spell checking comments and strings in the editor |
| [fortls](https://fortls.fortran-lang.org) | optional | The Fortran language server: hover info, completion, go to definition/implementation, references, rename, outline, diagnostics |
| [fortitude](https://fortitude.readthedocs.io) (`fortitude-lint`) | optional | Linting Fortran files as you type, and its automatic fixes |
| [fprettify](https://github.com/fortran-lang/fprettify) | optional | **Format** (re-indenting Fortran files) |
| [LFortran](https://lfortran.org) | optional | Another compiler to build with (choose it in *Build options*) |
| [Ollama](https://ollama.com) (server and `ollama` Python client) | optional | AI code review with a local model |
| [pytest](https://pytest.org), [pytest-qt](https://pytest-qt.readthedocs.io), [pytest-timeout](https://github.com/pytest-dev/pytest-timeout), [pytest-cov](https://pytest-cov.readthedocs.io) | development | The tests, run in Qt's offscreen platform, and the coverage report |
| [Ruff](https://docs.astral.sh/ruff/) | development | Linting the app's Python code |
| [pdoc](https://pdoc.dev) | development | The API documentation website |

Without an optional tool the app still runs; only the features that use it
are unavailable. *Help → About VibeFPMGUI → Packages…* lists everything
installed in the environment the app runs in (from `pixi list` or `conda
list`, or the Python packages otherwise), with versions, builds, channels
and licenses; *Copy List* copies it for a bug report.

## Tests

```sh
pixi run test                       # all of them (pytest)
pixi run test tests/test_editor.py  # one file; add -k word_wrap for one test
pixi run coverage                   # all, with a code coverage report in web/coverage
```

To run them (or the app) on Linux from a Mac, as CI does, use the Docker
image in [docker/](docker/README.md).

The tests in [tests/](tests) drive the real windows with
[pytest-qt](https://pytest-qt.readthedocs.io). They use Qt's offscreen
platform, so no windows appear and they also run on CI machines without a
display (they run on every push), and a settings file of their own, so your
settings are never touched. To watch them, set `QT_QPA_PLATFORM=cocoa`
(macOS), `xcb` (Linux) or `windows`. CI publishes the
[test coverage report](https://jacobwilliams.github.io/VibeFPMGUI/coverage/)
with the website; it's linked from the API docs too.

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
quit (a server you started yourself is left alone); *AI → Stop Ollama
Server* stops it sooner, freeing the memory the loaded model holds (it
starts again when the AI is next used). On Apple Silicon the
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
