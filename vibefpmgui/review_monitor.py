"""The "Review" tab: progress and findings of an AI code review."""

from __future__ import annotations

import time

from qtpy.QtCore import QEvent, QRectF, QSize, Qt, QTimer, Signal
from qtpy.QtGui import QColor, QFont, QFontDatabase, QFontMetricsF, QPainter, QPen
from qtpy.QtWidgets import (
    QAbstractItemView, QApplication, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QStyledItemDelegate, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .ai_review import CATEGORIES, Finding
from .build_monitor import (
    PALETTES, ROW_HEIGHT, ProgressLine, SortableTable, _tint, app_is_light, chip_stylesheet,
    panel_stylesheet, scaled,
)

CATEGORY_COLORS = {"bug": "#e03131", "warning": "#f08c00", "performance": "#0c8599",
                   "style": "#7048e8", "suggestion": "#1c7ed6"}
CATEGORY_ICONS = {"bug": "✕", "warning": "⚠", "performance": "⚡", "style": "✎", "suggestion": "💡"}


class FindingDelegate(QStyledItemDelegate):
    def __init__(self, monitor: "ReviewMonitor"):
        super().__init__(monitor.table)
        self.monitor = monitor

    def sizeHint(self, option, index):
        return QSize(100, scaled(ROW_HEIGHT))

    def paint(self, p: QPainter, option, index):
        mon = self.monitor
        if index.row() >= len(mon.findings):
            return
        f = mon.findings[index.row()]
        pal = mon.palette_colors
        r = QRectF(option.rect)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        if index.row() == mon.selected_row:
            p.fillRect(r, _tint(QColor(pal["accent1"]), 28))
            if index.column() == 0:
                p.fillRect(QRectF(r.left(), r.top() + 6, 3, r.height() - 12), QColor(pal["accent1"]))
        elif index.row() == mon.hover_row:
            p.fillRect(r, QColor(pal["hover"]))
        p.setPen(QPen(QColor(pal["line"]), 1))
        p.drawLine(r.bottomLeft(), r.bottomRight())
        small = QFont(option.font)
        small.setPointSizeF(max(option.font.pointSizeF() - 2, 7))
        color = QColor(CATEGORY_COLORS.get(f.category, pal["muted"]))
        if index.column() == 0:
            mono = QFontDatabase.systemFont(QFontDatabase.FixedFont)
            mono.setPointSizeF(option.font.pointSizeF())
            p.setFont(mono)
            p.setPen(QColor(pal["text"]))
            text = str(f.line) if f.end_line == f.line else f"{f.line}–{f.end_line}"
            p.drawText(r.adjusted(14, 0, -4, 0), Qt.AlignVCenter | Qt.AlignLeft, text)
        elif index.column() == 1:
            font = QFont(small)
            font.setWeight(QFont.DemiBold)
            label = f"{CATEGORY_ICONS.get(f.category, '')} {f.category}"
            pill = QRectF(r.left() + 6, r.center().y() - 11,
                          QFontMetricsF(font).horizontalAdvance(label) + 18, 22)
            p.setPen(Qt.NoPen)
            p.setBrush(_tint(color, 40))
            p.drawRoundedRect(pill, 11, 11)
            p.setFont(font)
            p.setPen(color.darker(115) if mon.is_light else color)
            p.drawText(pill, Qt.AlignCenter, label)
        else:
            p.setFont(option.font)
            p.setPen(QColor(pal["text"]))
            text_r = r.adjusted(8, 4, -8, -4)
            # Two lines of the comment, elided; the tooltip has all of it.
            p.drawText(text_r, Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap,
                       self._fit(f.comment, option.font, text_r))
        p.restore()

    @staticmethod
    def _fit(text: str, font: QFont, rect: QRectF) -> str:
        fm = QFontMetricsF(font)
        lines = max(1, int(rect.height() // fm.lineSpacing()))
        if fm.horizontalAdvance(text) <= rect.width() * lines * 0.95:
            return text
        return fm.elidedText(text, Qt.ElideRight, rect.width() * lines * 0.9)


def _category_rank(category: str) -> int:
    return CATEGORIES.index(category) if category in CATEGORIES else len(CATEGORIES)


class ReviewMonitor(SortableTable, QWidget):
    """Findings table with chips, a progress line and the model's summary.
    Clicking a finding emits :attr:`finding_selected` with its line."""

    COLUMNS = ["LINE", "CATEGORY", "COMMENT"]
    finding_selected = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.findings: list[Finding] = []
        self.hover_row = -1
        self.selected_row = -1
        self.filter_category: str | None = None
        self.running = False
        self._started = 0.0
        self.is_light = True
        self.palette_colors = PALETTES["light"]

        self.setObjectName("ReviewMonitor")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.title = QLabel("AI review")
        self.subtitle = QLabel()
        header = QHBoxLayout()
        header.setSpacing(6)
        header.addWidget(self.title)
        header.addSpacing(4)
        header.addWidget(self.subtitle)
        header.addStretch(1)
        self.chips: dict[str, QPushButton] = {}
        for category in CATEGORIES:
            chip = QPushButton()
            chip.setCheckable(True)
            chip.setCursor(Qt.PointingHandCursor)
            chip.setToolTip(f"Show only “{category}” findings (click again to show all)")
            chip.clicked.connect(lambda checked, c=category: self.set_filter(c if checked else None))
            chip.hide()
            header.addWidget(chip)
            self.chips[category] = chip

        self.progress = ProgressLine(self)
        self.progress_label = QLabel()
        progress_row = QHBoxLayout()
        progress_row.setSpacing(10)
        progress_row.addWidget(self.progress, 1)
        progress_row.addWidget(self.progress_label)

        self.table = QTableWidget(0, len(self.COLUMNS))
        self.table.setHorizontalHeaderLabels(self.COLUMNS)
        self.table.setItemDelegate(FindingDelegate(self))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(scaled(ROW_HEIGHT))
        self.table.setShowGrid(False)
        self.table.setFrameShape(QTableWidget.NoFrame)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.setFocusPolicy(Qt.NoFocus)
        self.table.setMouseTracking(True)
        self.table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.cellEntered.connect(self._set_hover)
        self.table.cellClicked.connect(self._row_clicked)
        self.table.viewport().installEventFilter(self)
        hh = self.table.horizontalHeader()
        hh.setHighlightSections(False)
        hh.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        hh.setSectionResizeMode(1, QHeaderView.Fixed)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self._init_sorting({
            0: lambda f: (f.line, f.end_line),
            1: lambda f: (_category_rank(f.category), f.line),
            2: lambda f: f.comment.lower(),
        })

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.empty = QLabel()
        self.empty.setAlignment(Qt.AlignCenter)
        self.empty.setWordWrap(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 6)
        layout.setSpacing(8)
        layout.addLayout(header)
        layout.addLayout(progress_row)
        layout.addWidget(self.table, 1)
        layout.addWidget(self.empty, 1)
        layout.addWidget(self.summary)

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._tick)
        self._tokens = 0
        self._status = ""
        self.refresh_fonts()
        self.show_message("Open a file in the editor and choose AI → Review Current File.")

    # ------------------------------------------------------------- styling
    def refresh_palette(self):
        self.is_light = app_is_light()
        self.palette_colors = pal = PALETTES["light" if self.is_light else "dark"]
        self.setStyleSheet(panel_stylesheet(self.objectName(), pal))
        for category, chip in self.chips.items():
            chip.setStyleSheet(chip_stylesheet(pal, CATEGORY_COLORS[category]))
        self.empty.setStyleSheet(f"color: {pal['muted']};")
        self.subtitle.setStyleSheet(f"color: {pal['muted']};")
        self.table.viewport().update()
        self.progress.update()

    def refresh_fonts(self):
        title_font = QFont(QApplication.font())
        title_font.setPointSizeF(title_font.pointSizeF() + 2)
        title_font.setWeight(QFont.Bold)
        self.title.setFont(title_font)
        self.table.verticalHeader().setDefaultSectionSize(scaled(ROW_HEIGHT))
        self.table.setColumnWidth(0, scaled(70))
        self.table.setColumnWidth(1, scaled(140))
        self.refresh_palette()

    # ------------------------------------------------------------- state
    def show_message(self, message: str | None):
        self.empty.setText(message or "")
        self.empty.setVisible(bool(message))
        self.table.setVisible(not message)

    def reset(self):
        """Back to the empty state (e.g. when another project is opened)."""
        self._timer.stop()
        self.running = False
        self.findings = []
        self.table.setRowCount(0)
        self.selected_row = -1
        self.summary.clear()
        self.subtitle.clear()
        self.set_filter(None)
        self._update_chips()
        self._started = 0.0
        self._status = ""
        self.progress.set(0, "running")
        self._update_label()
        self.show_message("Open a file in the editor and choose AI → Review Current File.")

    def begin(self, description: str, status: str = "Starting…"):
        """A review (or model download) is starting."""
        self.findings = []
        self.table.setRowCount(0)
        self.selected_row = -1
        self.summary.clear()
        self.set_filter(None)
        self.subtitle.setText(description)
        self._update_chips()
        self.running = True
        self._started = time.monotonic()
        self._tokens = 0
        self._status = status
        self.progress.set_busy(True)  # how long the model takes is unknown
        self.show_message("The model is reading the file…")
        self._timer.start()
        self._update_label()

    def set_status(self, status: str, fraction: float = -1.0):
        """Download progress (fraction 0..1) or a status text (fraction < 0)."""
        self._status = status
        if fraction >= 0:
            self.progress.set(fraction, "running")  # e.g. a download with a known size
        else:
            self.progress.set_busy(True)
        self._update_label()

    def add_text(self, text: str):
        self._tokens += 1  # streamed pieces are roughly tokens
        if self._tokens == 1:
            self.show_message("Reviewing… (findings appear when the model has finished)")
        self._status = "Reviewing"

    def set_findings(self, findings: list[Finding], summary: str):
        self.findings = sorted(findings, key=lambda f: (f.line, _category_rank(f.category)))
        for seq, f in enumerate(self.findings):
            f.seq = seq  # original order (by line), for "unsorted"
        self.table.setRowCount(len(self.findings))
        for row in range(len(self.findings)):
            for column in range(len(self.COLUMNS)):
                self.table.setItem(row, column, QTableWidgetItem())
            self._sync_row(row)
        self.apply_sort()  # keep the user's chosen order
        self._update_chips()
        self.summary.setText(f"<b>Summary:</b> {summary}" if summary else "")
        self.show_message(None if self.findings else
                          "No specific findings." + ("" if summary else
                                                     " (See the Output panel for the full answer.)"))

    def finish(self, ok: bool, message: str = ""):
        self.running = False
        self._timer.stop()
        self.progress.set(1.0, "ok" if ok else "failed")
        self._status = message
        if not ok:
            self.show_message(message)
        self._update_label()

    # ------------------------------------------------------------- display
    def _update_chips(self):
        counts = {c: 0 for c in CATEGORIES}
        for f in self.findings:
            counts[f.category] = counts.get(f.category, 0) + 1
        for category, chip in self.chips.items():
            chip.setText(f"{CATEGORY_ICONS[category]} {counts.get(category, 0)}")
            chip.setVisible(counts.get(category, 0) > 0)

    def set_filter(self, category: str | None):
        self.filter_category = category
        for c, chip in self.chips.items():
            chip.setChecked(c == category)
        for row, f in enumerate(self.findings):
            self.table.setRowHidden(row, category is not None and f.category != category)

    def _update_label(self):
        pal = self.palette_colors
        muted = f"<span style='color:{pal['muted']}'>"
        seconds = time.monotonic() - self._started if self._started else 0
        if self.running:
            extra = f" · {self._tokens} tokens" if self._tokens else ""
            text = f"<b>{self._status or 'Working'}…</b> {muted}{seconds:.0f} s{extra}</span>"
        elif self.findings or self._status:
            n = len(self.findings)
            text = f"<b>{n} finding{'s' * (n != 1)}</b> {muted}· {self._status}</span>"
        else:
            text = ""
        self.progress_label.setText(text)

    def _tick(self):
        self._update_label()  # elapsed time and token count

    # SortableTable hooks
    def rows(self) -> list[Finding]:
        return self.findings

    def _sync_row(self, row: int):
        f = self.findings[row]
        for column in range(len(self.COLUMNS)):
            self.table.item(row, column).setToolTip(f"Line {f.line}: {f.category}\n{f.comment}")

    def _reapply_filter(self):
        self.set_filter(self.filter_category)

    def _row_clicked(self, row: int, _column: int):
        if row < len(self.findings):
            self.selected_row = row
            self.table.viewport().update()
            self.finding_selected.emit(self.findings[row].line)

    def _set_hover(self, row: int, _column: int):
        if row != self.hover_row:
            self.hover_row = row
            self.table.viewport().update()

    def eventFilter(self, obj, event):
        if obj is self.table.viewport() and event.type() == QEvent.Leave:
            self._set_hover(-1, 0)
        return super().eventFilter(obj, event)
