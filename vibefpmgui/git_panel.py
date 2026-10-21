"""The *Git* tab, with three views to switch between in its header:

* *Diff*: the project's repository's status (its branch, and the changed
  files: staged, not staged, untracked, in conflict), and the diff of the
  selected file (see `vibefpmgui.git_status`, `vibefpmgui.diff_view`).
  Double-clicking a line of the diff opens the file there.
* *History*: the commits, with their branches and tags and the graph of
  how they join; a commit's details, files and diffs (see
  `vibefpmgui.git_log`, `vibefpmgui.git_log_view`).
* *Stats*: numbers about the repository and each person's commits over time
  (see `vibefpmgui.git_stats`, `vibefpmgui.git_stats_view`).

The History and the Stats are read when they're on show and HEAD has moved
since (or Refresh is clicked; or something of their own changed, such as
the History's All branches: see `reload_key`). Each of those pages has the
same few methods for this (`reload_key`, `reader`, `show_result`,
`show_message`), so the panel needs nothing of either's own.

What git says is read on a pool thread.
"""

from __future__ import annotations

from pathlib import Path

from qtpy.QtCore import QSize, Qt, Signal
from qtpy.QtGui import QBrush, QColor, QFont
from qtpy.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QMenu,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import git_status as gs
from .background import call_in_background
from .build_monitor import PALETTES, TitleIcon, app_is_light, panel_stylesheet
from .file_diff import FileDiff
from .git_log_view import GitLogView
from .git_stats_view import GitStatsView
from .icons import make_icon
from .widgets import StatusLabel

CHANGE_ROLE = Qt.UserRole + 1  # a file's row: its git_status.Change
DIFF, HISTORY, STATS = "diff", "history", "stats"  # the panel's views
VIEW_TITLES = {DIFF: "Diff", HISTORY: "History", STATS: "Stats"}
VIEW_TIPS = {DIFF: "The changed files, and the selected one's diff",
             HISTORY: "The commits, with their branches and tags and the graph of how they join",
             STATS: "Statistics about the repository, and everyone's commits over time"}
LETTER_COLORS = {"M": "#e8a317", "A": "#2f9e44", "D": "#e03131", "R": "#4c6ef5", "T": "#4c6ef5",
                 "U": "#2f9e44", "!": "#e03131"}


