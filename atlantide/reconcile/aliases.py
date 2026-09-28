"""Resolve ``aliases`` (rename-without-replace) by migrating prior state.

A resource renamed in config gets a new node id ``{stack}:{type}:{name}``, which the
diff would plan as DELETE old + CREATE new. Declaring the old id (or bare old logical
name) in ``Lifecycle(aliases=...)`` maps the existing state node onto the new id.

:func:`resolve_aliases` returns a migrated copy of prior state: each aliased node is
rekeyed old -> new, and every ``$ref`` marker, dependency and input hash is updated
to match. The diff runs against that copy; :func:`persist_migration` writes the
rekey back to the backend under the state lock.
"""

from __future__ import annotations

from dataclasses import replace

from atlantide.core.markers import remap_refs
from atlantide.core.node_id import format_node_id, stack_of, type_name_of
from atlantide.graph import build_graph
from atlantide.graph.order import topological_order
from atlantide.ir.merkle import merkle_hashes
from atlantide.ir.model import IRGraph
from atlantide.reconcile.state_ir import ir_from_state
from atlantide.state import NO_INPUT_HASH, StateBackend, StateGraph, StateNode


def _expand_alias(alias: str, new_id: str) -> str:
    """The full node id for ``alias``.

    An alias containing ``:`` is returned as-is; a bare logical name takes
    ``new_id``'s stack and type.
    """
    if ":" in alias:
        return alias
    return format_node_id(stack_of(new_id), type_name_of(new_id), alias)


def alias_remap(prior: StateGraph, ir: IRGraph) -> dict[str, str]:
    """Map ``old_id -> new_id`` for every renamed node the aliases can resolve.

    An entry is added only when the new id is absent from state and the alias id
    is present, so an alias is inert after migration and in an environment that
    never held the old id. The first matching alias wins; each old id maps to at
    most one new id.
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
    """One state node with its id, dependencies, ``depends_on`` and ``$ref`` markers migrated."""
    return replace(
        node,
        id=remap.get(node.id, node.id),
        properties=remap_refs(node.properties, remap),
        dependencies=tuple(remap.get(dep, dep) for dep in node.dependencies),
        depends_on=tuple(remap.get(dep, dep) for dep in node.depends_on),
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
    migrated = {
        nid: n if n.input_hash == NO_INPUT_HASH else replace(n, input_hash=hashes[nid])
        for nid, n in rekeyed.nodes.items()
    }
    return StateGraph(nodes=migrated), remap


def _rehash(state: StateGraph, ignore_by_id: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Recompute each node's Merkle input_hash over migrated state.

    The node id is not hashed, but a dependent's ``$ref`` marker embeds the
    referenced id, so dependents are re-hashed to keep a pure rename a NOOP.
    ``ignore_changes`` comes from the desired IR because state does not persist it.
    The caller keeps a :data:`~atlantide.state.NO_INPUT_HASH` row as it is: the mark
    records drift or a lost row that no rename resolves.
    """
    synthetic = ir_from_state(state, with_properties=True, ignore_changes=ignore_by_id)
    return merkle_hashes(synthetic, topological_order(build_graph(synthetic).unwrap()))


def persist_migration(
    backend: StateBackend, prior: StateGraph, migrated: StateGraph, remap: dict[str, str]
) -> None:
    """Write an alias rekey back to state: drop old ids, upsert changed nodes.

    Must run under the state lock before the executor. Idempotent: already-migrated
    state yields an empty ``remap``.

    The delete and the upsert go through one
    :meth:`~atlantide.state.backend.StateBackend.replace_many` so state always holds
    one of the two ids: ``alias_remap`` requires the old id, so a partial write
    cannot be recovered by re-running.
    """
    backend.replace_many(
        remap,
        (node for node_id, node in migrated.nodes.items() if prior.nodes.get(node_id) != node),
    )
