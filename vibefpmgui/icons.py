"""Small vector icons for the command toolbar, drawn with QPainter so they
match the current text colour (light or dark mode) at any screen scale."""

from __future__ import annotations

from qtpy.QtCore import QPointF, QRectF, Qt
from qtpy.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

SIZE = 24  # logical size; drawn at several pixel ratios for crisp HiDPI


def _canvas(ratio: float) -> tuple[QPixmap, QPainter]:
    pixmap = QPixmap(int(SIZE * ratio), int(SIZE * ratio))
    pixmap.setDevicePixelRatio(ratio)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    return pixmap, painter


def _pen(color: QColor, width: float = 1.8) -> QPen:
    return QPen(color, width, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)


def _hammer(p: QPainter, c: QColor):
    p.translate(12, 12)
    p.rotate(-45)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawRoundedRect(QRectF(-1.3, -3, 2.6, 13), 1.2, 1.2)  # handle
    head = QPainterPath()
    head.addRoundedRect(QRectF(-6.5, -8, 11, 5.5), 1.5, 1.5)
    p.drawPath(head)
    p.drawRoundedRect(QRectF(4, -7.2, 2.6, 3.9), 0.8, 0.8)  # striking face


def _play(p: QPainter, c: QColor):
    path = QPainterPath(QPointF(8, 5.5))
    path.lineTo(18.5, 12)
    path.lineTo(8, 18.5)
    path.closeSubpath()
    p.setPen(QPen(c, 2.2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    p.setBrush(c)
    p.drawPath(path)


def _flask(p: QPainter, c: QColor):
    body = QPainterPath(QPointF(9.5, 4))
    body.lineTo(9.5, 10)
    body.lineTo(4.5, 18.2)
    body.quadTo(3.6, 20, 5.6, 20)
    body.lineTo(18.4, 20)
    body.quadTo(20.4, 20, 19.5, 18.2)
    body.lineTo(14.5, 10)
    body.lineTo(14.5, 4)
    p.setPen(_pen(c))
    p.setBrush(Qt.NoBrush)
    p.drawPath(body)
    p.drawLine(QPointF(8, 4), QPointF(16, 4))
    liquid = QPainterPath(QPointF(6.8, 14.5))
    liquid.lineTo(17.2, 14.5)
    liquid.lineTo(19.0, 17.6)
    liquid.quadTo(19.6, 18.8, 18.2, 18.8)
    liquid.lineTo(5.8, 18.8)
    liquid.quadTo(4.4, 18.8, 5.0, 17.6)
    liquid.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPath(liquid)


def _download(p: QPainter, c: QColor):
    p.setPen(_pen(c, 2))
    p.drawLine(QPointF(12, 3.5), QPointF(12, 14))
    p.drawPolyline([QPointF(7.5, 9.8), QPointF(12, 14.3), QPointF(16.5, 9.8)])
    tray = QPainterPath(QPointF(4, 14.5))
    tray.lineTo(4, 18.5)
    tray.quadTo(4, 20, 5.5, 20)
    tray.lineTo(18.5, 20)
    tray.quadTo(20, 20, 20, 18.5)
    tray.lineTo(20, 14.5)
    p.setBrush(Qt.NoBrush)
    p.drawPath(tray)


def _broom(p: QPainter, c: QColor):
    p.setPen(_pen(c, 2))
    p.drawLine(QPointF(18.5, 3.5), QPointF(12.6, 11.4))  # handle
    bristles = QPainterPath(QPointF(10.2, 10.0))
    bristles.lineTo(15.0, 13.6)
    bristles.quadTo(13.8, 18.5, 9.8, 20.5)
    bristles.lineTo(3.6, 15.9)
    bristles.quadTo(7.6, 14.6, 10.2, 10.0)
    bristles.closeSubpath()
    p.setPen(_pen(c, 1.6))
    p.setBrush(Qt.NoBrush)
    p.drawPath(bristles)
    p.drawLine(QPointF(7.2, 17.9), QPointF(9.6, 14.6))
    p.drawLine(QPointF(10.0, 19.4), QPointF(12.0, 16.4))


def _trash(p: QPainter, c: QColor):
    p.setPen(_pen(c))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(4.5, 6.5), QPointF(19.5, 6.5))
    p.drawPolyline([QPointF(9.5, 6.5), QPointF(9.5, 4), QPointF(14.5, 4), QPointF(14.5, 6.5)])
    can = QPainterPath(QPointF(6.2, 6.5))
    can.lineTo(7.2, 18.6)
    can.quadTo(7.4, 20, 8.8, 20)
    can.lineTo(15.2, 20)
    can.quadTo(16.6, 20, 16.8, 18.6)
    can.lineTo(17.8, 6.5)
    p.drawPath(can)
    p.drawLine(QPointF(10.2, 10), QPointF(10.2, 16.5))
    p.drawLine(QPointF(13.8, 10), QPointF(13.8, 16.5))


def _stop(p: QPainter, c: QColor):
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawRoundedRect(QRectF(6, 6, 12, 12), 2.5, 2.5)


def _robot(p: QPainter, c: QColor):
    p.setPen(_pen(c, 1.7))
    p.setBrush(Qt.NoBrush)
    # Antenna with a ball on top.
    p.drawLine(QPointF(12, 4.6), QPointF(12, 7.2))
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 3.4), 1.6, 1.6)
    # Head.
    p.setPen(_pen(c, 1.7))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(4.5, 7.2, 15, 12.3), 3.6, 3.6)
    # Ears.
    p.drawLine(QPointF(2.6, 11.6), QPointF(2.6, 15.0))
    p.drawLine(QPointF(21.4, 11.6), QPointF(21.4, 15.0))
    # Eyes and mouth.
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawEllipse(QPointF(9.0, 12.3), 1.7, 1.7)
    p.drawEllipse(QPointF(15.0, 12.3), 1.7, 1.7)
    p.setPen(_pen(c, 1.5))
    p.drawLine(QPointF(9.6, 16.2), QPointF(14.4, 16.2))


DRAWERS = {
    "build": _hammer,
    "run": _play,
    "test": _flask,
    "update": _download,
    "clean": _broom,
    "clean-all": _trash,
    "stop": _stop,
    "robot": _robot,
}


def make_icon(name: str, color: QColor | str) -> QIcon:
    """An icon by name (see ``DRAWERS``) in the given colour; Qt derives the
    greyed-out disabled look from it."""
    color = QColor(color)
    icon = QIcon()
    for ratio in (1, 2, 3):
        pixmap, painter = _canvas(ratio)
        DRAWERS[name](painter, color)
        painter.end()
        icon.addPixmap(pixmap)
    return icon

