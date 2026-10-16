"""Drawing a procedure's control-flow graph (`vibefpmgui.control_flow`) with
pyqtgraph: a box per block (its first lines of code), a hexagon per decision,
a rounded box per loop, pills for start, end and stop; arrows labelled with
the way taken ("true", "case (1)", "loop"…), a loop's way back curving round
its right side. Wheel zooms, drag pans, clicking a node opens its lines.
"""

from __future__ import annotations

import math
from itertools import pairwise

import numpy as np
import pyqtgraph as pg
from qtpy.QtCore import QPointF, QRectF, Qt, Signal
from qtpy.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from qtpy.QtWidgets import QLabel, QStackedLayout, QWidget

from . import control_flow as cf
from .graph_view import LAYERED_TB, _bezier, edge_geometry

PADDING = 8
COLORS = {
    "light": {"background": "#ffffff", "text": "#1f2328", "edge": "#8b949e", "label": "#57606a",
              cf.BLOCK: ("#f6f8fa", "#d0d7de"), cf.DECISION: ("#fff4e5", "#e8a317"), cf.LOOP: ("#e7f5ff", "#4c6ef5"),
              cf.START: ("#ebfbee", "#2f9e44"), cf.END: ("#ebfbee", "#2f9e44"), cf.STOP: ("#fff5f5", "#e03131")},
    "dark": {"background": "#1e1f22", "text": "#e6e8eb", "edge": "#7d8590", "label": "#9da7b3",
             cf.BLOCK: ("#2a2d31", "#454a51"), cf.DECISION: ("#3a2f1a", "#e8a317"), cf.LOOP: ("#1d2a45", "#748ffc"),
             cf.START: ("#1f3326", "#40c057"), cf.END: ("#1f3326", "#40c057"), cf.STOP: ("#3d1f22", "#fa5252")},
}


class CfgNode(pg.GraphicsObject):
    """One node: its shape and text, centered on its place."""

    def __init__(self, view: CfgView, node: cf.Node):
        super().__init__()
        self.view, self.node = view, node
        self.hovered = False
        metrics = QFontMetricsF(view.code_font)
        width = max((metrics.horizontalAdvance(line) for line in node.text), default=20) + 2 * PADDING
        if node.kind == cf.DECISION:
            width += 24  # (the hexagon's points)
        height = metrics.height() * max(len(node.text), 1) + 2 * PADDING
        self.size = (max(width, 56), height)
        self.rect = QRectF(-self.size[0] / 2, -self.size[1] / 2, *self.size)
        self.setZValue(10)
        self.setAcceptHoverEvents(True)
        if node.first:
            self.setCursor(Qt.PointingHandCursor)
            lines = f"line {node.first}" if node.first == node.last else f"lines {node.first}–{node.last}"
            self.setToolTip(f"{lines}: click to open")

    def boundingRect(self):
        return self.rect.adjusted(-3, -3, 3, 3)

    def scene_rect(self) -> QRectF:
        return self.rect.translated(self.pos())

    def shape_path(self) -> QPainterPath:
        r = self.rect
        path = QPainterPath()
        kind = self.node.kind
        if kind == cf.DECISION:
            d = min(12, r.height() / 2)
            path.addPolygon(QPolygonF([QPointF(r.left() + d, r.top()), QPointF(r.right() - d, r.top()),
                                       QPointF(r.right(), r.center().y()), QPointF(r.right() - d, r.bottom()),
                                       QPointF(r.left() + d, r.bottom()), QPointF(r.left(), r.center().y())]))
            path.closeSubpath()
        elif kind in (cf.START, cf.END, cf.STOP):
            path.addRoundedRect(r, r.height() / 2, r.height() / 2)
        elif kind == cf.LOOP:
            path.addRoundedRect(r, 9, 9)
        else:
            path.addRoundedRect(r, 3, 3)
        return path

    def paint(self, painter: QPainter, option, widget=None):
        colors = self.view.colors
        fill, border = colors[self.node.kind]
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(border), 2.2 if self.hovered else 1.3))
        painter.setBrush(QBrush(QColor(fill)))
        painter.drawPath(self.shape_path())
        painter.setPen(QColor(colors["text"]))
        painter.setFont(self.view.code_font)
        metrics = QFontMetricsF(self.view.code_font)
        y = self.rect.top() + PADDING
        centered = self.node.kind in (cf.START, cf.END, cf.STOP, cf.DECISION)
        for line in self.node.text:
            box = QRectF(self.rect.left() + PADDING, y, self.rect.width() - 2 * PADDING, metrics.height())
            painter.drawText(box, (Qt.AlignHCenter if centered else Qt.AlignLeft) | Qt.AlignVCenter, line)
            y += metrics.height()

    def hoverEnterEvent(self, event):
        self.hovered = True
        self.update()

    def hoverLeaveEvent(self, event):
        self.hovered = False
        self.update()

    def mouseClickEvent(self, event):
        if event.button() == Qt.LeftButton and self.node.first:
            event.accept()
            self.view.line_clicked.emit(self.node.first)


