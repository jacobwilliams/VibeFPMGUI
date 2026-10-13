# VibeFPMGUI code review

*Reviewed: 2026-10-12 · Scope: all of `vibefpmgui/` (34 modules, ~17,600 lines) and the test suite · No code was changed.*

## Summary

The code base is in good shape for its size. Module docstrings explain the
design, the tricky Qt/PySide behaviour is documented where it's worked
around, long-running work (search, AI review, dependency checks, lexing big
files) is already off the GUI thread, and the test suite is broad (296 tests,
84% line coverage) and runs headless in CI.

The main issues, most important first:

1. ~~**Two real bugs that users can hit today:** a shortcut assigned twice, so
   neither action works (§1.1), and quitting with a build running, then
   answering *No*, leaves the window running with the language server,
   terminal and linting shut down (§1.2).~~ ✅ Fixed.
2. ~~**Output can be corrupted** when a UTF-8 character is split between two
   reads of a process's output (§1.3).~~ ✅ Fixed.
3. **A few places block the GUI thread**: pasting into a busy terminal
   (§1.4), Ollama status checks (§2.1), fortitude/fprettify fixes and
   formatting (§2.2), and scanning build logs during a big build (§2.3).
4. **Work repeated on every keystroke or repaint** in big files: spelling
   squiggles, extra selections and the Problems panel (§2.4–2.6).
5. **`main_window.py` (3,470 lines) and `editor.py` (2,310 lines) do too
   much.** Splitting them along the seams that already exist would make
   further work much safer (§3.1).

Severity: **High** = wrong behaviour a user is likely to hit · **Medium** =
wrong behaviour in less common cases, or a noticeable slowdown · **Low** =
edge cases, robustness, tidiness.

---

## 1. Bugs

### ~~1.1 High — ⌘⇧O is assigned to two actions, so neither works~~

> ✅ **Done.** *Open fpm.toml in New Window…* is now ⌘⇧N (*Open File in
> Editor…* keeps ⌘⇧O); a test checks that no two window actions share a key.

`main_window.py:209` (*Open fpm.toml in New Window…*) and
`main_window.py:221` (*Open File in Editor…*) both use `Ctrl+Shift+O`.
When two actions in one window share a shortcut, Qt reports it as ambiguous
(`activatedAmbiguously`) and triggers **neither**.

*Fix:* give one of them another key (e.g. ⌘⇧N for the new window, or ⌘P
for *Open File*, as in VS Code). Add a test that collects every action's
shortcut in the window and asserts there are no duplicates. The rest of the
shortcuts were checked and are unique.

### ~~1.2 High — answering *No* to "fpm is running, quit?" leaves a broken window~~

> ✅ **Done.** `closeEvent` asks about unsaved files and the running command
> before shutting anything down; `open_manifest` asks about the running command
> before closing the project's files. Tests cover *No* (nothing changes) and
> *Yes* for both.

`closeEvent` (`main_window.py:1055–1090`) asks about unsaved files, then
**shuts down** the editors' linting, the language server, the search
threads and the terminal (lines 1064–1068), and only **then** asks whether
to stop the running fpm command. Answering *No* calls `event.ignore()`, but
the window stays open without fortls (no hover, completion or outline), with
a dead terminal and no linting.

`open_manifest` (`main_window.py:1135–1148`) has the same ordering
problem: all editors are closed before the "fpm is running" question, so
*No* keeps the old project but has already closed its files.

*Fix:* ask every question first (unsaved files, running process), and only
start shutting things down once the answer is "go ahead".

### ~~1.3 Medium — UTF-8 characters split across reads become "�"~~

> ✅ **Done.** The running command's output goes through an incremental UTF-8
> decoder (reset for each command, flushed when it finishes). A test runs a
> program that writes "°" in two halves, half a second apart.

`_read_output` (`main_window.py:2785`) decodes each chunk on its own with
`errors="replace"`. A multi-byte character (e.g. `°`, `µ`, `→` in compiler
messages or program output, or any non-English text) that straddles two
`readyRead` chunks is replaced by two "�". The same applies to test output
used for the Tests tab's per-test output.

*Fix:* keep a `codecs.getincrementaldecoder("utf-8")(errors="replace")` per
process and feed it the bytes, as `pyte.ByteStream` already does for the
terminal.

### ~~1.4 Medium — pasting into a busy terminal can freeze the app~~

