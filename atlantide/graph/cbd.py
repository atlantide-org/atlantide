"""Create-before-destroy propagation: which nodes a REPLACE must create first.

Terraform's rule: a node declared ``create_before_destroy`` makes everything it
depends on (transitively) create-before-destroy too. Replacing an upstream
destroy-first while a create-before-destroy dependent is kept alive would delete
something the dependent still uses; a real cloud refuses it (``DependencyViolation``).

Computed from the IR alone, so the diff (which flags each REPLACE) and the lock
scope (which must cover each ``<id>~replaced`` companion) agree.
"""

from __future__ import annotations

from collections.abc import Mapping

from atlantide.ir.model import IRGraph

__all__ = ["cbd_forcers", "effective_cbd"]


def cbd_forcers(ir: IRGraph) -> Mapping[str, frozenset[str]]:
    """Every create-before-destroy node -> the declared ones that make it so.

    A declared node lists itself, plus any declared dependent. Edges are
    :meth:`~atlantide.ir.model.IRNode.edges` (``$ref`` dependencies and
    ``depends_on``): both order the dependent after its upstream, so both need
    the upstream alive while the dependent is.
    """
    by_id = {node.id: node for node in ir.nodes}
    forcers: dict[str, set[str]] = {}
    for origin in (node for node in ir.nodes if node.create_before_destroy):
        stack = [origin.id]
        while stack:
            node_id = stack.pop()
            seen = forcers.setdefault(node_id, set())
            if origin.id in seen:
                continue
            seen.add(origin.id)
            node = by_id.get(node_id)
            if node is not None:
                stack.extend(node.edges())
    return {node_id: frozenset(origins) for node_id, origins in forcers.items()}


def effective_cbd(ir: IRGraph) -> frozenset[str]:
    """The ids a REPLACE runs create-before-destroy: declared, plus their dependency closure."""
    return frozenset(cbd_forcers(ir))
