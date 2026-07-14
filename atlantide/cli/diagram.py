"""Dependency-graph export for the ``graph`` command (Graphviz dot / Mermaid)."""

from __future__ import annotations

from atlantide.core.node_id import group_by_stack, short_id
from atlantide.graph.model import DiGraph


def to_dot(graph: DiGraph) -> str:
    lines = ["digraph atlantide {", "  rankdir=LR;", '  node [shape=box];']
    for node_id in graph.node_ids:
        lines.append(f'  "{node_id}";')
        for dependency in graph.deps[node_id]:
            lines.append(f'  "{dependency}" -> "{node_id}";')
    lines.append("}")
    return "\n".join(lines)


def to_mermaid(graph: DiGraph) -> str:
    ids = {node_id: f"n{i}" for i, node_id in enumerate(graph.node_ids)}

    # Group nodes by stack (the ``stack:type:name`` prefix) into boxed subgraphs.
    by_stack = group_by_stack(list(graph.node_ids))

    lines = ["graph LR"]
    for cluster, (stack, node_ids) in enumerate(by_stack.items()):
        lines.append(f'  subgraph cluster{cluster}["{stack}"]')
        for node_id in node_ids:
            # the box already names the stack, so drop it from the node label
            lines.append(f'    {ids[node_id]}["{short_id(node_id)}"]')
        lines.append("  end")
    for node_id in graph.node_ids:
        for dependency in graph.deps[node_id]:
            lines.append(f"  {ids[dependency]} --> {ids[node_id]}")
    return "\n".join(lines)
