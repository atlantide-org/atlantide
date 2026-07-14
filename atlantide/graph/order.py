"""Deterministic topological ordering via Kahn's algorithm."""

from __future__ import annotations

import heapq

from atlantide.graph.model import DiGraph


def topological_order(graph: DiGraph, *, reverse: bool = False) -> list[str]:
    """Return node ids in dependency order (dependencies first).

    ``reverse=True`` yields destroy order (dependents first). Ties are broken by
    sorted node id, so the order is deterministic. Assumes the graph is acyclic
    (guaranteed by :func:`build_graph`).
    """
    indegree = {nid: len(graph.predecessors(nid, reverse=reverse)) for nid in graph.node_ids}
    ready = [nid for nid, deg in indegree.items() if deg == 0]
    heapq.heapify(ready)

    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for succ in graph.successors(node, reverse=reverse):
            indegree[succ] -= 1
            if indegree[succ] == 0:
                heapq.heappush(ready, succ)
    return order
