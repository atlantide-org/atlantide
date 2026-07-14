"""Helpers to build IR graphs from a simple {node: [deps]} spec."""

from __future__ import annotations

from atlantide.ir.model import IRGraph, IRNode


def ir_from(spec: dict[str, list[str]]) -> IRGraph:
    """Build an IRGraph where ``spec[node]`` lists that node's dependencies."""
    nodes = tuple(
        IRNode(
            id=nid,
            type="test.Node",
            provider="test",
            provider_version="1.0.0",
            properties={},
            dependencies=tuple(deps),
        )
        for nid, deps in spec.items()
    )
    return IRGraph(nodes=nodes)
