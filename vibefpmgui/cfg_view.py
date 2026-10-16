"""Drawing a procedure's control-flow graph (`vibefpmgui.control_flow`) with
pyqtgraph: a box per block (its first lines of code), a hexagon per decision,
a rounded box per loop, pills for start, end and stop; arrows labelled with
the way taken ("true", "case (1)", "loop"…), a loop's way back curving round
its right side; a node whose lines are in an included file has a dashed
border. Wheel zooms, drag pans, clicking a node opens its lines,
double-clicking the background fits the graph to the view; right-click for
a menu (open or copy a node's code, center on it, fit, zoom, actual size,
edge labels on or off, copy or save the graph as an image).
"""

from __future__ import annotations

import math
from itertools import pairwise
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from qtpy.QtCore import QPointF, QRectF, Qt, Signal
from qtpy.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
)
from qtpy.QtWidgets import (
    QApplication,
    QFileDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QStackedLayout,
    QWidget,
)

from . import control_flow as cf
from .graph_view import LAYERED_TB, _bezier, edge_geometry
from .source_text import read_source

PADDING = 8
MAX_FIT_SCALE = 1.3  # (fitting doesn't blow a small graph up beyond 130 %)
IMAGE_SCALE = 2  # (an image's pixels per unit of the graph)
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
        self.included = node.path is not None and view.graph is not None and node.path != view.graph.path
        if node.first:
            self.setCursor(Qt.PointingHandCursor)
            lines = f"line {node.first}" if node.first == node.last else f"lines {node.first}–{node.last}"
            where = f"{node.path.name} (included), " if self.included else ""
            self.setToolTip(f"{where}{lines}: click to open")

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
        pen = QPen(QColor(border), 2.2 if self.hovered else 1.3)
        if self.included:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
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
            self.view.open_node(self.node)


class EdgeLabel(pg.GraphicsObject):
    """An edge's label, in the graph's units (so it zooms and exports with
    the graph); ``right``: its right edge at its place, not its left."""

    def __init__(self, text: str, font: QFont, color: str, right: bool):
        super().__init__()
        self.text, self.font, self.color = text, font, color
        metrics = QFontMetricsF(font)
        width = metrics.horizontalAdvance(text)
        self.rect = QRectF(-width if right else 0, 0, width, metrics.height())
        self.setZValue(3)

    def boundingRect(self):
        return self.rect

    def paint(self, painter: QPainter, option, widget=None):
        painter.setPen(QColor(self.color))
        painter.setFont(self.font)
        painter.drawText(self.rect, Qt.AlignLeft | Qt.AlignVCenter, self.text)


