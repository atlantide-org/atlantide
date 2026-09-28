"""Desired IR vs current state -> a ChangeSet of per-node actions.

Classification, in order:

- id only in desired            -> CREATE
- id in both, ``input_hash`` eq -> NOOP    (Merkle skip: no provider read); a
                                   *state-only* NOOP when only the
                                   ``prevent_destroy`` flag differs from state
- id in both, hashes differ     -> UPDATE, or REPLACE if a *changed* field is
                                   ``immutable()``; when every immutable changed
                                   field carries an unresolved ``$ref`` the
                                   replace is known-after-apply, a *conditional*
                                   REPLACE the executor confirms with
                                   :func:`~atlantide.reconcile.classify.reclassify`
                                   once the refs resolve.
- id only in state              -> DELETE

A REPLACE is create-before-destroy when its node, or anything that depends on
it, declares ``create_before_destroy`` (:func:`~atlantide.graph.cbd.effective_cbd`).
A conditional REPLACE with an ``immutable()`` ref to an upstream replaced
destroy-first is made unconditional
(:func:`~atlantide.reconcile.ordering.behind_destroy_first`): the upstream's
delete must wait for it.

Comparison is symbolic (properties keep ``$ref`` markers), matching the Merkle
hash, so a dependency-value change (same markers, different hash) is attributed
to the ref-bearing fields.

Symbols cannot show an upstream output that moved *without* the dependent being
re-applied (an apply that stopped between the two, or a ``--target`` run): the
markers still match. Each row records what its ``$ref`` fields resolved to when
it was written (see :mod:`atlantide.reconcile.applied`), and the caller passes
``consumed``, the per-field :class:`~atlantide.reconcile.upstream.Consumed`
verdicts against the upstreams' stored outputs. A field reported
:attr:`~atlantide.reconcile.upstream.Consumed.MOVED` whose upstreams all keep
their outputs this run is a *known* change: an ``immutable()`` one makes an
unconditional REPLACE, even behind the Merkle skip. A row that predates the
record (:attr:`~atlantide.reconcile.upstream.Consumed.UNRECORDED`) is only
consulted when nothing else explains its hash mismatch; its ref fields are then
treated as possibly moved.

The hash depends on config alone and does not reflect state-side changes. Two
mechanisms carry them into the diff: a
:data:`~atlantide.state.model.NO_INPUT_HASH` written by ``refresh --write``,
which no digest equals, and :func:`_stale_dependents`, which pulls a node out of
NOOP when an upstream node is being recreated.

The per-node rules are :mod:`~atlantide.reconcile.classify`'s; this module is
the whole-graph pass that feeds them.
"""

from __future__ import annotations

from collections.abc import Mapping, Set
from dataclasses import dataclass

from atlantide.core.actions import Action
from atlantide.graph.cbd import effective_cbd
from atlantide.ir.model import IRGraph, IRNode
from atlantide.reconcile.changes import FORCED_FIELD, Change, ChangeSet, TypeMutability
from atlantide.reconcile.classify import classify_node, forced
from atlantide.reconcile.ordering import behind_destroy_first, create_first
from atlantide.reconcile.upstream import NO_VERDICTS, ConsumedRefs, settled

# From the model module, not `atlantide.state`: the package imports the sqlite
# backend, and the diff must not import storage (import-linter contract "diff and
# guards are pure").
from atlantide.state.model import (
    NO_INPUT_HASH,
    NodeStatus,
    StateGraph,
    StateNode,
)

__all__ = ["diff"]


def diff(
    desired: IRGraph,
    desired_hashes: Mapping[str, str],
    prior: StateGraph,
    mutability: TypeMutability,
    *,
    replace: frozenset[str] = frozenset(),
    consumed: ConsumedRefs | None = None,
) -> ChangeSet:
    """Compute the ChangeSet from desired IR + its Merkle hashes vs prior state.

    ``replace`` forces the named nodes to REPLACE *before* the refinement pass,
    so their dependents are re-examined as for a diff-produced REPLACE. Forcing
    afterwards would leave dependents NOOPed with refs to the destroyed physical id.

    ``consumed`` carries each node's
    :class:`~atlantide.reconcile.upstream.Consumed` verdicts (see the module
    doc); ``None``, e.g. for state that records no provider outputs, classifies
    symbolically alone, as before the record existed.
    """
    inputs = _DiffInputs(
        desired_by_id={node.id: node for node in desired.nodes},
        desired_hashes=desired_hashes,
        prior=prior,
        mutability=mutability,
        verdicts=consumed or {},
    )
    all_ids = sorted(set(inputs.desired_by_id) | set(prior.nodes))
    changes = tuple(inputs.change_for(node_id) for node_id in all_ids)
    if replace:
        changes = tuple(forced(c) if c.node_id in replace else c for c in changes)
    stale = _stale_dependents(changes, inputs.desired_by_id)
    # Upstreams whose resolved values may move this run: every non-NOOP node,
    # plus the stale dependents themselves (recreation is transitive). Used to
    # attribute a symbolically-unchanged ref field to its changing upstream.
    moved = frozenset(c.node_id for c in changes if c.action is not Action.NOOP) | stale
    cbd = effective_cbd(desired)
    refined = ChangeSet(changes).map(
        lambda change: create_first(inputs.refine(change, stale=stale, moved=moved), cbd)
    )
    return behind_destroy_first(refined, mutability)


