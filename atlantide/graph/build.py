"""Build a :class:`DiGraph` from Atlas IR and reject cycles.

Cycle detection uses iterative Tarjan SCC (deep chains do not overflow the
recursion stack). The error lists every SCC of size > 1 plus any self-loop.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from returns.result import Failure, Result, Success

from atlantide.core.errors import CycleError, IRError
from atlantide.graph.model import DiGraph
from atlantide.ir.model import IRGraph


def build_graph(ir: IRGraph) -> Result[DiGraph, CycleError]:
    """Construct the dependency graph, or Failure(CycleError) if cyclic.

    Raises :class:`IRError` if a node depends on an id absent from the graph.
    """
    node_ids = tuple(sorted(n.id for n in ir.nodes))
    deps, dependents = _adjacency(ir, node_ids)

    cycles = _find_cycles(node_ids, deps)
    if cycles:
        return Failure(CycleError(cycles))
    return Success(DiGraph(node_ids=node_ids, deps=deps, dependents=dependents))


def _adjacency(
    ir: IRGraph, node_ids: tuple[str, ...]
) -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[str, ...]]]:
    """Build the forward (``deps``) and reverse (``dependents``) adjacency maps.

    Both are keyed by every node id with sorted value tuples for determinism.
    """
    known = set(node_ids)
    deps: dict[str, tuple[str, ...]] = {}
    dependents_acc: dict[str, list[str]] = {nid: [] for nid in node_ids}

    for node in ir.nodes:
        for dep in node.dependencies:
            if dep not in known:
                raise IRError(f"node {node.id!r} depends on unknown node {dep!r}")
            dependents_acc[dep].append(node.id)
        deps[node.id] = tuple(sorted(node.dependencies))

    dependents = {nid: tuple(sorted(v)) for nid, v in dependents_acc.items()}
    return deps, dependents


@dataclass(slots=True)
class _Frame:
    """One node's position in the iterative DFS: which child to visit next."""

    node: str
    next_child: int = 0


def _find_cycles(node_ids: tuple[str, ...], deps: dict[str, tuple[str, ...]]) -> list[list[str]]:
    """Return every cycle: each SCC of size > 1, plus each self-loop."""
    tarjan = _Tarjan(deps)
    for root in node_ids:
        tarjan.visit(root)

    self_loops = [[nid] for nid in node_ids if nid in deps[nid]]
    return tarjan.sccs + self_loops


@dataclass(slots=True)
class _Tarjan:
    """Iterative Tarjan SCC state."""

    deps: dict[str, tuple[str, ...]]
    index_of: dict[str, int] = field(default_factory=dict)
    low: dict[str, int] = field(default_factory=dict)
    on_stack: set[str] = field(default_factory=set)
    scc_stack: list[str] = field(default_factory=list)
    sccs: list[list[str]] = field(default_factory=list)
    counter: int = 0

    def visit(self, root: str) -> None:
        """Explore the component reachable from ``root`` (no-op if already seen)."""
        if root in self.index_of:
            return

        work = [_Frame(root)]
        while work:
            frame = work[-1]
            node = frame.node
            if frame.next_child == 0:
                self._enter(node)

            children = self.deps[node]
            if frame.next_child < len(children):
                child = children[frame.next_child]
                frame.next_child += 1
                if child not in self.index_of:
                    work.append(_Frame(child))
                elif child in self.on_stack:
                    self.low[node] = min(self.low[node], self.index_of[child])
                continue

            # All children explored: settle this node's SCC if it roots one.
            if self.low[node] == self.index_of[node]:
                self._pop_scc(node)

            work.pop()
            if work:  # propagate lowlink up to the parent frame.
                parent = work[-1].node
                self.low[parent] = min(self.low[parent], self.low[node])

    def _enter(self, node: str) -> None:
        """First visit to ``node``: assign its index and push it on the SCC stack."""
        self.index_of[node] = self.low[node] = self.counter
        self.counter += 1
        self.scc_stack.append(node)
        self.on_stack.add(node)

    def _pop_scc(self, root: str) -> None:
        """Pop the SCC rooted at ``root``; record it only if it is a real cycle."""
        component: list[str] = []
        while True:
            w = self.scc_stack.pop()
            self.on_stack.discard(w)
            component.append(w)
            if w == root:
                break
        if len(component) > 1:
            self.sccs.append(sorted(component))