class CfgView(QWidget):
    """A control-flow graph, or a message instead of one."""

    line_clicked = Signal(str, int)  # the clicked node's file and first line (1-based)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.graph: cf.Graph | None = None
        self.sources: dict[Path, list[str]] = {}  # (files' lines, for Copy Code)
        self.show_labels = True
        self.labels: list[EdgeLabel] = []
        self.menu: QMenu | None = None
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
        self.plot.scene().sigMouseClicked.connect(self._scene_clicked)
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
            self.show_graph(self.graph, fit=False)

    def show_message(self, text: str):
        self.graph = None
        self.vb.clear()
        self.nodes = {}
        self.message.setText(text)
        self.stack.setCurrentWidget(self.message)

    def show_graph(self, graph: cf.Graph, source: str | None = None, fit: bool = True):
        """Draw ``graph`` (``source``: its file's text, for Copy Code; kept if
        not given); ``fit``: fit it to the view, else keep the view's range."""
        if source is not None or self.graph is None or graph.path != self.graph.path:
            self.sources = {graph.path: source.splitlines()} if source is not None and graph.path else {}
        view_range = self.vb.viewRange()
        self.graph = graph
        self.vb.clear()
        self.labels = []
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
                points, direction = _back_edge(source.scene_rect(), target.scene_rect(), right_edge + 18 * backs,
                                               8 + 4 * (backs % 3))
            elif index in places.routes:
                points, direction = _routed(source.scene_rect(), target.scene_rect(), places.routes[index])
            else:
                points, direction = edge_geometry(source.scene_rect(), target.scene_rect(), LAYERED_TB, True)
            self._add_edge(points, direction, edge.label)
        self.stack.setCurrentWidget(self.plot)
        if fit:
            self.fit()
        else:
            self.vb.setRange(xRange=view_range[0], yRange=view_range[1], padding=0)

    # ------------------------------------------------------------ the view
    def bounds(self) -> QRectF:
        """The graph's extent, in its own units."""
        rect = QRectF()
        for item in self.nodes.values():
            rect = rect.united(item.mapRectToView(item.boundingRect()))
        for item in self.vb.addedItems:
            if isinstance(item, pg.PlotCurveItem) or (isinstance(item, EdgeLabel) and item.isVisible()):
                rect = rect.united(item.mapRectToView(item.boundingRect()))
        return rect

    def fit(self):
        """The whole graph in view (a small one at most 130 %)."""
        if not self.nodes:
            return
        rect = self.bounds()
        self.vb.setRange(rect=rect, padding=0.04)
        px = self.vb.viewPixelSize()[0]
        if 0 < px < 1 / MAX_FIT_SCALE:
            factor = 1 / (MAX_FIT_SCALE * px)
            self.vb.scaleBy((factor, factor), center=rect.center())

    def zoom(self, factor: float, center: QPointF | None = None):
        """Zoom in (``factor`` > 1) or out about ``center`` (the view's)."""
        self.vb.scaleBy((1 / factor, 1 / factor), center=center if center is not None else self.vb.viewRect().center())

    def actual_size(self, center: QPointF | None = None):
        """100 %: a unit of the graph a pixel (its text the code's size)."""
        px = self.vb.viewPixelSize()[0]
        if px > 0:
            self.zoom(px, center)

    def center_on(self, point: QPointF):
        here = self.vb.viewRect().center()
        self.vb.translateBy(x=point.x() - here.x(), y=point.y() - here.y())

    def set_show_labels(self, on: bool):
        self.show_labels = on
        for label in self.labels:
            label.setVisible(on)

    def node_at(self, scene_pos: QPointF) -> CfgNode | None:
        point = self.vb.mapSceneToView(scene_pos)
        return next((item for item in self.nodes.values() if item.scene_rect().contains(point)), None)

    def open_node(self, node: cf.Node):
        path = node.path or (self.graph.path if self.graph else None)
        self.line_clicked.emit(str(path or ""), node.first)

    def node_code(self, node: cf.Node) -> str:
        """The node's lines of code, as they are in the file."""
        path = node.path or (self.graph.path if self.graph else None)
        if path is not None and path not in self.sources:
            try:
                self.sources[path] = read_source(path).text.splitlines()
            except OSError:
                self.sources[path] = []
        source = self.sources.get(path, []) if path is not None else []
        if not node.first or not source:
            return "\n".join(node.text)
        lines = source[node.first - 1:node.last]
        indent = min((len(line) - len(line.lstrip()) for line in lines if line.strip()), default=0)
        return "\n".join(line[indent:] for line in lines)

    # ------------------------------------------------------------ the menu
    def _scene_clicked(self, event):
        if self.graph is None:
            return
        if event.button() == Qt.RightButton:
            event.accept()
            self.menu = self.context_menu(event.scenePos())
            self.menu.popup(event.screenPos().toPoint())
        elif event.button() == Qt.LeftButton and event.double() and self.node_at(event.scenePos()) is None:
            self.fit()

    def context_menu(self, scene_pos: QPointF) -> QMenu:
        """The menu for a right-click at ``scene_pos``: a node's actions
        first, if it's on one, then the view's."""
        menu = QMenu(self)
        point = self.vb.mapSceneToView(scene_pos)
        item = self.node_at(scene_pos)
        if item is not None:
            node = item.node
            if node.first:
                lines = f"Line {node.first}" if node.first == node.last else f"Lines {node.first}–{node.last}"
                menu.addAction(f"Open {lines}", lambda: self.open_node(node))
                menu.addAction("Copy Code", lambda: QApplication.clipboard().setText(self.node_code(node)))
            center = item.pos()
            menu.addAction("Center on This", lambda: self.center_on(center))
            menu.addSeparator()
        menu.addAction("Fit to View", self.fit)
        menu.addAction("Zoom In", lambda: self.zoom(1.25, point))
        menu.addAction("Zoom Out", lambda: self.zoom(0.8, point))
        menu.addAction("Actual Size", lambda: self.actual_size(point))
        menu.addSeparator()
        labels = menu.addAction("Show Edge Labels")
        labels.setCheckable(True)
        labels.setChecked(self.show_labels)
        labels.toggled.connect(self.set_show_labels)
        menu.addSeparator()
        menu.addAction("Copy Image", lambda: QApplication.clipboard().setImage(self.image()))
        menu.addAction("Save Image…", self.save_image)
        return menu

    # ------------------------------------------------------------ images
    def _render(self, paint_device, size) -> None:
        """Draw the whole graph (fitted, the view put back after) onto
        ``paint_device`` of ``size``."""
        view_range = self.vb.viewRange()
        self.vb.setRange(rect=self.bounds(), padding=0)
        try:
            bounds = self.bounds().adjusted(-6, -6, 6, 6)
            source = self.vb.mapViewToScene(bounds).boundingRect()
            target = QRectF(0, 0, size.width(), size.height())
            painter = QPainter(paint_device)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.fillRect(target, QColor(self.colors["background"]))
            self.plot.scene().render(painter, target, source)
            painter.end()
        finally:
            self.vb.setRange(xRange=view_range[0], yRange=view_range[1], padding=0)

    def _image_size(self):
        return self.bounds().adjusted(-6, -6, 6, 6).size() * IMAGE_SCALE

    def image(self) -> QImage:
        """The whole graph as an image (2 pixels per unit)."""
        size = self._image_size().toSize()
        image = QImage(size, QImage.Format_ARGB32)
        self._render(image, size)
        return image

    def export_image(self, filename: str) -> bool:
        """Save the whole graph as PNG/JPG, or SVG if the name ends in .svg."""
        try:
            if filename.lower().endswith(".svg"):
                from qtpy.QtSvg import QSvgGenerator

                size = self._image_size() / IMAGE_SCALE
                svg = QSvgGenerator()
                svg.setFileName(filename)
                svg.setSize(size.toSize())
                svg.setViewBox(QRectF(0, 0, size.width(), size.height()))
                svg.setTitle(self.graph.name if self.graph else "control flow")
                self._render(svg, size)
                return Path(filename).exists()
            return self.image().save(filename)
        except Exception:  # noqa: BLE001 - report failure to the caller
            return False

    def save_image(self):
        name = (self.graph.name if self.graph else "control-flow") + ".png"
        filename, _ = QFileDialog.getSaveFileName(self, "Save control-flow graph", name,
                                                  "PNG image (*.png);;SVG image (*.svg)")
        if filename and not self.export_image(filename):
            QMessageBox.warning(self, "Save failed", f"Could not write {filename}")

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
            at = points[min(len(points) - 1, max(2, len(points) // 4))]
            left = points[-1, 0] < points[0, 0] - 1  # (heading left: the label on its left)
            text = EdgeLabel(label if len(label) <= 28 else label[:27] + "…", font, self.colors["label"], left)
            text.setPos(at[0] + (-4 if left else 4), at[1])
            text.setVisible(self.show_labels)
            self.labels.append(text)
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


def _back_edge(source: QRectF, target: QRectF, x: float, drop: float):
    """A loop's way back: down out of the source's bottom by ``drop`` (into
    the gap below its layer, clear of the nodes beside it), right to ``x``,
    up, and into the target's right side."""
    cx, below = source.center().x(), source.bottom() + drop
    ty = target.center().y()
    points = np.array([[cx, source.bottom()], [cx, below], [x, below], [x, ty], [target.right(), ty]])
    return points, (target.right() - x, 0.0)
