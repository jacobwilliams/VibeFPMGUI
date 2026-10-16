"""The main window's parts: `vibefpmgui.main_window.MainWindow` is made of
these mixins, one per area of the window, which share the window's state
(``self``).

| Module | What it does |
|---|---|
| `vibefpmgui.window.panels` | The docks, their layout, and the editor in the middle |
| `vibefpmgui.window.view` | The View menu: text size, fonts, theme, colors, editor options |
| `vibefpmgui.window.project` | Opening a project, recent projects, the session, reloading |
| `vibefpmgui.window.packages` | The dependency graph: a package's details and menu, updates |
| `vibefpmgui.window.editor_tabs` | The editor's tabs: open, preview, close, rename, delete, go to |
| `vibefpmgui.window.editing` | The Edit menu, formatting, fortitude's fixes, line endings |
| `vibefpmgui.window.language` | The language server: symbols, references, rename |
| `vibefpmgui.window.search` | Find and Replace in Files, the Problems panel |
| `vibefpmgui.window.git` | The Git panel: reading the repository's status when it may have changed |
| `vibefpmgui.window.complexity` | The Complexity panel: measuring the project's code (again on a save) |
| `vibefpmgui.window.running` | Build options, fpm commands, features |
| `vibefpmgui.window.build_outputs` | The Build outputs panel, running a built program, compile commands |
| `vibefpmgui.window.output` | The Output panel |
| `vibefpmgui.window.testing` | Test and Run, in parallel too |
| `vibefpmgui.window.coverage` | Test coverage after a test run |
| `vibefpmgui.window.ai` | The AI menu, Ollama, reviews, the AI's threads |
| `vibefpmgui.window.ai_chat` | Explain, the chat, and the AI's edits |
"""
