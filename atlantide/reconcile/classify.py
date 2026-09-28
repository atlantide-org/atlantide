"""The per-node rules: which action a node present in config and state takes.

One set of rules for both halves of a run. The plan applies them to symbolic
properties (:func:`classify_node`, via :func:`~atlantide.reconcile.diff.diff`),
and the apply re-applies them to resolved values when it reaches a conditional
REPLACE (:func:`reclassify`), so a replace confirmed at apply is decided exactly
as the plan would have decided it with the values known.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping, Set
from typing import Any

from atlantide.core.actions import Action
from atlantide.core.fields import Mutability
from atlantide.core.markers import has_ref_key
from atlantide.ir.model import IRNode
from atlantide.reconcile.changes import FORCED_FIELD, Change
from atlantide.reconcile.upstream import NO_VERDICTS, Consumed, settled, touches, unrecorded

# From the model module, not `atlantide.state`: see `changes.py`.
from atlantide.state.model import StateNode

__all__ = [
    "RecordedMatch",
    "classify_node",
    "forced",
    "reclassify",
]

#: ``(field, resolved value, recorded digest) -> same?``, or ``None`` when the
#: digest cannot be checked: how :func:`reclassify` consults a row's record.
type RecordedMatch = Callable[[str, Any, str], bool | None]


def forced(change: Change) -> Change:
    """``change`` as a REPLACE, for ``diff(..., replace=)``, whatever the diff decided.

    For a resource that is broken in a way config cannot see, e.g. a corrupted
    volume or a half-configured instance. NOOPs are upgraded too; a node already
    being deleted or replaced is left unchanged.
    """
    if change.action in (Action.DELETE, Action.REPLACE):
        return change
    if change.desired is None or change.prior is None:
        return change  # nothing recorded to replace; a CREATE already builds it fresh
    return Change(
        node_id=change.node_id,
        action=Action.REPLACE,
        desired=change.desired,
        prior=change.prior,
        changed_fields=(FORCED_FIELD,),
        create_before_destroy=change.desired.create_before_destroy,
    )


#: Distinguishes a key missing from prior state from one holding None. Otherwise a
#: newly added field defaulting to None compares as unchanged, and an
#: `immutable()` one reaches `update()` instead of REPLACE.
_ABSENT = object()


def _changed_fields(
    desired: IRNode,
    prior: StateNode,
    moved: Set[str] = frozenset(),
    known: Set[str] = frozenset(),
) -> tuple[str, ...]:
    """The fields that differ, symbolically or through an upstream's value.

    ``known``: ref fields whose consumed value is known to have moved since the
    node was last applied (see :func:`~atlantide.reconcile.upstream.settled`).
    """
    ignored = set(desired.ignore_changes)
    changed = {
        name
        for name, value in desired.properties.items()
        if value != prior.properties.get(name, _ABSENT)
    }
    changed |= {name for name in prior.properties if name not in desired.properties}
    changed -= ignored
    # A ref-bearing field whose referenced upstream is in ``moved`` resolves to a
    # new value although its marker is unchanged, so it counts as changed.
    # Attribution is per referenced upstream: a field can be both ref-bearing and
    # `immutable()` (`SecurityGroup.vpc_id`, `Route53Record.zone_id`), so a ref to
    # an *unchanged* upstream must not plan a REPLACE. A poisoned row (see
    # NO_INPUT_HASH) follows the same rule: the upstream's action decides, not the
    # hash mismatch.
    changed |= {
        name
        for name, value in desired.properties.items()
        if name not in ignored and name not in changed and touches(desired, value, moved)
    }
    changed |= set(known) - ignored
    return tuple(sorted(changed))


def classify_node(
    desired: IRNode,
    prior: StateNode,
    mutability: Mapping[str, Mutability],
    moved: Set[str] = frozenset(),
    verdicts: Mapping[str, Consumed] = NO_VERDICTS,
    *,
    unexplained: bool = False,
) -> Change:
    """Classify a node whose row is not a Merkle-skip NOOP.

    ``verdicts`` are the node's :class:`~atlantide.reconcile.upstream.Consumed`
    fields. ``unexplained``: the stored hash differs from the config's for a
    reason the caller could not name (not a poisoned row); only then are
    :attr:`~atlantide.reconcile.upstream.Consumed.UNRECORDED` fields consulted,
    and only if no field changed otherwise. That is the signature of an upstream
    moved by an earlier, interrupted run over a row that predates the record:
    its ref fields may have moved, so an immutable one plans a conditional
    REPLACE the apply confirms.
    """
    known = settled(desired, verdicts, moved)
    changed = _changed_fields(desired, prior, moved, known)
    if unexplained and not changed:
        changed = unrecorded(desired, verdicts, moved)
    upstream_moved = tuple(name for name in changed if name in known)
    if desired.is_data:
        # A changed data-source query is re-read, never replaced: atlantide created
        # nothing, and destroy-then-create would call `delete` on a resource it
        # does not own.
        return Change(
            node_id=desired.id,
            action=Action.UPDATE,
            desired=desired,
            prior=prior,
            changed_fields=changed,
            upstream_moved=upstream_moved,
        )
    immutable_changed = [f for f in changed if mutability.get(f) is Mutability.IMMUTABLE]
    if immutable_changed:
        # Conditional only if every immutable change hangs on a ref whose value is
        # not yet known: a literal immutable change, or a known moved value, is a
        # replace whatever the other refs resolve to.
        conditional = all(
            has_ref_key(desired.properties.get(f)) and f not in known for f in immutable_changed
        )
        return Change(
            node_id=desired.id,
            action=Action.REPLACE,
            desired=desired,
            prior=prior,
            changed_fields=changed,
            conditional=conditional,
            create_before_destroy=desired.create_before_destroy,
            upstream_moved=upstream_moved,
        )
    return Change(
        node_id=desired.id,
        action=Action.UPDATE,
        desired=desired,
        prior=prior,
        changed_fields=changed,
        upstream_moved=upstream_moved,
    )


#: Stands in for a recorded value that moved: unequal to any resolved value.
_MOVED_SINCE_APPLY = object()


def reclassify(
    change: Change,
    *,
    desired_properties: Mapping[str, Any],
    prior_properties: Mapping[str, Any],
    mutability: Mapping[str, Mutability],
    recorded: Mapping[str, str] | None = None,
    matches: RecordedMatch | None = None,
) -> Change:
    """Re-diff a conditional REPLACE once its ``$ref`` fields have resolved.

    The executor calls this when it reaches the node, after its upstreams have
    applied: ``desired_properties`` are the config's values resolved against the
    upstreams' new outputs. A field the row's ``recorded`` digests cover is
    compared with the record through ``matches``: the record is what the
    resource was last applied with, even when an earlier run moved an upstream
    and stopped before this node. Any other field (a literal, or a ref on a row
    that predates the record, or whose digest cannot be checked) is compared
    with ``prior_properties``, the stored row resolved against the outputs this
    run started from. The classification is :func:`classify_node`'s, over
    concrete values instead of markers.

    Returns ``change`` itself when an immutable value really moved (so the
    planner's create-before-destroy resolution stands); otherwise the same node
    as an UPDATE of the fields that moved, or a NOOP when none did.
    """
    assert change.desired is not None and change.prior is not None  # a REPLACE has both
    prior = dict(prior_properties)
    if recorded and matches is not None:
        for name, recorded_digest in recorded.items():
            if name not in desired_properties:
                continue  # dropped from config: a change either way
            same = matches(name, desired_properties[name], recorded_digest)
            if same is not None:
                prior[name] = desired_properties[name] if same else _MOVED_SINCE_APPLY
    fresh = classify_node(
        dataclasses.replace(change.desired, properties=dict(desired_properties)),
        dataclasses.replace(change.prior, properties=prior),
        mutability,
    )
    if fresh.action is Action.REPLACE:
        return change
    return dataclasses.replace(
        change,
        action=Action.UPDATE if fresh.changed_fields else Action.NOOP,
        changed_fields=fresh.changed_fields,
        conditional=False,
        create_before_destroy=False,
    )
