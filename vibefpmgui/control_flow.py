"""A Fortran procedure's control-flow graph, and a layout for drawing it (no Qt).

`build` turns a procedure's statements (read as `vibefpmgui.complexity`
reads them) into nodes and edges:

- a run of statements without a branch is one *block*;
- ``if (…) then`` / ``else if``, one-line ``if``, ``select case`` (and
  ``select type``, ``select rank``) and ``where`` are *decisions*, their
  edges labelled ("true", "false", "case (1)"…);
- a loop (``do``, ``do while``, ``forall`` block…) is a *loop* node: "loop"
  into its body, "done" out of it, the body's end back to it; ``cycle`` goes
  back to it, ``exit`` out of it;
- ``return`` goes to the *end* node, ``stop`` / ``error stop`` to a *stop*
  node; ``goto`` (computed too) and an arithmetic ``if`` go to their labels.

For a graph with one way in, ``edges - nodes + 2`` is the number of its
decisions plus 1: its cyclomatic complexity (without the ``.and.`` /
``.or.`` that `vibefpmgui.complexity`'s CCN also counts).

`layout` places the nodes in layers, top to bottom (loops' back edges set
aside), ordered to cross few edges.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import complexity as cx

START, END, STOP, BLOCK, DECISION, LOOP = "start", "end", "stop", "block", "decision", "loop"
MAX_STATEMENTS_SHOWN = 4  # a block's lines shown (then "…")


@dataclass
class Node:
    id: int
    kind: str                     # START, END, STOP, BLOCK, DECISION, LOOP
    first: int = 0                # its lines (1-based; 0 for start/end)
    last: int = 0
    text: list[str] = field(default_factory=list)  # what's shown: its source lines (or "start", "end"…)


@dataclass
class Edge:
    source: int
    target: int
    label: str = ""               # "true", "false", "loop", "done", "case (1)"…
    back: bool = False            # (set by layout: a loop's way back)


@dataclass
class Graph:
    name: str
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)

    @property
    def complexity(self) -> int:
        """edges - nodes + 2 (with an edge from the stop node, if there's one,
        to the end: the way out of the procedure is one)."""
        stops = sum(1 for n in self.nodes if n.kind == STOP)
        return len(self.edges) + stops - len(self.nodes) + 2


# ------------------------------------------------------------------ statements
@dataclass
class _Statement:
    kind: str           # see _classify
    text: str           # (lowercase, strings blanked)
    label: str
    first: int
    last: int
    detail: str = ""    # a decision's condition, a case's selector, a goto's labels…
    shown: str = ""     # what's shown for it ("": its source line)


# The specification part: declarations and the like, before the first statement that runs.
SPECIFICATION_RE = re.compile(r"^(use|implicit|import|integer|real|double\s*precision|complex|logical|character|"
                              r"type\s*\(|class\s*\(|procedure\s*\(|dimension|parameter|external|intrinsic|save|"
                              r"data|common|equivalence|namelist|allocatable|pointer|target|optional|intent|private|"
                              r"public|protected|contiguous|asynchronous|volatile|value|bind|format|entry|include)\b")
GOTO_RE = re.compile(r"^go\s*to\s*(\d+)\s*$")
COMPUTED_GOTO_RE = re.compile(r"^go\s*to\s*\(([\d\s,]+)\)")
ARITHMETIC_IF_RE = re.compile(r"^(\d+)\s*,\s*(\d+)\s*,\s*(\d+)$")


def _classify(s: str) -> tuple[str, str]:
    """(kind, detail) of a statement."""
    if cx.END_RE.match(s):
        word = (cx.END_RE.match(s).group(1) or "").replace(" ", "")
        return {"if": "end_if", "do": "end_do", "select": "end_select", "where": "end_where",
                "forall": "end_do", "associate": "end_block", "block": "end_block", "critical": "end_block",
                "team": "end_block", "interface": "end_declaration", "type": "end_declaration",
                "enum": "end_declaration"}.get(word, "end_unit"), ""
    if s == "contains":
        return "contains", ""
    if cx.INTERFACE_RE.match(s) or cx.TYPE_DEF_RE.match(s) or cx.ENUM_RE.match(s):
        return "declaration", ""  # (an interface block, a type's or an enum's definition: up to its end)
    if cx._is_assignment(s):
        return "simple", ""
    if re.match(r"^if\s*\(", s):
        close = cx._close_paren(s, s.index("("))
        condition, after = s[:close], s[close:].strip()
        if after == "then":
            return "if_then", condition
        if ARITHMETIC_IF_RE.match(after):
            return "arithmetic_if", after
        return "if_one", condition + "\0" + after
    if re.match(r"^else\s*if\s*\(", s):
        return "else_if", s[:cx._close_paren(s, s.index("("))]
    if re.match(r"^else\s*$", s):
        return "else", ""
    if re.match(r"^else\s*where\s*\(", s):
        return "else_where_mask", s
    if re.match(r"^else\s*where\s*$", s):
        return "else", ""
    if re.match(r"^where\s*\(", s):
        rest = s[cx._close_paren(s, s.index("(")):].strip()
        return ("where_block", s) if not rest else ("if_one", s[:len(s) - len(rest)].strip() + "\0" + rest)
    if re.match(r"^forall\s*\(", s):
        rest = s[cx._close_paren(s, s.index("(")):].strip()
        return ("do", s) if not rest else ("simple", "")
    found = cx.DO_RE.match(s)
    if found:
        return "do", found.group(1) or ""
    if cx.SELECT_RE.match(s):
        return "select", s
    if re.match(r"^(case\s+default|class\s+default|rank\s+default)\b", s):
        return "case_default", s
    if cx.GUARD_RE.match(s):
        return "case", s
    if re.match(r"^cycle\b", s):
        return "cycle", ""
    if re.match(r"^exit\b", s):
        return "exit", ""
    if re.match(r"^return\b", s):
        return "return", ""
    if re.match(r"^(error\s+)?stop\b", s):
        return "stop", ""
    found = GOTO_RE.match(s)
    if found:
        return "goto", found.group(1)
    found = COMPUTED_GOTO_RE.match(s)
    if found:
        return "computed_goto", found.group(1)
    if re.match(r"^(associate\s*\(|block\s*$|critical\b|change\s+team\s*\()", s):
        return "begin_block", ""
    return "simple", ""


def procedure_statements(text: str, path: Path, start_line: int) -> list[_Statement]:
    """The statements of the procedure that starts at ``start_line``: its
    executable part, after its header, up to its ``end`` or ``contains``
    (interface blocks, type and enum definitions in it left out: declarations)."""
    found = cx.statements(text, path.suffix.lower() in cx.FIXED_FORM)
    out: list[_Statement] = []
    started = False
    declaring = 0  # (inside interface blocks, type or enum definitions: declarations)
    for st in found:
        if not started:
            started = st.lines[0] == start_line
            continue
        kind, detail = _classify(st.text)
        if kind == "declaration":
            declaring += 1
            continue
        if declaring:
            declaring -= kind == "end_declaration"
            continue
        if kind in ("contains", "end_unit"):
            break
        if not out and (SPECIFICATION_RE.match(st.text) or "::" in st.text):
            continue  # (its declarations: nothing runs there)
        out.append(_Statement(kind, st.text, st.label, st.lines[0], st.lines[-1], detail))
    return out


# ------------------------------------------------------------------ the graph
class _Builder:
    def __init__(self, name: str, source_lines: list[str]):
        self.graph = Graph(name)
        self.source = source_lines
        self.start = self._node(START, text=["start"])
        self.end = self._node(END, text=["end"])
        self.stop_node: int | None = None
        self.labels: dict[str, int] = {}          # statement label -> the node it starts
        self.gotos: list[tuple[int, str, str]] = []  # (node, label it goes to, edge label)
        self.loops: list[tuple[int, list]] = []   # (loop node, the "exit"s out of it)
        self.block: int | None = None              # the block statements are being added to

    def _node(self, kind: str, first: int = 0, last: int = 0, text: list[str] | None = None) -> int:
        node = Node(len(self.graph.nodes), kind, first, last, text or [])
        self.graph.nodes.append(node)
        return node.id

    def _edge(self, source: int, target: int, label: str = ""):
        self.graph.edges.append(Edge(source, target, label))

    def _connect(self, preds: list, target: int):
        for source, label in preds:
            self._edge(source, target, label)

    def _line(self, number: int) -> str:
        """A line of the source as shown: its code, without its comment."""
        text = _without_comment(self.source[number - 1]).strip() if 0 < number <= len(self.source) else ""
        return text if len(text) <= 60 else text[:57] + "…"

    def _decision(self, st: _Statement, preds: list, kind: str = DECISION, shown: str = "") -> int:
        self.block = None
        node = self._node(kind, st.first, st.last, [shown or self._line(st.first)])
        self._connect(preds, node)
        if st.label:
            self.labels[st.label] = node
        return node

    def _simple(self, st: _Statement, preds: list) -> list:
        """A statement without a branch: into the open block (a new one if
        something else leads in, or it's a jump's target)."""
        node = self.graph.nodes[self.block] if self.block is not None else None
        if node is None or st.label or preds != [(node.id, "")]:
            self.block = self._node(BLOCK, st.first, st.last)
            node = self.graph.nodes[self.block]
            self._connect(preds, node.id)
            if st.label:
                self.labels[st.label] = node.id
        node.last = st.last
        if len(node.text) < MAX_STATEMENTS_SHOWN:
            node.text.append(st.shown or self._line(st.first))
        elif node.text[-1] != "…":
            node.text.append("…")
        return [(node.id, "")]

    def _stop(self) -> int:
        if self.stop_node is None:
            self.stop_node = self._node(STOP, text=["stop"])
        return self.stop_node

    def sequence(self, stmts: list[_Statement], i: int, preds: list, until: set[str],
                 loop_label: str = "") -> tuple[int, list]:
        """Statements from ``i`` up to one of the kinds in ``until`` (not taken),
        or a labelled loop's last statement (taken). (index after, the ways
        out at the end)."""
        while i < len(stmts):
            st = stmts[i]
            if st.kind in until:
                return i, preds
            i, preds = self.statement(stmts, i, preds)
            if loop_label and stmts[i - 1].label == loop_label:
                return i, preds  # (also when a loop inside ended on the same label: "do 10 … do 10 …")
        return i, preds

    def statement(self, stmts: list[_Statement], i: int, preds: list) -> tuple[int, list]:
        st = stmts[i]
        kind = st.kind
        if kind in ("simple", "begin_block", "end_block") or kind in ("case", "case_default", "else", "else_if",
                                                                     "else_where_mask", "end_if", "end_do",
                                                                     "end_select", "end_where"):
            if kind in ("simple", "begin_block", "end_block"):
                return i + 1, self._simple(st, preds) if preds or st.label else preds
            return i + 1, preds  # (out of place: read as nothing)
        if kind == "if_one":
            _condition, inner = st.detail.split("\0", 1)
            condition_shown, inner_shown = _split_one_line(self._line(st.first))
            node = self._decision(st, preds, shown=condition_shown)
            inner_kind, inner_detail = _classify(inner)
            inner_st = _Statement(inner_kind, inner, "", st.first, st.last, inner_detail, inner_shown)
            _j, inner_out = self.statement([inner_st], 0, [(node, "true")])
            self.block = None
            return i + 1, inner_out + [(node, "false")]
        if kind in ("if_then", "where_block"):
            ends = {"if_then": ("else_if", "else", "end_if"), "where_block": ("else_where_mask", "else", "end_where")}
            branch_kinds, end_kind = set(ends[kind][:2]), ends[kind][2]
            node = self._decision(st, preds)
            out = []
            i, branch = self.sequence(stmts, i + 1, [(node, "true")], branch_kinds | {end_kind})
            out += branch
            has_else = False
            while i < len(stmts) and stmts[i].kind in branch_kinds:
                if stmts[i].kind == "else":
                    self.block = None
                    has_else = True
                    i, branch = self.sequence(stmts, i + 1, [(node, "false")], {end_kind})
                else:
                    node = self._decision(stmts[i], [(node, "false")])
                    i, branch = self.sequence(stmts, i + 1, [(node, "true")], branch_kinds | {end_kind})
                out += branch
            if not has_else:
                out.append((node, "false"))
            self.block = None
            return i + 1, out
        if kind == "select":
            node = self._decision(st, preds)
            out, has_default = [], False
            i += 1
            while i < len(stmts) and stmts[i].kind in ("simple",) and not stmts[i].label:
                i += 1  # (nothing runs between "select" and its first case)
            while i < len(stmts) and stmts[i].kind in ("case", "case_default"):
                guard = stmts[i]
                has_default |= guard.kind == "case_default"
                label = self._line(guard.first)
                self.block = None
                i, branch = self.sequence(stmts, i + 1, [(node, label)], {"case", "case_default", "end_select"})
                out += branch
            if not has_default:
                out.append((node, "default"))
            self.block = None
            return i + 1, out
        if kind == "do":
            node = self._decision(st, preds, LOOP)
            exits: list = []
            self.loops.append((node, exits))
            if st.detail:  # ("do 10 i = …": up to the statement labelled 10)
                i, body = self.sequence(stmts, i + 1, [(node, "loop")], set(), loop_label=st.detail)
            else:
                i, body = self.sequence(stmts, i + 1, [(node, "loop")], {"end_do"})
                i += 1
            self.loops.pop()
            self._connect(body, node)
            self.block = None
            endless = st.text.strip() == "do"
            return i, exits + ([] if endless else [(node, "done")])
        if kind in ("cycle", "exit"):
            preds = self._simple(st, preds)
            if self.loops:
                loop, exits = self.loops[-1]
                if kind == "cycle":
                    self._connect(preds, loop)
                else:
                    exits.extend(preds)
            self.block = None
            return i + 1, []
        if kind == "return":
            preds = self._simple(st, preds)
            self._connect(preds, self.end)
            self.block = None
            return i + 1, []
        if kind == "stop":
            preds = self._simple(st, preds)
            self._connect(preds, self._stop())
            self.block = None
            return i + 1, []
        if kind == "goto":
            preds = self._simple(st, preds)
            for source, _label in preds:
                self.gotos.append((source, st.detail, ""))
            self.block = None
            return i + 1, []
        if kind in ("computed_goto", "arithmetic_if"):
            node = self._decision(st, preds)
            targets = re.findall(r"\d+", st.detail)
            names = ["<0", "=0", ">0"] if kind == "arithmetic_if" else [f"{n + 1}" for n in range(len(targets))]
            ways: dict[str, list[str]] = {}  # (a label given twice: one edge, both names)
            for target, name in zip(targets, names, strict=False):
                ways.setdefault(target, []).append(name)
            for target, labels in ways.items():
                self.gotos.append((node, target, ", ".join(labels)))
            return i + 1, [] if kind == "arithmetic_if" else [(node, "else")]
        return i + 1, self._simple(st, preds)

    def finish(self, exits: list) -> Graph:
        self._connect(exits, self.end)
        for source, label, name in self.gotos:
            if label in self.labels:
                self._edge(source, self.labels[label], name)
        return self.graph


def _without_comment(line: str) -> str:
    """A line without its "!" comment (a "!" in a string isn't one)."""
    quote = None
    for i, ch in enumerate(line):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
        elif ch == "!":
            return line[:i].rstrip()
    return line


def _split_one_line(line: str) -> tuple[str, str]:
    """A one-line "if (c) statement" (or "where (m) …") as written: ("if (c)",
    "statement"); ("", "") if it isn't one on a line of its own."""
    found = re.match(r"\s*(if|where)\s*\(", line, re.IGNORECASE)
    if not found:
        return "", ""
    close = cx._close_paren(line, found.end() - 1)
    return line[:close].strip(), line[close:].strip()


def build(text: str, path: Path | str, procedure: cx.Procedure) -> Graph:
    """The control-flow graph of a procedure of ``text`` (the file it's in)."""
    path = Path(path)
    stmts = procedure_statements(text, path, procedure.start)
    builder = _Builder(procedure.name, text.split("\n"))
    _i, exits = builder.sequence(stmts, 0, [(builder.start, "")], set())
    return builder.finish(exits)


# ------------------------------------------------------------------ the layout
@dataclass
class Layout:
    positions: dict[int, tuple[float, float]]   # node -> its center
    layers: list[list[int]]
    routes: dict[int, list[tuple[float, float]]] = field(default_factory=dict)
    # ^ an edge (its index) that crosses layers -> the points it goes through, one per layer crossed


WAYPOINT = (14.0, 10.0)  # (a long edge's place in a layer it crosses)


def layout(graph: Graph, sizes: dict[int, tuple[float, float]], h_gap: float = 40, v_gap: float = 46) -> Layout:
    """Layers top to bottom from the start node (a loop's back edges set
    aside: they're marked ``back``), ordered by their neighbours' places. An
    edge that crosses layers gets a waypoint in each, ordered with the nodes:
    it goes around them, not through them."""
    succ: dict[int, list[int]] = {n.id: [] for n in graph.nodes}
    for edge in graph.edges:
        succ[edge.source].append(edge.target)
    # Back edges: to a node still being explored (depth first from the start).
    state: dict[int, int] = {}  # 1: being explored, 2: done
    back: set[tuple[int, int]] = set()
    stack = [(graph.nodes[0].id, iter(succ[graph.nodes[0].id]))] if graph.nodes else []
    if stack:
        state[stack[0][0]] = 1
    while stack:
        node, children = stack[-1]
        child = next(children, None)
        if child is None:
            state[node] = 2
            stack.pop()
        elif state.get(child) == 1:
            back.add((node, child))
        elif child not in state:
            state[child] = 1
            stack.append((child, iter(succ[child])))
    for edge in graph.edges:
        edge.back = (edge.source, edge.target) in back
    forward = [e for e in graph.edges if not e.back]
    # Longest path from the start (in the edges left: no cycles).
    depth = {graph.nodes[0].id: 0} if graph.nodes else {}
    order = _topological(graph, forward)
    outgoing: dict[int, list[Edge]] = {n.id: [] for n in graph.nodes}
    for e in forward:
        outgoing[e.source].append(e)
    for node in order:
        if node in depth:
            for e in outgoing[node]:
                depth[e.target] = max(depth.get(e.target, 0), depth[node] + 1)
    for n in graph.nodes:  # (not reached from the start: a label nothing jumps to)
        depth.setdefault(n.id, max(depth.values(), default=0) + 1)
    bottom = max(depth.values(), default=0)
    for n in graph.nodes:  # (the end and stop nodes at the bottom)
        if n.kind in (END, STOP):
            depth[n.id] = bottom
    sizes = dict(sizes)
    layers: list[list[int]] = [[] for _ in range(bottom + 1)]
    for node in order + [n.id for n in graph.nodes if n.id not in order]:
        if node not in layers[depth[node]]:
            layers[depth[node]].append(node)
    parents: dict[int, list[int]] = {n.id: [] for n in graph.nodes}
    children: dict[int, list[int]] = {n.id: [] for n in graph.nodes}
    chains: dict[int, list[int]] = {}  # an edge's index -> its waypoints, top to bottom
    next_id = max((n.id for n in graph.nodes), default=0) + 1
    for index, e in enumerate(graph.edges):
        if e.back:
            continue
        previous = e.source
        chain = []
        for layer in range(depth[e.source] + 1, depth[e.target]):
            waypoint, next_id = next_id, next_id + 1
            sizes[waypoint] = WAYPOINT
            layers[layer].append(waypoint)
            parents[waypoint], children[waypoint] = [previous], []
            children[previous].append(waypoint)
            chain.append(waypoint)
            previous = waypoint
        children[previous].append(e.target)
        parents[e.target].append(previous)
        if chain:
            chains[index] = chain

    def barycenter(node, neighbours, index):
        places = [index[n] for n in neighbours[node] if n in index]
        return sum(places) / len(places) if places else index.get(node, 0)

    for _ in range(4):
        for k in range(1, len(layers)):
            index = {n: j for j, n in enumerate(layers[k - 1])}
            layers[k].sort(key=lambda n, ix=index: barycenter(n, parents, ix))
        for k in range(len(layers) - 2, -1, -1):
            index = {n: j for j, n in enumerate(layers[k + 1])}
            layers[k].sort(key=lambda n, ix=index: barycenter(n, children, ix))
    positions: dict[int, tuple[float, float]] = {}
    y = 0.0
    for layer in layers:
        if not layer:
            continue
        width = sum(sizes[n][0] for n in layer) + h_gap * (len(layer) - 1)
        height = max(sizes[n][1] for n in layer)
        x = -width / 2
        for n in layer:
            positions[n] = (x + sizes[n][0] / 2, y + height / 2)
            x += sizes[n][0] + h_gap
        y += height + v_gap
    routes = {index: [positions[w] for w in chain] for index, chain in chains.items()}
    nodes = {n.id for n in graph.nodes}
    return Layout({n: p for n, p in positions.items() if n in nodes},
                  [[n for n in layer if n in nodes] for layer in layers if any(n in nodes for n in layer)], routes)


def _topological(graph: Graph, edges: list[Edge]) -> list[int]:
    incoming = {n.id: 0 for n in graph.nodes}
    succ: dict[int, list[int]] = {n.id: [] for n in graph.nodes}
    for e in edges:
        incoming[e.target] += 1
        succ[e.source].append(e.target)
    ready = [n.id for n in graph.nodes if incoming[n.id] == 0]
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for child in succ[node]:
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
    return order
