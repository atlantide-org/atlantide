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

The hash is a function of config alone and cannot see a state-side change. Two
channels carry one to the diff: a
:data:`~atlantide.state.backend.NO_INPUT_HASH` written by ``refresh --write``,
which no digest equals, and :func:`_stale_dependents`, which pulls a node out of
NOOP when an upstream node is being recreated.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping, Set
from dataclasses import dataclass

from atlantide.core.actions import DESTRUCTIVE_ACTIONS, Action
from atlantide.core.fields import Mutability
from atlantide.core.markers import collect_ref_targets, has_ref_key
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

    def map(self, fn: Callable[[Change], Change]) -> ChangeSet:
        """A new ChangeSet with ``fn`` applied to every change (order kept)."""
        return ChangeSet(tuple(fn(change) for change in self.changes))

    def fingerprint(self) -> frozenset[tuple[str, str, tuple[str, ...], bool, bool]]:
        """What this changeset *does*, as a comparable value.

        Everything a reviewer is shown and nothing they are not: NOOPs are
        excluded (they are absence of action, and one becoming another node's
        NOOP changes nothing to approve), and the `IRNode`/`StateNode` payloads
        are excluded because two runs of the same config produce equal-but-not-
        identical objects.

        A set rather than a sequence: the plan is a graph, and the order the
        scheduler happens to pick is not part of what was approved.
        """
        return frozenset(
            (
                change.node_id,
                change.action.value,
                change.changed_fields,
                change.conditional,
                change.create_before_destroy,
            )
            for change in self.actionable
        )


def restrict(changeset: ChangeSet, selected: frozenset[str]) -> ChangeSet:
    """Downgrade every change outside ``selected`` to NOOP.

    This is why ``--target`` filters the *changeset* rather than the IR. Lowering
    a subset of the config would change each remaining node's Merkle hash — the
    hash folds in its dependencies' — and the apply would persist those altered
    hashes, so the next *full* run would see spurious changes on resources nobody
    touched. Compiling everything and then declining to act keeps every
    untargeted node's stored ``input_hash`` exactly as it was, because a NOOP
    writes nothing at all.
    """
    return changeset.map(
        lambda change: change if change.node_id in selected else Change(change.node_id, Action.NOOP)
    )


def force_replace(changeset: ChangeSet, node_ids: frozenset[str]) -> ChangeSet:
    """Turn the named nodes into REPLACEs, whatever the diff decided.

    The escape hatch for a resource that is wrong in a way config cannot see —
    a corrupted volume, a half-configured instance. Nodes with no change to make
    (a NOOP) are exactly the interesting case, so they are upgraded too; a node
    that is already being replaced is left alone.
    """
    return changeset.map(lambda change: _forced(change) if change.node_id in node_ids else change)


def _forced(change: Change) -> Change:
    if change.action in (Action.DELETE, Action.REPLACE):
        return change  # already going, or already being replaced
    if change.desired is None or change.prior is None:
        return change  # nothing recorded to replace (a CREATE is the same thing)
    return Change(
        node_id=change.node_id,
        action=Action.REPLACE,
        desired=change.desired,
        prior=change.prior,
        changed_fields=("(forced)",),
        create_before_destroy=change.desired.create_before_destroy,
    )


#: Distinguishes "the prior state has no such key" from "it holds None". Reading
#: a missing key as None makes a newly-added field defaulting to None look
#: unchanged, so an `immutable()` one reaches `update()` rather than REPLACE.
_ABSENT = object()


def _changed_fields(
    desired: IRNode, prior: StateNode, moved: Set[str] = frozenset()
) -> tuple[str, ...]:
    ignored = set(desired.ignore_changes)
    changed = {
        name
        for name, value in desired.properties.items()
        if value != prior.properties.get(name, _ABSENT)
    }
    changed |= {name for name in prior.properties if name not in desired.properties}
    changed -= ignored  # ignore_changes fields never count as changed
    # A ref-bearing field whose referenced upstream is itself changing (``moved``)
    # will resolve to a new value even though its marker is symbolically
    # unchanged, so it counts as changed. Attribution is per referenced upstream,
    # never blanket "every ref field": a field can be both ref-bearing and
    # `immutable()` (`SecurityGroup.vpc_id`, `Route53Record.zone_id`), and
    # attributing a ref to an *unchanged* upstream would invent a REPLACE that
    # destroys and recreates working infrastructure. A poisoned row (see
    # NO_INPUT_HASH) gets the same treatment — the evidence here is the
    # upstream's own action, not an inference from the hash mismatch.
    changed |= {
        name
        for name, value in desired.properties.items()
        if name not in ignored
        and name not in changed
        and not collect_ref_targets(value).isdisjoint(moved)
    }
    return tuple(sorted(changed))