@dataclass(frozen=True, slots=True)
class _DiffInputs:
    """One :func:`diff`'s inputs, shared by its per-node steps."""

    desired_by_id: Mapping[str, IRNode]
    desired_hashes: Mapping[str, str]
    prior: StateGraph
    mutability: TypeMutability
    verdicts: ConsumedRefs

    def classify(self, want: IRNode, have: StateNode, moved: Set[str]) -> Change:
        """Classify one node present in both config and state, given the ids moving this run."""
        # A mismatch nothing names: not a poisoned row (whose own refresh
        # verdict explains it), nor a Merkle-equal node pulled in by the caller.
        unexplained = have.input_hash not in (self.desired_hashes[want.id], NO_INPUT_HASH)
        return classify_node(
            want,
            have,
            self.mutability.get(want.type, {}),
            moved,
            self.verdicts.get(want.id, NO_VERDICTS),
            unexplained=unexplained,
        )

    def change_for(self, node_id: str) -> Change:
        """Classify a single node id present in the desired IR, prior state, or both."""
        want = self.desired_by_id.get(node_id)
        have = self.prior.get(node_id)
        if want is None:
            return Change(node_id, Action.DELETE, prior=have)
        if have is None:
            return Change(node_id, Action.CREATE, desired=want)
        if have.status != NodeStatus.CREATED:  # write-ahead/failed create -> re-create, never NOOP
            return Change(node_id, Action.CREATE, desired=want, prior=have)
        if self.desired_hashes[node_id] == have.input_hash:  # Merkle skip: no provider read
            verdicts = self.verdicts.get(node_id, NO_VERDICTS)
            if settled(want, verdicts, frozenset()) - set(want.ignore_changes):
                # The config is unchanged, but a value this node consumes moved since
                # it was applied: an upstream recreated for a state-side reason (a
                # `--replace`, a lost create) by a run that stopped before this node.
                return self.classify(want, have, frozenset())
            # `prevent_destroy` is not hashed, so a protect-only edit lands here; the
            # apply persists it without a provider call.
            state_only = want.prevent_destroy != have.prevent_destroy
            return Change(node_id, Action.NOOP, desired=want, prior=have, state_only=state_only)
        return self.classify(want, have, frozenset())

    def refine(self, change: Change, *, stale: frozenset[str], moved: frozenset[str]) -> Change:
        """Re-classify ``change`` knowing which upstream nodes move this run.

        A stale NOOP is pulled back into the diff, and an UPDATE or REPLACE is
        re-attributed: an UPDATE whose immutable ref field points at a recreated
        upstream becomes a REPLACE, and a value known to have moved since the last
        apply becomes conditional again once its upstream changes too. A forced
        REPLACE is left as forced.
        """
        want = self.desired_by_id.get(change.node_id)
        have = self.prior.get(change.node_id)
        if want is None or have is None:
            return change
        if change.action is Action.NOOP:
            if change.node_id not in stale:
                return change
            return self.classify(want, have, moved)
        if change.action in (Action.UPDATE, Action.REPLACE) and FORCED_FIELD not in (
            change.changed_fields
        ):
            return self.classify(want, have, moved)
        return change


#: Actions giving a node a new physical identity, so every dependent's resolved
#: inputs move even though its own hash does not.
_REIDENTIFYING = frozenset({Action.CREATE, Action.REPLACE})


def _stale_dependents(
    changes: tuple[Change, ...], desired_by_id: Mapping[str, IRNode]
) -> frozenset[str]:
    """Ids whose resolved inputs moved because an upstream node is being recreated.

    The Merkle hash folds in each dependency's desired hash, derived from config
    alone. A dependency recreated for a state-side reason (missing from state, or
    an unconfirmed create) keeps its config, so every dependent hashes identically
    and the Merkle skip would NOOP them while the provider issues a new physical
    id. Recreation is transitive: a dependent forced to re-apply may itself
    receive new outputs.
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
