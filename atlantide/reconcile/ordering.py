"""Replace ordering over a ChangeSet: create-first, and what must go before a destroy-first delete.

Which nodes a REPLACE creates first is decided from the IR alone
(:mod:`atlantide.graph.cbd`, shared with the lock scope); this module applies
that decision to the changes, falls back to destroy-first where the
replacement would collide with the old resource (:func:`resolve_cbd`), and
settles the one ordering constraint it implies for conditional REPLACEs
(:func:`behind_destroy_first`).
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Set

from returns.result import Failure, Result, Success

from atlantide.core.actions import Action
from atlantide.core.errors import AtlantideError
from atlantide.core.fields import Mutability, physical_name_field
from atlantide.core.resource import Resource
from atlantide.reconcile.changes import Change, ChangeSet, TypeMutability
from atlantide.reconcile.upstream import touches

__all__ = ["behind_destroy_first", "create_first", "resolve_cbd"]


def create_first(change: Change, cbd: frozenset[str]) -> Change:
    """``change`` with create-before-destroy set if it is a REPLACE of a node in ``cbd``.

    ``cbd`` is :func:`~atlantide.graph.cbd.effective_cbd`: the declared nodes
    and everything they depend on. Only ever sets the flag; the planner's
    identity-collision check (:func:`resolve_cbd`) may still clear it.
    """
    if change.action is Action.REPLACE and change.node_id in cbd:
        return dataclasses.replace(change, create_before_destroy=True)
    return change


def resolve_cbd(
    changeset: ChangeSet,
    *,
    types: Mapping[str, type[Resource]],
    mutability: TypeMutability,
    forcers: Mapping[str, frozenset[str]] | None = None,
) -> Result[tuple[ChangeSet, tuple[str, ...]], AtlantideError]:
    """Downgrade create-before-destroy REPLACEs that would collide on identity.

    CBD needs the new resource to coexist with the old. When the replacement
    keeps the old identity (same physical name or, for types that declare
    none, no immutable field changed), it falls back to destroy-before-create,
    with a warning.

    Unless another create-before-destroy node depends on it (``forcers``, see
    :func:`~atlantide.graph.cbd.cbd_forcers`): destroying it first would
    delete what that dependent still uses, so the plan fails instead, naming
    both. Whether this node declares the flag itself does not matter.

    A downgrade makes an upstream destroy-first, so the caller re-runs
    :func:`behind_destroy_first` over the result.
    """
    forcers = forcers or {}
    changes: list[Change] = []
    warnings: list[str] = []
    conflicts: list[str] = []
    for change in changeset.changes:
        if not _cbd_collides(change, types, mutability):
            changes.append(change)
            continue
        if dependents := sorted(forcers.get(change.node_id, frozenset()) - {change.node_id}):
            conflicts.append(
                f"{change.node_id} (depended on by create_before_destroy {', '.join(dependents)})"
            )
            continue
        changes.append(dataclasses.replace(change, create_before_destroy=False))
        warnings.append(
            f"{change.node_id}: create_before_destroy not possible "
            "(replacement shares the old identity); using destroy-before-create"
        )
    if conflicts:
        return Failure(
            AtlantideError(
                "cannot replace create-before-destroy: "
                + "; ".join(conflicts)
                + " — the replacement shares the old identity, so it cannot be "
                "created first, and destroying it first would delete what a "
                "create_before_destroy dependent still uses. Give it a new "
                "physical name, or drop create_before_destroy from the dependent"
            )
        )
    return Success((ChangeSet(tuple(changes)), tuple(warnings)))


def _cbd_collides(
    change: Change, types: Mapping[str, type[Resource]], mutability: TypeMutability
) -> bool:
    """Whether a create-before-destroy REPLACE would clash with the old resource."""
    if not (change.action is Action.REPLACE and change.create_before_destroy):
        return False
    assert change.desired is not None and change.prior is not None
    type_name = change.desired.type
    cls = types.get(type_name)
    name_field = physical_name_field(cls) if cls is not None else None
    if name_field is not None:
        # Distinct only when the cloud name itself changes.
        return change.desired.properties.get(name_field) == change.prior.properties.get(name_field)
    # No declared identity: the replacement is distinct only if an immutable
    # field changed; otherwise it occupies the same slot as the prior resource.
    muts = mutability.get(type_name, {})
    return not any(muts.get(f) is Mutability.IMMUTABLE for f in change.changed_fields)


def _destroyed_first(change: Change) -> bool:
    """Whether ``change`` deletes its old resource before creating the new one."""
    return (
        change.action is Action.REPLACE
        and not change.create_before_destroy
        and change.prior is not None
    )


def behind_destroy_first(changeset: ChangeSet, mutability: TypeMutability) -> ChangeSet:
    """Make every conditional REPLACE behind a destroy-before-create upstream unconditional.

    A conditional REPLACE waits for its refs to resolve, which puts it after its
    upstream in the forward pass. When an ``immutable()`` field it changes refers
    to an upstream replaced destroy-first, that upstream's delete would then run
    while the dependent still uses it; and the recreated upstream almost surely
    has a new identity anyway. So the replace is known now: the executor
    destroys the dependent first (its phase 0), before the upstream's delete.
    Transitive: a dependent made unconditional is itself replaced destroy-first.

    The trade-off: behind a destroy-first upstream a dependent can no longer
    survive as an UPDATE or NOOP when the recreated upstream's value turns out
    unchanged. Behind a create-before-destroy upstream (declared, or propagated
    by :func:`~atlantide.graph.cbd.effective_cbd`) it still can.

    Idempotent; run by :func:`~atlantide.reconcile.diff.diff` after
    create-before-destroy is resolved, and again by whoever clears a change's
    create-before-destroy afterwards.
    """
    first = {c.node_id for c in changeset.changes if _destroyed_first(c)}
    pinned: set[str] = set()
    grew = True
    while grew:
        grew = False
        for change in changeset.changes:
            if change.node_id in pinned or not _behind(change, first, mutability):
                continue
            pinned.add(change.node_id)
            if not change.create_before_destroy:
                first.add(change.node_id)
            grew = True
    if not pinned:
        return changeset
    return changeset.map(
        lambda change: (
            dataclasses.replace(change, conditional=False) if change.node_id in pinned else change
        )
    )


def _behind(change: Change, first: Set[str], mutability: TypeMutability) -> bool:
    """Whether ``change`` is a conditional REPLACE with an immutable ref into ``first``."""
    if not (change.action is Action.REPLACE and change.conditional and change.desired):
        return False
    muts = mutability.get(change.desired.type, {})
    return any(
        muts.get(name) is Mutability.IMMUTABLE
        and touches(change.desired, change.desired.properties.get(name), first)
        for name in change.changed_fields
    )
