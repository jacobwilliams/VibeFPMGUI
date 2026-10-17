"""The editor's tabs: opening, previewing, closing, renaming and deleting files.

Also moving around them: Go to a location, Go Back and Go Forward.
"""

from __future__ import annotations

import os
from pathlib import Path

from qtpy.QtCore import QFile, QPoint, QRect, QSize, Qt, QUrl, Signal
from qtpy.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QDesktopServices,
    QFont,
    QFontMetrics,
    QKeySequence,
)
from qtpy.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QStyle,
    QStyleOptionTab,
    QStylePainter,
    QTabBar,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..editor import EditorTab
from ..icons import make_icon
from ..wrapping_tabs import WrappingTabBar


class ScrollingTabBar(QTabBar):
    """A tab bar where the mouse wheel or a trackpad scrolls the tabs
    sideways when they don't all fit, rather than switching tabs; a preview
    tab's name is in italics (see set_italic)."""

    changed = Signal()  # a tab added, removed or renamed (for the tabs on several rows: WrappingTabBar)
    empty_menu_requested = Signal(QPoint)  # a right-click beside the tabs (on none): where, on the screen

    def set_tab_name(self, index: int, name: str, italic: bool = False):
        """A tab's name, in italics or not. Qt has no font per tab: an italic
        one's own text is left empty, and its name drawn here (paintEvent)."""
        self.setTabData(index, name if italic else None)
        self.setTabText(index, "" if italic else name)
        self.updateGeometry()
        self.update()
        self.changed.emit()

    def tabInserted(self, index):
        super().tabInserted(index)
        self.changed.emit()

    def tabRemoved(self, index):
        super().tabRemoved(index)
        self.changed.emit()

    def tab_name(self, index: int) -> str:
        return self.italic_name(index) or self.tabText(index)

    def italic_name(self, index: int) -> str | None:
        name = self.tabData(index)
        return name if isinstance(name, str) else None

    def _italic_font(self) -> QFont:
        font = QFont(self.font())
        font.setItalic(True)
        return font

    NAME_GAP = 6  # between the name and the icon or close button beside it

    def tabSizeHint(self, index):
        size = super().tabSizeHint(index)
        name = self.italic_name(index)
        if name is not None:  # (room for the name it doesn't have as text)
            size.setWidth(size.width() + QFontMetrics(self._italic_font()).horizontalAdvance(name) + 4)
        # No more room around the name than NAME_GAP on each side: some styles
        # (macOS's) leave its buttons at the tab's edges and center the name in
        # a much wider space. As wide as the style leaves for the name, less
        # what's spare (a style that leaves none, as Fusion, is unchanged).
        option = QStyleOptionTab()
        self.initStyleOption(option, index)
        option.rect = QRect(QPoint(0, 0), size)
        room = self.style().subElementRect(QStyle.SE_TabBarTabText, option, self).width()
        font = self._italic_font() if name is not None else self.font()
        spare = room - QFontMetrics(font).horizontalAdvance(self.tab_name(index)) - 2 * self.NAME_GAP
        if spare > 0:
            size.setWidth(size.width() - spare)
        return size

    def paintEvent(self, event):
        super().paintEvent(event)
        names = [(i, self.italic_name(i)) for i in range(self.count())]
        if not any(name for _i, name in names):
            return
        painter = QStylePainter(self)
        painter.setFont(self._italic_font())
        for index, name in names:
            if name is not None:
                option = QStyleOptionTab()
                self.initStyleOption(option, index)
                option.text = name
                painter.drawControl(QStyle.CE_TabBarTabLabel, option)

    def contextMenuEvent(self, event):
        if self.tabAt(event.pos()) < 0:
            self.empty_menu_requested.emit(event.globalPos())
            event.accept()
        else:
            super().contextMenuEvent(event)

    def wheelEvent(self, event):
        arrows = [b for b in self.findChildren(QToolButton) if b.isVisible() and b.arrowType() in
                  (Qt.LeftArrow, Qt.RightArrow)]
        if not arrows:
            super().wheelEvent(event)
            return
        delta = event.angleDelta()
        step = delta.x() if abs(delta.x()) > abs(delta.y()) else delta.y()
        self._wheel_rest = getattr(self, "_wheel_rest", 0) + step
        while abs(self._wheel_rest) >= 120:  # one notch (or that much trackpad travel) per tab
            back = self._wheel_rest > 0
            self._wheel_rest -= 120 if back else -120
            for button in arrows:
                if button.arrowType() == (Qt.LeftArrow if back else Qt.RightArrow) and button.isEnabled():
                    button.click()
        event.accept()