> ✅ **Done.** `PtyShell.write` sends what the pty takes and keeps the rest,
> sent from a write `QSocketNotifier` as the shell reads. A test writes 1 MB to
> a pipe nobody reads: `write` returns at once, and every byte arrives once
> the other end reads.

`PtyShell.write` (`terminal.py:158–166`) loops on a non-blocking file
descriptor, and on `BlockingIOError` it just `continue`s. If the shell isn't
reading (a long-running command, or a big paste that fills the pty buffer),
this spins at 100% CPU **on the GUI thread** until the shell catches up.

*Fix:* keep a pending-write buffer and send it from a
`QSocketNotifier(fd, QSocketNotifier.Write)` when the fd is writable.

### ~~1.5 Medium — dialogs are never deleted~~

> ✅ **Done.** A `run_dialog()` helper runs a dialog and then `deleteLater()`s
> it. It's used for About, Compile command, Dependency updates, Go to Symbol,
> the rename preview and the Replace in Files confirmation. A test checks that
> no dialogs stay attached to the window.

`AboutDialog(…, self).exec()` (`main_window.py:880`), the compile command
dialog (`1387`), `UpdatesDialog` (`1432`), `SymbolSearchDialog` (`1803`) and
`RenamePreviewDialog` (`1839`) are parented to the window and never deleted.
Each use leaves a dialog, and its widgets, in memory until the app quits.
The update checker's dialog also keeps receiving results from its thread
after it's closed.

*Fix:* `dialog.setAttribute(Qt.WA_DeleteOnClose)`, or `dialog.deleteLater()`
after `exec()`.

### ~~1.6 Medium — a failed dependency check can leave its thread running forever~~

> ✅ **Done.** Any exception from one check is shown as that row's error, and
> `finished` is always emitted, so the thread quits. A test makes one check
> raise an `OSError`.

`CheckWorker.run` (`dep_updates.py:270–274`) calls `future.result()`.
`check_dependency` only catches `RuntimeError`. Anything else (an
`OSError` such as a permission error running `git`, or a decoding error)
propagates out of `run`, so `finished` is never emitted and the `QThread`
never quits.

*Fix:* catch `Exception` around each `future.result()` and report it as that
row's error, and emit `finished` in a `finally`.

### ~~1.7 Low — `git ls-remote` over SSH can wait for a prompt~~

> ✅ **Done.** `dep_updates.git_environment()` adds
> `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` (unless the user has their own
> `GIT_SSH_COMMAND` or `GIT_SSH`), so SSH fails at once instead of waiting out
> the timeout. Tried on a real SSH URL without a key in the agent: 0.5 s. Git's
> errors are now shown by their cause (the first line and the `fatal:` one),
> not the last line ("and the repository exists."), with a hint for SSH key and
> host-key failures.

`ls_remote` (`dep_updates.py:112`) sets `GIT_TERMINAL_PROMPT=0`, which stops
HTTPS password prompts but not SSH (host-key confirmation, key passphrase).
Those either fail or wait out the 30-second timeout per repository.

*Fix:* also set `GIT_SSH_COMMAND="ssh -o BatchMode=yes"` (unless the user
already set one).

### ~~1.8 Low — *AI Review* clicked twice while Ollama starts launches two servers~~

> ✅ **Done** (with §2.1). While a review is getting ready, *AI Review* is
> disabled and `start_review` ignores a second call; a test clicks it twice.

`_ensure_ollama` (`main_window.py:3151–3176`) has no "already starting"
guard. `start_review` only checks `_ai_thread`, which is still `None` while
the server starts, so a second click starts another `ollama serve` and
overwrites `_ollama_process`. The first one is then never terminated on
quit.

*Fix:* return early while `_ollama_process` is running or a poll is pending,
and disable the action meanwhile.

### ~~1.9 Low — the highlighter's thread can outlive its tab~~

> ✅ **Done.** Reproduced first (`RuntimeError: Signal source has been
> deleted` in the thread). `PygmentsHighlighter.close()`, called by the tab's
> `close_down()`, stops any further lexing; a thread still lexing then drops
> its answer, and also ignores the error if the highlighter was deleted without
> `close()`. Its two delayed calls (`QTimer.singleShot`) now name the
> highlighter as their context, so Qt cancels them if it's deleted first. A
> test covers both closing and plain deletion.

