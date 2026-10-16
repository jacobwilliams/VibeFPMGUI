"""The View menu: text size, fonts, theme, colors and the editor's options.

Each option is saved in the settings and applied to every open editor.
"""

from __future__ import annotations

import sys

from qtpy.QtCore import QEvent, QSize, QUrl
from qtpy.QtGui import (
    QAction,
    QActionGroup,
    QDesktopServices,
    QFont,
    QFontDatabase,
    QKeySequence,
)
from qtpy.QtWidgets import QApplication, QFontDialog, QMessageBox, QPlainTextEdit

from .. import editor as editor_mod
from .. import highlighter as hl
from .. import lint, lsp, spelling, themes
from ..about import FPM_DOCS_URL, AboutDialog
from ..build_monitor import set_ui_scale
from ..code_editor import CodeEditor
from ..editor import EditorTab, monospace_font
from ..editor_shared import INDENT_WIDTHS
from ..widgets import run_dialog


class ViewMixin:
    """The View menu and how the window looks (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_view_menu(self):
        """View menu: text size, editor options, theme and colors (Panels: _build_docks)."""
        # View menu: text size for the whole window.
        # The system font, recorded once: later windows would otherwise see the
        # already-enlarged application font as "normal".
        app = QApplication.instance()
        if app.property("vibefpm_base_font") is None:
            app.setProperty("vibefpm_base_font", QFont(app.font()))
        self._base_font = QFont(app.property("vibefpm_base_font"))
        self.font_delta = 0
        view_menu = self.view_menu = self.menuBar().addMenu("&View")
        self.bigger_action = QAction("Bigger Text", self)
        self.bigger_action.setShortcuts([QKeySequence(QKeySequence.ZoomIn), QKeySequence("Ctrl+=")])
        self.bigger_action.triggered.connect(lambda: self.set_font_delta(self.font_delta + 1))
        self.smaller_action = QAction("Smaller Text", self)
        self.smaller_action.setShortcut(QKeySequence(QKeySequence.ZoomOut))
        self.smaller_action.triggered.connect(lambda: self.set_font_delta(self.font_delta - 1))
        self.reset_font_action = QAction("Actual Size", self)  # (Ctrl+0 is Graph → Fit to window)
        self.reset_font_action.triggered.connect(lambda: self.set_font_delta(0))
        view_menu.addActions([self.bigger_action, self.smaller_action, self.reset_font_action])
        editor_mod.editor_font_family = self.settings.value("editor_font_family", "") or ""
        editor_mod.editor_font_size = self.settings.value("editor_font_size", 0.0, type=float) or 0.0
        self.editor_font_action = view_menu.addAction("Editor Font…")
        self.editor_font_action.setStatusTip("Choose the font for the editor and the fpm.toml tab")
        self.editor_font_action.triggered.connect(self.choose_editor_font)
        self.reset_editor_font_action = view_menu.addAction("Reset Editor Font")
        self.reset_editor_font_action.setStatusTip("Use the system's fixed-width font in the editor again")
        self.reset_editor_font_action.triggered.connect(lambda: self.set_editor_font("", 0.0))
        self.reset_editor_font_action.setEnabled(bool(editor_mod.editor_font_family
                                                      or editor_mod.editor_font_size))
        view_menu.addSeparator()
        self.use_editor_action = QAction("Open Files in Built-in Editor", self, checkable=True)
        self.use_editor_action.setChecked(self.settings.value("use_editor", True, type=bool))
        self.use_editor_action.toggled.connect(lambda on: self.settings.setValue("use_editor", on))
        self.use_editor_action.setStatusTip(
            "Double-clicked files open in a tab here; turn off to use your default application")
        view_menu.addAction(self.use_editor_action)
        self.lint_action = QAction("Lint Fortran Files (fortitude)", self, checkable=True)
        EditorTab.lint_enabled = self.settings.value("lint", True, type=bool)
        self.lint_action.setChecked(EditorTab.lint_enabled)
        self.lint_action.toggled.connect(self.set_lint_enabled)
        if lint.executable() is None:
            self.lint_action.setEnabled(False)
            self.lint_action.setText("Lint Fortran Files (fortitude not found)")
        self.lint_action.setStatusTip("Check free-form Fortran files in the editor with fortitude "
                                      "as you type; findings are underlined and marked in the gutter")
        view_menu.addAction(self.lint_action)
        self.lsp_diagnostics_action = QAction("Language Server Diagnostics (fortls)", self, checkable=True)
        EditorTab.lsp_diagnostics_enabled = self.settings.value("lsp_diagnostics", True, type=bool)
        self.lsp_diagnostics_action.setChecked(EditorTab.lsp_diagnostics_enabled)
        self.lsp_diagnostics_action.toggled.connect(self.set_lsp_diagnostics_enabled)
        self.lsp_diagnostics_action.setStatusTip(
            "Show the problems fortls finds in a Fortran file when it's opened or saved (e.g. USE after "
            "IMPLICIT, misplaced CONTAINS, interface mismatches), marked like fortitude's")
        view_menu.addAction(self.lsp_diagnostics_action)
        self.spelling_action = QAction("Check Spelling in Comments and Strings", self, checkable=True)
        EditorTab.spelling_enabled = self.settings.value("spelling", True, type=bool)
        spelling.user_words = {w for w in str(self.settings.value("spelling_words", "") or "").split()}
        self.spelling_action.setChecked(EditorTab.spelling_enabled)
        self.spelling_action.toggled.connect(self.set_spelling_enabled)
        if not spelling.available():
            self.spelling_action.setEnabled(False)
            self.spelling_action.setText("Check Spelling (pyspellchecker not found)")
        self.spelling_action.setStatusTip(
            "Underline misspelled words in comments and strings (right-click one for suggestions)")
        view_menu.addSeparator()  # (spelling: its two items together)
        view_menu.addAction(self.spelling_action)
        spelling.set_language(str(self.settings.value("spelling_language", "en") or "en"))
        self.spelling_language_menu = view_menu.addMenu("Spelling Language")  # (kept: PySide would let it go)
        group = QActionGroup(self.spelling_language_menu)
        codes = sorted(spelling.languages(), key=lambda c: spelling.LANGUAGE_NAMES.get(c, c))
        for code in codes:
            action = self.spelling_language_menu.addAction(f"{spelling.LANGUAGE_NAMES.get(code, code)} ({code})")
            action.setCheckable(True)
            action.setChecked(code == spelling.language)
            action.setData(code)
            group.addAction(action)
            action.triggered.connect(lambda _=False, c=code: self.set_spelling_language(c))
        self.spelling_language_menu.setEnabled(bool(codes))
        view_menu.addSeparator()
        self.gremlins_action = QAction("Show Gremlins (Non-ASCII and Invisible Characters)", self, checkable=True)
        EditorTab.gremlins_enabled = self.settings.value("gremlins", True, type=bool)
        self.gremlins_action.setChecked(EditorTab.gremlins_enabled)
        self.gremlins_action.toggled.connect(self.set_gremlins_enabled)
        self.gremlins_action.setStatusTip(
            "Mark lines with characters that aren't plain ASCII (accented letters, smart quotes, "
            "non-breaking or zero-width spaces, control characters) with a gremlin in the gutter")
        view_menu.addAction(self.gremlins_action)
        self.minimap_action = QAction("Show Minimap", self, checkable=True)
        EditorTab.minimap_enabled = self.settings.value("minimap", True, type=bool)
        self.minimap_action.setChecked(EditorTab.minimap_enabled)
        self.minimap_action.setStatusTip("A zoomed-out view of the whole file at the right of the editor")
        self.minimap_action.toggled.connect(self.set_minimap_enabled)
        view_menu.addAction(self.minimap_action)
        self.coverage_action = QAction("Show Coverage in the Editor", self, checkable=True)
        CodeEditor.show_coverage = self.settings.value("show_coverage", True, type=bool)
        self.coverage_action.setChecked(CodeEditor.show_coverage)
        self.coverage_action.setStatusTip("After a test run with Build options → Coverage: green and red bars in the "
                                          "gutter for the lines the tests ran and didn't")
        self.coverage_action.toggled.connect(self.set_show_coverage)
        view_menu.addAction(self.coverage_action)
        self.matches_action = QAction("Highlight Other Occurrences of the Selection", self, checkable=True)
        CodeEditor.highlight_matches = self.settings.value("highlight_matches", True, type=bool)
        self.matches_action.setChecked(CodeEditor.highlight_matches)
        self.matches_action.setStatusTip("Selecting text in the editor highlights it everywhere else in the file "
                                         "(Match case and Whole words as set in the find bar)")
        self.matches_action.toggled.connect(self.set_highlight_matches)
        view_menu.addAction(self.matches_action)
        self.word_wrap_action = QAction("Word Wrap", self, checkable=True)
        self.word_wrap_action.setShortcut(QKeySequence("Alt+Z"))
        EditorTab.word_wrap = self.settings.value("word_wrap", False, type=bool)
        self.word_wrap_action.setChecked(EditorTab.word_wrap)
        self.word_wrap_action.setStatusTip("Wrap long lines in the editor, the terminal and the output at the "
                                           "window's edge instead of scrolling sideways")
        self.word_wrap_action.toggled.connect(self.set_word_wrap)
        view_menu.addAction(self.word_wrap_action)
        # Indentation: what Tab types, and how wide tabs are shown.
        width = self.settings.value("indent_width", 4, type=int)
        CodeEditor.indent_width = width if width in INDENT_WIDTHS else 4
        CodeEditor.use_tabs = self.settings.value("indent_tabs", False, type=bool)
        self.indent_menu = view_menu.addMenu("Indentation")  # (kept: PySide would let it go)
        self.indent_actions: dict[object, QAction] = {}
        kinds = QActionGroup(self.indent_menu)
        for use_tabs, text, tip in ((False, "Indent with Spaces", "Tab types spaces, to the next tab stop"),
                                    (True, "Indent with Tabs", "Tab types a tab character")):
            action = self.indent_menu.addAction(text)
            action.setCheckable(True)
            action.setStatusTip(tip)
            action.triggered.connect(lambda _=False, t=use_tabs: self.set_indentation(use_tabs=t))
            kinds.addAction(action)
            self.indent_actions[use_tabs] = action
        self.indent_menu.addSeparator()
        widths = QActionGroup(self.indent_menu)
        for w in INDENT_WIDTHS:
            action = self.indent_menu.addAction(f"Width: {w}")
            action.setCheckable(True)
            action.setStatusTip(f"An indent level is {w} columns (tabs are shown {w} wide too)")
            action.triggered.connect(lambda _=False, n=w: self.set_indentation(width=n))
            widths.addAction(action)
            self.indent_actions[w] = action
        self._check_indent_actions()
        view_menu.addSeparator()
        self.terminal_action = QAction("Terminal", self)
        # ⌃` as in VS Code (the Control key on macOS too: ⌘` switches windows).
        self.terminal_action.setShortcut(QKeySequence("Meta+`" if sys.platform == "darwin" else "Ctrl+`"))
        self.terminal_action.setStatusTip("Your shell, in the project's folder (Terminal tab)")
        self.terminal_action.triggered.connect(self.show_terminal)
        view_menu.addAction(self.terminal_action)
        self.fortls_action = QAction("Fortran Language Server (fortls)", self, checkable=True)
        self.fortls_action.setChecked(self.settings.value("fortls", True, type=bool))
        self.fortls_action.toggled.connect(self.set_fortls_enabled)
        if lsp.executable() is None:
            self.fortls_action.setEnabled(False)
            self.fortls_action.setText("Fortran Language Server (fortls not found)")
        self.fortls_action.setStatusTip("Hover information, Go to Definition, the outline and "
                                        "Go to Symbol for Fortran files, from fortls")
        view_menu.addAction(self.fortls_action)
        view_menu.addSeparator()
        self.theme_menu = view_menu.addMenu("Theme")  # (kept: PySide would let it go)
        self.theme_menu.setStatusTip("The colors of the whole window (Syntax Colors are the editor's)")
        self.theme_actions: dict[str, QAction] = {}
        group = QActionGroup(self.theme_menu)
        previous = None
        for key, theme in themes.THEMES.items():
            kind = "native" if theme.native else "dark" if theme.dark else "light"
            if previous is not None and kind != previous:
                self.theme_menu.addSeparator()  # (System/Light/Dark · dark colorful · light colorful)
            previous = kind
            action = self.theme_menu.addAction(theme.title)
            action.setCheckable(True)
            action.setChecked(key == themes.current)
            action.triggered.connect(lambda _=False, k=key: self.set_theme(k))
            group.addAction(action)
            self.theme_actions[key] = action
        hl.chosen_style = self.settings.value("syntax_style", hl.AUTO) or hl.AUTO
        self.style_menu = view_menu.addMenu("Syntax Colors")
        self.style_menu.setStatusTip("The Pygments color style for the editor and fpm.toml")
        self.style_menu.aboutToShow.connect(self._fill_style_menu)

    def _apply_highlight_style(self):
        """The chosen Pygments style, or one matching the system's light/dark mode."""
        for tree in getattr(self, "result_trees", []):
            tree.refresh_style()

    def _fill_style_menu(self):
        """View → Syntax Colors: Automatic, then every installed Pygments style."""
        menu = self.style_menu
        menu.clear()
        group = QActionGroup(menu)
        auto = menu.addAction(f"Automatic ({hl.LIGHT_STYLE} / {hl.DARK_STYLE}, following light or dark mode)")
        auto.setData(hl.AUTO)
        menu.addSeparator()
        light, dark = hl.available_styles()
        actions = [auto]
        for title, names in (("Light Styles", light), ("Dark Styles", dark)):
            sub = menu.addMenu(title)
            for name in names:
                action = sub.addAction(name)
                action.setData(name)
                actions.append(action)
        for action in actions:
            action.setCheckable(True)
            action.setChecked(action.data() == hl.chosen_style)
            group.addAction(action)
            action.triggered.connect(lambda _checked=False, a=action: self.set_syntax_style(a.data()))

    def set_theme(self, key: str):
        """View → Theme: the whole window's colors (and, with Syntax Colors
        on Automatic, the editor's)."""
        themes.apply(key)
        self.settings.setValue("app_theme", themes.current)
        for k, action in self.theme_actions.items():
            action.setChecked(k == themes.current)
        self._apply_highlight_style()  # (the automatic syntax style may change with it)
        for editor in self.editors():
            editor.refresh_style()

    def set_syntax_style(self, name: str):
        hl.chosen_style = name
        self.settings.setValue("syntax_style", name)
        self._apply_highlight_style()
        for editor in self.editors():
            editor.refresh_style()
        self.review_monitor.refresh_palette()  # (code in the AI's answers: the same colors)
        self.git_panel.refresh_palette()  # (the diff's colors too)

    def changeEvent(self, event):
        if event.type() == QEvent.PaletteChange and hasattr(self, "terminal_panel"):  # (the window is built)
            self._apply_highlight_style()
            self.graph_view.refresh_theme()  # "Auto" theme follows the system
            self._update_legend()
            self.build_monitor.refresh_palette()
            self.test_monitor.refresh_palette()
            self.run_monitor.refresh_palette()
            self.review_monitor.refresh_palette()
            self.coverage_panel.refresh_palette()
            self.git_panel.refresh_palette()
            self.complexity_panel.refresh_palette()
            self.terminal_panel.refresh_colors()
            for editor in self.editors():
                editor.refresh_style()
            self.project_tree.refresh_marks()
            self._update_command_icons()
            for editor in self.editors():
                if getattr(editor, "close_button", None) is not None:
                    self._style_close_button(editor.close_button)
            self.build_monitor.set_package_colors(self.graph_view.package_colors())
            if self.graph:  # (the details are HTML: its links' color is the palette's when it's made)
                self.show_package(self.graph_view.selected or self.graph.root)
        super().changeEvent(event)

    FONT_DELTA_RANGE = (-4, 12)  # points relative to the system size

    def set_font_delta(self, delta: int, announce: bool = True):
        """Make all text in the window ``delta`` points bigger (or smaller)
        than the system size."""
        lo, hi = self.FONT_DELTA_RANGE
        self.font_delta = max(lo, min(hi, int(delta)))
        base = self._base_font.pointSizeF()
        size = base + self.font_delta
        factor = size / base
        set_ui_scale(factor)

        font = QFont(self._base_font)
        font.setPointSizeF(size)
        QApplication.setFont(font)
        # Widgets with their own fonts don't follow the application font.
        mono = self._mono_font()
        for view in (self.console, self.test_output):
            view.setFont(mono)
        code = self._mono_font(editor=True)  # View → Editor Font
        for editor in self.editors():
            editor.set_font(code)
        if hasattr(self, "terminal_panel"):
            self.terminal_panel.set_font(code)
        if hasattr(self, "git_panel"):
            self.git_panel.set_code_font(code)
        if hasattr(self, "complexity_panel"):
            self.complexity_panel.set_code_font(code)
        self.command_toolbar.setIconSize(QSize(round(22 * factor), round(22 * factor)))
        self.build_monitor.refresh_fonts()
        self.test_monitor.refresh_fonts()
        self.run_monitor.refresh_fonts()
        self.review_monitor.refresh_fonts()
        self.coverage_panel.refresh_fonts()
        self.git_panel.refresh_fonts()
        self.complexity_panel.refresh_fonts()
        for tree in self.result_trees:
            tree.mono = mono
            tree.doItemsLayout()
        self.graph_view.refresh_fonts()
        if self.graph:
            self.show_package(self.graph_view.selected or self.graph.root)

        self.bigger_action.setEnabled(self.font_delta < hi)
        self.smaller_action.setEnabled(self.font_delta > lo)
        self.reset_font_action.setEnabled(self.font_delta != 0)
        self.settings.setValue("font_delta", self.font_delta)
        if announce:
            self.statusBar().showMessage(f"Text size {round(factor * 100)}%", 3000)

    def _mono_font(self, editor: bool = False) -> QFont:
        base = self._base_font.pointSizeF()
        return monospace_font((base + self.font_delta) / base, editor=editor)

    def choose_editor_font(self):
        """View → Editor Font…: pick a fixed-width family and size (at 100%
        text size; Bigger/Smaller Text still scale it)."""
        current = monospace_font(1.0, editor=True)
        # Qt's own dialog: the macOS font panel ignores the fixed-width filter.
        result = QFontDialog.getFont(current, self, "Editor Font",
                                     QFontDialog.MonospacedFonts | QFontDialog.DontUseNativeDialog)
        # PySide returns (ok, font), PyQt (font, ok).
        ok, font = result if isinstance(result[0], bool) else (result[1], result[0])
        if ok:
            self.set_editor_font(font.family(), font.pointSizeF())

    def set_editor_font(self, family: str, size: float):
        system = QFontDatabase.systemFont(QFontDatabase.FixedFont)
        if family == system.family() and abs(size - system.pointSizeF()) < 0.01:
            family, size = "", 0.0  # the same as the default
        editor_mod.editor_font_family, editor_mod.editor_font_size = family, size
        self.settings.setValue("editor_font_family", family)
        self.settings.setValue("editor_font_size", size)
        self.reset_editor_font_action.setEnabled(bool(family or size))
        self.set_font_delta(self.font_delta, announce=False)  # re-applies the fonts
        shown = f"{family or system.family()} {round(size or system.pointSizeF(), 1):g} pt"
        self.statusBar().showMessage(f"Editor font: {shown}", 4000)

    def set_show_coverage(self, on: bool):
        CodeEditor.show_coverage = on
        self.settings.setValue("show_coverage", on)
        for editor in self.editors():
            editor.editor.refresh_gutter()

    def set_highlight_matches(self, on: bool):
        CodeEditor.highlight_matches = on
        self.settings.setValue("highlight_matches", on)
        for editor in self.editors():
            editor.editor.update_occurrences()

    def set_minimap_enabled(self, on: bool):
        EditorTab.minimap_enabled = on
        self.settings.setValue("minimap", on)
        for editor in self.editors():
            editor.minimap.setVisible(on)

    def set_indentation(self, width: int | None = None, use_tabs: bool | None = None):
        """View → Indentation: spaces or tabs, and the width of an indent level."""
        cls = CodeEditor
        cls.indent_width = cls.indent_width if width is None else width
        cls.use_tabs = cls.use_tabs if use_tabs is None else use_tabs
        self.settings.setValue("indent_width", cls.indent_width)
        self.settings.setValue("indent_tabs", cls.use_tabs)
        for editor in self.editors():
            editor.editor.set_indentation(cls.indent_width, cls.use_tabs)
            editor.update_status()
        self._check_indent_actions()

    def _check_indent_actions(self):
        cls = CodeEditor
        for key, action in self.indent_actions.items():
            # (bool keys are spaces/tabs; ints are widths: True == 1 and False == 0 never collide with them)
            action.setChecked(key is cls.use_tabs if isinstance(key, bool) else key == cls.indent_width)

    def set_word_wrap(self, on: bool):
        EditorTab.word_wrap = on
        self.settings.setValue("word_wrap", on)
        for editor in self.editors():
            editor.editor.set_word_wrap(on)
        self.terminal_panel.set_word_wrap(on)
        self._set_output_wrap(on)

    def _set_output_wrap(self, on: bool):
        """The Output panel's text: wrapped at its edge, or scrolled sideways."""
        mode = QPlainTextEdit.WidgetWidth if on else QPlainTextEdit.NoWrap
        for text in (self.console, self.test_output):
            text.setLineWrapMode(mode)

    def set_lsp_diagnostics_enabled(self, on: bool):
        EditorTab.lsp_diagnostics_enabled = on
        self.settings.setValue("lsp_diagnostics", on)
        for editor in self.editors():
            editor.show_lsp_diagnostics(self._lsp_diagnostics_by_file.get(editor.path, []))

    def set_spelling_enabled(self, on: bool):
        EditorTab.spelling_enabled = on
        self.settings.setValue("spelling", on)
        for editor in self.editors():
            editor.set_spelling_enabled(on)

    def set_gremlins_enabled(self, on: bool):
        EditorTab.gremlins_enabled = on
        self.settings.setValue("gremlins", on)
        for editor in self.editors():
            editor.set_gremlins_enabled(on)

    def set_spelling_language(self, code: str):
        """View → Spelling Language: check with that language's dictionary."""
        spelling.set_language(code)
        self.settings.setValue("spelling_language", spelling.language)
        for action in self.spelling_language_menu.actions():
            action.setChecked(action.data() == spelling.language)
        for editor in self.editors():
            editor.check_spelling()

    def add_spelling_word(self, word: str):
        """"Add to Dictionary": not a misspelling anymore, in any file."""
        spelling.user_words.add(word.lower())
        self.settings.setValue("spelling_words", " ".join(sorted(spelling.user_words)))
        for editor in self.editors():
            editor.check_spelling()
        self.statusBar().showMessage(f"Added “{word}” to the dictionary", 4000)

    def set_lint_enabled(self, on: bool):
        EditorTab.lint_enabled = on
        self.settings.setValue("lint", on)
        for editor in self.editors():
            editor.set_lint_enabled(on)
        self._update_editor_actions()  # (the Fix … Issues items)

    def _build_help_menu(self):
        """Help menu (added last, so it is the rightmost menu)."""
        menu = self.menuBar().addMenu("&Help")
        docs = menu.addAction("fpm Documentation")
        docs.setStatusTip(FPM_DOCS_URL)
        docs.triggered.connect(lambda: QDesktopServices.openUrl(QUrl(FPM_DOCS_URL)))
        menu.addSeparator()
        about = menu.addAction("About VibeFPMGUI")
        about.setMenuRole(QAction.AboutRole)  # macOS: moved to the application menu
        about.triggered.connect(self.show_about)
        self.about_qt_action = menu.addAction("About Qt")
        self.about_qt_action.setMenuRole(QAction.AboutQtRole)  # macOS: moved to the application menu
        self.about_qt_action.setStatusTip("Qt's own About window: the Qt version and license")
        self.about_qt_action.triggered.connect(lambda: QMessageBox.aboutQt(self, "About Qt"))

    def show_about(self):
        compiler, source = self.effective_compiler()
        run_dialog(AboutDialog(self._fpm_executable(), compiler, source, self))
