# Ideas for future updates

Features that would make working with fpm easier, roughly grouped. Ticked items
are done. Effort is a rough guess: small / medium / large.

## Gaps worth filling first

- [x] **Project file tree** — a sidebar listing the project's files (`src/`,
  `app/`, `test/`, `example/`, …) and those of its dependencies; double-click to
  open in the editor. *(small–medium)*
- [ ] **Program and test pickers** — fill the Target field from
  `fpm run --list` / `fpm test --list` instead of typing, and support example
  programs (`fpm run --example`), which can't be run from the GUI yet. *(small)*
- [ ] **Input for interactive programs** — an input box under the Output panel
  so programs that read from the keyboard (stdin) don't hang. *(small)*
- [ ] **Problems list** — every warning and error from the last build in one
  list, with Next/Previous Problem (F8) to jump through them in the editor.
  *(small; the Build tab already collects them)*

## Creating and editing projects

- [ ] **New Project…** — a form around `fpm new` (library / app / tests /
  examples).
- [ ] **Add Dependency…** — pick a git repository and tag/branch (or a path), as
  a normal or dev dependency; writes `fpm.toml` without losing comments or
  formatting (`tomlkit`), then runs Update deps.
- [ ] **Form view of `fpm.toml`** — fields for name/version, `[build]`,
  `[fortran]`, `[preprocess]`, `[install]` and features, with tooltips and
  validation, as an alternative to editing the raw text.
- [ ] **Install / Publish** — `fpm install` with a prefix picker, and
  `fpm publish --dry-run`.

## Editing Fortran code

- [ ] **fortls (Fortran language server, on conda-forge)** — go to definition,
  hover documentation, completion, and an outline of the open file.
  *(the biggest editor improvement; large)*
- [ ] **fprettify (on conda-forge)** — Format File / Format Selection.
- [ ] **Search the whole project** — search across the project and its
  downloaded dependencies; results open in the editor.

## Testing

- [ ] **Re-run Failed Tests**, and right-click a test → *Run Only This Test*.
- [ ] **test-drive cases** — list the individual cases inside each test program
  (e.g. `mean`, `variance` in the demo's `check_stats`) instead of one pass/fail
  per program.
- [ ] **Coverage** — build with coverage flags, run the tests, and mark covered
  and uncovered lines in the editor gutter (`gcovr`, on conda-forge).

## Dependencies

- [ ] **Check for updates** — each git dependency's pinned tag next to its
  latest tag, with "update to latest", and what changed after Update deps.
- [ ] **License summary** — the licenses of all dependencies in one table (the
  graph already reads them).
- [ ] **Module dependency graph** — which Fortran modules use which (from
  `fpm build --dump` or by scanning `use` statements), to track down circular
  dependencies and slow rebuilds; complements the package graph.

## Convenience

- [ ] **Saved run configurations** — named presets of profile, compiler, flags,
  features and arguments per project, switchable from the toolbar.
- [ ] **Watch mode** — rebuild (or re-run tests) automatically when a file is
  saved.
- [ ] **Notifications** — a system notification when a long build or test run
  finishes while the app is in the background.
- [ ] **Command palette (⇧⌘P)** — type to find and run any action.