`PygmentsHighlighter._lex_in_thread` (`highlighter.py:357–367`) is a daemon
`threading.Thread` that emits `_lexed` when it's done. If the tab is closed
meanwhile (common for a big file opened by mistake), it emits on a deleted
QObject, which raises `RuntimeError` in that thread and prints a traceback.

*Fix:* catch `RuntimeError` around the emit, or check a "closed" flag that
`close_down()` sets.

### ~~1.10 Low — a reload from disk asks a modal question from a timer~~

> ✅ **Done.** No dialog any more: an orange bar at the top of the tab says the
> file changed on disk while it has unsaved edits, with **Reload** (one Undo
> step brings the edits back) and **Keep Mine** (that version isn't asked about
> again). A newer change while it waits replaces the offer; saving dismisses
> it. Tests check that no dialog opens.

`_check_disk` (`editor.py:2155–2182`) runs from a `QTimer` and can open
`QMessageBox.question` while another modal dialog (Save As, Rename, Replace
in Files…) is already open. That nests event loops, which is a common source
of hard-to-reproduce bugs.

*Fix:* defer the question until no modal is active
(`QApplication.activeModalWidget() is None`), or show a non-modal bar in the
tab ("Changed on disk — Reload / Keep mine"), as VS Code does.

### ~~1.11 Low — files with mixed line endings are normalised on save~~

> ✅ **Done.** The most common line ending wins (`editor.line_endings`): a
> mostly-LF file with one stray CRLF stays LF, so saving changes that one line,
> not all of them. A mixed file says so: "LF (mixed)" in the status line, with a
> tooltip on what saving does, and *Convert Line Endings* ticks neither until
> one is chosen. Choosing one (even the common one) counts as an unsaved change.

`EditorTab._read` (`editor.py:2059–2068`) sets `newline = "\r\n"` if the
file contains *any* `\r\n`, and saving then writes `\r\n` everywhere. A file
with mixed endings gets every line changed in git.

*Fix:* only use CRLF when every line ending is CRLF (or most of them), and
say "Mixed" in the status line otherwise.

### ~~1.12 Low — smaller items~~

> ✅ **Done.** All three below.

- ~~**Auto-indent and Backspace-over-indent take several Undo steps.**~~
  ✅ Fixed with View → Indentation (each is one edit block; tested).
  Return then `insertText(indent)` (`editor.py:1162–1168`), and Backspace
  deleting an indent one character at a time (`1169–1176`), aren't in an
  edit block.
- ~~**The full-output log is never removed.**~~ ✅ Deleted in `closeEvent` (tested).
  `vibefpmgui-output-<pid>.log` (`main_window.py:177`) stays in the temp
  folder after the app quits. Delete it in `closeEvent`.
- ~~**A local `import sys` shadows the module import.**~~ ✅ Removed. `main()`
  (`main_window.py:3454`) does `import sys` again; harmless, but confusing.

---

## 2. Performance

### ~~2.1 Medium — Ollama calls block the GUI thread~~

> ✅ **Done.** `ai.call_in_background(fn, then)` runs a blocking call on Qt's
> thread pool and hands the result back on the GUI thread. The Model menu shows
> the last known list (or "Checking…") at once and fills in when Ollama
> answers. Checking for the server, starting and polling it, and checking the
> model all go through it. A test makes Ollama take 0.8 s to answer and checks
> that the window doesn't wait.

`server_running()` (2 s timeout) and `installed_models()` (5 s timeout) in
`ai_review.py:229–249` are called on the GUI thread:
- `_fill_model_menu` (`main_window.py:3112`): every time the *AI → Model*
  menu opens;
- `_ensure_ollama` (`3153`): every 300 ms while the server starts;
- `_check_model` (`3180`).

If the server is up but slow, for example while loading a model, each call
freezes the window for up to 2–5 s.

*Fix:* run these on the existing worker pattern, or a `QThreadPool` task,
and update the menu or continue when they answer. For the menu, show the
cached list immediately and refresh it in the background.

### ~~2.2 Medium — *Fix All Issues* and *Format* run synchronously~~

> ✅ **Done.** They run on a pool thread (`background.call_in_background`, now
> shared with the AI review). The result is applied only if the file is still
> open and its text hasn't changed meanwhile; otherwise the status line says to
> try again. A second Format or Fix All on the same file while one runs is
> ignored. A test uses a slow fake fprettify.

