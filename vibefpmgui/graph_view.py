"""Interactive dependency diagram drawn with pyqtgraph.

The diagram lives in a pyqtgraph ``ViewBox`` (mouse-wheel zoom, drag to pan,
auto-fit, image/SVG export).  Edges are ``PlotCurveItem`` curves with
``ArrowItem`` heads; packages are custom ``GraphicsObject`` boxes that can be
dragged around.

Several layouts (``LAYOUTS``), color themes (``THEMES``) and edge styles
(``EDGE_STYLES``) are available.  Clicking a package highlights everything it
depends on and everything that depends on it.
"""

from __future__ import annotations

import html
import math
import os
import time
from itertools import pairwise

import qtpy

# Make sure pyqtgraph uses the same Qt binding that qtpy picked.
os.environ.setdefault("PYQTGRAPH_QT_LIB", qtpy.API_NAME)

import numpy as np
import pyqtgraph as pg
from qtpy.QtCore import (
    QEvent,
    QPoint,
    QPointF,
    QRectF,
    QStringListModel,
    Qt,
    QTimer,
    Signal,
)
from qtpy.QtGui import (
    QBrush,
    QColor,
    QFont,
    QFontMetricsF,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
)
from qtpy.QtWidgets import (
    QApplication,
    QCompleter,
    QLabel,
    QLineEdit,
    QStackedLayout,
    QWidget,
)

from . import manifest as m

pg.setConfigOptions(antialias=True)

KIND_LABELS = {
    m.ROOT: "Project",
    m.GIT: "Git",
    m.PATH: "Local path",
    m.REGISTRY: "Registry",
    m.META: "Metapackage",
    m.MISSING: "Not found",
}
KIND_ORDER = (m.ROOT, m.GIT, m.PATH, m.REGISTRY, m.META, m.MISSING)

# ----------------------------------------------------------------- styles
LAYERED_TB, LAYERED_LR, RADIAL, CIRCULAR, FORCE = (
    "layered-tb", "layered-lr", "radial", "circular", "force")
LAYOUTS = {
    LAYERED_TB: "Layered (top → bottom)",
    LAYERED_LR: "Layered (left → right)",
    RADIAL: "Radial tree",
    CIRCULAR: "Circular",
    FORCE: "Force-directed",
}
EDGE_STYLES = {"curved": "Curved", "straight": "Straight"}

# fill / border are per node kind; border None means "a darker fill".
THEMES = {
    "light": {
        "name": "Light", "background": "#f8f9fa", "text": "#1b1b1b", "subtext": "#495057",
        "edge": "#8a8f98", "down": "#e8590c", "up": "#1c7ed6", "shadow": 40, "glow": False,
        "fill": {m.ROOT: "#f6c85f", m.GIT: "#9dd0f5", m.PATH: "#a8e6a1",
              m.REGISTRY: "#d7b8f3", m.META: "#f3c6a5", m.MISSING: "#e9ecef"},
        "border": None},
    "dark": {
        "name": "Dark", "background": "#1e1f22", "text": "#f1f3f5", "subtext": "#ced4da",
        "edge": "#6c717a", "down": "#ff922b", "up": "#4dabf7", "shadow": 90, "glow": False,
        "fill": {m.ROOT: "#b8860b", m.GIT: "#2f6aa3", m.PATH: "#2f7d43",
              m.REGISTRY: "#6a45a0", m.META: "#a35a2a", m.MISSING: "#3d4046"},
        "border": None},
    "blueprint": {
        "name": "Blueprint", "background": "#0b3d6b", "text": "#ffffff", "subtext": "#cfe3f7",
        "edge": "#9cc3e6", "down": "#ffd43b", "up": "#63e6be", "shadow": 0, "glow": False,
        "fill": {k: "#0e4f8a" for k in KIND_ORDER},
        "border": {m.ROOT: "#ffffff", m.GIT: "#a5d8ff", m.PATH: "#b2f2bb",
                m.REGISTRY: "#e5dbff", m.META: "#ffd8a8", m.MISSING: "#748ba3"}},
    "neon": {
        "name": "Neon", "background": "#0d0221", "text": "#ffffff", "subtext": "#c9c2e6",
        "edge": "#4a3d7a", "down": "#ff2a6d", "up": "#05d9e8", "shadow": 0, "glow": True,
        "fill": {k: "#160a3a" for k in KIND_ORDER},
        "border": {m.ROOT: "#f9f871", m.GIT: "#00f5ff", m.PATH: "#39ff14",
                m.REGISTRY: "#ff00e6", m.META: "#ff9f1c", m.MISSING: "#6c6c8a"}},
}
AUTO_THEME = "auto"
THEME_CHOICES = {AUTO_THEME: "Auto (follow system)", **{k: v["name"] for k, v in THEMES.items()}}

H_GAP = 40
V_GAP = 90
PAD_X = 16
PAD_Y = 9


def resolve_theme(key: str) -> dict:
    if key == AUTO_THEME:
        dark = QApplication.palette().color(QPalette.Base).lightness() < 128
        key = "dark" if dark else "light"
    return THEMES.get(key, THEMES["light"])


