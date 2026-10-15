"""The Edit menu, and the commands that rewrite a file (format, fix, line endings)."""

from __future__ import annotations

import sys

from qtpy.QtGui import QAction, QKeySequence
from qtpy.QtWidgets import QApplication, QMessageBox

from .. import formatter, lint
from ..background import call_in_background
from ..editor import EditorTab


class EditingMixin:
    """The Edit menu and the commands that rewrite a file (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_edit_menu(self):
        menu = self.menuBar().addMenu("&Edit")

        def focused(method):
            """Apply to the widget with focus (an editor, a text field…)."""
            def run():
                widget = QApplication.focusWidget()
                if widget is not None and hasattr(widget, method):
                    getattr(widget, method)()
                elif self.current_editor() is not None:
                    getattr(self.current_editor().editor, method)()
            return run

        for item in [("Undo", QKeySequence.Undo, "undo"),
                     ("Redo", QKeySequence.Redo, "redo"), None,
                     ("Cut", QKeySequence.Cut, "cut"),
                     ("Copy", QKeySequence.Copy, "copy"),
                     ("Paste", QKeySequence.Paste, "paste"),
                     ("Select All", QKeySequence.SelectAll, "selectAll")]:
            if item is None:
                menu.addSeparator()
                continue
            text, key, method = item
            action = menu.addAction(text)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(focused(method))
        menu.addSeparator()

        def editor_action(text, key, slot, into=None):
            action = (into or menu).addAction(text)
            action.setShortcut(QKeySequence(key))
            action.triggered.connect(lambda: self._with_editor(slot))
            return action

        # Find, Go To and fortitude's fixes in submenus (the menu was getting long).
        find_menu = self.find_menu = menu.addMenu("Find")  # (kept: PySide would let it go)
        self.find_action = editor_action("Find…", QKeySequence.Find, lambda e: e.find_bar.open_bar(), find_menu)
        # With the dependency graph in front, Find goes to its package search.
        self.find_action.triggered.disconnect()
        self.find_action.triggered.connect(self._find)
        self.replace_action = editor_action("Find and Replace…", "Ctrl+Alt+F",
                                            lambda e: e.find_bar.open_bar(replace=True), find_menu)
        self.find_next_action = editor_action("Find Next", QKeySequence.FindNext,
                                              lambda e: e.find_bar.find_next(), find_menu)
        self.find_previous_action = editor_action("Find Previous", QKeySequence.FindPrevious,
                                                  lambda e: e.find_bar.find_previous(), find_menu)
        find_menu.addSeparator()
        self.find_files_action = find_menu.addAction("Find in Files…")
        self.find_files_action.setShortcut(QKeySequence("Ctrl+Shift+F"))
        self.find_files_action.setStatusTip("Search all the files of the project and its dependencies "
                                            "(Search tab)")
        self.find_files_action.triggered.connect(lambda: self.find_in_files())
        self.replace_files_action = find_menu.addAction("Replace in Files…")
        self.replace_files_action.setShortcut(QKeySequence("Ctrl+Shift+H"))
        self.replace_files_action.setStatusTip("Search and replace across the project and its "
                                               "dependencies (Search tab)")
        self.replace_files_action.triggered.connect(lambda: self.find_in_files(replace=True))
        go_menu = self.go_menu = menu.addMenu("Go To")  # (kept: PySide would let it go)
        self.goto_action = editor_action("Go to Line…", "Ctrl+L", lambda e: e.go_to_line_dialog(), go_menu)
        self.symbol_action = go_menu.addAction("Go to Symbol in Project…")
        self.symbol_action.setShortcut(QKeySequence("Ctrl+T"))
        self.symbol_action.setStatusTip("Find a module, type or procedure in the project "
                                        "and its dependencies")
        self.symbol_action.triggered.connect(self.go_to_symbol)
        go_menu.addSeparator()
        self.definition_action = editor_action("Go to Definition", "F12", lambda e: e.go_to_definition(), go_menu)
        self.definition_action.setStatusTip("Open the definition of the name at the cursor "
                                            "(or ⌘/Ctrl-click the name)")
        self.implementation_action = editor_action("Go to Implementation", "Ctrl+F12",
                                                   lambda e: e.go_to_implementation(), go_menu)
        self.implementation_action.setStatusTip("For a type-bound procedure, open the procedure it binds; for a "
                                                "generic interface, its first module procedure")
        go_menu.addSeparator()
        self.back_action = go_menu.addAction("Go Back")
        self.back_action.setShortcut(QKeySequence(QKeySequence.Back))
        self.back_action.triggered.connect(lambda: self._go_history(back=True))
        self.forward_action = go_menu.addAction("Go Forward")
        self.forward_action.setShortcut(QKeySequence(QKeySequence.Forward))
        self.forward_action.triggered.connect(lambda: self._go_history(back=False))
        self._back: list[tuple[str, int, int]] = []     # where Go to Definition jumped from
        self._forward: list[tuple[str, int, int]] = []
        go_menu.addSeparator()
        self.next_missed_action = editor_action("Next Missed Line", "F6", lambda e: self._go_to_missed(e, True),
                                                go_menu)
        self.next_missed_action.setStatusTip("Go to the next lines the last test run didn't run "
                                             "(Build options → Coverage)")
        self.previous_missed_action = editor_action("Previous Missed Line", "Shift+F6",
                                                    lambda e: self._go_to_missed(e, False), go_menu)
        self.previous_missed_action.setStatusTip("Go to the previous lines the last test run didn't run")
        menu.addSeparator()
        self.references_action = editor_action("Find References", "Shift+F12", lambda e: e.find_references())
        self.references_action.setStatusTip("List every use of the name at the cursor (References tab)")
        self.rename_action = editor_action("Rename Symbol…", "F2", lambda e: e.rename_symbol())
        self.rename_action.setStatusTip("Rename the name at the cursor everywhere it is used "
                                        "(with a preview)")
        # ⌃Space on macOS (⌘Space is Spotlight), Ctrl+Space elsewhere.
        self.complete_action = editor_action("Show Completions",
                                             "Meta+Space" if sys.platform == "darwin" else "Ctrl+Space",
                                             lambda e: e.request_completion(manual=True))
        self.complete_action.setStatusTip("Offer completions for the name being typed "
                                          "(they also appear by themselves as you type)")
        menu.addSeparator()
        self.format_action = editor_action("Format with fprettify", "Ctrl+Shift+I", self.format_file)
        self.format_action.setIconText("Format")
        keys = self.format_action.shortcut().toString(QKeySequence.NativeText)
        self.format_action.setToolTip(f"Format ({keys}) — tidy the Fortran file in the editor "
                                      "with fprettify (undo to revert)")
        self.format_action.setStatusTip("Re-indent and re-space the Fortran file in the editor "
                                        "with fprettify")
        fix_menu = self.fix_menu = menu.addMenu("Fix Issues (fortitude)")  # (kept: PySide would let it go)
        self.fix_safe_action = editor_action("Fix All Safe Issues", "",
                                             lambda e: self.fix_lint_issues(e, unsafe=False), fix_menu)
        self.fix_safe_action.setStatusTip("Apply fortitude's safe fixes (they don't change what the "
                                          "code does) to the file in the editor; Undo reverts")
        self.fix_all_action = editor_action("Fix All Issues, Including Unsafe", "",
                                            lambda e: self.fix_lint_issues(e, unsafe=True), fix_menu)
        self.fix_all_action.setStatusTip("Also apply fortitude's unsafe fixes, which can change what the "
                                         "code does: check them before saving; Undo reverts")
        menu.addSeparator()
        self.line_endings_menu = menu.addMenu("Convert Line Endings")  # (kept: PySide would let it go)
        self.line_ending_actions: dict[str, QAction] = {}
        for newline, text, tip in (("\n", "To Unix Line Endings (LF)", "macOS and Linux"),
                                   ("\r\n", "To Windows Line Endings (CRLF)", "Windows")):
            action = self.line_endings_menu.addAction(text)
            action.setCheckable(True)
            action.setStatusTip(f"Save every line of the file in the editor with {tip}'s line ending")
            action.triggered.connect(lambda _=False, n=newline: self.convert_line_endings(n))
            self.line_ending_actions[newline] = action
        self.line_endings_menu.aboutToShow.connect(self._check_line_endings)

    def _graph_in_use(self) -> bool:
        """Find goes to the graph's search box: the graph panel has the focus,
        or it's shown and no file is open."""
        dock = self.docks["graph"]
        focus = QApplication.focusWidget()
        if focus is not None and dock.isAncestorOf(focus):
            return True
        return self.current_editor() is None and self.panel_in_front(self._content(dock))

    def _find(self):
        if self._graph_in_use():
            self.show_panel(self._content(self.docks["graph"]))
            self.graph_view.focus_search()
        else:
            self._with_editor(lambda e: e.find_bar.open_bar())

    def _update_editor_actions(self, *_):
        has_editor = self.current_editor() is not None
        for action in (self.save_action, self.save_as_action, self.close_tab_action,
                       self.find_action, self.replace_action, self.find_next_action,
                       self.find_previous_action, self.goto_action, self.next_missed_action,
                       self.previous_missed_action):
            action.setEnabled(has_editor)
        graph = getattr(self, "docks", {}).get("graph")  # (the panels exist)
        self.find_action.setEnabled(has_editor or (graph is not None and self.panel_in_front(self._content(graph))))
        editor = self.current_editor()
        self.format_action.setEnabled(editor is not None and formatter.can_format(editor.path)
                                      and formatter.executable() is not None)
        for action in (self.fix_safe_action, self.fix_all_action):
            action.setEnabled(editor is not None and editor.can_lint())
        self.line_endings_menu.setEnabled(editor is not None)
        for action in (self.definition_action, self.implementation_action, self.references_action,
                       self.rename_action, self.complete_action):
            action.setEnabled(editor is not None and editor.lsp is not None)
        self.symbol_action.setEnabled(self.lsp_client.is_running())
        self.back_action.setEnabled(bool(self._back))
        self.forward_action.setEnabled(bool(self._forward))
        self._update_review_actions()

    def _rewrite_text(self, editor: EditorTab, doing: str, rewrite, done):
        """Run ``rewrite(text) -> (new text or None, error)`` (a program: it can
        take seconds on a big file) on a pool thread, then ``done(text, new,
        error)`` in the GUI thread, if the file is still open and unchanged
        (otherwise the result would undo what was typed meanwhile)."""
        if editor.rewriting:
            self.statusBar().showMessage(f"{editor.path.name}: still {doing}…", 3000)
            return
        editor.rewriting = True
        text = editor.editor.toPlainText()
        self.statusBar().showMessage(f"{doing.capitalize()} {editor.path.name}…")

        def finished(result):
            editor.rewriting = False
            if editor not in self.editors():
                return  # (closed meanwhile)
            if editor.editor.toPlainText() != text:
                self.statusBar().showMessage(f"{editor.path.name} changed while {doing}: nothing was changed "
                                             "(try again)", 6000)
                return
            done(text, *result)

        call_in_background(lambda: rewrite(text), finished, self)

    def convert_line_endings(self, newline: str):
        """Edit → Convert Line Endings: the whole file, saved with these from now on."""
        editor = self.current_editor()
        if editor is None:
            return
        kind = "Unix (LF)" if newline == "\n" else "Windows (CRLF)"
        if editor.convert_line_endings(newline):
            self.statusBar().showMessage(f"{editor.path.name}: {kind} line endings (not saved yet)", 5000)
        else:
            self.statusBar().showMessage(f"{editor.path.name} already has {kind} line endings", 4000)
        self._check_line_endings()

    def _check_line_endings(self):
        """Tick the current file's line ending in the menu."""
        editor = self.current_editor()
        for newline, action in self.line_ending_actions.items():
            action.setChecked(editor is not None and editor.newline == newline and not editor.mixed_newlines)

    def fix_lint_issues(self, editor: EditorTab, unsafe: bool):
        """Edit → Fix Issues (fortitude) → Fix All … Issues: fortitude --fix on the editor's text (one
        Undo step), run in the background."""
        if not editor.can_lint():
            return
        expected = sum(1 for i in editor.editor.lint_issues if i.fixable and (unsafe or i.fix_safe))
        path = editor.path

        def done(text, fixed, error):
            if fixed is None:
                QMessageBox.warning(self, "fortitude", f"{path.name} could not be fixed:\n\n{error}")
            elif fixed == text:
                kind = "fixable" if unsafe else "safely fixable"
                self.statusBar().showMessage(f"{path.name}: no {kind} issues", 4000)
            else:
                editor.replace_text(fixed)
                count = f"{expected} " if expected else ""
                self.statusBar().showMessage(f"Applied {count}fortitude fix{'es' * (expected != 1)} to {path.name} "
                                             "(Undo to revert; not saved yet)", 6000)

        self._rewrite_text(editor, "fixing", lambda text: lint.fix_text(text, path, unsafe=unsafe), done)

    def format_file(self, editor: EditorTab):
        """Format the editor's (Fortran) text with fprettify, in the background."""
        if not formatter.can_format(editor.path):
            return
        path = editor.path

        def done(text, formatted, error):
            if formatted is None:
                QMessageBox.warning(self, "fprettify failed", f"{path.name} could not be formatted:\n\n{error}")
            elif formatted == text:
                self.statusBar().showMessage(f"{path.name} is already formatted", 4000)
            else:
                editor.replace_text(formatted)
                self.statusBar().showMessage(f"Formatted {path.name} with fprettify "
                                             "(Undo to revert; not saved yet)", 6000)

        self._rewrite_text(editor, "formatting", lambda text: formatter.format_text(text, path), done)