`fix_lint_issues` and `format_file` (`main_window.py:2354–2396`) call
`lint.fix_text` and `formatter.format_text`, which use `subprocess.run`
(`lint.py:101`, `formatter.py:38`) on the GUI thread, behind a wait cursor.
For a big file (the 23,000-line one) this freezes the window for seconds.

*Fix:* use `QProcess` asynchronously, as `EditorTab.lint()` already does,
and apply the result only if the text hasn't changed meanwhile.

### ~~2.3 Medium — every "done." line in a build rescans the build folders~~

> ✅ **Done.** A `LogFinder` (in `build_monitor.py`) works out the log's
> name from the file's known source paths (fpm names it after the path relative
> to the project) and checks for it in each build folder: no listings. Only
> when that finds nothing does it list the folders, and it keeps each listing
> until that folder changes; the list of build folders is also kept until one
> is added. For 400 files × 6 configurations: 0.85 s before, 0.06 s now. Tests
> are in `tests/test_build_monitor_logs.py`.

`BuildMonitor._read_log` (`build_monitor.py:855–870`) runs for each
finished source file. Each time it globs `build/*_*/*`, then globs each of
those folders for `*<name>.o.log`. For a project with hundreds of sources
and several build configurations, that's quadratic directory listing on the
GUI thread while the build output streams. The *Build* tab falls behind the
build on large projects.

*Fix:* index the `.o.log` files once per build (or once per directory,
refreshed when its mtime changes) in a dict `{name: [paths]}`.

### ~~2.4 Medium — spelling squiggles look at every misspelled word on every repaint~~

> ✅ **Done.** `set_lint` keeps the misspelled words sorted by line; the
> squiggles `bisect` to the first visible line and stop at the first word
> below the area. A test with 2,000 misspelled words checks that only the
> visible lines are looked at.

`squiggle_spans` (`editor.py:1053–1090`) runs from `paintEvent`. That
includes cursor blinks with multiple cursors (every ~0.5 s), scrolling and
typing. It loops over **all** spelling issues (up to 2,000) and calls
`findBlockByNumber` + `blockBoundingGeometry` for each. Issues below the
visible area `continue` (line 1070) instead of ending the loop.

*Fix:* the issues are sorted by line, so `bisect` to the first visible line
and `break` at the first one below `area.bottom()`. The cost is then
proportional to what's on screen.

### ~~2.5 Medium — extra selections are rebuilt on every cursor move~~

> ✅ **Done.** The markers', issues' and gremlins' highlights are built by
> `_marker_and_issue_selections` and kept; they're rebuilt only when they change
> (or the colors or wrapping do). Cursor moves, extra cursors and find matches
> only redo their own parts. A test moves the cursor and checks no underline is
> rebuilt.

`_update_extra_selections` (`editor.py:788–824`) runs on every
`cursorPositionChanged`. It rebuilds the selections for **every** marker
line and **every** lint/fortls issue (a `QTextCursor` and format each), only
to move the current-line highlight. With a few hundred issues in a big file,
moving the cursor with the arrow keys does a lot of repeated work.

*Fix:* cache the marker and issue selections, rebuilt only when
`set_markers`, `set_lint` or `set_find_selections` change them, and add
just the current-line part on cursor moves.

### ~~2.6 Medium — spelling and Problems redo work when nothing changed~~

> ✅ **Done.** An unchanged spell check returns early. `check_spelling` asks
> the highlighter `is_current()` (no lexing waiting or running) instead of
> copying the text to compare (the revision can't be used: recoloring changes
> it). The Problems panel updates only while it's in front: it tracks Qt's
> `visibilityChanged`, since `isVisible()` isn't reliable for a panel behind
> another in a stack. It catches up when shown, and bursts of changes are
> combined into one update (150 ms).

- **Every relex triggers a full refresh chain.** After every highlighter
  relex (each typing pause), `check_spelling` → `_show_spelling`
  (`editor.py:2013–2029`) calls `_show_issues()` even if the new issue list
  equals the old one. That rebuilds all extra selections, repaints the
  gutter and minimap, and emits `problems_changed`.
- **The Problems tree is rebuilt even when hidden.** `problems_changed`
  makes the window rebuild the whole Problems tree (`_update_problems`,
  `main_window.py:1905`; `ResultTree.set_problems`,
  `result_tree.py:310`), whether or not the Problems panel is visible.
- **`check_spelling` copies the whole document each time.** It calls
  `toPlainText()` just to compare it with the highlighter's text.