class EditorTabsMixin:
    """The editor's tabs (a part of `vibefpmgui.main_window.MainWindow`)."""

    def _build_editor_tabs(self):
        """The editor's tabs (the middle of the window)."""
        self.tabs = QTabWidget()
        self.tabs.setTabBar(ScrollingTabBar())  # (before any tabs are added)
        self.tabs.setDocumentMode(True)
        # With many files open, scroll the tab bar (arrows, wheel or trackpad)
        # rather than squeezing every tab; macOS's style turns the arrows off
        # and shortens the names by default.
        self.tabs.setUsesScrollButtons(True)
        self.tabs.setElideMode(Qt.ElideNone)
        self.tabs.tabBar().setExpanding(False)
        # A list of every open tab, at the right end of the tab bar.
        tab_list = self.tab_list = QMenu(self.tabs)
        tab_list.aboutToShow.connect(lambda: self._fill_tab_list(tab_list))
        self.tab_list_button = self._tab_list_button()
        self.tabs.setCornerWidget(self.tab_list_button, Qt.TopRightCorner)
        # The middle of the window: files open in the editor, each in a closable tab.
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_editor)
        self.tabs.tabBarDoubleClicked.connect(lambda i: self._make_permanent(self.tabs.widget(i)))
        self.tabs.currentChanged.connect(self._update_editor_actions)
        self.tabs.currentChanged.connect(self._reveal_current_file)
        self.tabs.currentChanged.connect(self._update_central)
        self.tabs.currentChanged.connect(self._update_problems)
        # The tabs on several rows instead of one that scrolls (View → Editor Tabs):
        # above the tabs, which then hide their own bar.
        self.tab_rows = WrappingTabBar(self.tabs)
        self.tab_rows.close_requested.connect(self.close_editor)
        self.tabs.tabBar().changed.connect(self.tab_rows.refresh)
        # Right-click beside the tabs: the Editor Tabs choices (as in the View menu).
        self.tabs.tabBar().empty_menu_requested.connect(self._tabs_menu)
        self.tab_rows.empty_menu_requested.connect(self._tabs_menu)
        # (The tab list, with Close All Editor Tabs, at the rows' end: as at the one row's.)
        self.tab_rows_list_button = self._tab_list_button()
        rows = QHBoxLayout()
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(0)
        rows.addWidget(self.tab_rows, 1)
        rows.addWidget(self.tab_rows_list_button, 0, Qt.AlignTop)
        self.editor_area = QWidget()
        area = QVBoxLayout(self.editor_area)
        area.setContentsMargins(0, 0, 0, 0)
        area.setSpacing(0)
        area.addLayout(rows)
        area.addWidget(self.tabs, 1)
        menu = self.tab_rows_menu = self.view_menu.addMenu("Editor Tabs")  # (kept: PySide would let it go)
        group = QActionGroup(menu)
        self.tab_rows_actions: dict[bool, QAction] = {}
        for wrap, text, tip in ((False, "One Row (Scroll)", ("The editor's tabs side by side, scrolling when they "
                                 "don't all fit (the wheel or a trackpad scrolls them)")),
                                (True, "Several Rows (Wrap)", ("The editor's tabs on as many rows as they need, "
                                 "all in view"))):
            action = menu.addAction(text)
            action.setCheckable(True)
            action.setStatusTip(tip)
            action.triggered.connect(lambda _=False, w=wrap: self.set_tab_rows(w))
            group.addAction(action)
            self.tab_rows_actions[wrap] = action
        self.set_tab_rows(self.settings.value("tab_rows_wrap", False, type=bool), save=False)

    def _tab_list_button(self) -> QToolButton:
        """A button for the list of every open tab (the tab bar's corner, or the rows' end)."""
        button = QToolButton()
        button.setArrowType(Qt.DownArrow)
        button.setAutoRaise(True)
        button.setToolTip("All open tabs")
        button.setPopupMode(QToolButton.InstantPopup)
        # Just the one arrow (a button with a menu also gets a menu indicator).
        button.setStyleSheet("QToolButton::menu-indicator { image: none; width: 0px; }")
        button.setMenu(self.tab_list)
        return button

    def _tabs_menu(self, where: QPoint):
        """The menu for a right-click beside the editor's tabs: View → Editor Tabs."""
        menu = self.tabs_menu = QMenu(self)  # (kept while it's open)
        for action in self.tab_rows_menu.actions():  # (the same actions: checked alike)
            menu.addAction(action)
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(where)

    def set_tab_rows(self, wrap: bool, save: bool = True):
        """View → Editor Tabs: one row that scrolls, or several rows."""
        self.tab_rows_actions[wrap].setChecked(True)
        if save:
            self.settings.setValue("tab_rows_wrap", wrap)
        self.tabs.tabBar().setVisible(not wrap)
        self.tab_list_button.setVisible(not wrap)  # (the rows have their own, at their end)
        self.tab_rows.setVisible(wrap)
        self.tab_rows_list_button.setVisible(wrap)
        self.tab_rows.refresh()

    def editors(self) -> list[EditorTab]:
        return [w for i in range(self.tabs.count())
                if isinstance(w := self.tabs.widget(i), EditorTab)]

    def current_editor(self) -> EditorTab | None:
        widget = self.tabs.currentWidget()
        return widget if isinstance(widget, EditorTab) else None

    def _with_editor(self, action):
        editor = self.current_editor()
        if editor is not None:
            action(editor)

    def browse_file(self):
        start = str(self.manifest_path.parent) if self.manifest_path else str(Path.home())
        filename, _ = QFileDialog.getOpenFileName(self, "Open file in editor", start)
        if filename:
            self.open_file(filename, force_editor=True)

    def preview_file(self, path: str):
        """Project files → one click: show the file in the preview tab (its
        name in italics), in place of the one previewed before. Editing it,
        or double-clicking the file or the tab, keeps it open."""
        file = Path(path).resolve()
        if self.use_editor_action.isChecked() and file.is_file() and EditorTab.is_probably_text(file):
            self.open_file(str(file), force_editor=True, preview=True)

    def _preview_editor(self) -> EditorTab | None:
        return self._preview if self._preview in self.editors() else None

    def _keep_open(self, path: str):
        """A file opened as a preview, kept open (a result double-clicked)."""
        editor = next((e for e in self.editors() if e.path == Path(path).resolve()), None)
        if editor is not None:
            self._make_permanent(editor)

    def _make_permanent(self, editor):
        """A preview tab stays open from now on (its name no longer in italics)."""
        if editor is not None and editor is self._preview:
            self._preview = None
            self._update_editor_title(editor)

    def open_file(self, path: str, diagnostics: list | None = None, force_editor: bool = False,
                  preview: bool = False):
        """Open a file in the built-in editor (or the default application),
        marking any compiler diagnostics and jumping to the first one.
        ``preview``: in the preview tab (see preview_file), and the keyboard
        stays where it is."""
        file = Path(path).resolve()
        editor = next((e for e in self.editors() if e.path == file), None)  # (open already: there, even unsaved)
        use_editor = force_editor or self.use_editor_action.isChecked()
        if editor is None and (not use_editor or not file.is_file() or not EditorTab.is_probably_text(file)):
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(file)))
            return
        if editor is not None and not preview:
            self._make_permanent(editor)  # (opened for real: a preview no longer)
        if editor is None:
            try:
                editor = EditorTab(file)
            except OSError as exc:
                QMessageBox.warning(self, "Can't open file", f"{file}:\n{exc}")
                return
            # A new preview takes the old one's place (unless that one has changes: then it stays).
            replaced = self._preview_editor() if preview else None
            index = self.tabs.indexOf(replaced) if replaced is not None and not replaced.is_dirty() else -1
            if preview:
                self._preview = editor
            self._add_editor(editor, index)
            if index >= 0:
                self._close_tab(replaced)
        self.tabs.setCurrentWidget(editor)
        if diagnostics:
            editor.show_diagnostics(diagnostics)
            editor.editor.go_to_line(diagnostics[0].line)
        elif not preview:
            editor.editor.setFocus()

    def new_file(self):
        """File → New File: an empty tab for a file not saved yet ("Untitled-1"…,
        in the project's folder when it's saved, unless another is chosen).
        Plain text until Save As gives it a name, and with it a file type."""
        folder = self.manifest_path.parent if self.manifest_path else Path.home()
        taken = {e.path.name for e in self.editors()}
        number = 1
        while f"Untitled-{number}" in taken or (folder / f"Untitled-{number}").exists():
            number += 1
        editor = EditorTab(folder / f"Untitled-{number}", untitled=True)
        self._add_editor(editor)
        self.tabs.setCurrentWidget(editor)
        editor.editor.setFocus()

    def _file_created(self, path: str):
        """Save As made a new file: in Project files now."""
        self.project_tree.refresh()
        self.project_tree.reveal(path)

    def _add_editor(self, editor: EditorTab, index: int = -1):
        """A new tab for ``editor`` (at ``index``, or the end), connected to the window."""
        editor.set_font(self._mono_font(editor=True))
        self._show_coverage(editor)
        editor.dirty_changed.connect(lambda _dirty, e=editor: self._update_editor_title(e))
        editor.path_changed.connect(lambda _p, e=editor: self._update_editor_title(e))
        editor.path_changed.connect(self._update_editor_actions)
        editor.saved.connect(self._editor_saved)
        editor.created.connect(self._file_created)
        editor.navigate_requested.connect(self.navigate_to)
        editor.references_found.connect(self._show_references)
        editor.rename_ready.connect(self._preview_rename)
        editor.editor.word_added.connect(self.add_spelling_word)
        editor.explain_requested.connect(lambda problem, e=editor: self.explain_problem(e, problem))
        editor.editor.edit_selection_requested.connect(lambda e=editor: self.edit_with_ai(e))
        editor.problems_changed.connect(lambda e=editor: self._problems_timer.start() if e is self.current_editor()
                                        else None)
        editor.editor.textChanged.connect(  # (a new file not saved yet: not in Find in Files)
            lambda e=editor: None if e.untitled else self.search_panel.file_changed(e.path))
        editor.attach_lsp(self.lsp_client)
        # (Changing a preview's text keeps it open.)
        editor.dirty_changed.connect(lambda dirty, e=editor: dirty and self._make_permanent(e))
        if index >= 0:
            self.tabs.insertTab(index, editor, editor.path.name)
        else:
            self.tabs.addTab(editor, editor.path.name)
        self._add_close_button(editor)
        self._update_editor_title(editor)
        self._save_session()

    def _close_tab(self, editor: EditorTab):
        """Close an editor tab without asking (see close_editor for that)."""
        editor.close_down()
        self.tabs.removeTab(self.tabs.indexOf(editor))
        editor.deleteLater()
        if editor is self._preview:
            self._preview = None

    def _add_close_button(self, editor: EditorTab):
        """The file's icon on the left of an editor tab's name, and our own ×
        on the right (macOS's dark-mode close button is hard, or impossible,
        to see). Each side gets one small widget: macOS's style places the
        name as if the widget beside it were the usual size, so a wider one
        (or a tab icon) covers the start of the name."""
        button = QToolButton()
        button.setAutoRaise(True)
        button.setFixedSize(18, 18)
        button.setIconSize(QSize(12, 12))
        button.setCursor(Qt.ArrowCursor)
        keys = self.close_tab_action.shortcut().toString(QKeySequence.NativeText)
        button.setToolTip(f"Close ({keys})" if keys else "Close")
        button.setStyleSheet("QToolButton { border: none; border-radius: 4px; padding: 0; }"
                             "QToolButton:hover { background: rgba(128, 128, 128, 80); }")
        button.clicked.connect(lambda _=False, e=editor: self.close_editor(self.tabs.indexOf(e)))
        editor.close_button = button
        self._style_close_button(button)
        icon = QLabel()
        icon.setFixedSize(16, 16)
        editor.tab_icon = icon
        self._set_tab_icon(editor)
        bar = self.tabs.tabBar()
        index = self.tabs.indexOf(editor)
        bar.setTabButton(index, QTabBar.LeftSide, icon)
        bar.setTabButton(index, QTabBar.RightSide, button)

    def _set_tab_icon(self, editor: EditorTab):
        """The file's icon, as in Project files (it changes with Save As)."""
        label = getattr(editor, "tab_icon", None)
        if label is not None:
            label.setPixmap(self.project_tree.icon_for(editor.path).pixmap(16, 16))

    @staticmethod
    def _style_close_button(button: QToolButton):
        # A mid gray: seen on the dark tabs and on the light selected one
        # (macOS's dark mode draws the selected tab light).
        button.setIcon(make_icon("close", QColor(140, 140, 140)))

    def _fill_tab_list(self, menu: QMenu):
        """The tab-list menu: every open tab (the current one ticked)."""
        menu.clear()
        current = self.tabs.currentIndex()
        for index in range(self.tabs.count()):  # (every tab is a file open in the editor)
            icon = self.project_tree.icon_for(self.tabs.widget(index).path)
            action = menu.addAction(icon, self.tabs.tabBar().tab_name(index))
            action.setCheckable(True)
            action.setChecked(index == current)
            action.setToolTip(self.tabs.tabToolTip(index))
            action.triggered.connect(lambda _checked=False, i=index: self.tabs.setCurrentIndex(i))
        menu.addSeparator()
        close_all = menu.addAction("Close All Editor Tabs")
        close_all.setToolTip("Close every file open in the editor (unsaved changes are asked about)")
        close_all.setEnabled(bool(self.editors()))
        close_all.triggered.connect(self.close_all_editor_tabs)
        menu.setToolTipsVisible(True)

    def close_all_editor_tabs(self):
        """The tab list's Close All Editor Tabs (Cancel on an unsaved file
        keeps them all open)."""
        if self.close_all_editors():
            self._save_session()  # so they don't come back next time
            self._update_editor_actions()

    def _update_editor_title(self, editor: EditorTab):
        index = self.tabs.indexOf(editor)
        if index >= 0:
            self.tabs.tabBar().set_tab_name(index, ("• " if editor.is_dirty() else "") + editor.path.name,
                                            italic=editor is self._preview)
            self.tabs.setTabToolTip(index, editor.display_path())
            self._set_tab_icon(editor)
        self._update_editor_actions()

    def _editor_saved(self, path: str):
        self.statusBar().showMessage(f"Saved {path}", 4000)
        self.refresh_git()
        self._complexity_file_saved(path)
        if self.manifest_path and Path(path) == self.manifest_path:
            self.reload()  # graph, features and profiles may have changed

    def rename_file(self, path: str):
        """Project files → Rename…: rename a file on disk; a tab it's open in
        follows it (unsaved changes included)."""
        old = Path(path).resolve()
        name, ok = QInputDialog.getText(self, "Rename", f"New name for {old.name}:", text=old.name)
        name = name.strip() if ok else ""
        if not name or name == old.name:
            return
        if "/" in name or "\\" in name or name in (".", ".."):
            QMessageBox.warning(self, "Rename", f"“{name}” isn't a file name (it can't contain / or \\).")
            return
        new = old.with_name(name)
        if new.exists() and not os.path.samefile(new, old):  # (another file; a change of case is fine)
            QMessageBox.warning(self, "Rename", f"There's already a file named {name} in {old.parent.name}.")
            return
        try:
            old.rename(new)
        except OSError as exc:
            QMessageBox.warning(self, "Rename", f"Couldn't rename {old.name}:\n{exc}")
            return
        editor = self._open_editor_for(old)
        if editor is not None:
            editor.renamed(new)
            self._save_session()
        self._lsp_diagnostics_by_file.pop(old, None)
        self.search_panel.file_changed(old)
        self.search_panel.file_changed(new)
        self.project_tree.refresh()
        self.project_tree.reveal(str(new))
        self.statusBar().showMessage(f"Renamed {old.name} to {name}", 4000)

    def delete_file(self, path: str):
        """Project files → Move to Trash…: after asking (and, for a file open
        with unsaved changes, whether to save them first), move a file to the
        trash and close its tab."""
        file = Path(path).resolve()
        editor = self._open_editor_for(file)
        if editor is not None and editor.is_dirty():
            self.tabs.setCurrentWidget(editor)
            answer = QMessageBox.question(
                self, "Move to Trash",
                f"{file.name} has unsaved changes. Save them before moving it to the trash?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer == QMessageBox.Cancel or (answer == QMessageBox.Save and not editor.save()):
                return
        else:
            answer = QMessageBox.question(self, "Move to Trash", f"Move {file.name} to the trash?",
                                          QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if answer != QMessageBox.Yes:
                return
        moved = QFile.moveToTrash(str(file))
        moved = moved[0] if isinstance(moved, tuple) else moved  # (PySide: (ok, path in the trash))
        if not moved:
            QMessageBox.warning(self, "Move to Trash", f"Couldn't move {file.name} to the trash.")
            return
        if editor is not None:
            self._close_tab(editor)
            self._save_session()
        self._lsp_diagnostics_by_file.pop(file, None)
        self.search_panel.file_changed(file)
        self.project_tree.refresh()
        self.statusBar().showMessage(f"Moved {file.name} to the trash", 4000)

    def close_all_editors(self) -> bool:
        """Close every editor tab. Unsaved files are asked about first; if
        any answer is Cancel, nothing is closed and False is returned."""
        editors = self.editors()
        for editor in editors:
            if editor.is_dirty():
                self.tabs.setCurrentWidget(editor)
                if not editor.maybe_save():
                    return False
        for editor in editors:
            self._close_tab(editor)
        return True

    def close_editor(self, index: int):
        widget = self.tabs.widget(index)
        if not isinstance(widget, EditorTab):
            return
        self.tabs.setCurrentIndex(index)
        if widget.maybe_save():
            self._close_tab(widget)
            self._save_session()

    def _location(self) -> tuple[str, int, int] | None:
        editor = self.current_editor()
        if editor is None:
            return None
        cursor = editor.editor.textCursor()
        return str(editor.path), cursor.blockNumber() + 1, cursor.positionInBlock()

    def navigate_to(self, path: str, line: int, column: int, record: bool = True, preview: bool = False):
        """Open a file at a line and column (Go to Definition / Symbol);
        Go Back returns to where this was called from. ``preview``: in the
        preview tab, if it isn't open already (see preview_file)."""
        here = self._location()
        if record and here is not None and here != (path, line, column):
            self._back.append(here)
            del self._back[:-100]
            self._forward.clear()
        self.open_file(path, force_editor=True, preview=preview)
        editor = self.current_editor()
        if editor is not None and editor.path == Path(path).resolve():
            editor.editor.go_to_position(line, column)
            editor.editor.setFocus()
        self._update_editor_actions()

    def _go_history(self, back: bool):
        source, target = (self._back, self._forward) if back else (self._forward, self._back)
        while source:
            path, line, column = source.pop()
            if Path(path).is_file():
                here = self._location()
                if here is not None:
                    target.append(here)
                self.navigate_to(path, line, column, record=False)
                return
        self._update_editor_actions()
