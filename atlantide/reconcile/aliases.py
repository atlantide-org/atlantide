"""Resolve ``aliases`` (rename-without-replace) by migrating prior state.

A resource renamed in config gets a new node id ``{stack}:{type}:{name}``. Left
alone, the diff would DELETE the old id and CREATE the new one. Declaring the old
id (or bare old logical name) in ``Lifecycle(aliases=...)`` instead maps the
existing state node onto the new id.

:func:`resolve_aliases` produces a *migrated* copy of prior state where each
aliased node is rekeyed old -> new and every other node's ``$ref`` markers and
``dependencies`` are rewritten to match. Input hashes are recomputed over the
migrated graph (the node id is not hashed, but a dependent's ``$ref`` marker
embeds the referenced id, so a rename moves the dependent's hash unless the
markers are rewritten and the hash recomputed) — so a pure rename stays NOOP.

The diff/planner run against the migrated graph; :func:`persist_migration` writes
the rekey back to the backend under the state lock so the executor and future
runs see the new ids.
"""

from __future__ import annotations

from dataclasses import replace

from atlantide.core.markers import remap_refs
from atlantide.core.node_id import format_node_id, stack_of, type_name_of
from atlantide.graph import build_graph
from atlantide.graph.order import topological_order
from atlantide.ir.merkle import merkle_hashes
from atlantide.ir.model import IRGraph
from atlantide.reconcile.context import ir_from_state
from atlantide.state.backend import StateBackend, StateGraph, StateNode


def _expand_alias(alias: str, new_id: str) -> str:
    """A full node id for ``alias``: as-is if it names a stack/type, else a bare
    old logical name resolved against ``new_id``'s own stack and type."""
    if ":" in alias:
        return alias
    return format_node_id(stack_of(new_id), type_name_of(new_id), alias)


def alias_remap(prior: StateGraph, ir: IRGraph) -> dict[str, str]:
    """Map ``old_id -> new_id`` for every renamed node the aliases can resolve.

    A remap entry is added only when the new id is absent from state and exactly
    the alias id is present — so an alias is inert once the migration has run (or
    in a fresh environment that never held the old id). First matching alias wins;
    an old id is claimed by at most one new id.
    """
    prior_ids = set(prior.nodes)
    remap: dict[str, str] = {}
    for node in ir.nodes:
        if node.id in prior_ids or not node.aliases:
            continue
        for alias in node.aliases:
            old_id = _expand_alias(alias, node.id)
            if old_id in prior_ids and old_id not in remap:
                remap[old_id] = node.id
                break
    return remap


def _rekey(node: StateNode, remap: dict[str, str]) -> StateNode:
    """One state node with its id, dependencies, and ``$ref`` markers migrated."""
    return replace(
        node,
        id=remap.get(node.id, node.id),
        properties=remap_refs(node.properties, remap),
        dependencies=tuple(remap.get(dep, dep) for dep in node.dependencies),
    )


def resolve_aliases(prior: StateGraph, ir: IRGraph) -> tuple[StateGraph, dict[str, str]]:
    """Return ``(migrated_state, remap)``; ``prior`` unchanged if nothing aliases."""
    remap = alias_remap(prior, ir)
    if not remap:
        return prior, {}

    rekeyed = StateGraph(
        nodes={new.id: new for new in (_rekey(n, remap) for n in prior.nodes.values())}
    )
    hashes = _rehash(rekeyed, {node.id: node.ignore_changes for node in ir.nodes})
    migrated = {nid: replace(n, input_hash=hashes[nid]) for nid, n in rekeyed.nodes.items()}
    return StateGraph(nodes=migrated), remap


def _rehash(state: StateGraph, ignore_by_id: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Recompute each node's Merkle input_hash over migrated state.

    A rename does not change a node's own hash (the id is not hashed), but a
    dependent's ``$ref`` marker embeds the referenced id — so once markers are
    rewritten the dependents must be re-hashed, or a pure rename would look like an
    UPDATE. ``ignore_changes`` comes from the desired IR (state does not persist it).
    """
    synthetic = ir_from_state(state, with_properties=True, ignore_changes=ignore_by_id)
    return merkle_hashes(synthetic, topological_order(build_graph(synthetic).unwrap()))


def persist_migration(
    backend: StateBackend, prior: StateGraph, migrated: StateGraph, remap: dict[str, str]
) -> None:
    """Write an alias rekey back to state: drop old ids, upsert changed nodes.

    Call under the state lock before the executor runs, so the reconcile sees the
    new ids. Idempotent: a re-run whose state is already migrated computes an empty
    ``remap`` and never reaches here.
    """
    for old_id in remap:
        backend.delete(old_id)
    for node_id, node in migrated.nodes.items():
        if prior.nodes.get(node_id) != node:
            backend.put(node)
