"""Small vector icons for the command toolbar, drawn with QPainter so they
match the current text color (light or dark mode) at any screen scale."""

from __future__ import annotations

import math
import re
from functools import cache
from pathlib import Path

from qtpy.QtCore import QPointF, QRectF, Qt
from qtpy.QtGui import (
    QColor,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)

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


def _format(p: QPainter, c: QColor):
    # Lines of code, indented as a formatter would.
    p.setPen(_pen(c, 2.0))
    for y, x0, x1 in ((5.5, 3.5, 14), (10, 8, 20.5), (14.5, 8, 17), (19, 3.5, 12)):
        p.drawLine(QPointF(x0, y), QPointF(x1, y))
    # A small sparkle.
    path = QPainterPath(QPointF(19.5, 14.5))
    for x, y in ((20.4, 17.1), (23, 18), (20.4, 18.9), (19.5, 21.5), (18.6, 18.9), (16, 18), (18.6, 17.1)):
        path.lineTo(x, y)
    path.closeSubpath()
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawPath(path)


def _more(p: QPainter, c: QColor):
    # ">>": the toolbar's overflow button.
    p.setPen(_pen(c, 1.9))
    for x in (6.5, 12.5):
        p.drawPolyline([QPointF(x, 7), QPointF(x + 5, 12), QPointF(x, 17)])


def _close(p: QPainter, c: QColor):
    # A small "×" (editor tab close buttons).
    p.setPen(_pen(c, 2.2))
    p.drawLine(QPointF(7, 7), QPointF(17, 17))
    p.drawLine(QPointF(17, 7), QPointF(7, 17))


def _coverage(p: QPainter, c: QColor):
    """A gutter of lines, most of them run (filled), one not (outlined)."""
    p.setPen(_pen(c))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(4, 4, 16, 16), 2.5, 2.5)
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    for y, width in ((7, 8), (11, 11), (15, 6)):
        p.drawRect(QRectF(7, y, width, 2))
    p.setPen(_pen(c, 1.4))
    p.setBrush(Qt.NoBrush)
    p.drawRect(QRectF(13.5, 14.5, 3.5, 3))


def _git(p: QPainter, c: QColor):
    """A branch: two commits on a line, and one branching off it."""
    p.setPen(_pen(c, 1.9))
    p.setBrush(Qt.NoBrush)
    p.drawLine(QPointF(7, 8.6), QPointF(7, 15.4))
    path = QPainterPath(QPointF(17, 9.6))
    path.cubicTo(QPointF(17, 14), QPointF(7, 12), QPointF(7, 15.4))
    p.drawPath(path)
    for x, y in ((7, 6), (7, 18), (17, 7)):
        p.drawEllipse(QPointF(x, y), 2.6, 2.6)


DRAWERS = {
    "build": _hammer,
    "run": _play,
    "test": _flask,
    "coverage": _coverage,
    "git": _git,
    "update": _download,
    "clean": _broom,
    "clean-all": _trash,
    "stop": _stop,
    "robot": _robot,
    "format": _format,
    "more": _more,
    "close": _close,
}


def make_icon(name: str, color: QColor | str) -> QIcon:
    """An icon by name (see ``DRAWERS``) in the given color; Qt derives the
    greyed-out disabled look from it."""
    color = QColor(color)
    icon = QIcon()
    for ratio in (1, 2, 3):
        pixmap, painter = _canvas(ratio)
        DRAWERS[name](painter, color)
        painter.end()
        icon.addPixmap(pixmap)
    return icon


# ------------------------------------------------------------ folder badges
def _badge_code(p: QPainter, c: QColor):
    p.setPen(_pen(c, 2.6))
    p.drawPolyline([QPointF(8.5, 7), QPointF(4, 12), QPointF(8.5, 17)])
    p.drawPolyline([QPointF(15.5, 7), QPointF(20, 12), QPointF(15.5, 17)])
    p.drawLine(QPointF(13.6, 5.5), QPointF(10.4, 18.5))


def _badge_bulb(p: QPainter, c: QColor):
    p.setPen(Qt.NoPen)
    p.setBrush(c)
    p.drawEllipse(QPointF(12, 9.5), 6, 6)
    p.drawRoundedRect(QRectF(9, 14, 6, 4.5), 1, 1)
    p.drawRoundedRect(QRectF(9.8, 19.2, 4.4, 2), 1, 1)


def _badge_hash(p: QPainter, c: QColor):
    p.setPen(_pen(c, 2.4))
    p.drawLine(QPointF(9.5, 4.5), QPointF(8, 19.5))
    p.drawLine(QPointF(16, 4.5), QPointF(14.5, 19.5))
    p.drawLine(QPointF(5, 9.5), QPointF(19.5, 9.5))
    p.drawLine(QPointF(4.5, 14.5), QPointF(19, 14.5))