def _classify(
    desired: IRNode,
    prior: StateNode,
    mutability: Mapping[str, Mutability],
    moved: Set[str] = frozenset(),
) -> Change:
    changed = _changed_fields(desired, prior, moved)
    if desired.kind == "data":
        # A data source's query changed, so the answer must be re-read. There is
        # nothing to replace: no resource was created, and destroy-then-create
        # would call `delete` on something atlantide does not own.
        return Change(
            node_id=desired.id,
            action=Action.UPDATE,
            desired=desired,
            prior=prior,
            changed_fields=changed,
        )
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


#: Actions giving a node a new physical identity, so every dependent's resolved
#: inputs move even though its own hash does not.
_REIDENTIFYING = frozenset({Action.CREATE, Action.REPLACE})


def _stale_dependents(
    changes: tuple[Change, ...], desired_by_id: Mapping[str, IRNode]
) -> frozenset[str]:
    """Ids whose resolved inputs moved because an upstream node is being recreated.

    The Merkle hash folds in each dependency's desired hash, derived from config
    alone. A dependency recreated for a state-side reason — missing from state, or
    a create that never confirmed — keeps its config, so every dependent hashes
    identically and the Merkle skip NOOPs them while the provider issues a new
    physical id. Recreation is transitive: a dependent forced to re-apply may
    itself be handed new outputs.
    """
    dependents: dict[str, list[str]] = {}
    for node in desired_by_id.values():
        for dep in node.edges():
            dependents.setdefault(dep, []).append(node.id)

    stale: set[str] = set()
    queue = [c.node_id for c in changes if c.action in _REIDENTIFYING]
    while queue:
        for child in dependents.get(queue.pop(), ()):
            if child not in stale:
                stale.add(child)
                queue.append(child)
    return frozenset(stale)


def diff(
    desired: IRGraph,
    desired_hashes: Mapping[str, str],
    prior: StateGraph,
    mutability: TypeMutability,
    *,
    replace: frozenset[str] = frozenset(),
) -> ChangeSet:
    """Compute the ChangeSet from desired IR + its Merkle hashes vs prior state.

    ``replace`` forces the named nodes to REPLACE *before* the refinement pass,
    so their dependents are re-examined exactly as they are for a diff-produced
    REPLACE — applying the force afterwards would leave every dependent NOOPed
    while still holding refs to the destroyed physical id.
    """
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
    if replace:
        changes = tuple(_forced(c) if c.node_id in replace else c for c in changes)
    stale = _stale_dependents(changes, desired_by_id)
    # Upstreams whose resolved values may move this run: every non-NOOP node,
    # plus the stale dependents themselves (recreation is transitive). Used to
    # attribute a symbolically-unchanged ref field to its changing upstream.
    moved = frozenset(c.node_id for c in changes if c.action is not Action.NOOP) | stale

    def refined(change: Change) -> Change:
        want = desired_by_id.get(change.node_id)
        have = prior.get(change.node_id)
        if want is None or have is None:
            return change
        others = moved - {change.node_id}
        if change.action is Action.NOOP:
            if change.node_id not in stale:
                return change
            return _classify(want, have, mutability.get(want.type, {}), others)
        if change.action in (Action.UPDATE, Action.REPLACE) and "(forced)" not in (
            change.changed_fields
        ):
            # Re-attribute with upstream knowledge: an UPDATE whose immutable
            # ref field points at a recreated upstream is really a REPLACE.
            return _classify(want, have, mutability.get(want.type, {}), others)
        return change

    return ChangeSet(changes).map(refined)