class CfgView(QWidget):
    """A control-flow graph, or a message instead of one."""

    line_clicked = Signal(int)  # the clicked node's first line (1-based)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.graph: cf.Graph | None = None
        self.code_font = QFont()
        self.colors = COLORS["light"]
        self.nodes: dict[int, CfgNode] = {}
        self.plot = pg.GraphicsLayoutWidget()
        self.plot.ci.setContentsMargins(0, 0, 0, 0)
        self.vb = self.plot.addViewBox()
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)
        self.vb.setMenuEnabled(False)
        self.plot.setBackground(self.colors["background"])
        self.message = QLabel()
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.stack = QStackedLayout(self)
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.plot)
        self.show_message("Select a procedure to see its control flow.")

    def set_code_font(self, font: QFont):
        self.code_font = QFont(font)
        self.code_font.setPointSizeF(max(font.pointSizeF() - 1, 7))
        if self.graph is not None:
            self.show_graph(self.graph)

    def set_dark(self, dark: bool):
        self.colors = COLORS["dark" if dark else "light"]
        self.plot.setBackground(self.colors["background"])
        if self.graph is not None:
            self.show_graph(self.graph)

    def show_message(self, text: str):
        self.graph = None
        self.vb.clear()
        self.nodes = {}
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def show_graph(self, graph: cf.Graph):
        self.graph = graph
        self.vb.clear()
        self.nodes = {n.id: CfgNode(self, n) for n in graph.nodes}
        places = cf.layout(graph, {i: item.size for i, item in self.nodes.items()})
        for i, item in self.nodes.items():
            x, y = places.positions[i]
            item.setPos(x, y)
            self.vb.addItem(item)
        right_edge = max((item.scene_rect().right() for item in self.nodes.values()), default=0)
        backs = 0
        for index, edge in enumerate(graph.edges):
            source, target = self.nodes[edge.source], self.nodes[edge.target]
            if edge.back:
                backs += 1
                points, direction = _back_edge(source.scene_rect(), target.scene_rect(), right_edge + 18 * backs)
            elif index in places.routes:
                points, direction = _routed(source.scene_rect(), target.scene_rect(), places.routes[index])
            else:
                points, direction = edge_geometry(source.scene_rect(), target.scene_rect(), LAYERED_TB, True)
            self._add_edge(points, direction, edge.label)
        self.stack.setCurrentWidget(self.plot)
        self.vb.autoRange(padding=0.04)

    def _add_edge(self, points: np.ndarray, direction, label: str):
        color = QColor(self.colors["edge"])
        curve = pg.PlotCurveItem(points[:, 0], points[:, 1], pen=pg.mkPen(color, width=1.4))
        curve.setZValue(1)
        arrow = pg.ArrowItem(headLen=9, tipAngle=34, tailLen=None, pen=pg.mkPen(color), brush=pg.mkBrush(color),
                             angle=math.degrees(math.atan2(-direction[1], -direction[0])))
        arrow.setPos(points[-1, 0], points[-1, 1])
        arrow.setZValue(2)
        self.vb.addItem(curve)
        self.vb.addItem(arrow)
        if label:
            font = QFont(self.code_font)
            font.setPointSizeF(max(font.pointSizeF() - 1, 6))
            text = pg.TextItem(label if len(label) <= 28 else label[:27] + "…", color=self.colors["label"],
                               anchor=(0, 0))
            text.setFont(font)
            at = points[min(len(points) - 1, max(2, len(points) // 4))]
            text.setPos(at[0] + 3, at[1])
            text.setZValue(3)
            self.vb.addItem(text)


def _routed(source: QRectF, target: QRectF, waypoints: list[tuple[float, float]]):
    """An edge across layers: from the source's bottom through its waypoints
    to the target's top, an S-curve between each two."""
    stops = [QPointF(source.center().x(), source.bottom())] + [QPointF(x, y) for x, y in waypoints] \
        + [QPointF(target.center().x(), target.top())]
    parts = []
    for a, b in pairwise(stops):
        dy = (b.y() - a.y()) / 2
        parts.append(_bezier(a, QPointF(a.x(), a.y() + dy), QPointF(b.x(), b.y() - dy), b))
    points = np.vstack(parts)
    d = points[-1] - points[-3]
    return points, (d[0], d[1])


def _back_edge(source: QRectF, target: QRectF, x: float):
    """A loop's way back: out of the source's right side, up along ``x``, into
    the target's right side."""
    p0 = QPointF(source.right(), source.center().y())
    p3 = QPointF(target.right(), target.center().y())
    points = np.array([[p0.x(), p0.y()], [x, p0.y()], [x, p3.y()], [p3.x(), p3.y()]])
    return points, (p3.x() - x, 0.0)