def _badge_cube(p: QPainter, c: QColor):
    p.setPen(_pen(c, 2.0))
    p.setBrush(Qt.NoBrush)
    top = [QPointF(12, 3.5), QPointF(20, 7.5), QPointF(12, 11.5), QPointF(4, 7.5), QPointF(12, 3.5)]
    p.drawPolyline(top)
    p.drawPolyline([QPointF(4, 7.5), QPointF(4, 16.5), QPointF(12, 20.5), QPointF(20, 16.5),
                    QPointF(20, 7.5)])
    p.drawLine(QPointF(12, 11.5), QPointF(12, 20.5))


BADGES = {
    "code": _badge_code,
    "run": _play,
    "test": _flask,
    "example": _badge_bulb,
    "include": _badge_hash,
    "manifest": _badge_cube,
}
# Badge background colors by fpm folder role.
BADGE_COLORS = {
    "code": "#1c7ed6", "run": "#2f9e44", "test": "#7048e8", "example": "#e8590c",
    "include": "#868e96", "manifest": "#e8590c",
}


def badged_icon(base: QIcon, badge: str, sizes=(16, 20, 32)) -> QIcon:
    """``base`` (e.g. the system folder icon) with a small colored badge in
    its lower-right corner showing ``badge`` (see ``BADGES``)."""
    color = QColor(BADGE_COLORS.get(badge, "#868e96"))
    icon = QIcon()
    for size in sizes:
        for ratio in (1, 2):
            pixmap = QPixmap(int(size * ratio), int(size * ratio))
            pixmap.setDevicePixelRatio(ratio)
            pixmap.fill(Qt.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.Antialiasing)
            base.paint(painter, 0, 0, size, size)
            d = size * 0.62  # badge diameter
            x, y = size - d, size - d
            painter.setPen(QPen(QColor(255, 255, 255, 230), max(1.0, size / 16)))
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x, y, d, d))
            painter.save()
            painter.translate(x + d * 0.18, y + d * 0.18)
            painter.scale(d * 0.64 / SIZE, d * 0.64 / SIZE)
            BADGES[badge](painter, QColor("white"))
            painter.restore()
            painter.end()
            icon.addPixmap(pixmap)
    return icon



# ----------------------------------------------------------------- app icon
LOGO_PATH = Path(__file__).with_name("resources") / "fpm-logo-color.svg"
FORTRAN_FILE_PATH = Path(__file__).with_name("resources") / "fortran-file.svg"
# Fortran sources (any case, e.g. .F90): shown with the Fortran file icon.
FORTRAN_SUFFIXES = {".f", ".for", ".ftn", ".f77", ".f90", ".f95", ".f03", ".f08", ".f18", ".f23",
                    ".fpp"}
# Fortran include files: the same icon in gray.
FORTRAN_INCLUDE_SUFFIXES = {".inc", ".if"}
_fortran_icons: dict[bool, QIcon] = {}


def fortran_file_icon(include: bool = False) -> QIcon:
    """The icon for Fortran source files (a page with the Fortran "F"), or
    in gray for include files, drawn crisply at each size."""
    icon = _fortran_icons.get(include)
    if icon is None:
        from qtpy.QtCore import QByteArray
        from qtpy.QtSvg import QSvgRenderer

        svg = FORTRAN_FILE_PATH.read_text(encoding="utf-8")
        if include:
            svg = svg.replace("#734f96", "#6c757d").replace("#a98cc9", "#adb5bd")
        renderer = QSvgRenderer(QByteArray(svg.encode()))
        icon = _fortran_icons[include] = QIcon()
        if renderer.isValid():
            for size in (16, 20, 24, 32, 40, 48, 64):
                pixmap = QPixmap(size, size)
                pixmap.fill(Qt.transparent)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.Antialiasing)
                renderer.render(painter)
                painter.end()
                icon.addPixmap(pixmap)
    return icon


def app_icon() -> QIcon:
    """The application icon: the fpm logo centred on a white rounded square
    (the usual macOS app-icon shape), drawn crisply at each size."""
    from qtpy.QtSvg import QSvgRenderer

    renderer = QSvgRenderer(str(LOGO_PATH))
    icon = QIcon()
    if not renderer.isValid():
        return icon
    for size in (16, 32, 48, 64, 128, 256, 512, 1024):
        pixmap = QPixmap(size, size)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        draw_app_icon(p, size, renderer)
        p.end()
        icon.addPixmap(pixmap)
    return icon


