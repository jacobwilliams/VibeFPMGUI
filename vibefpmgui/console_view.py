"""The Output panel's text: fpm's build progress ("[ 25%]" at a line's
start) drawn as a rounded badge, "25%", its color going from blue at 0%
to green at 100%. Drawn over the text, which stays as fpm wrote it (so
it's selected and copied as such).
"""

from __future__ import annotations

import re

from qtpy.QtCore import QRectF, Qt
from qtpy.QtGui import QColor, QFont, QPainter, QTextCursor
from qtpy.QtWidgets import QPlainTextEdit

PROGRESS_RE = re.compile(r"^\[\s*(\d{1,3})%\]")


def progress_color(percent: int) -> QColor:
    """Blue at 0%, green at 100% (through teal), bright enough for white text."""
    fraction = max(0, min(100, percent)) / 100
    return QColor.fromHsvF(0.60 - 0.24 * fraction, 0.70, 0.78)


class ConsoleView(QPlainTextEdit):
    """A read-only text view for command output, with fpm's progress as badges."""

    def progress_badges(self) -> list[tuple[int, QRectF]]:
        """(percent, where its badge is drawn) for the lines on view."""
        out = []
        block = self.firstVisibleBlock()
        offset = self.contentOffset()
        height = self.viewport().height()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.top() > height:
                break
            match = PROGRESS_RE.match(block.text())
            if match and block.isVisible():
                start, end = QTextCursor(block), QTextCursor(block)
                start.setPosition(block.position() + match.start())
                end.setPosition(block.position() + match.end())
                left, right = self.cursorRect(start), self.cursorRect(end)
                if left.top() == right.top():  # (not wrapped in the middle)
                    out.append((int(match.group(1)), QRectF(left.left(), left.top() + 1, right.left() - left.left(),
                                                            left.height() - 2)))
            block = block.next()
        return out

    def paintEvent(self, event):
        super().paintEvent(event)
        badges = self.progress_badges()
        if not badges:
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing)
        font = QFont(self.font())
        font.setBold(True)
        font.setPointSizeF(max(6.0, font.pointSizeF() * 0.85))
        painter.setFont(font)
        for percent, rect in badges:
            painter.setPen(Qt.NoPen)
            painter.setBrush(progress_color(percent))
            painter.drawRoundedRect(rect, rect.height() / 2, rect.height() / 2)
            painter.setPen(QColor("white"))
            painter.drawText(rect, Qt.AlignCenter, f"{percent}%")
        painter.end()
