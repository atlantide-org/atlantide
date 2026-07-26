"""Refresh: reconcile persisted state against live provider reads (drift).

Reads run concurrently and never mutate the provider; ``write=True`` folds the
detected drift back into state. Apply lives in :mod:`atlantide.reconcile.executor`;
the two share only handle resolution (:mod:`atlantide.reconcile.resolve`).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any

from atlantide.core.context import Context
from atlantide.core.fields import sensitive_fields
from atlantide.core.markers import canonicalize
from atlantide.core.resource import Resource
from atlantide.reconcile.context import (
    PHASE_FAIL,
    PHASE_FINISH,
    PHASE_START,
    ApplyEnv,
    RefreshProgress,
    node_failure,
    provider_for,
)
from atlantide.reconcile.resolve import (
    live_outputs,
    reconstruct,
    seal_outputs,
    unseal_outputs,
)
from atlantide.secrets import SecretsRegistry
from atlantide.state.backend import (
    NO_INPUT_HASH,
    STATUS_CREATED,
    StateBackend,
    StateGraph,
    StateNode,
)


class Drift(Enum):
    """How one node's live state compares to what state records."""

    IN_SYNC = "in_sync"  # live outputs match the persisted ones
    DRIFTED = "drifted"  # live outputs differ (see NodeDrift.changed)
    MISSING = "missing"  # the resource no longer exists at the provider


@dataclass(frozen=True, slots=True)
class NodeDrift:
    node_id: str
    kind: Drift
    #: For DRIFTED: field -> (persisted, live). Empty otherwise.
    changed: dict[str, tuple[Any, Any]] = field(default_factory=dict)
    #: Input fields the provider's ``read`` did not report, so this node's
    #: verdict says nothing about them. See :func:`_unobserved_inputs`.
    unobserved: tuple[str, ...] = ()
    #: Input fields the read did report, i.e. what IN_SYNC actually covers.
    observed: tuple[str, ...] = ()


@dataclass(slots=True)
class DriftReport:
    nodes: list[NodeDrift] = field(default_factory=list)

    def _of_kind(self, kind: Drift) -> list[NodeDrift]:
        return [n for n in self.nodes if n.kind is kind]

    @property
    def drifted(self) -> list[NodeDrift]:
        return self._of_kind(Drift.DRIFTED)

    @property
    def missing(self) -> list[NodeDrift]:
        return self._of_kind(Drift.MISSING)

    @property
    def in_sync(self) -> list[NodeDrift]:
        return self._of_kind(Drift.IN_SYNC)

    @property
    def has_drift(self) -> bool:
        return any(n.kind is not Drift.IN_SYNC for n in self.nodes)


def _noop_refresh_progress(node_id: str, phase: str) -> None:
    pass


async def refresh(
    *,
    prior: StateGraph,
    env: ApplyEnv,
    write: bool = False,
    prune: bool = False,
    progress: RefreshProgress | None = None,
) -> DriftReport:
    """Read every recorded resource's live state and report drift vs. persisted state.

    Reads run concurrently (bounded by ``env.parallelism``); they never mutate the
    provider. With ``write=True`` the detected drift is synced back into state:
    a DRIFTED node's outputs are overwritten with the live ones.

    A node the provider could not find is *reported* but not removed unless
    ``prune=True``. "MISSING" is only ever as trustworthy as the read that
    produced it, and a read can be wrong for reasons that have nothing to do with
    the resource — an unpaginated listing, a permission the caller lacks, an
    eventually-consistent view. Deleting the row on that evidence turns a
    transient misread into a permanent loss of the only record that the resource
    exists, and the next apply builds a second one. Reporting is recoverable;
    deleting is not.

    Report order is deterministic (sorted by node id).
    """
    on_progress = progress or _noop_refresh_progress
    ctx = Context()
    outputs = live_outputs(prior, env.secrets)
    semaphore = asyncio.Semaphore(env.parallelism)

    async def check(node: StateNode) -> NodeDrift:
        async with semaphore:
            on_progress(node.id, PHASE_START)
            try:
                res = reconstruct(node, env, outputs)
                live = await provider_for(env.providers, node.provider).read(ctx, res)
            except Exception as exc:
                on_progress(node.id, PHASE_FAIL)
                raise node_failure(node.id, "read", exc) from exc
            on_progress(node.id, PHASE_FINISH)
        cls = env.types.get(node.type)
        # `res` is the object the provider was handed, so its input values are
        # the baseline comparable with what `read` returned.
        resolved = resolved_properties(node, res)
        if write:
            _sync_state(node, resolved, live, env.backend, cls, env.secrets, prune=prune)
        sensitive = frozenset(sensitive_fields(cls)) if cls is not None else frozenset()
        return classify_drift(node, resolved, live, sensitive, env.secrets)

    # Sorted so the report (and any state writes) are deterministic. A TaskGroup,
    # not `gather`: when one read fails, the remaining checks must be cancelled
    # and awaited — a detached check with `write=True` would keep issuing state
    # writes after the caller has raised and released the state lock.
    ordered = [node for _, node in sorted(prior.nodes.items())]
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(check(node)) for node in ordered]
    return DriftReport(nodes=[task.result() for task in tasks])


def resolved_properties(node: StateNode, res: Resource) -> dict[str, Any]:
    """The node's input properties as values, keyed as they are stored.

    ``properties`` is symbolic: it keeps the ``$ref`` / ``$secret_ref`` /
    ``$transform`` markers that make the diff a pure symbolic comparison. A
    provider's ``read`` reports resolved values, since ``reconstruct`` resolved
    them before the call, so the two are only comparable through this.
    """
    inputs = res.input_values()
    return {key: inputs.get(key, node.properties[key]) for key in node.properties}


