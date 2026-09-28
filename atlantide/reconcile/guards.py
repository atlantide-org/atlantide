"""Enforce the ``prevent_destroy`` guard on a ChangeSet.

Any DELETE or REPLACE of a protected resource fails the whole plan before any
mutation, with one deferral: a *conditional* REPLACE (known after apply) whose
refs point only at upstreams that keep their identity this run is allowed at plan
time and judged at apply, once the executor knows whether an immutable value
really moves. The executor then refuses it before any provider call on the node
if it does. Refusing at plan time instead would block every edit to an upstream,
a bucket's tags say, of a protected dependent that in the common case is not
replaced at all.
"""

from __future__ import annotations

from collections.abc import Set

from returns.result import Failure, Result, Success

from atlantide.core.actions import DESTRUCTIVE_ACTIONS, Action
from atlantide.core.errors import PreventDestroyError
from atlantide.core.markers import collect_ref_targets
from atlantide.reconcile.changes import Change, ChangeSet

#: Actions giving a node a new physical identity: a ref to one almost surely
#: resolves to a new value, so a conditional replace behind it is as good as certain.
_REIDENTIFYING = frozenset({Action.CREATE, Action.REPLACE})


def deferred_to_apply(changeset: ChangeSet, protected: Set[str]) -> frozenset[str]:
    """Protected conditional REPLACEs whose ``prevent_destroy`` verdict waits for apply.

    Deferred only while every upstream the node references through a changed
    field keeps its identity this run (it is updated, not created or replaced).
    Behind a recreated upstream the replace is as good as certain, and refusing
    at plan time avoids recreating the upstream only to stop at the dependent.
    """
    recreated = {c.node_id for c in changeset.changes if c.action in _REIDENTIFYING}
    return frozenset(
        change.node_id
        for change in changeset.changes
        if change.node_id in protected
        and change.action is Action.REPLACE
        and change.conditional
        and _referenced(change).isdisjoint(recreated)
    )


def _referenced(change: Change) -> frozenset[str]:
    """Upstream ids ``change`` references through any of its changed fields."""
    if change.desired is None:
        return frozenset()
    properties = change.desired.properties
    return frozenset().union(
        *(collect_ref_targets(properties.get(name)) for name in change.changed_fields)
    )


def check_prevent_destroy(
    changeset: ChangeSet, protected: Set[str]
) -> Result[ChangeSet, PreventDestroyError]:
    """Validate the ChangeSet; Failure if a destructive action hits a protected id.

    A conditional REPLACE in :func:`deferred_to_apply` passes: the executor
    enforces the guard once the replace is confirmed.
    """
    deferred = deferred_to_apply(changeset, protected)
    blocked = [
        c.node_id
        for c in changeset.changes
        if c.action in DESTRUCTIVE_ACTIONS and c.node_id in protected and c.node_id not in deferred
    ]
    if blocked:
        joined = ", ".join(sorted(blocked))
        return Failure(PreventDestroyError(f"prevent_destroy blocks destroying: {joined}"))
    return Success(changeset)
