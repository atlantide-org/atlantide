"""Desired IR vs current state -> a ChangeSet of per-node actions.

Classification, in order:

- id only in desired            -> CREATE
- id in both, ``input_hash`` eq -> NOOP    (Merkle skip: no provider read)
- id in both, hashes differ     -> UPDATE, or REPLACE if a *changed* field is
                                   ``immutable()``; an immutable changed field
                                   carrying an unresolved ``$ref`` is
                                   known-after-apply, so a *conditional* REPLACE.
- id only in state              -> DELETE

Comparison is symbolic (properties keep ``$ref`` markers), matching the Merkle
hash — so a pure dependency-value change (same markers, different hash) is
attributed to the ref-bearing fields.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass

from atlantide.core.actions import DESTRUCTIVE_ACTIONS, Action
from atlantide.core.fields import Mutability
from atlantide.core.markers import has_ref_key
from atlantide.ir.model import IRGraph, IRNode
from atlantide.state.backend import STATUS_CREATED, StateGraph, StateNode

__all__ = ["DESTRUCTIVE_ACTIONS", "Action", "Change", "ChangeSet", "diff"]

TypeMutability = Mapping[str, Mapping[str, Mutability]]


@dataclass(frozen=True, slots=True)
class Change:
    node_id: str
    action: Action
    desired: IRNode | None = None
    prior: StateNode | None = None
    changed_fields: tuple[str, ...] = ()
    conditional: bool = False  # known-after-apply REPLACE
    create_before_destroy: bool = False  # REPLACE creates new before destroying old


@dataclass(frozen=True, slots=True)
class ChangeSet:
    changes: tuple[Change, ...]

    def by_action(self, action: Action) -> list[Change]:
        return [c for c in self.changes if c.action is action]

    @property
    def actionable(self) -> list[Change]:
        return [c for c in self.changes if c.action is not Action.NOOP]

    def __iter__(self) -> Iterator[Change]:
        return iter(self.changes)


def _changed_fields(desired: IRNode, prior: StateNode) -> tuple[str, ...]:
    ignored = set(desired.ignore_changes)
    changed = {
        name
        for name, value in desired.properties.items()
        if value != prior.properties.get(name)
    }
    changed |= {name for name in prior.properties if name not in desired.properties}
    changed -= ignored  # ignore_changes fields never count as changed
    if not changed:
        # Hashes differ but symbols match => a dependency's value changed.
        # Attribute it to the ref-bearing fields (their resolved values moved).
        changed = {
            name
            for name, value in desired.properties.items()
            if has_ref_key(value) and name not in ignored
        }
    return tuple(sorted(changed))


def _classify(
    desired: IRNode, prior: StateNode, mutability: Mapping[str, Mutability]
) -> Change:
    changed = _changed_fields(desired, prior)
    immutable_changed = [f for f in changed if mutability.get(f) is Mutability.IMMUTABLE]
    if immutable_changed:
        conditional = any(has_ref_key(desired.properties.get(f)) for f in immutable_changed)
        return Change(
            node_id=desired.id,
            action=Action.REPLACE,
            desired=desired,
            prior=prior,
            changed_fields=changed,
            conditional=conditional,
            create_before_destroy=desired.create_before_destroy,
        )
    return Change(
        node_id=desired.id,
        action=Action.UPDATE,
        desired=desired,
        prior=prior,
        changed_fields=changed,
    )


def _change_for(
    node_id: str,
    want: IRNode | None,
    have: StateNode | None,
    desired_hashes: Mapping[str, str],
    mutability: TypeMutability,
) -> Change:
    """Classify a single node id present in the desired IR, prior state, or both."""
    if want is None:  # only in prior state
        return Change(node_id, Action.DELETE, prior=have)
    if have is None:  # only in desired IR
        return Change(node_id, Action.CREATE, desired=want)
    if have.status != STATUS_CREATED:  # write-ahead/failed create -> re-create, never NOOP
        return Change(node_id, Action.CREATE, desired=want, prior=have)
    if desired_hashes[node_id] == have.input_hash:  # Merkle skip: no provider read
        return Change(node_id, Action.NOOP, desired=want, prior=have)
    return _classify(want, have, mutability.get(want.type, {}))


def diff(
    desired: IRGraph,
    desired_hashes: Mapping[str, str],
    prior: StateGraph,
    mutability: TypeMutability,
) -> ChangeSet:
    """Compute the ChangeSet from desired IR + its Merkle hashes vs prior state."""
    desired_by_id = {node.id: node for node in desired.nodes}
    all_ids = sorted(set(desired_by_id) | set(prior.nodes))
    changes = tuple(
        _change_for(
            node_id,
            desired_by_id.get(node_id),
            prior.get(node_id),
            desired_hashes,
            mutability,
        )
        for node_id in all_ids
    )
    return ChangeSet(changes)
