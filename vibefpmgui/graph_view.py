"""Interactive dependency diagram drawn with QGraphicsView.

Packages are laid out top-down in layers (the root at the top, each package
placed one layer below its deepest parent) and the order inside each layer is
refined with a few barycenter sweeps to reduce edge crossings.
"""

from __future__ import annotations

from qtpy.QtCore import QPointF, QRectF, Qt, Signal
from qtpy.QtGui import (
    QBrush, QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPolygonF,
)
from qtpy.QtWidgets import (
    QGraphicsItem, QGraphicsObject, QGraphicsPathItem, QGraphicsScene, QGraphicsView,
)

from . import manifest as m

NODE_COLORS = {
    m.ROOT: "#f6c85f",
    m.GIT: "#9dd0f5",
    m.PATH: "#a8e6a1",
    m.REGISTRY: "#d7b8f3",
    m.META: "#f3c6a5",
    m.MISSING: "#e0e0e0",
}
KIND_LABELS = {
    m.ROOT: "Project",
    m.GIT: "Git",
    m.PATH: "Local path",
    m.REGISTRY: "Registry",
    m.META: "Metapackage",
    m.MISSING: "Not found",
}

H_GAP = 40
V_GAP = 80
PAD_X = 14
PAD_Y = 8


class EdgeItem(QGraphicsPathItem):
    def __init__(self, source: "NodeItem", target: "NodeItem", dev: bool):
        super().__init__()
        self.source, self.target, self.dev = source, target, dev
        self.setZValue(-1)
        self._arrow = QPolygonF()
        self.set_highlight(False)
        self.update_path()

    def set_highlight(self, on: bool):
        color = QColor("#e8590c") if on else QColor("#8a8a8a")
        pen = QPen(color, 2.4 if on else 1.4)
        if self.dev:
            pen.setStyle(Qt.DashLine)
        self.setPen(pen)
        self.setZValue(0 if on else -1)

    def update_path(self):
        s = self.source.sceneBoundingRect()
        t = self.target.sceneBoundingRect()
        start = QPointF(s.center().x(), s.bottom())
        end = QPointF(t.center().x(), t.top())
        if end.y() <= start.y():  # target not below source: route sideways
            start = QPointF(s.right(), s.center().y())
            end = QPointF(t.right(), t.center().y())
            bend = max(s.right(), t.right()) + 60
            c1, c2 = QPointF(bend, start.y()), QPointF(bend, end.y())
        else:
            dy = (end.y() - start.y()) * 0.5
            c1, c2 = QPointF(start.x(), start.y() + dy), QPointF(end.x(), end.y() - dy)
        path = QPainterPath(start)
        path.cubicTo(c1, c2, end)
        # Arrow head pointing along the final tangent of the curve.
        direction = end - c2
        length = max((direction.x() ** 2 + direction.y() ** 2) ** 0.5, 1e-6)
        ux, uy = direction.x() / length, direction.y() / length
        size = 9
        left = QPointF(end.x() - ux * size - uy * size * 0.5, end.y() - uy * size + ux * size * 0.5)
        right = QPointF(end.x() - ux * size + uy * size * 0.5, end.y() - uy * size - ux * size * 0.5)
        self.prepareGeometryChange()
        self._arrow = QPolygonF([end, left, right])
        self.setPath(path)

    def boundingRect(self):
        return super().boundingRect().united(self._arrow.boundingRect()).adjusted(-3, -3, 3, 3)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(self.pen())
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.path())
        painter.setBrush(self.pen().color())
        solid = QPen(self.pen())
        solid.setStyle(Qt.SolidLine)
        painter.setPen(solid)
        painter.drawPolygon(self._arrow)