*Fix:*
- skip `_show_issues` when the issues are equal;
- refresh the Problems panel lazily, when it's shown or when the current
  editor changes, with a short coalescing timer while it's visible;
- compare with a revision counter rather than the full text (the
  highlighter could record `document().revision()` alongside `_text`,
  ignoring its own formatting changes).

### ~~2.7 Medium — *Replace All* is quadratic in big files~~

> ✅ **Done.** `replacement_edits` counts lines as it goes, like `find_matches`;
> the unused `_position` helper is gone. A test does 60,000 replacements in a
> 20,000-line file in under a second and checks the positions.

`replacement_edits` (`project_search.py:227–242`) calls `_position()` for
each match. That counts newlines **from the start of the file** every time
(`text.count("\n", 0, offset)`), so thousands of matches in a large file
take O(matches × file size). `find_matches` right above it already counts
lines incrementally.

*Fix:* reuse `find_matches`'s incremental line counting (or precompute the
line-start offsets once and `bisect`).

### 2.8 Low — terminal resize reflows all 10,000 scrollback lines per resize event
`TerminalView.resizeEvent` → `_fit` → `TerminalScreen._reflow`
(`terminal.py:375–424`) joins and re-wraps the whole scrollback (up to
10,000 rows × up to 300 cells without word wrap) in Python. Dragging a dock
splitter sends dozens of resize events per second.

*Fix:* debounce `_fit` with a ~50 ms single-shot timer during resizes, and
consider a lower `SCROLLBACK`, or reflowing the scrollback lazily.

### 2.9 Low — the terminal repaints every cell of every visible row
`TerminalView.paintEvent` (`terminal.py:977–1011`) walks every cell of
every row in the dirty rectangle, calling `_selected()`, which sorts the
selection, per cell. Output triggers a whole-widget `update()` at most every
10 ms. Running something chatty (`cat` of a big file, a verbose build in the
terminal) uses a lot of CPU.

*Fix:* repaint only the rows pyte marks in `screen.dirty` (then clear it),
and normalise the selection once per paint.

### 2.10 Low — other notes
- **The LSP client sends the whole file on each change.** `change_document`
  (`lsp.py:528–536`) sends the full text 300 ms after each typing pause.
  For very big files, incremental `didChange` ranges would cut the JSON
  traffic; fortls supports them.
- **The LSP read buffer is re-sliced for every message.** `LspClient._read`
  (`lsp.py:471–491`) is O(n²) when many messages arrive in one chunk. Use
  an offset into the buffer and slice once at the end.
- **"Why Is This Here?" can be exponential in dense graphs.**
  `_why_is_this_here` (`main_window.py:1700–1715`) enumerates paths with no
  pruning, so in a dense graph where few paths reach the target it explores
  far more than the 50 chains it keeps. Prune with the set of nodes that can
  reach `name` (one reverse BFS).
- **`_remove_overlaps` is O(n² × 80) in pure Python.** In
  `graph_view.py:564–586` that's fine for typical fpm graphs (tens of
  packages), but slow for hundreds. Vectorise with NumPy, as `layout_force`
  already is.

---

## 3. Modularity and maintainability

### 3.1 `MainWindow` and `EditorTab` do too much
`main_window.py` is 3,470 lines; `MainWindow` alone covers:
- menus and the toolbar;
- docks and the layout;
- settings and per-project session state;
- running fpm and parsing its output;
- Test/Run orchestration;
- the full-output log;
- the dependency graph's details HTML, package menu and "why is this here";
- dependency updates;
- the AI review flow (Ollama start, model pull, review);
- language-server wiring;
- find/replace in files, rename symbol, file rename and delete.

`editor.py` similarly holds `CodeEditor` (painting, multi-cursor, hover,
context menu), `FindBar`, the outline delegate and `EditorTab` (file I/O,
lint, spelling, LSP features, status, problems).

There are natural seams for splitting them:

| New module | Takes from | What |
|---|---|---|
| `fpm_runner.py` | `MainWindow.run_fpm`, `run_program`, `_read_output`, `_append`, `_begin_output`/`_end_output`, `_finished`, `_flush_monitor`, `_test_line`, `kill_process_tree` | Owns the `QProcess`, output decoding, the output limit and the log file; emits signals (`line`, `finished`) the window connects to the Build/Tests/Run panels. Also removes the duplicated process setup between `run_fpm` and `run_program` (`2659` vs `1329`). |
| `ai_controller.py` | `_build_ai_menu` … `_shutdown_ai` (~260 lines) | The Ollama start/pull/review state machine. |
| `package_details.py` | `show_package`, `_why_is_this_here`, `package_menu`, `_dependency_line`, `toml_line`, `repo_web_url` etc. | Graph details HTML and actions; pure functions are easy to unit-test. |
| `layout.py` (docks) | `_make_dock`, `default_layout`, `_default_sizes`, `show_panel`, `_keep_on_screen`, `_panel_shown/_docked` | Panel management. |
| `file_actions.py` | `_replace_in_files` + `_preview_rename` (duplicated "apply edits to open editor or to the file on disk" loop), `rename_file`, `delete_file` | One `apply_edits_everywhere(plan) -> (written, changed_open, failed)` helper. |
| `editor_lsp.py` | `EditorTab`'s hover/definition/implementation/completion/signature/references/rename methods | An `EditorLanguageFeatures` helper object. |

### 3.2 Duplicated code
- **The per-project settings key is written out four times.**
  `hashlib.sha1(str(self.manifest_path).encode()).hexdigest()[:16]`
  appears in `_session_key`, `_config_records`, `_features_key` and
  `_durations_key`. Use one `_project_key(prefix)` helper.
- **Three file readers with slightly different rules:**
  - `MainWindow._read_source` (`main_window.py:1813`);
  - `EditorTab._read` (`editor.py:2059`);
  - `result_tree.read_text` (`result_tree.py:88`).

  All try UTF-8 then Latin-1, but differ in size limits, binary detection
  and line-ending handling. A single `read_source(path) -> (text, encoding,
  newline)` in one module would keep Search, Rename and the editor
  consistent.
- **"The word at the cursor" is implemented three times.**
  `EditorTab._name_at_cursor` (`editor.py:1839`), `CodeEditor.word_at`
  (`editor.py:557`) and `EditorTab._name_start` (`1729`) each scan for word
  boundaries with `WORD_RE`. Extract one `word_bounds(text, column)`
  function.
- **`go_to_definition` and `go_to_implementation` are almost identical**
  (`editor.py:1624–1671`): only the request and the message differ.
- **Fortran comment/string scanning is written three times:**
  `lsp.in_comment_or_string`, `lsp._code_spans` and `lsp.argument_index`.
  The highlighter's tokens already know where comments and strings are,
  which would also fix the cases these ignore (continuation lines, `;`).

### 3.3 Global mutable state
Settings are pushed into module-level and class-level globals:
- `EditorTab.lint_enabled`, `.spelling_enabled`, `.word_wrap`, …
- `hl.chosen_style`;
- `editor_mod.editor_font_family` and `editor_font_size`;
- `spelling.user_words` and `spelling.language`;
- `build_monitor.set_ui_scale`.

This works because each window runs in its own process, but:
- **Tests must reset it by hand.** The spelling tests already need a fixture
  to restore `spelling.language`.
- **The flow is hard to follow.** It isn't obvious which code reads which
  global, or when.
- **It blocks multi-window.** A future "several windows in one process"
  wouldn't be possible.

*Suggestion:* a small `AppSettings` object (a dataclass with a `changed`
signal) owned by the window and passed to the editor tabs and panels.

### 3.4 Private attributes used across classes
- ~~`hl._text`: `EditorTab.check_spelling`, `editor.py:2021`.~~ ✅ Now
  `hl.text` and `hl.is_current()`.
- `monitor._result`: `MainWindow._after_marked`, `main_window.py:2973`.
- `editor._compiler_markers`: `MainWindow._run_review`,
  `main_window.py:3214`.
- `self.project_tree._apply_marks()`: `main_window.py:797`.
- `client._client.close()`, the ollama package's internal httpx client:
  `ai_review.py:276`. This one can break on any ollama upgrade.

*Suggestion:* expose small public methods or properties for these, and pin
or wrap the ollama dependency with a fallback (e.g. a cancellable `httpx`
stream of the REST API, which is simple).

### 3.5 Attributes created outside `__init__`
Many attributes first appear deep inside methods and are read with
`getattr(self, "...", default)`:
- `MainWindow`: `_folder_snapshot`, `_marker_kind`, `_test_building`,
  `_revs_before`, `_process_label`, `_clearing_settings`,
  `_layout_restored`, `_update_check_thread`, `_review_text`,
  `_review_keys`;