class GitPanel(QWidget):
    """Its header (the views' buttons, the branch, Unified and Refresh) over
    the view chosen: Diff (the changed files, left, and the selected one's
    diff, right), History or Stats."""

    refresh_requested = Signal()
    open_requested = Signal(str, int)  # file, line
    reveal_requested = Signal(str)  # a changed file's "Show in Project Files": its path
    discard_requested = Signal(object)  # a changed file's "Discard Changes…": its gs.Change
    view_changed = Signal(str)  # DIFF, HISTORY or STATS chosen in the header

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status: gs.RepoStatus | None = None
        self.icon_for = None  # a file's QIcon, as in Project files (set by the window)
        self.is_light = True
        self.palette_colors = PALETTES["light"]
        self._shown: dict[str, tuple] = {}    # HISTORY, STATS: what's on show ((root, HEAD…): see _key)
        self._reading: dict[str, tuple] = {}  # and what's being read
        self.setObjectName("GitPanel")
        self.setAttribute(Qt.WA_StyledBackground, True)

        self.title_icon = TitleIcon("git")
        self.title = QLabel("Git")
        self.subtitle = StatusLabel()
        # Icon buttons (as the Debug panel's: see refresh_palette), their tooltips saying what they do.
        self.unified_button = QToolButton()
        self.unified_button.setText("Unified")
        self.unified_button.setCheckable(True)
        self.unified_button.setToolTip("Show the diff as one list (removed lines, then added ones) "
                                       "instead of side by side")
        self.unified_button.toggled.connect(self._set_unified)
        refresh = self.refresh_button = QToolButton()
        refresh.setText("Refresh")
        refresh.setToolTip("Read the repository's status again")
        refresh.clicked.connect(self._refresh_clicked)
        # The views: a pair of buttons, one of them on.
        self.view_buttons: dict[str, QToolButton] = {}
        self.view_group = QButtonGroup(self)
        for view in (DIFF, HISTORY, STATS):
            button = self.view_buttons[view] = QToolButton()
            button.setObjectName("GitView")
            button.setText(VIEW_TITLES[view])
            button.setToolTip(VIEW_TIPS[view])
            button.setCheckable(True)
            button.setAutoRaise(True)
            button.clicked.connect(lambda _checked=False, v=view: self.set_view(v, user=True))
            self.view_group.addButton(button)
        self.view_buttons[DIFF].setChecked(True)
        for button in (self.unified_button, refresh):
            button.setToolButtonStyle(Qt.ToolButtonIconOnly)
            button.setIconSize(QSize(18, 18))
            button.setAutoRaise(True)
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title_icon)
        header.addWidget(self.title)
        header.addSpacing(8)
        for button in self.view_buttons.values():
            header.addWidget(button)
        header.addSpacing(8)
        header.addWidget(self.subtitle, 1)
        header.addWidget(self.unified_button)
        header.addWidget(refresh)

        self.files = QTreeWidget()
        self.files.setHeaderHidden(True)
        self.files.setRootIsDecorated(False)
        self.files.setIndentation(10)
        self.files.setColumnCount(2)
        self.files.setUniformRowHeights(True)
        self.files.currentItemChanged.connect(self._show_selected)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._file_menu)
        self.file_menu: QMenu | None = None  # (kept while it's open)
        self.file_diff = FileDiff()
        self.file_diff.open_requested.connect(self._open_line)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.files)
        self.splitter.addWidget(self.file_diff)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setStretchFactor(1, 3)

        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)

        diff_page = QWidget()
        diff_layout = QVBoxLayout(diff_page)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        diff_layout.addWidget(self.splitter, 1)
        diff_layout.addWidget(self.empty, 1)
        self.stats_view = GitStatsView()
        self.log_view = GitLogView()
        self.log_view.open_requested.connect(self.open_requested)
        self.log_view.reveal_requested.connect(self.reveal_requested)
        # The pages read from the repository (see the module's description).
        self.read_pages: dict[str, GitLogView | GitStatsView] = {HISTORY: self.log_view, STATS: self.stats_view}
        for page in self.read_pages.values():
            page.reload_requested.connect(self._read_view)
        self.pages = QStackedWidget()
        self.views: dict[str, QWidget] = {DIFF: diff_page, **self.read_pages}
        for page in self.views.values():
            self.pages.addWidget(page)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addWidget(self.pages, 1)
        self.refresh_fonts()
        self.show_message("Open a project in a git repository to see its changes here.")

    # ------------------------------------------------------------ styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        # (Unified, while on: a background, so it shows.)
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal)
                           + f"#{self.objectName()} QToolButton:checked {{ background: rgba(128,128,128,70);"
                             " border: none; border-radius: 5px; }"
                           + f"#{self.objectName()} QToolButton#GitView {{ padding: 2px 10px; color: {pal['muted']}; }}"
                           + f"#{self.objectName()} QToolButton#GitView:checked {{ color: {pal['text']}; }}")
        self.title_icon.draw(pal["text"], self.title)
        self.unified_button.setIcon(make_icon("unified", pal["text"]))
        self.refresh_button.setIcon(make_icon("refresh", pal["text"]))
        for label in (self.subtitle, self.empty):
            label.setStyleSheet(f"color: {pal['muted']};")
        self.file_diff.refresh_style(pal["muted"])
        for page in self.read_pages.values():
            page.refresh_palette(pal, not self.is_light)

    def refresh_fonts(self):
        font = QFont(QApplication.font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.Bold)
        self.title.setFont(font)
        self.refresh_palette()

    def set_code_font(self, font: QFont):
        """The diffs' font (the editor's)."""
        self.file_diff.set_font(font)
        self.log_view.set_code_font(font)

    def _set_unified(self, on: bool):
        """Unified: the diffs (the Diff page's, the History's) as one list."""
        for diff in (self.file_diff, self.log_view.file_diff):
            diff.set_unified(on)

    # -------------------------------------------------------------- views
    def view(self) -> str:
        page = self.pages.currentWidget()
        return next((view for view, shown in self.views.items() if shown is page), DIFF)

    def set_view(self, view: str, user: bool = False):
        """Show the Diff, the History or the Stats (``user``: chosen in the
        header: said with view_changed, to be remembered)."""
        view = view if view in self.views else DIFF
        self.view_buttons[view].setChecked(True)
        self.pages.setCurrentWidget(self.views[view])
        self.unified_button.setVisible(view in (DIFF, HISTORY))  # (both have a diff)
        self._read_view()
        if user:
            self.view_changed.emit(view)

    def _refresh_clicked(self):
        # (Read again even if HEAD hasn't moved: a new branch, a tag, a fetch… And a read under way
        # since before: its answer may be from before, so it's dropped.)
        self._shown.clear()
        self._reading.clear()
        self.refresh_requested.emit()

    def _key(self, view: str) -> tuple:
        """What the view's on show for: the repository, HEAD, and what the page adds (its reload_key)."""
        return (self.status.root, self.status.head, *self.read_pages[view].reload_key())

    def _read_view(self):
        """The view on show (History or Stats), read again if what it's for
        has changed (see _key)."""
        view = self.view()
        if view not in self.read_pages or self.status is None:
            return
        page, key = self.read_pages[view], self._key(view)
        if key in (self._shown.get(view), self._reading.get(view)):
            return
        if self.status.head is None:
            page.show_message(f"No commits in {self.status.root.name} yet.")
            self._shown[view] = key
            return
        if self._shown.get(view) is None or self._shown[view][0] != key[0]:
            page.show_message("Reading the repository…")
        self._reading[view] = key
        read = page.reader(self.status.root)
        call_in_background(lambda: self._safely(read), lambda result: self._view_read(view, key, result), self)

    @staticmethod
    def _safely(read):
        try:
            return read()
        except (gs.GitError, OSError) as error:
            return str(error) or type(error).__name__

    def _view_read(self, view: str, key: tuple, result):
        if self._reading.get(view) != key:
            return  # (another one asked for meanwhile, Refresh clicked, or the project closed)
        self._reading.pop(view)
        self._shown[view] = key
        page = self.read_pages[view]
        if isinstance(result, str):
            page.show_message(f"Couldn't read the repository's history: {result}")
        elif result is None:
            page.show_message(f"No commits in {key[0].name} yet.")
        else:
            page.show_result(result)
        if self.status is None or self._key(view) != key:
            self._read_view()  # (it moved on meanwhile)

    # -------------------------------------------------------------- state
    def show_message(self, message: str):
        """Nothing to list: why."""
        self.status = None
        self._shown.clear()
        self._reading.clear()
        for page in self.read_pages.values():
            page.show_message(message)
        self.subtitle.setText("")
        self.empty.setText(message)
        self.empty.show()
        self.splitter.hide()
        self.files.clear()
        self.file_diff.clear()

    def show_status(self, status: gs.RepoStatus):
        """A repository's status; the file shown before stays selected if it's
        still changed."""
        current = self.selected()
        self.status = status
        self.subtitle.setText(status.summary())
        self.subtitle.setToolTip(str(status.root))
        self.files.blockSignals(True)
        self.files.clear()
        select = None
        for area in gs.AREAS:
            changes = status.in_area(area)
            if not changes:
                continue
            group = QTreeWidgetItem([f"{gs.AREA_TITLES[area]} ({len(changes)})"])
            group.setFlags(Qt.ItemIsEnabled)
            bold = QFont(self.files.font())
            bold.setWeight(QFont.DemiBold)
            group.setFont(0, bold)
            self.files.addTopLevelItem(group)
            for change in changes:
                item = QTreeWidgetItem([change.path, change.letter])
                item.setData(0, CHANGE_ROLE, change)
                item.setToolTip(0, f"{change.path}\n{gs.KIND_WORDS[change.kind]} · {gs.AREA_TITLES[area].lower()}")
                item.setForeground(1, QBrush(QColor(LETTER_COLORS.get(change.letter, "#868e96"))))
                item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
                if self.icon_for is not None:
                    item.setIcon(0, self.icon_for(status.root / change.path))
                group.addChild(item)
                if change == current:
                    select = item
            group.setExpanded(True)
        self.files.header().setStretchLastSection(False)
        self.files.header().setSectionResizeMode(0, self.files.header().ResizeMode.Stretch)
        self.files.header().setSectionResizeMode(1, self.files.header().ResizeMode.ResizeToContents)
        self.files.blockSignals(False)
        self._read_view()
        if not status.changes:
            self.empty.setText(f"No changes in {status.root.name} (on {status.summary()}).")
            self.empty.show()
            self.splitter.hide()
            self.file_diff.clear()
            return
        self.empty.hide()
        self.splitter.show()
        if select is None:
            select = self._first_file()
        self.files.setCurrentItem(select)
        self._show_selected(select)

    def selected(self) -> gs.Change | None:
        item = self.files.currentItem()
        return item.data(0, CHANGE_ROLE) if item is not None else None

    def _first_file(self) -> QTreeWidgetItem | None:
        for i in range(self.files.topLevelItemCount()):
            group = self.files.topLevelItem(i)
            if group.childCount():
                return group.child(0)
        return None

    def _show_selected(self, item, _previous=None):
        change = item.data(0, CHANGE_ROLE) if item is not None else None
        if change is None or self.status is None:
            return
        root = self.status.root

        def read():
            old, new, old_label, new_label = gs.versions(root, change)
            return old, new, f"{old_label} → {new_label}" if old_label else f"new file ({new_label})"

        self.file_diff.load((root, change), change.path, read)

    def _file_menu(self, pos):
        """A changed file's menu (a right-click on it): Show in Project Files,
        Discard Changes… (asked first: see window.git)."""
        item = self.files.itemAt(pos)
        change = item.data(0, CHANGE_ROLE) if item is not None else None
        if change is None or self.status is None:
            return  # (a group's title)
        path = self.status.root / change.path
        menu = self.file_menu = QMenu(self)
        show = menu.addAction("Show in Project Files", lambda: self.reveal_requested.emit(str(path)))
        if not path.exists():
            show.setEnabled(False)
            show.setText("Show in Project Files (deleted)")
        menu.addSeparator()
        discard = menu.addAction("Delete File…" if change.area == gs.UNTRACKED else "Discard Changes…",
                                 lambda: self.discard_requested.emit(change))
        if change.area == gs.CONFLICT:
            discard.setEnabled(False)
            discard.setText("Discard Changes… (resolve the conflict, or abort the merge, first)")
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.files.viewport().mapToGlobal(pos))

    def _open_line(self, line: int):
        change = self.selected()
        if change is not None and self.status is not None:
            path = self.status.root / change.path
            if Path(path).is_file():
                self.open_requested.emit(str(path), line)