CONFLICT_COLOR = "#f08c00"  # the version-conflict badge


def node_border(theme: dict, kind: str) -> QColor:
    if theme["border"]:
        return QColor(theme["border"][kind])
    return QColor(theme["fill"][kind]).darker(160)


# ------------------------------------------------------------------ items
class NodeItem(pg.GraphicsObject):
    """A package box, positioned by its top-left corner in view coordinates."""

    def __init__(self, view: DependencyGraphView, package: m.Package):
        super().__init__()
        self.view = view
        self.hovered = False
        self.selected = False
        self.dimmed = False
        self.conflict: list[m.Declaration] = []  # different declarations of this package
        self._drag_offset = QPointF()
        self._last_click = 0.0
        self._make_fonts()
        self.set_package(package)
        self.setZValue(10)
        self.setCursor(Qt.PointingHandCursor)

    def _make_fonts(self):
        """Fonts based on the current application font (follows View → text size)."""
        self.title_font = QFont(QApplication.font())
        self.title_font.setBold(True)
        self.title_font.setPointSizeF(self.title_font.pointSizeF() + 1)
        self.sub_font = QFont(QApplication.font())
        self.sub_font.setPointSizeF(max(self.title_font.pointSizeF() - 3, 7))

    def refresh_fonts(self):
        self._make_fonts()
        self.set_package(self.package)  # recomputes the box size

    def set_package(self, package: m.Package):
        self.package = package
        self.subtitle = f"v{package.version}" if package.version else KIND_LABELS[package.kind]
        tw = QFontMetricsF(self.title_font).horizontalAdvance(package.name)
        sw = QFontMetricsF(self.sub_font).horizontalAdvance(self.subtitle)
        th = QFontMetricsF(self.title_font).height()
        sh = QFontMetricsF(self.sub_font).height()
        self.prepareGeometryChange()
        self.rect = QRectF(0, 0, max(tw, sw, 70) + 2 * PAD_X, th + sh + 2 * PAD_Y)
        tip = [f"<b>{package.name}</b> ({KIND_LABELS[package.kind]})"]
        if package.description:
            tip.append(package.description)
        if package.error:
            tip.append(f"<i>{package.error}</i>")
        if self.conflict:
            tip.append("<b style='color:#e8590c'>⚠ Version conflict</b> — asked for differently:")
            used = m.spec_key(self.conflict[0])
            tip += [f"&nbsp;&nbsp;{html.escape(d.parent)}: {html.escape(m.describe_spec(d.spec))}"
                    + (" <i>(used)</i>" if i == 0 else " <i>(the same)</i>" if m.spec_key(d) == used else "")
                    for i, d in enumerate(self.conflict)]
        self.setToolTip("<br>".join(tip))
        self.update()

    def set_conflict(self, declarations: list[m.Declaration] | None):
        self.conflict = list(declarations or [])
        self.set_package(self.package)  # (the tooltip)

    # geometry helpers (view coordinates)
    def center(self) -> QPointF:
        return self.pos() + self.rect.center()

    def scene_rect(self) -> QRectF:
        return self.rect.translated(self.pos())

    def set_center(self, point: QPointF):
        self.setPos(point - self.rect.center())

    def boundingRect(self):
        return self.rect.adjusted(-12, -12, 12, 12)

    def paint(self, painter: QPainter, option, widget=None):
        theme = self.view.theme
        kind = self.package.kind
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setOpacity(0.3 if self.dimmed else 1.0)
        radius = 9
        fill = QColor(theme["fill"][kind])
        border = node_border(theme, kind)
        highlight = self.selected or self.hovered

        if theme["shadow"]:
            painter.setPen(Qt.NoPen)
            for i, grow in enumerate((4, 2)):
                painter.setBrush(QColor(0, 0, 0, theme["shadow"] // (i + 2)))
                painter.drawRoundedRect(self.rect.adjusted(-grow + 2, -grow + 4, grow + 2, grow + 4),
                                        radius + grow, radius + grow)
        if theme["glow"] or highlight:
            glow = QColor(theme["down"]) if self.selected else QColor(border)
            strength = 1.0 if highlight else 0.5
            painter.setBrush(Qt.NoBrush)
            for width, alpha in ((9, 25), (6, 45), (3, 80)):
                glow.setAlpha(int(alpha * strength))
                painter.setPen(QPen(glow, width))
                painter.drawRoundedRect(self.rect, radius, radius)

        gradient = QLinearGradient(self.rect.topLeft(), self.rect.bottomLeft())
        gradient.setColorAt(0, fill.lighter(118))
        gradient.setColorAt(1, fill)
        pen = QPen(QColor(theme["down"]) if self.selected else border, 2.4 if highlight else 1.4)
        if kind == m.MISSING:
            pen.setStyle(Qt.DashLine)
        painter.setPen(pen)
        painter.setBrush(QBrush(gradient))
        painter.drawRoundedRect(self.rect, radius, radius)

        # Kind accent stripe for themes that use one fill color for all kinds.
        if theme["border"]:
            body = QPainterPath()
            body.addRoundedRect(self.rect.adjusted(3, 3, -3, -3), radius - 2, radius - 2)
            strip = QPainterPath()
            strip.addRect(QRectF(self.rect.left(), self.rect.top(), 7, self.rect.height()))
            painter.setPen(Qt.NoPen)
            painter.setBrush(border)
            painter.drawPath(body.intersected(strip))

        th = QFontMetricsF(self.title_font).height()
        painter.setFont(self.title_font)
        painter.setPen(QColor(theme["text"]))
        painter.drawText(QRectF(0, PAD_Y, self.rect.width(), th), Qt.AlignCenter, self.package.name)
        painter.setFont(self.sub_font)
        painter.setPen(QColor(theme["subtext"]))
        painter.drawText(QRectF(0, PAD_Y + th, self.rect.width(), self.rect.height() - th - 2 * PAD_Y),
                         Qt.AlignCenter, self.subtitle)

        if self.conflict:  # an orange "!" badge on the top-right corner
            badge = QRectF(self.rect.right() - 11, self.rect.top() - 9, 20, 20)
            painter.setPen(QPen(QColor("white"), 1.5))
            painter.setBrush(QColor(CONFLICT_COLOR))
            painter.drawEllipse(badge)
            font = QFont(self.title_font)
            font.setPointSizeF(max(self.sub_font.pointSizeF(), 9))
            painter.setFont(font)
            painter.setPen(QColor("white"))
            painter.drawText(badge, Qt.AlignCenter, "!")

    # pyqtgraph mouse events
    def hoverEvent(self, ev):
        if ev.isExit():
            if self.hovered:
                self.hovered = False
                self.view._hover_changed(self, False)
            return
        ev.acceptClicks(Qt.LeftButton)
        ev.acceptDrags(Qt.LeftButton)
        if not self.hovered:
            self.hovered = True
            self.view._hover_changed(self, True)

    def mouseClickEvent(self, ev):
        if ev.button() == Qt.RightButton:  # the package's menu (see MainWindow)
            ev.accept()
            self.view.select(self.package.name, center=False)
            self.view.package_context_menu.emit(self.package.name, ev.screenPos().toPoint())
            return
        if ev.button() != Qt.LeftButton:
            ev.ignore()
            return
        ev.accept()
        # pyqtgraph only flags ev.double() for some event orders, so also
        # detect a second click within the system double-click interval.
        now = time.monotonic()
        interval = QApplication.doubleClickInterval() / 1000
        if ev.double() or now - self._last_click < interval:
            self._last_click = 0.0
            self.view.package_activated.emit(self.package.name)
        else:
            self._last_click = now
            self.view.select(self.package.name, center=False)

    def mouseDragEvent(self, ev):
        if ev.button() != Qt.LeftButton:
            ev.ignore()
            return
        ev.accept()
        if ev.isStart():
            self._drag_offset = self.pos() - self.mapToParent(ev.buttonDownPos())
            self.view._stop_animation()
        self.setPos(self.mapToParent(ev.pos()) + self._drag_offset)
        self.view._update_edges(self)


class EdgeItem:
    """A dependency arrow: a curve plus an arrow head, both pyqtgraph items."""

    def __init__(self, view: DependencyGraphView, source: NodeItem, target: NodeItem, dev: bool):
        self.view, self.source, self.target, self.dev = view, source, target, dev
        self.curve = pg.PlotCurveItem()
        self.arrow = pg.ArrowItem(headLen=12, tipAngle=32, tailLen=None, pen=None)
        self.state = "normal"  # normal | hover | down | up | dim

    def add_to(self, viewbox):
        viewbox.addItem(self.curve)
        viewbox.addItem(self.arrow)

    def remove_from(self, viewbox):
        viewbox.removeItem(self.curve)
        viewbox.removeItem(self.arrow)

    def apply_style(self):
        theme = self.view.theme
        emphasis = self.state in ("down", "up", "hover")
        color = QColor({"down": theme["down"], "up": theme["up"],
                        "hover": theme["down"]}.get(self.state, theme["edge"]))
        if self.state == "dim":
            color.setAlpha(60)
        pen = pg.mkPen(color, width=2.6 if emphasis else 1.5,
                       style=Qt.DashLine if self.dev else Qt.SolidLine)
        self.curve.setPen(pen)
        self.arrow.setStyle(brush=pg.mkBrush(color), pen=pg.mkPen(color))
        self.curve.setZValue(2 if emphasis else 0)
        self.arrow.setZValue(3 if emphasis else 1)

    def update_geometry(self):
        points, (dx, dy) = edge_geometry(self.source.scene_rect(), self.target.scene_rect(),
                                         self.view.layout_name, self.view.edge_style == "curved")
        self.curve.setData(points[:, 0], points[:, 1])
        # ArrowItem points left at angle 0 and rotates clockwise on screen.
        self.arrow.setStyle(angle=math.degrees(math.atan2(-dy, -dx)))
        self.arrow.setPos(points[-1, 0], points[-1, 1])


# --------------------------------------------------------------- geometry
def _clip_to_rect(rect: QRectF, toward: QPointF) -> QPointF:
    """Point where the ray from the rect centre toward ``toward`` leaves it."""
    c = rect.center()
    dx, dy = toward.x() - c.x(), toward.y() - c.y()
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return c
    hw, hh = rect.width() / 2 + 1, rect.height() / 2 + 1
    t = min(hw / abs(dx) if dx else math.inf, hh / abs(dy) if dy else math.inf)
    return QPointF(c.x() + dx * t, c.y() + dy * t)


def _bezier(p0, c1, c2, p3, n=28) -> np.ndarray:
    t = np.linspace(0, 1, n)[:, None]
    pts = np.array([[p.x(), p.y()] for p in (p0, c1, c2, p3)])
    return ((1 - t) ** 3 * pts[0] + 3 * (1 - t) ** 2 * t * pts[1]
            + 3 * (1 - t) * t ** 2 * pts[2] + t ** 3 * pts[3])


def edge_geometry(s: QRectF, t: QRectF, layout: str, curved: bool):
    """Return (Nx2 points, end direction) for an arrow from rect s to rect t."""
    sc, tc = s.center(), t.center()
    if curved and layout == LAYERED_TB and t.top() > s.bottom() + 4:
        p0, p3 = QPointF(sc.x(), s.bottom()), QPointF(tc.x(), t.top())
        dy = (p3.y() - p0.y()) / 2
        c1, c2 = QPointF(p0.x(), p0.y() + dy), QPointF(p3.x(), p3.y() - dy)
    elif curved and layout == LAYERED_LR and t.left() > s.right() + 4:
        p0, p3 = QPointF(s.right(), sc.y()), QPointF(t.left(), tc.y())
        dx = (p3.x() - p0.x()) / 2
        c1, c2 = QPointF(p0.x() + dx, p0.y()), QPointF(p3.x() - dx, p3.y())
    elif curved:
        # A gentle arc bowing to one side of the straight line.
        v = tc - sc
        ctrl = (sc + tc) / 2 + QPointF(-v.y(), v.x()) * 0.18
        p0, p3 = _clip_to_rect(s, ctrl), _clip_to_rect(t, ctrl)
        c1, c2 = p0 + (ctrl - p0) * (2 / 3), p3 + (ctrl - p3) * (2 / 3)
    else:
        p0, p3 = _clip_to_rect(s, tc), _clip_to_rect(t, sc)
        pts = np.array([[p0.x(), p0.y()], [p3.x(), p3.y()]])
        return pts, (p3.x() - p0.x(), p3.y() - p0.y())
    pts = _bezier(p0, c1, c2, p3)
    d = pts[-1] - pts[-3]
    return pts, (d[0], d[1])


# ----------------------------------------------------------------- layouts
def _adjacency(graph: m.DependencyGraph):
    children = {name: [] for name in graph.packages}
    parents = {name: [] for name in graph.packages}
    for e in graph.edges:
        if e.child not in children[e.parent]:
            children[e.parent].append(e.child)
            parents[e.child].append(e.parent)
    return children, parents


def compute_layers(graph: m.DependencyGraph) -> list[list[str]]:
    """Assign each package to a layer (longest path from the root) and order
    the layers to reduce crossings."""
    children, parents = _adjacency(graph)
    depth = {graph.root: 0}
    # Longest-path layering; the iteration cap guards against cycles.
    for _ in range(len(graph.packages)):
        changed = False
        for e in graph.edges:
            new_depth = depth[e.parent] + 1 if e.parent in depth else None
            if new_depth is not None and depth.get(e.child, -1) < new_depth < len(graph.packages):
                depth[e.child] = new_depth
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
        for i in range(len(layers) - 2, 0, -1):  # upward sweep
            index = {n: k for k, n in enumerate(layers[i + 1])}
            layers[i].sort(key=lambda n: barycenter(n, children, index))
    return [layer for layer in layers if layer]


def layout_layered(graph, sizes, horizontal=False) -> dict[str, QPointF]:
    layers = compute_layers(graph)
    pos = {}
    if not horizontal:
        y = 0.0
        for layer in layers:
            width = sum(sizes[n][0] for n in layer) + H_GAP * (len(layer) - 1)
            height = max(sizes[n][1] for n in layer)
            x = -width / 2
            for name in layer:
                pos[name] = QPointF(x + sizes[name][0] / 2, y + height / 2)
                x += sizes[name][0] + H_GAP
            y += height + V_GAP
    else:
        gap = 22
        x = 0.0
        for layer in layers:
            col_w = max(sizes[n][0] for n in layer)
            total = sum(sizes[n][1] for n in layer) + gap * (len(layer) - 1)
            y = -total / 2
            for name in layer:
                pos[name] = QPointF(x + col_w / 2, y + sizes[name][1] / 2)
                y += sizes[name][1] + gap
            x += col_w + V_GAP * 1.3
    return pos


def layout_radial(graph, sizes) -> dict[str, QPointF]:
    """Root in the middle, a breadth-first spanning tree fanned out in rings."""
    children, _ = _adjacency(graph)
    rank = {n: i for i, n in enumerate(n for layer in compute_layers(graph) for n in layer)}
    tree = {n: [] for n in graph.packages}
    depth = {graph.root: 0}
    queue = [graph.root]
    while queue:
        node = queue.pop(0)
        for child in sorted(children[node], key=rank.get):
            if child not in depth:
                depth[child] = depth[node] + 1
                tree[node].append(child)
                queue.append(child)
    for n in graph.packages:  # anything unreachable hangs off the root
        if n not in depth:
            depth[n] = 1
            tree[graph.root].append(n)

    leaves: dict[str, int] = {}

    def count(n):
        leaves[n] = sum(count(c) for c in tree[n]) or 1
        return leaves[n]

    angle: dict[str, float] = {}

    def assign(n, a0, a1):
        angle[n] = (a0 + a1) / 2
        a = a0
        for c in tree[n]:
            span = (a1 - a0) * leaves[c] / leaves[n]
            assign(c, a, a + span)
            a += span

    count(graph.root)
    assign(graph.root, -math.pi / 2, 3 * math.pi / 2)

    # Every leaf gets an equal slice of the circle, so neighbours on a ring
    # are at least one slice apart; make each ring big enough for that.
    radius = [0.0]
    slice_angle = 2 * math.pi / leaves[graph.root]
    step = max(max(h for _, h in sizes.values()) * 3, 150)
    for d in range(1, max(depth.values()) + 1):
        widest = max(sizes[n][0] for n in graph.packages if depth[n] == d)
        radius.append(max(radius[-1] + step, (widest + H_GAP) / slice_angle))
    pos = {n: QPointF(radius[depth[n]] * math.cos(angle[n]),
                      radius[depth[n]] * math.sin(angle[n])) for n in graph.packages}
    return _remove_overlaps(pos, sizes)


def layout_circular(graph, sizes) -> dict[str, QPointF]:
    order = [n for layer in compute_layers(graph) for n in layer]
    r = max(sum(sizes[n][0] + H_GAP for n in order) / (2 * math.pi), 150)
    step = 2 * math.pi / max(len(order), 1)
    return {n: QPointF(r * math.cos(-math.pi / 2 + i * step), r * math.sin(-math.pi / 2 + i * step))
            for i, n in enumerate(order)}


def layout_force(graph, sizes) -> dict[str, QPointF]:
    """Fruchterman-Reingold, seeded from the layered layout (deterministic)."""
    names = list(graph.packages)
    start = layout_layered(graph, sizes)
    n = len(names)
    if n < 2:
        return start
    pos = np.array([[start[k].x(), start[k].y()] for k in names], dtype=float)
    index = {k: i for i, k in enumerate(names)}
    edges = np.array([(index[e.parent], index[e.child]) for e in graph.edges], dtype=int).reshape(-1, 2)
    k = 190.0
    temperature = 120.0
    for _ in range(400):
        delta = pos[:, None, :] - pos[None, :, :]
        dist2 = (delta ** 2).sum(-1) + np.eye(n)
        disp = (delta * (k * k / dist2)[:, :, None]).sum(1)  # repulsion
        if len(edges):  # attraction along edges
            d = pos[edges[:, 1]] - pos[edges[:, 0]]
            pull = d * (np.sqrt((d ** 2).sum(-1))[:, None] / k)
            np.add.at(disp, edges[:, 0], pull)
            np.add.at(disp, edges[:, 1], -pull)
        disp -= (pos - pos.mean(0)) * 0.01  # gentle gravity
        length = np.sqrt((disp ** 2).sum(-1))[:, None] + 1e-9
        pos += disp / length * np.minimum(length, temperature)
        temperature = max(1.0, temperature * 0.985)
    result = {name: QPointF(*pos[i]) for i, name in enumerate(names)}
    return _remove_overlaps(result, sizes)


def _remove_overlaps(pos: dict[str, QPointF], sizes, margin=14, iterations=80):
    """Push apart any boxes that overlap, along the axis of least overlap."""
    names = list(pos)
    p = {k: [pos[k].x(), pos[k].y()] for k in names}
    for _ in range(iterations):
        moved = False
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                ox = (sizes[a][0] + sizes[b][0]) / 2 + margin - abs(p[a][0] - p[b][0])
                oy = (sizes[a][1] + sizes[b][1]) / 2 + margin - abs(p[a][1] - p[b][1])
                if ox > 0 and oy > 0:
                    moved = True
                    axis, amount = (0, ox) if ox < oy else (1, oy)
                    s = 1 if p[a][axis] >= p[b][axis] else -1
                    p[a][axis] += s * amount / 2
                    p[b][axis] -= s * amount / 2
        if not moved:
            break
    return {k: QPointF(*v) for k, v in p.items()}


LAYOUT_FUNCTIONS = {
    LAYERED_TB: lambda g, s: layout_layered(g, s, horizontal=False),
    LAYERED_LR: lambda g, s: layout_layered(g, s, horizontal=True),
    RADIAL: layout_radial,
    CIRCULAR: layout_circular,
    FORCE: layout_force,
}


# ------------------------------------------------------------------- view
class DependencyGraphView(QWidget):
    """Shows a `vibefpmgui.manifest.DependencyGraph`. Wheel zooms, drag pans,
    nodes can be dragged, click selects (highlighting what the package
    depends on and what uses it), double-click opens."""

    package_selected = Signal(str)
    package_activated = Signal(str)
    package_context_menu = Signal(str, QPoint)  # right-click: package name, screen position

    def __init__(self, parent=None):
        super().__init__(parent)
        self.layout_name = LAYERED_TB
        self.edge_style = "curved"
        self.theme_key = AUTO_THEME
        self.theme = resolve_theme(self.theme_key)
        self.graph: m.DependencyGraph | None = None
        self.conflicts: dict[str, list[m.Declaration]] = {}
        self.nodes: dict[str, NodeItem] = {}
        self.edges: list[EdgeItem] = []
        self.selected: str | None = None

        self.plot = pg.GraphicsLayoutWidget()
        self.plot.ci.setContentsMargins(0, 0, 0, 0)
        self.vb = self.plot.addViewBox()
        self.vb.setAspectLocked(True)
        self.vb.invertY(True)  # y grows downward, like the screen
        self.vb.setMenuEnabled(False)
        self.plot.scene().sigMouseClicked.connect(self._scene_clicked)

        # "Find package…": over the graph's top-left corner.
        self.search = QLineEdit(self.plot)
        self.search.setPlaceholderText("Find package…")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(220)
        # Readable on every graph theme (light, dark, blueprint, neon).
        self.search.setStyleSheet("QLineEdit { background: rgba(128, 128, 128, 55); border: 1px solid "
                                  "rgba(128, 128, 128, 140); border-radius: 7px; padding: 4px 8px; }"
                                  "QLineEdit:focus { border-color: rgba(77, 148, 255, 220); }")
        self.search.setToolTip("Type to highlight matching packages; Enter selects the next match, "
                               "Esc clears")
        self.search_model = QStringListModel(self)
        completer = QCompleter(self.search_model, self.search)
        completer.setCaseSensitivity(Qt.CaseInsensitive)
        completer.setFilterMode(Qt.MatchContains)
        completer.activated.connect(lambda text: self._jump_to_match(text))
        self.search.setCompleter(completer)
        self.search.textChanged.connect(self._highlight_matches)
        self.search.returnPressed.connect(lambda: self._jump_to_match(None))
        self.search.installEventFilter(self)
        self.search.move(10, 10)

        self.message = QLabel()
        self.message.setAlignment(Qt.AlignCenter)
        self.message.setWordWrap(True)
        self.stack = QStackedLayout(self)
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.plot)

        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(16)
        self._anim_timer.timeout.connect(self._animate_step)
        self._anim_from: dict[str, QPointF] = {}
        self._anim_to: dict[str, QPointF] = {}
        self._anim_frame = 0
        self._anim_frames = 24

        self._apply_theme_colors()
        self.clear()

    # ------------------------------------------------------------ options
    def set_layout(self, name: str, animate: bool = True):
        if name in LAYOUTS:
            self.layout_name = name
            self.relayout(animate)

    def set_edge_style(self, style: str):
        if style in EDGE_STYLES:
            self.edge_style = style
            self._update_edges()

    def set_theme(self, key: str):
        self.theme_key = key if key in THEME_CHOICES else AUTO_THEME
        self.refresh_theme()

    def refresh_theme(self):
        """Re-resolve the theme (e.g. after the system switches dark mode)."""
        self.theme = resolve_theme(self.theme_key)
        self._apply_theme_colors()
        for node in self.nodes.values():
            node.update()
        for edge in self.edges:
            edge.apply_style()

    def _apply_theme_colors(self):
        self.plot.setBackground(self.theme["background"])
        self.message.setStyleSheet(
            f"background:{self.theme['background']}; color:{self.theme['subtext']};")

    def package_colors(self) -> dict[str, str]:
        """The color that identifies each package kind in the current theme."""
        return {k: (node_border(self.theme, k).name() if self.theme["border"] else self.theme["fill"][k])
                for k in KIND_ORDER}

    def refresh_fonts(self):
        """Resize the package boxes after the text size changed, keeping each
        box centred where it was, then tidy overlaps by re-running the layout
        only if boxes now collide."""
        for node in self.nodes.values():
            center = node.center()
            node.refresh_fonts()
            node.set_center(center)
        rects = [n.scene_rect() for n in self.nodes.values()]
        if any(a.intersects(b) for i, a in enumerate(rects) for b in rects[i + 1:]):
            self.relayout(animate=False)
        else:
            self._update_edges()

    def legend_html(self) -> str:
        theme = self.theme
        parts = [f'<span style="background:{theme["fill"][k]}; color:{theme["text"]}">'
                 f'&nbsp;<span style="color:{node_border(theme, k).name()}">●</span>'
                 f'&nbsp;{KIND_LABELS[k]}&nbsp;</span>' for k in KIND_ORDER]
        text = (" ".join(parts)
                + f" &nbsp;<span style='color:{theme['down']}'>━</span> depends on"
                + f" &nbsp;<span style='color:{theme['up']}'>━</span> used by"
                + " &nbsp;<i>dashed = dev</i>")
        if self.conflicts:
            text += (f" &nbsp;<span style='color:{CONFLICT_COLOR}'><b>!</b> version conflict"
                     f"{'s' * (len(self.conflicts) != 1)}</span>")
        return text

    # -------------------------------------------------------------- graph
    def clear(self, message="Open an fpm.toml file to see its dependency graph."):
        self.graph = None
        self.conflicts = {}
        self._clear_items()
        self.message.setText(message)
        self.stack.setCurrentWidget(self.message)

    def _clear_items(self):
        self._stop_animation()
        for node in self.nodes.values():
            self.vb.removeItem(node)
        for edge in self.edges:
            edge.remove_from(self.vb)
        self.nodes, self.edges, self.selected = {}, [], None

    def set_graph(self, graph: m.DependencyGraph):
        self._clear_items()
        self.graph = graph
        self.stack.setCurrentWidget(self.plot)
        for name, pkg in graph.packages.items():
            node = NodeItem(self, pkg)
            self.vb.addItem(node)
            self.nodes[name] = node
        seen = set()
        for e in graph.edges:
            if (e.parent, e.child) in seen:
                continue
            seen.add((e.parent, e.child))
            edge = EdgeItem(self, self.nodes[e.parent], self.nodes[e.child], e.dev)
            edge.add_to(self.vb)
            edge.apply_style()
            self.edges.append(edge)
        self._mark_conflicts()
        self.relayout(animate=False)

    def update_packages(self, graph: m.DependencyGraph):
        """Swap in refreshed package details without redoing the layout."""
        self.graph = graph
        for name, node in self.nodes.items():
            if name in graph.packages:
                node.set_package(graph.packages[name])
        self._mark_conflicts()
        self._update_edges()

    def _mark_conflicts(self):
        self.conflicts = m.conflicts(self.graph) if self.graph else {}
        for name, node in self.nodes.items():
            node.set_conflict(self.conflicts.get(name))
        names = sorted(self.nodes, key=str.lower)
        self.search_model.setStringList(names)

    def relayout(self, animate: bool = True):
        """Place every package with the current layout (undoing any drags)."""
        if not self.graph or not self.nodes:
            return
        sizes = {n: (node.rect.width(), node.rect.height()) for n, node in self.nodes.items()}
        target = LAYOUT_FUNCTIONS[self.layout_name](self.graph, sizes)
        if animate and self.isVisible():
            self._anim_from = {n: node.center() for n, node in self.nodes.items()}
            self._anim_to = target
            self._anim_frame = 0
            self._anim_timer.start()
        else:
            self._stop_animation()
            for name, point in target.items():
                self.nodes[name].set_center(point)
            self._update_edges()
            self.fit()

    def _animate_step(self):
        self._anim_frame += 1
        t = min(self._anim_frame / self._anim_frames, 1.0)
        ease = 1 - (1 - t) ** 3
        for name, node in self.nodes.items():
            a, b = self._anim_from.get(name), self._anim_to.get(name)
            if a is not None and b is not None:
                node.set_center(a + (b - a) * ease)
        self._update_edges()
        if t >= 1.0:
            self._anim_timer.stop()
            self.fit()

    def _stop_animation(self):
        self._anim_timer.stop()

    def _update_edges(self, node: NodeItem | None = None):
        for edge in self.edges:
            if node is None or edge.source is node or edge.target is node:
                edge.update_geometry()

    # ---------------------------------------------------------- selection
    def select(self, name: str | None, center: bool = True):
        """Select a package (None clears) and highlight its dependency chains."""
        if name is not None and name not in self.nodes:
            return
        self.selected = name
        down, up = self._reachable(name, forward=True), self._reachable(name, forward=False)
        for n, node in self.nodes.items():
            node.selected = n == name
            node.dimmed = name is not None and n != name and n not in down and n not in up
            node.update()
        for edge in self.edges:
            p, c = edge.source.package.name, edge.target.package.name
            if name is None:
                edge.state = "hover" if edge.source.hovered or edge.target.hovered else "normal"
            elif p in down | {name} and c in down:
                edge.state = "down"
            elif c in up | {name} and p in up:
                edge.state = "up"
            else:
                edge.state = "dim"
            edge.apply_style()
        if name is not None:
            self.package_selected.emit(name)
            if center:
                self.vb.translateBy(self.nodes[name].center() - self.vb.viewRect().center())

    def show_chains(self, name: str, chains: list[list[str]]):
        """Highlight the dependency chains from the project down to ``name``
        (Why Is This Here?); everything else is dimmed. A click on the
        background clears it, like a selection."""
        on_chain = {n for chain in chains for n in chain}
        links = {(a, b) for chain in chains for a, b in pairwise(chain)}
        self.selected = name
        for n, node in self.nodes.items():
            node.selected = n == name
            node.dimmed = n not in on_chain
            node.update()
        for edge in self.edges:
            pair = (edge.source.package.name, edge.target.package.name)
            edge.state = "down" if pair in links else "dim"
            edge.apply_style()

    # ---------------------------------------------------------- searching
    def focus_search(self):
        self.search.setFocus()
        self.search.selectAll()

    def _matches(self, text: str) -> list[str]:
        """Package names containing ``text``: ones starting with it first."""
        text = text.strip().lower()
        if not text:
            return []
        names = sorted(self.nodes, key=str.lower)
        return ([n for n in names if n.lower().startswith(text)]
                + [n for n in names if text in n.lower() and not n.lower().startswith(text)])

    def _highlight_matches(self, text: str):
        """While typing: the matching packages stand out, the rest are dimmed."""
        if not text.strip():
            self.select(self.selected, center=False) if self.selected else self.select(None)
            return
        matches = set(self._matches(text))
        for name, node in self.nodes.items():
            node.selected = False
            node.dimmed = name not in matches
            node.update()
        for edge in self.edges:
            ends = {edge.source.package.name, edge.target.package.name}
            edge.state = "normal" if ends <= matches else "dim"
            edge.apply_style()

    def _jump_to_match(self, name: str | None):
        """Enter: select (and center) the next matching package."""
        matches = [name] if name in self.nodes else self._matches(self.search.text())
        if not matches:
            return
        index = (matches.index(self.selected) + 1) % len(matches) if self.selected in matches else 0
        self.select(matches[index], center=True)

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.search.clear()
            self.plot.setFocus()
            return True
        return super().eventFilter(obj, event)

    def _reachable(self, name: str | None, forward: bool) -> set[str]:
        if name is None or not self.graph:
            return set()
        nxt: dict[str, set[str]] = {}
        for e in self.graph.edges:
            a, b = (e.parent, e.child) if forward else (e.child, e.parent)
            nxt.setdefault(a, set()).add(b)
        seen, stack = set(), [name]
        while stack:
            for n in nxt.get(stack.pop(), ()):
                if n not in seen:
                    seen.add(n)
                    stack.append(n)
        seen.discard(name)
        return seen

    def _hover_changed(self, node: NodeItem, entered: bool):
        node.update()
        if self.selected is not None:
            return  # keep the selection highlighting
        for edge in self.edges:
            if edge.source is node or edge.target is node:
                edge.state = "hover" if entered else "normal"
                edge.apply_style()

    def _scene_clicked(self, ev):
        # A click on empty space clears the selection.
        if ev.button() != Qt.LeftButton or ev.double():
            return
        if not any(isinstance(i, NodeItem) for i in self.plot.scene().items(ev.scenePos())):
            self.select(None)

    # --------------------------------------------------------------- view
    def fit(self):
        if self.nodes:
            rect = QRectF()
            for node in self.nodes.values():
                rect = rect.united(node.scene_rect())
            self.vb.setRange(rect=rect, padding=0.08)
            # Don't blow small graphs up beyond 130 %.
            px = self.vb.viewPixelSize()[0]
            if 0 < px < 1 / 1.3:
                factor = 1 / (1.3 * px)
                self.vb.scaleBy((factor, factor), center=rect.center())

    def zoom(self, factor: float):
        self.vb.scaleBy((1 / factor, 1 / factor))

    def export_image(self, filename: str) -> bool:
        """Save the whole diagram as PNG/JPG, or SVG if the name ends in .svg."""
        from pyqtgraph import exporters

        x_range, y_range = self.vb.viewRange()
        self.fit()
        try:
            if filename.lower().endswith(".svg"):
                # pyqtgraph's SVGExporter chokes on these custom items, so
                # render the scene straight into Qt's SVG generator instead.
                from qtpy.QtSvg import QSvgGenerator

                source = self.vb.sceneBoundingRect()
                target = QRectF(0, 0, source.width(), source.height())
                gen = QSvgGenerator()
                gen.setFileName(filename)
                gen.setSize(source.size().toSize())
                gen.setViewBox(target)
                gen.setTitle(self.graph.root if self.graph else "dependencies")
                painter = QPainter(gen)
                painter.fillRect(target, QColor(self.theme["background"]))
                self.plot.scene().render(painter, target, source)
                painter.end()
            else:
                exporter = exporters.ImageExporter(self.plot.scene())
                params = exporter.parameters()
                params["width"] = int(self.plot.width() * 2)
                params["antialias"] = True
                exporter.export(filename)
            return os.path.exists(filename)
        except Exception:  # noqa: BLE001 - report failure to the caller
            return False
        finally:
            self.vb.setRange(xRange=x_range, yRange=y_range, padding=0)