class NodeItem(QGraphicsObject):
    clicked = Signal(str)
    double_clicked = Signal(str)

    def __init__(self, package: m.Package):
        super().__init__()
        self.package = package
        self.edges: list[EdgeItem] = []
        self.title_font = QFont()
        self.title_font.setBold(True)
        self.sub_font = QFont()
        self.sub_font.setPointSizeF(max(self.title_font.pointSizeF() - 2, 7))
        self.subtitle = package.version and f"v{package.version}" or KIND_LABELS[package.kind]
        tw = QFontMetricsF(self.title_font).horizontalAdvance(package.name)
        sw = QFontMetricsF(self.sub_font).horizontalAdvance(self.subtitle)
        th = QFontMetricsF(self.title_font).height()
        sh = QFontMetricsF(self.sub_font).height()
        self._rect = QRectF(0, 0, max(tw, sw, 60) + 2 * PAD_X, th + sh + 2 * PAD_Y)
        self._hover = False
        self.setAcceptHoverEvents(True)
        self.setFlag(QGraphicsItem.ItemIsMovable)
        self.setFlag(QGraphicsItem.ItemIsSelectable)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges)
        self.setCursor(Qt.PointingHandCursor)
        tip = [f"<b>{package.name}</b> ({KIND_LABELS[package.kind]})"]
        if package.description:
            tip.append(package.description)
        if package.error:
            tip.append(f"<i>{package.error}</i>")
        self.setToolTip("<br>".join(tip))

    def boundingRect(self):
        return self._rect.adjusted(-3, -3, 3, 3)

    def paint(self, painter, option, widget=None):
        painter.setRenderHint(QPainter.Antialiasing)
        fill = QColor(NODE_COLORS[self.package.kind])
        if self._hover:
            fill = fill.lighter(108)
        border = QColor("#e8590c") if self.isSelected() else fill.darker(160)
        pen = QPen(border, 2.5 if self.isSelected() else 1.2)
        if self.package.kind == m.MISSING:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(fill))
        painter.drawRoundedRect(self._rect, 8, 8)
        painter.setPen(QColor("#1b1b1b"))
        th = QFontMetricsF(self.title_font).height()
        painter.setFont(self.title_font)
        painter.drawText(QRectF(0, PAD_Y, self._rect.width(), th), Qt.AlignCenter, self.package.name)
        painter.setFont(self.sub_font)
        painter.setPen(QColor("#444444"))
        painter.drawText(
            QRectF(0, PAD_Y + th, self._rect.width(), self._rect.height() - th - 2 * PAD_Y),
            Qt.AlignCenter, self.subtitle,
        )

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            for edge in self.edges:
                edge.update_path()
        elif change == QGraphicsItem.ItemSelectedHasChanged:
            for edge in self.edges:
                edge.set_highlight(bool(value) or self._hover)
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event):
        self._hover = True
        for edge in self.edges:
            edge.set_highlight(True)
        self.update()
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event):
        self._hover = False
        for edge in self.edges:
            edge.set_highlight(self.isSelected() or edge.source.isSelected() or edge.target.isSelected())
        self.update()
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event):
        super().mousePressEvent(event)
        self.clicked.emit(self.package.name)

    def mouseDoubleClickEvent(self, event):
        self.double_clicked.emit(self.package.name)
        super().mouseDoubleClickEvent(event)