def _observed_drift(
    resolved: dict[str, Any], outputs: dict[str, Any], live: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Per-key (stored, live) for every value the provider observed that changed.

    Compares each key the provider's ``read`` reported against stored state —
    inputs (``resolved``) and computed ``outputs`` (plaintext) alike — so a
    provider that observes input fields (e.g. an S3 bucket's versioning/tags)
    detects in-place drift, not just output drift. Keys the provider did not
    report are unobserved, hence never flagged.

    Both sides go through :func:`~atlantide.core.markers.canonicalize` first. An
    input declared as a nested model — a ``SgRule``, a ``Route`` — is held as that
    model on the config side and comes back from a provider as a plain mapping,
    and comparing the two directly is never equal. That reports drift on a
    security group nobody touched, on every run, forever; and because
    ``refresh --write`` clears ``input_hash`` whenever it believes an input
    drifted, the phantom then survives into the next plan.
    """
    baseline = canonicalize({**resolved, **outputs})
    return {
        key: (baseline.get(key), value)
        for key, value in sorted(canonicalize(live).items())
        if baseline.get(key) != value
    }


def _unobserved_inputs(
    resolved: dict[str, Any], live: dict[str, Any]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Split the node's inputs into (observed, unobserved) by what ``read`` reported.

    :func:`_observed_drift` can only flag keys the provider returned, so an
    unreported input is not "in sync" — it is *unchecked*, and saying otherwise
    makes IN_SYNC a claim the read never supported.

    Derived from the live read rather than from a per-handler declaration on
    purpose: a declaration is a second source of truth that can drift from what
    the handler actually does, and the thing being reported here is precisely
    what the handler actually did.
    """
    observed = tuple(key for key in sorted(resolved) if key in live)
    unobserved = tuple(key for key in sorted(resolved) if key not in live)
    return observed, unobserved


#: Stands in for both sides of a drifted value on a ``sensitive`` field.
REDACTED = "(sensitive)"


def classify_drift(
    node: StateNode,
    resolved: dict[str, Any],
    live: dict[str, Any] | None,
    sensitive: frozenset[str],
    secrets: SecretsRegistry,
) -> NodeDrift:
    """Pure comparison of a node's persisted state to what its provider observed.

    Persisted sensitive outputs are unsealed for the comparison, then values of
    ``sensitive`` fields are replaced with :data:`REDACTED` in the report — drift
    on a generated secret is flagged without echoing it.

    The verdict is scoped to what the read reported: ``observed`` / ``unobserved``
    record which inputs it covered, so IN_SYNC can be rendered as the partial
    claim it is.
    """
    if live is None:
        return NodeDrift(node.id, Drift.MISSING)
    outputs = unseal_outputs(node.outputs, secrets)
    changed = {
        key: ((REDACTED, REDACTED) if key in sensitive else pair)
        for key, pair in _observed_drift(resolved, outputs, live).items()
    }
    observed, unobserved = _unobserved_inputs(resolved, live)
    return NodeDrift(
        node.id,
        Drift.DRIFTED if changed else Drift.IN_SYNC,
        changed,
        unobserved=unobserved,
        observed=observed,
    )


def _sync_state(
    node: StateNode,
    resolved: dict[str, Any],
    live: dict[str, Any] | None,
    backend: StateBackend,
    cls: type[Resource] | None,
    secrets: SecretsRegistry,
    *,
    prune: bool = False,
) -> None:
    """Reconcile state to the live read: drop a gone node, else fold observed values
    back into the right column — inputs into ``properties``, the rest into ``outputs``.

    Outputs are unsealed, merged with the live read, then re-sealed so a synced
    sensitive value is never written back in the clear.

    Two rules keep the write from corrupting state:

    * A property whose stored form is symbolic keeps its marker. Replacing a
      ``$ref`` with the value it resolved to erases the dependency from state, and
      the next config change diffs the config's marker against that literal —
      a spurious REPLACE when the field is ``immutable()``.
    * Input drift clears ``input_hash``. The diff is symbolic, so config and state
      hash identically after a drift; :data:`NO_INPUT_HASH` is the only channel
      through which the next plan can see it.
    """
    if live is None:
        # A write-ahead row carries no physical id, so `read` reports MISSING
        # whether or not the create leaked. The row is the only record of the
        # attempt, so it is kept for the next apply to reclaim.
        #
        # A confirmed row is dropped only when explicitly asked for: see the
        # `refresh` docstring on why one failed read is not enough evidence to
        # discard the sole record of a resource. The hash is cleared either way,
        # so the next plan re-checks the node instead of skipping it.
        if node.status == STATUS_CREATED:
            if prune:
                backend.delete(node.id)
            else:
                backend.put(replace(node, input_hash=NO_INPUT_HASH))
        return

    properties = dict(node.properties)
    outputs = unseal_outputs(node.outputs, secrets)
    drifted_inputs = False
    # Canonicalized on both sides for the same reason :func:`_observed_drift` is,
    # and it has to be done here too: this decides whether to poison `input_hash`,
    # so a comparison that reports a phantom difference does not merely mis-report
    # it — it writes the phantom into state, where the next plan reads it as a
    # reason to act.
    comparable = canonicalize(resolved)
    for key, value in canonicalize(live).items():
        if key not in properties:
            outputs[key] = value
        elif comparable.get(key) != value:
            drifted_inputs = True
            if properties[key] == comparable.get(key):  # a literal, safe to record
                properties[key] = value

    backend.put(
        replace(
            node,
            properties=properties,
            outputs=outputs if cls is None else seal_outputs(outputs, cls, secrets),
            input_hash=NO_INPUT_HASH if drifted_inputs else node.input_hash,
        )
    )
