"""The Git panel's *History* page (see `vibefpmgui.git_log`): the commits,
newest first, each with the graph's lanes (its dot, the lines to its
parents), its branches and tags (badges: the branch checked out in bold),
subject, author, date and hash; every branch's, or only what's checked out
(*All branches*). Choosing a commit shows its card (as git blame's: the
whole message, links to its web page and issues), the files it changed, and
the chosen file's diff (double-click a line: the file, there).
"""

from __future__ import annotations

import time
from pathlib import Path

from qtpy.QtCore import (
    QAbstractTableModel,
    QModelIndex,
    QPointF,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from qtpy.QtGui import QBrush, QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from qtpy.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QSplitter,
    QStackedLayout,
    QStyle,
    QStyledItemDelegate,
    QTextBrowser,
    QToolButton,
    QTreeView,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import git_blame as gb
from . import git_log as gl
from . import git_status as gs
from .background import call_in_background
from .diff_view import DiffView

COLUMNS = ["Commit", "Author", "Date", "Hash"]
MAX_LANES = 16  # lanes drawn (a wider graph: the rest cut off)
LANE_COLORS = {
    "light": ["#4c6ef5", "#e8590c", "#2f9e44", "#ae3ec9", "#1098ad", "#e03131", "#f08c00", "#5c940d"],
    "dark": ["#748ffc", "#ff922b", "#51cf66", "#da77f2", "#3bc9db", "#ff6b6b", "#fcc419", "#94d82d"],
}
REF_COLORS = {gl.HEAD: "#e03131", gl.BRANCH: "#2f9e44", gl.REMOTE: "#1c7ed6", gl.TAG: "#e8a317"}
COMMIT_ROLE = Qt.UserRole + 1   # a row's git_log.LogCommit
ROW_ROLE = Qt.UserRole + 2      # and its git_log.GraphRow
CHANGE_ROLE = Qt.UserRole + 3   # a changed file's git_blame.FileChange


def date_words(seconds: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(seconds)) if seconds else ""


class LogModel(QAbstractTableModel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.log: gl.Log | None = None
        self.lanes = 1  # the graph's width (lanes), the widest row's

    def set_log(self, log: gl.Log | None):
        self.beginResetModel()
        self.log = log
        self.lanes = min(MAX_LANES, max((r.width for r in log.rows), default=1)) if log else 1
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):  # noqa: B008 - Qt's signature
        return 0 if parent.isValid() or self.log is None else len(self.log.commits)

    def columnCount(self, parent=QModelIndex()):  # noqa: B008 - Qt's signature
        return 0 if parent.isValid() else len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        if not index.isValid() or self.log is None:
            return None
        commit = self.log.commits[index.row()]
        column = index.column()
        if role == Qt.DisplayRole:
            return [commit.subject, commit.author, date_words(commit.time), commit.sha[:8]][column]
        if role == Qt.ToolTipRole:
            refs = ", ".join(r.name for r in commit.refs)
            return [f"{commit.subject}" + (f"\n{refs}" if refs else ""), f"{commit.author} <{commit.email}>",
                    None, commit.sha][column]
        if role == COMMIT_ROLE:
            return commit
        if role == ROW_ROLE:
            return self.log.rows[index.row()]
        return None


class GraphDelegate(QStyledItemDelegate):
    """The Commit column: the graph's lanes, the branch and tag badges, the subject."""

    def __init__(self, view: GitLogView):
        super().__init__(view.table)
        self.view = view

    def lane_width(self, option) -> float:
        return max(12.0, QFontMetrics(option.font).height() * 0.8)

    def sizeHint(self, option, index):
        size = super().sizeHint(option, index)
        return QSize(size.width(), max(size.height(), round(QFontMetrics(option.font).height() * 1.5)))

    def paint(self, painter, option, index):
        commit: gl.LogCommit = index.data(COMMIT_ROLE)
        row: gl.GraphRow = index.data(ROW_ROLE)
        style = option.widget.style() if option.widget else QApplication.style()
        style.drawPrimitive(QStyle.PE_PanelItemViewItem, option, painter, option.widget)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        rect = QRectF(option.rect)
        lane = self.lane_width(option)
        colors = LANE_COLORS["dark" if self.view.dark else "light"]
        top, bottom, middle = rect.top(), rect.bottom() + 1, rect.center().y()

        def x(i: int) -> float:
            return rect.left() + 4 + lane * (i + 0.5)

        def pen(color: int) -> QPen:
            line = QPen(QColor(colors[color % len(colors)]), 2)
            line.setCapStyle(Qt.RoundCap)
            return line

        painter.setClipRect(QRectF(rect.left(), top, 4 + lane * self.view.model.lanes, rect.height()))
        for i, color in row.through:
            painter.setPen(pen(color))
            painter.drawLine(QPointF(x(i), top), QPointF(x(i), bottom))
        for i, color in row.joining:  # (from its lane above, curving into the commit)
            painter.setPen(pen(color))
            path = QPainterPath(QPointF(x(i), top))
            path.cubicTo(QPointF(x(i), middle), QPointF(x(i), middle), QPointF(x(row.lane), middle))
            painter.drawPath(path)
        if row.from_above:
            painter.setPen(pen(row.color))
            painter.drawLine(QPointF(x(row.lane), top), QPointF(x(row.lane), middle))
        for i, color in row.parents:
            painter.setPen(pen(color))
            if i == row.lane:
                painter.drawLine(QPointF(x(i), middle), QPointF(x(i), bottom))
            else:  # (a merge's other parent, or a lane waiting for it: curving out to it)
                path = QPainterPath(QPointF(x(row.lane), middle))
                path.cubicTo(QPointF(x(i), middle), QPointF(x(i), middle), QPointF(x(i), bottom))
                painter.drawPath(path)
        radius = lane * 0.28
        dot = QColor(colors[row.color % len(colors)])
        center = QPointF(x(row.lane), middle)
        is_head = self.view.model.log is not None and commit.sha == self.view.model.log.head
        if len(commit.parents) > 1:  # (a merge: a ring)
            painter.setPen(QPen(dot, 2))
            painter.setBrush(QBrush(QColor(self.view.pal.get("bg", "#ffffff"))))
        else:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(dot))
        painter.drawEllipse(center, radius, radius)
        if is_head:  # (what's checked out: a ring around it)
            painter.setPen(QPen(QColor(self.view.pal.get("text", "#000000")), 1.5))
            painter.setBrush(Qt.NoBrush)
            painter.drawEllipse(center, radius + 3, radius + 3)
        painter.setClipping(False)

        selected = bool(option.state & QStyle.State_Selected)
        left = rect.left() + 4 + lane * self.view.model.lanes + 6
        small = QFont(option.font)
        small.setPointSizeF(max(7.0, option.font.pointSizeF() - 1.5))
        badge_metrics = QFontMetrics(small)
        for ref in commit.refs:
            if left > rect.right() - 40:
                break
            bold = QFont(small)
            bold.setBold(ref.current)
            text_width = QFontMetrics(bold).horizontalAdvance(ref.name)
            badge = QRectF(left, middle - badge_metrics.height() / 2 - 1, text_width + 10, badge_metrics.height() + 2)
            color = QColor(REF_COLORS[ref.kind])
            solid = ref.current or selected  # (on the selection's color: solid, so it shows)
            painter.setPen(QPen(QColor("white") if selected else color, 1.2 if ref.current else 1))
            fill = QColor(color)
            fill.setAlphaF(0.9 if solid else 0.18)
            painter.setBrush(fill)
            painter.drawRoundedRect(badge, 4, 4)
            painter.setFont(bold)
            painter.setPen(QColor("white") if solid else color if not self.view.dark else color.lighter(130))
            painter.drawText(badge, Qt.AlignCenter, ref.name)
            left = badge.right() + 4
        painter.setFont(option.font)
        painter.setPen(option.palette.color(option.palette.ColorRole.HighlightedText if selected
                                            else option.palette.ColorRole.Text))
        text_rect = QRectF(left + 2, rect.top(), rect.right() - left - 4, rect.height())
        painter.drawText(text_rect, Qt.AlignVCenter | Qt.AlignLeft,
                         QFontMetrics(option.font).elidedText(commit.subject, Qt.ElideRight, int(text_rect.width())))
        painter.restore()


