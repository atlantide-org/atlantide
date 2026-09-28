"""Persisted state read back as a graph: IR and dependency edges rebuilt from rows."""

from __future__ import annotations

from collections.abc import Mapping

from atlantide.graph import build_graph
from atlantide.graph.model import DiGraph
from atlantide.ir.model import IRGraph, IRNode
from atlantide.state import StateGraph

__all__ = ["ir_from_state", "state_digraph"]


def ir_from_state(
    state: StateGraph,
    *,
    with_properties: bool = False,
    ignore_changes: Mapping[str, tuple[str, ...]] | None = None,
) -> IRGraph:
    """Reconstruct an :class:`IRGraph` from persisted state.

    Edges to ids absent from state (e.g. a dependency removed by a partial
    rollback) are dropped: a missing dependency can neither order a delete nor be
    hashed. ``with_properties`` and ``ignore_changes`` matter only when the result
    feeds the Merkle hash.
    """
    present = set(state.nodes)
    ignore = ignore_changes or {}
    nodes = tuple(
        IRNode(
            id=n.id,
            type=n.type,
            provider=n.provider,
            provider_version=n.provider_version,
            properties=n.properties if with_properties else {},
            dependencies=tuple(dep for dep in n.dependencies if dep in present),
            depends_on=tuple(dep for dep in n.depends_on if dep in present),
            ignore_changes=ignore.get(n.id, ()),
        )
        for n in state.nodes.values()
    )
    return IRGraph(nodes=nodes)


def state_digraph(state: StateGraph) -> DiGraph:
    """Rebuild the dependency graph recorded in state (for delete ordering)."""
    # State is written only by acyclic applies, so its graph cannot contain a cycle.
    return build_graph(ir_from_state(state)).unwrap()