- `EditorTab`: `_lint_text`, `_opened_text`;
- `FindBar`: `_matches_cache`;
- `TerminalView`: `_selecting`;
- `ScrollingTabBar`: `_wheel_rest`;
- `editor.close_button` and `editor.tab_icon`, set on another class's
  instance.

This makes the state of each object hard to see and hides typos. Declare
them all in `__init__`; type checkers and readers both benefit.

### 3.6 Dead code and leftovers
- **The fixed-tab separator is dead.** `_fill_tab_list`
  (`main_window.py:2168–2170`) still adds a separator after "the fixed
  tabs", which were removed with the `fpm.toml` tab.
- **A comment has two halves glued together.** `build_arguments`
  (`main_window.py:2544`):
  `# our runner wraps the user's  # the test runner is handled by run_tests`.
- **`lsp.code_html` has a stray blank line** inside the function
  (`lsp.py:230–231`).

---

## 4. Readability

- **Comments and docstrings are a real strength:** they explain *why*
  (PySide crash workarounds, macOS style quirks, fpm's behaviour). Keep
  that standard.
- **Signals carry untyped tuples in several places**, which callers unpack
  by position, e.g. `references_found = Signal(str, object)` with `[(file,
  line, start, end)]`, the result tree's `PARTS_ROLE` tuples, and outline
  rows `(depth, kind, name, first, last)`. `NamedTuple`s (as `Match` and
  `Problem` already are) would make those self-describing.
- **Some methods are long and mix concerns.** For example,
  `MainWindow._build_ui` (~400 lines), `run_fpm` and `show_package`. A
  `_build_*` method per panel and an HTML template helper would make them
  easier to follow.
- **Magic numbers are spread through drawing code.** Paddings, alphas and
  pixel sizes in `paint_gutter`, `_paint_squiggles`, the result-tree
  delegate and the minimap. Naming the shared ones (badge padding, gutter
  marker size) would keep the panels consistent.
- **Imports inside functions as a circular-import workaround.** For example,
  `from .editor import CHANGE_COLORS, …` inside `Minimap.paintEvent`, and
  `from . import highlighter` inside `lsp` functions. Moving the shared
  constants (marker colours, severities) into a small `styles.py` would
  remove both cycles.

---

## 5. Tests

Coverage is 84% overall. The least-covered modules are `compat.py` (71%),
`formatter.py` (72%), `manifest.py` (76%), `editor.py` (79%) and
`terminal.py` (79%).

Gaps worth closing (they match the bugs above):
- ~~**Shortcuts:** no two actions share a key (§1.1).~~ ✅ Added.
- ~~**`closeEvent`:** *No* at the running-process question leaves everything
  working (§1.2).~~ ✅ Added.
- ~~**Output decoding:** split a multi-byte character across two
  `_read_output` chunks (§1.3).~~ ✅ Added.
- **`manifest.py`:**
  - malformed manifests (wrong types for `dependencies`, `[preprocess]`,
    `[features]`);
  - cycles between path dependencies;
  - metapackages. This is the code every other feature trusts.
- **`replacement_edits`:** a large-file timing test, like the existing
  big-file highlighter tests (§2.7).

Test hygiene:
- **Tests can hang instead of failing.** Some tests open real dialogs if a
  code path changes. The new `no_unexpected_dialogs` fixture in
  `tests/test_file_operations.py` turns that into a failure; moving it to
  `conftest.py` as an autouse fixture (tests that expect a dialog patch it
  anyway) would protect the whole suite.

---

## 6. Suggested order of work

1. ~~§1.1 duplicate shortcut, §1.2 quit/open-project ordering, §1.3 output
   decoding. These are small, contained fixes with clear tests.~~ ✅ Done.
2. ~~§1.4 terminal writes, §1.5 dialog leaks, §1.6 update-check thread.~~ ✅ Done.
3. ~~§2.1–2.3 blocking calls on the GUI thread (Ollama, fix/format, build
   logs).~~ ✅ Done.
4. ~~§2.4–2.7 per-keystroke and per-repaint work in big files.~~ ✅ Done.
5. §3.1 split `fpm_runner.py` out of `MainWindow` first (it removes the
   duplicated process setup and makes §1.3 a one-place fix), then the AI
   controller and file actions. Do this behind the existing tests, one
   module at a time.
6. §3.2–3.6 and §4 opportunistically, when touching the code nearby.