class GitLogView(QWidget):
    open_requested = Signal(str, int)        # a file of the working tree, a line
    reveal_requested = Signal(str)           # a changed file's "Show in Project Files": its path now
    all_branches_changed = Signal(bool)      # All branches toggled
    reload_requested = Signal()              # (All branches toggled: read again)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.log: gl.Log | None = None
        self.pal: dict = {}
        self.dark = False
        self.icon_for = None   # a file's QIcon (set by the panel)
        self._details_for: str | None = None   # the commit whose details are being read
        self._diff_for: tuple | None = None    # (commit, file) whose versions are being read

        self.all_branches = QToolButton()
        self.all_branches.setObjectName("GitView")
        self.all_branches.setText("All branches")
        self.all_branches.setToolTip("Every branch's commits (local and remote) and tags', or only those of what's "
                                     "checked out")
        self.all_branches.setCheckable(True)
        self.all_branches.setChecked(True)
        self.all_branches.setAutoRaise(True)
        self.all_branches.clicked.connect(self._all_branches_clicked)
        self.count = QLabel()
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.addWidget(self.count, 1)
        bar.addWidget(self.all_branches)

        self.model = LogModel(self)
        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setAllColumnsShowFocus(True)
        self.table.setItemDelegateForColumn(0, GraphDelegate(self))
        self.table.header().setStretchLastSection(False)
        self.table.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.table.header().setSectionResizeMode(column, QHeaderView.Interactive)
        self.table.selectionModel().currentRowChanged.connect(self._commit_chosen)

        self.card = QTextBrowser()
        self.card.setOpenLinks(False)
        self.card.anchorClicked.connect(self._link)
        self.files = QTreeWidget()
        self.files.setHeaderHidden(True)
        self.files.setRootIsDecorated(False)
        self.files.setColumnCount(3)  # (the file, its lines added and deleted, its letter: M, A, D…)
        self.files.setUniformRowHeights(True)
        self.files.currentItemChanged.connect(self._file_chosen)
        self.files.setContextMenuPolicy(Qt.CustomContextMenu)
        self.files.customContextMenuRequested.connect(self._file_menu)
        self.file_menu: QMenu | None = None  # (kept while it's open)
        self.diff_title = QLabel()
        self.diff = DiffView()
        self.diff.open_requested.connect(self._open_line)
        diff_side = QWidget()
        diff_layout = QVBoxLayout(diff_side)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        diff_layout.setSpacing(4)
        diff_layout.addWidget(self.diff_title)
        diff_layout.addWidget(self.diff, 1)
        details = QSplitter(Qt.Vertical)
        details.addWidget(self.card)
        details.addWidget(self.files)
        details.setStretchFactor(0, 3)
        details.setStretchFactor(1, 2)
        self.bottom = QSplitter(Qt.Horizontal)
        self.bottom.addWidget(details)
        self.bottom.addWidget(diff_side)
        self.bottom.setStretchFactor(0, 2)
        self.bottom.setStretchFactor(1, 3)
        self.splitter = QSplitter(Qt.Vertical)
        self.splitter.addWidget(self.table)
        self.splitter.addWidget(self.bottom)
        self.splitter.setStretchFactor(0, 3)
        self.splitter.setStretchFactor(1, 2)

        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(bar)
        layout.addWidget(self.splitter, 1)
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.stack = QStackedLayout(self)
        self.stack.addWidget(self.message)
        self.stack.addWidget(content)
        self.content = content
        self.show_message("Reading the repository…")

    # ------------------------------------------------------------ styling
    def refresh_palette(self, pal: dict, dark: bool):
        self.pal, self.dark = pal, dark
        for label in (self.count, self.message, self.diff_title):
            label.setStyleSheet(f"color: {pal['muted']};")
        self.diff.refresh_style()
        self.table.viewport().update()
        if self.log is not None:
            self._commit_chosen(self.table.currentIndex())  # (the card's colors)

    def set_code_font(self, font: QFont):
        self.diff.set_font(font)

    # -------------------------------------------------------------- state
    def show_message(self, text: str):
        self.log = None
        self.model.set_log(None)
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def show_log(self, log: gl.Log):
        """A repository's history; the commit chosen before stays chosen if it's still listed."""
        before = self.current_commit()
        self.log = log
        self.model.set_log(log)
        self.all_branches.setChecked(log.all_branches)
        n = len(log.commits)
        self.count.setText(f"{'The newest ' if log.limited else ''}{n:,} commit{'s' * (n != 1)}"
                           + (" of every branch" if log.all_branches else " of what's checked out"))
        width = QFontMetrics(self.table.font()).horizontalAdvance("0000-00-00 00:00") + 16
        self.table.setColumnWidth(1, 160)
        self.table.setColumnWidth(2, width)
        self.table.setColumnWidth(3, QFontMetrics(self.table.font()).horizontalAdvance("00000000") + 16)
        self.stack.setCurrentWidget(self.content)
        row = next((i for i, c in enumerate(log.commits) if before and c.sha == before.sha), None)
        if row is None:
            row = next((i for i, c in enumerate(log.commits) if c.sha == log.head), 0)
        self.table.setCurrentIndex(self.model.index(row, 0))
        self.table.scrollTo(self.model.index(row, 0), QTreeView.PositionAtCenter)

    def current_commit(self) -> gl.LogCommit | None:
        index = self.table.currentIndex()
        return index.data(COMMIT_ROLE) if index.isValid() else None

    def _all_branches_clicked(self, on: bool):
        self.all_branches_changed.emit(on)
        self.reload_requested.emit()

    # ------------------------------------------------------------ a commit
    def _commit_chosen(self, index, _previous=None):
        commit = index.data(COMMIT_ROLE) if index.isValid() else None
        self.files.clear()
        self.diff.clear()
        self.diff_title.setText("")
        if commit is None or self.log is None:
            self.card.clear()
            return
        self._details_for = commit.sha
        self._show_card(commit, None)
        root = self.log.root
        call_in_background(lambda: self._details(root, commit.sha),
                           lambda result: self._details_read(commit, result), self)

    @staticmethod
    def _details(root: Path, sha: str):
        try:
            return gb.commit_details(root, sha)
        except (gs.GitError, OSError) as error:
            return str(error) or type(error).__name__

    def _show_card(self, commit: gl.LogCommit, details):
        shown = details
        if isinstance(details, gb.CommitDetails):  # (its files: in the list under it)
            shown = gb.CommitDetails(details.email, details.message, details.committer, details.commit_time, None)
        text = QColor(self.pal.get("text", "#000000"))
        html = gb.commit_card_html(gb.Commit(commit.sha, commit.author, commit.time, commit.subject), shown,
                                   self.log.remote if self.log else "", text)
        self.card.setHtml(html)

    def _details_read(self, commit: gl.LogCommit, result):
        if self._details_for != commit.sha:
            return  # (another commit chosen meanwhile)
        self._show_card(commit, result)
        if not isinstance(result, gb.CommitDetails):
            return
        bold = QFont(self.files.font())
        bold.setBold(True)
        for change in result.files or []:
            name = f"{change.old_path} → {change.path}" if change.old_path else change.path
            counts = "binary" if change.added is None else f"+{change.added} −{change.deleted}"
            item = QTreeWidgetItem([name, counts, change.status])
            item.setData(0, CHANGE_ROLE, change)
            word, color = gb.STATUS_CHIPS.get(change.status, (change.status, "#868e96"))
            item.setToolTip(0, f"{change.path}\n{word}")
            item.setTextAlignment(1, Qt.AlignRight | Qt.AlignVCenter)
            item.setForeground(1, QBrush(QColor(self.pal.get("muted", "#888888"))))
            item.setForeground(2, QBrush(QColor(color)))
            item.setToolTip(2, word)
            if self.icon_for is not None and self.log is not None:
                item.setIcon(0, self.icon_for(self.log.root / change.path))
            self.files.addTopLevelItem(item)
        header = self.files.header()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        if self.files.topLevelItemCount():
            self.files.setCurrentItem(self.files.topLevelItem(0))

    # -------------------------------------------------------------- a file
    def _file_chosen(self, item, _previous=None):
        change = item.data(0, CHANGE_ROLE) if item is not None else None
        commit = self.current_commit()
        if change is None or commit is None or self.log is None:
            return
        self._diff_for = (commit.sha, change.path)
        self.diff_title.setText(f"<b>{change.path}</b> · reading…")
        root = self.log.root
        call_in_background(lambda: self._versions(root, commit, change),
                           lambda result: self._versions_read(commit, change, result), self)

    @staticmethod
    def _versions(root: Path, commit: gl.LogCommit, change: gb.FileChange):
        try:
            return gl.file_versions(root, commit, change)
        except (gs.GitError, OSError) as error:
            return str(error) or type(error).__name__

    def _versions_read(self, commit: gl.LogCommit, change: gb.FileChange, result):
        if self._diff_for != (commit.sha, change.path):
            return
        if isinstance(result, str):
            self.diff_title.setText(f"<b>{change.path}</b> · couldn't read it: {result}")
            self.diff.clear()
            return
        before, after = result
        if gs.BINARY in (before, after):
            self.diff_title.setText(f"<b>{change.path}</b> · binary or very big: no diff shown")
            self.diff.clear()
            return
        parent = commit.parents[0][:8] if commit.parents else ""
        self.diff_title.setText(f"<b>{change.path}</b> · " + (f"{parent} → {commit.sha[:8]}" if parent
                                                              else f"added in {commit.sha[:8]}"))
        self.diff.show_diff(before, after, change.path)

    def _file_menu(self, pos):
        """A changed file's menu (a right-click on it): Show in Project Files,
        if it's still there (where the commit left it) in the working tree."""
        item = self.files.itemAt(pos)
        change = item.data(0, CHANGE_ROLE) if item is not None else None
        if change is None or self.log is None:
            return
        path = self.log.root / change.path
        menu = self.file_menu = QMenu(self)
        show = menu.addAction("Show in Project Files", lambda: self.reveal_requested.emit(str(path)))
        if change.status == "D" or not path.exists():
            show.setEnabled(False)
            show.setText("Show in Project Files (not there now)")
        menu.aboutToHide.connect(menu.deleteLater)
        menu.popup(self.files.viewport().mapToGlobal(pos))

    def _open_line(self, line: int):
        item = self.files.currentItem()
        change = item.data(0, CHANGE_ROLE) if item is not None else None
        if change is not None and self.log is not None and (self.log.root / change.path).is_file():
            self.open_requested.emit(str(self.log.root / change.path), line)

    def _link(self, url):
        target = url.toString()
        if target.startswith("copy:"):
            QApplication.clipboard().setText(target.removeprefix("copy:"))
        elif target.startswith(("http:", "https:")):
            gb.open_web_page(target)
