"""The *Git* tab, with two views to switch between in its header:

* *Diff*: the project's repository's status (its branch, and the changed
  files: staged, not staged, untracked, in conflict), and the diff of the
  selected file (see `vibefpmgui.git_status`, `vibefpmgui.diff_view`).
  Double-clicking a line of the diff opens the file there.
* *History*: the commits, with their branches and tags and the graph of
  how they join; a commit's details, files and diffs (see
  `vibefpmgui.git_log`, `vibefpmgui.git_log_view`).
* *Stats*: numbers about the repository and each person's commits over time
  (see `vibefpmgui.git_stats`, `vibefpmgui.git_stats_view`), read when the
  view is on show and HEAD has moved since (or Refresh is clicked), as
  the History is.

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

from . import git_log as gl
from . import git_stats as gst
from . import git_status as gs
from .background import call_in_background
from .build_monitor import PALETTES, TitleIcon, app_is_light, panel_stylesheet
from .diff_view import DiffView
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
    """The changed files (left) and the selected one's diff (right)."""

    refresh_requested = Signal()
    open_requested = Signal(str, int)  # file, line
    reveal_requested = Signal(str)  # a changed file's "Show in Project Files": its path
    discard_requested = Signal(object)  # a changed file's "Discard Changes…": its gs.Change
    view_changed = Signal(str)  # DIFF, HISTORY or STATS chosen in the header

    def __init__(self, parent=None):
        super().__init__(parent)
        self.status: gs.RepoStatus | None = None
        self._showing: tuple[Path, gs.Change] | None = None  # (the change whose versions are being read)
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
        self.unified_button.toggled.connect(lambda on: (self.diff.set_unified(on),
                                                        self.log_view.diff.set_unified(on)))
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
        self.diff_title = StatusLabel()
        self.diff = DiffView()
        self.diff.open_requested.connect(self._open_line)
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_layout.addWidget(self.diff_title)
        right_layout.addWidget(self.diff, 1)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.addWidget(self.files)
        self.splitter.addWidget(right)
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
        self.log_view.reload_requested.connect(self._read_view)
        self.pages = QStackedWidget()
        self.pages.addWidget(diff_page)
        self.pages.addWidget(self.log_view)
        self.pages.addWidget(self.stats_view)

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
        for label in (self.subtitle, self.empty, self.diff_title):
            label.setStyleSheet(f"color: {pal['muted']};")
        self.diff.refresh_style()
        self.stats_view.refresh_palette(pal, not self.is_light)
        self.log_view.refresh_palette(pal, not self.is_light)

    def refresh_fonts(self):
        font = QFont(QApplication.font())
        font.setPointSizeF(font.pointSizeF() + 2)
        font.setWeight(QFont.Bold)
        self.title.setFont(font)
        self.refresh_palette()

    def set_code_font(self, font: QFont):
        """The diffs' font (the editor's)."""
        self.diff.set_font(font)
        self.log_view.set_code_font(font)

    # -------------------------------------------------------------- views
    def view(self) -> str:
        page = self.pages.currentWidget()
        return STATS if page is self.stats_view else HISTORY if page is self.log_view else DIFF

    def set_view(self, view: str, user: bool = False):
        """Show the Diff, the History or the Stats (``user``: chosen in the
        header: said with view_changed, to be remembered)."""
        view = view if view in VIEW_TITLES else DIFF
        self.view_buttons[view].setChecked(True)
        self.pages.setCurrentWidget({STATS: self.stats_view, HISTORY: self.log_view}.get(view, self.pages.widget(0)))
        self.unified_button.setVisible(view in (DIFF, HISTORY))  # (both have a diff)
        self._read_view()
        if user:
            self.view_changed.emit(view)

    def _refresh_clicked(self):
        self._shown.clear()  # (read again even if HEAD hasn't moved: a new branch, a tag, a fetch…)
        self.refresh_requested.emit()

    def _page(self, view: str):
        return {HISTORY: self.log_view, STATS: self.stats_view}[view]

    def _key(self, view: str) -> tuple:
        key = (self.status.root, self.status.head)
        return (*key, self.log_view.all_branches.isChecked()) if view == HISTORY else key

    def _read_view(self):
        """The History or the Stats, read again if they're on show and HEAD
        has moved (or, the History, All branches was toggled)."""
        view = self.view()
        if view == DIFF or self.status is None:
            return
        page, key = self._page(view), self._key(view)
        if key in (self._shown.get(view), self._reading.get(view)):
            return
        if self.status.head is None:
            page.show_message(f"No commits in {self.status.root.name} yet.")
            self._shown[view] = key
            return
        if self._shown.get(view) is None or self._shown[view][0] != key[0]:
            page.show_message("Reading the repository…")
        self._reading[view] = key
        root = self.status.root
        read = ((lambda: gl.read_log(root, all_branches=key[2])) if view == HISTORY
                else (lambda: gst.read_stats(root)))
        call_in_background(lambda: self._safely(read), lambda result: self._view_read(view, key, result), self)

    @staticmethod
    def _safely(read):
        try:
            return read()
        except (gs.GitError, OSError) as error:
            return str(error) or type(error).__name__

    def _view_read(self, view: str, key: tuple, result):
        if self._reading.get(view) != key:
            return  # (another one asked for meanwhile, or the project closed)
        self._reading.pop(view)
        self._shown[view] = key
        page = self._page(view)
        if isinstance(result, str):
            page.show_message(f"Couldn't read the repository's history: {result}")
        elif result is None:
            page.show_message(f"No commits in {key[0].name} yet.")
        elif view == HISTORY:
            page.show_log(result)
        else:
            page.show_stats(result)
        if self.status is None or self._key(view) != key:
            self._read_view()  # (it moved on meanwhile)

    # -------------------------------------------------------------- state
    def show_message(self, message: str):
        """Nothing to list: why."""
        self.status = None
        self._showing = None
        self._shown.clear()
        self._reading.clear()
        self.stats_view.stats = None
        self.stats_view.show_message(message)
        self.log_view.show_message(message)
        self.subtitle.setText("")
        self.empty.setText(message)
        self.empty.show()
        self.splitter.hide()
        self.files.clear()
        self.diff.clear()

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
            self._showing = None
            self.diff.clear()
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
        self._showing = (root, change)
        self.diff_title.setText(f"<b>{change.path}</b> · reading…")
        call_in_background(lambda: self._versions(root, change),
                           lambda result: self._show_versions(root, change, result), self)

    @staticmethod
    def _versions(root: Path, change: gs.Change):
        try:
            return gs.versions(root, change)
        except (gs.GitError, OSError) as error:
            return str(error)

    def _show_versions(self, root: Path, change: gs.Change, result):
        if self._showing != (root, change):
            return  # (another file chosen meanwhile)
        if isinstance(result, str):
            self.diff_title.setText(f"<b>{change.path}</b> · couldn't read it: {result}")
            self.diff.clear()
            return
        old, new, old_label, new_label = result
        if gs.BINARY in (old, new):
            self.diff_title.setText(f"<b>{change.path}</b> · binary or very big: no diff shown")
            self.diff.clear()
            return
        compared = f"{old_label} → {new_label}" if old_label else f"new file ({new_label})"
        self.diff_title.setText(f"<b>{change.path}</b> · {compared}")
        self.diff.show_diff(old, new, change.path)

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