def compute_layers(graph: m.DependencyGraph) -> list[list[str]]:
    """Assign each package to a layer (longest path from the root) and order
    the layers to reduce crossings."""
    children = {name: [] for name in graph.packages}
    parents = {name: [] for name in graph.packages}
    for e in graph.edges:
        if e.child not in children[e.parent]:
            children[e.parent].append(e.child)
            parents[e.child].append(e.parent)

    depth = {graph.root: 0}
    # Longest-path layering; the iteration cap guards against cycles.
    for _ in range(len(graph.packages)):
        changed = False
        for e in graph.edges:
            if e.parent in depth and depth.get(e.child, -1) < depth[e.parent] + 1:
                if depth[e.parent] + 1 < len(graph.packages):
                    depth[e.child] = depth[e.parent] + 1
                    changed = True
        if not changed:
            break
    for name in graph.packages:
        depth.setdefault(name, 1)

    layers: list[list[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for name in graph.packages:  # BFS order gives a sensible starting order
        layers[depth[name]].append(name)

    def barycenter(name, neighbours, index):
        pos = [index[n] for n in neighbours[name] if n in index]
        return sum(pos) / len(pos) if pos else index.get(name, 0)

    for _ in range(4):
        for i in range(1, len(layers)):  # downward sweep
            index = {n: k for k, n in enumerate(layers[i - 1])}
            layers[i].sort(key=lambda n: barycenter(n, parents, index))
        for i in range(len(layers) - 2, -1, -1):  # upward sweep
            index = {n: k for k, n in enumerate(layers[i + 1])}
            if i > 0:
                layers[i].sort(key=lambda n: barycenter(n, children, index))
    return [layer for layer in layers if layer]


class DependencyGraphView(QGraphicsView):
    """Shows a :class:`manifest.DependencyGraph`. Wheel zooms, drag pans,
    nodes can be dragged around, click selects, double-click opens."""

    package_selected = Signal(str)
    package_activated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setScene(QGraphicsScene(self))
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        self.nodes: dict[str, NodeItem] = {}
        self.graph: m.DependencyGraph | None = None
        self._placeholder("Open an fpm.toml file to see its dependency graph.")

    def _placeholder(self, text):
        scene = self.scene()
        scene.clear()
        self.nodes = {}
        item = scene.addText(text)
        item.setDefaultTextColor(self.palette().text().color())
        scene.setSceneRect(item.boundingRect())

    def clear(self, message="Open an fpm.toml file to see its dependency graph."):
        self.graph = None
        self._placeholder(message)

    def set_graph(self, graph: m.DependencyGraph):
        self.graph = graph
        scene = self.scene()
        scene.clear()
        self.nodes = {}
        for name, pkg in graph.packages.items():
            node = NodeItem(pkg)
            node.clicked.connect(self.package_selected)
            node.double_clicked.connect(self.package_activated)
            scene.addItem(node)
            self.nodes[name] = node

        layers = compute_layers(graph)
        widths = [sum(self.nodes[n]._rect.width() for n in layer) + H_GAP * (len(layer) - 1)
                  for layer in layers]
        max_width = max(widths)
        y = 0.0
        for layer, width in zip(layers, widths):
            x = (max_width - width) / 2
            height = max(self.nodes[n]._rect.height() for n in layer)
            for name in layer:
                node = self.nodes[name]
                node.setPos(x, y + (height - node._rect.height()) / 2)
                x += node._rect.width() + H_GAP
            y += height + V_GAP

        seen = set()
        for e in graph.edges:
            if (e.parent, e.child) in seen:
                continue
            seen.add((e.parent, e.child))
            edge = EdgeItem(self.nodes[e.parent], self.nodes[e.child], e.dev)
            self.nodes[e.parent].edges.append(edge)
            self.nodes[e.child].edges.append(edge)
            scene.addItem(edge)
        self._update_scene_rect()
        self.fit()

    def update_packages(self, graph: m.DependencyGraph):
        """Swap in refreshed package details without redoing the layout."""
        self.graph = graph
        for name, node in self.nodes.items():
            node.package = graph.packages.get(name, node.package)

    def _update_scene_rect(self):
        rect = self.scene().itemsBoundingRect().adjusted(-40, -40, 40, 40)
        self.scene().setSceneRect(rect)

    def select(self, name: str):
        node = self.nodes.get(name)
        if node:
            self.scene().clearSelection()
            node.setSelected(True)
            self.centerOn(node)

    def fit(self):
        self.resetTransform()
        rect = self.scene().sceneRect()
        self.fitInView(rect, Qt.KeepAspectRatio)
        # Don't blow small graphs up beyond 100 %.
        if self.transform().m11() > 1.0:
            self.resetTransform()
            self.centerOn(rect.center())

    def zoom(self, factor: float):
        scale = self.transform().m11() * factor
        if 0.05 < scale < 8:
            self.scale(factor, factor)

    def wheelEvent(self, event):
        self.zoom(1.15 if event.angleDelta().y() > 0 else 1 / 1.15)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        self._update_scene_rect()

    def export_image(self, filename: str) -> bool:
        """Save the diagram as PNG/JPG, or SVG if the name ends in .svg."""
        scene = self.scene()
        selected = scene.selectedItems()
        scene.clearSelection()
        rect = scene.sceneRect()
        try:
            if filename.lower().endswith(".svg"):
                from qtpy.QtSvg import QSvgGenerator

                gen = QSvgGenerator()
                gen.setFileName(filename)
                gen.setSize(rect.size().toSize())
                gen.setViewBox(QRectF(0, 0, rect.width(), rect.height()))
                gen.setTitle(self.graph.root if self.graph else "dependencies")
                painter = QPainter(gen)
                scene.render(painter, QRectF(0, 0, rect.width(), rect.height()), rect)
                painter.end()
                return True
            from qtpy.QtGui import QImage

            scale = 2.0
            image = QImage((rect.size() * scale).toSize(), QImage.Format_ARGB32)
            image.fill(Qt.white)
            painter = QPainter(image)
            painter.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
            scene.render(painter, QRectF(image.rect()), rect)
            painter.end()
            return image.save(filename)
        finally:
            for item in selected:
                item.setSelected(True)
