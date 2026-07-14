"""Assemble a Compiled from an IR — from fresh source or a stored artifact."""

from __future__ import annotations

from typing import Any

from returns.result import Result

from atlantide.core import AtlantideError, PolicyBinding, Resource
from atlantide.core._tree import tree_map
from atlantide.core.errors import RegistryError
from atlantide.core.lifecycle import Lifecycle
from atlantide.core.markers import is_ref_marker, ref_from_marker
from atlantide.core.node_id import local_name_of
from atlantide.engine.model import Compiled
from atlantide.graph import build_graph, topological_order
from atlantide.ir import merkle_hashes
from atlantide.ir.model import IRGraph
from atlantide.secrets import is_secret_ref_marker, secret_ref_from_marker


def assemble_compiled(
    ir: IRGraph,
    *,
    resources: dict[str, Resource],
    bindings: tuple[PolicyBinding, ...],
    outputs: dict[str, Any],
) -> Result[Compiled, AtlantideError]:
    """Build a :class:`Compiled` from an IR graph and its (source- or artifact-sourced) parts."""
    return build_graph(ir).map(
        lambda graph: Compiled(
            ir=ir,
            graph=graph,
            hashes=merkle_hashes(ir, topological_order(graph)),
            resources=resources,
            policy_bindings=bindings,
            outputs=outputs,
        )
    )


def rehydrate_resources(
    ir: IRGraph, types: dict[str, type[Resource]]
) -> dict[str, Resource]:
    """Rebuild live ``Resource`` objects from IR (for deploy — there is no source).

    ``{"$ref": "id#attr"}`` markers become ``Ref`` objects so validation passes
    them through and the executor resolves them at apply time. Node ids key the
    dict, so a resource's own (default-stack) node id is irrelevant.
    """
    resources: dict[str, Resource] = {}
    for node in ir.nodes:
        cls = types.get(node.type)
        if cls is None:
            raise RegistryError(
                f"cannot deploy {node.id!r}: resource type {node.type!r} is not registered"
            )
        name = local_name_of(node.id)
        properties = {key: _markers_to_refs(value) for key, value in node.properties.items()}
        lifecycle = Lifecycle(
            prevent_destroy=node.prevent_destroy,
            create_before_destroy=node.create_before_destroy,
            ignore_changes=node.ignore_changes,
        )
        resources[node.id] = cls(name, lifecycle=lifecycle, **properties)
    return resources


def _markers_to_refs(value: Any) -> Any:
    def leaf(v: Any) -> Any:
        if is_ref_marker(v):
            return ref_from_marker(v)
        if is_secret_ref_marker(v):
            return secret_ref_from_marker(v)
        return v

    return tree_map(value, leaf, include_sets=False)