def draw_app_icon(p: QPainter, size: float, renderer):
    """Paint the app icon (`app_icon`) at (0, 0), ``size`` across, with the
    logo from ``renderer`` (a QSvgRenderer)."""
    p.setRenderHint(QPainter.Antialiasing)
    p.setRenderHint(QPainter.SmoothPixmapTransform)
    view = renderer.viewBoxF()
    aspect = view.width() / view.height() if view.height() else 1.0
    # macOS icons leave ~10% margin around the rounded square.
    margin = size * 0.09
    tile = QRectF(margin, margin, size - 2 * margin, size - 2 * margin)
    radius = tile.width() * 0.225
    if size >= 32:  # a soft shadow (too muddy at tiny sizes)
        for i, alpha in enumerate((18, 12, 6)):
            grow = size * 0.006 * (i + 1)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0, 0, 0, alpha))
            p.drawRoundedRect(tile.adjusted(-grow, -grow + size * 0.012, grow, grow + size * 0.012),
                              radius + grow, radius + grow)
    gradient = QLinearGradient(tile.topLeft(), tile.bottomLeft())
    gradient.setColorAt(0, QColor("#ffffff"))
    gradient.setColorAt(1, QColor("#eef0f6"))
    p.setBrush(gradient)
    p.setPen(QPen(QColor(0, 0, 0, 28), max(1.0, size / 256)))
    p.drawRoundedRect(tile, radius, radius)
    # The logo, as large as fits with some breathing room.
    box = tile.adjusted(tile.width() * 0.16, tile.height() * 0.12,
                        -tile.width() * 0.16, -tile.height() * 0.12)
    h = box.height()
    w = h * aspect
    if w > box.width():
        w = box.width()
        h = w / aspect
    renderer.render(p, QRectF(box.center().x() - w / 2, box.center().y() - h / 2, w, h))


@cache
def _logo_text() -> str:
    return LOGO_PATH.read_text()


def recolored_logo(turn: float) -> bytes:
    """The fpm logo's SVG with each piece's color moved ``turn`` of the way
    around the color wheel (0 to 1), and a little more or less for each piece
    (so the colors ripple as ``turn`` grows), keeping their depth. At 0 (or
    1) it's the logo's own colors."""
    svg = _logo_text()
    pieces = iter(range(100))

    def shift(match: re.Match) -> str:
        color = QColor(match.group(1))
        h, s, v, _ = color.getHsvF()
        phase = 2 * math.pi * next(pieces) / 5
        ripple = 0.04 * (math.sin(4 * math.pi * turn + phase) - math.sin(phase))  # (0 at turn 0 and 1)
        color.setHsvF((max(h, 0) + turn + ripple) % 1.0, s, v)
        return f"fill:{color.name()}"

    return re.sub(r"fill:(#[0-9a-fA-F]{6})", shift, svg).encode()


# ------------------------------------------------------------ symbol kinds
# Outline icons: a colored badge with a letter for each kind of Fortran
# symbol (like the symbol icons of other editors).
SYMBOL_BADGES = {
    "module": ("M", "#8e44ad"),
    "submodule": ("M", "#b15dcc"),
    "program": ("P", "#5f3dc4"),
    "subroutine": ("S", "#1c7ed6"),
    "function": ("F", "#e8590c"),
    "type": ("T", "#0ca678"),
    "interface": ("I", "#2f9e44"),
    "enum": ("E", "#f08c00"),
    "constructor": ("C", "#c2255c"),
}
SYMBOL_INDENT = 12  # logical pixels per nesting level in lists of symbols
_symbol_icons: dict = {}


def symbol_icon(kind: str) -> QIcon:
    """A 16 px badge for a kind of symbol ("function", "module"…)."""
    if kind in _symbol_icons:
        return _symbol_icons[kind]
    letter, color = SYMBOL_BADGES.get(kind, ((kind[:1] or "?").upper(), "#868e96"))
    width, height = 16, 16
    icon = QIcon()
    for ratio in (1, 2, 3):
        pixmap = QPixmap(int(width * ratio), int(height * ratio))
        pixmap.setDevicePixelRatio(ratio)
        pixmap.fill(Qt.transparent)
        p = QPainter(pixmap)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        box = QRectF(1, 1, 14, 14)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(color))
        p.drawRoundedRect(box, 3.5, 3.5)
        font = p.font()
        font.setPixelSize(10)
        font.setBold(True)
        p.setFont(font)
        p.setPen(QColor("white"))
        p.drawText(box, Qt.AlignCenter, letter)
        p.end()
        icon.addPixmap(pixmap)
    _symbol_icons[kind] = icon
    return icon
