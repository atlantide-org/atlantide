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
from atlantide.reconcile.env import ApplyEnv, node_failure, provider_for
from atlantide.reconcile.progress import (
    Phase,
    RefreshProgress,
    no_refresh_progress,
)
from atlantide.reconcile.resolve import (
    live_outputs,
    reconstruct,
    seal_outputs,
    unseal_outputs,
)
from atlantide.reconcile.writer import writer_for
from atlantide.secrets import SecretsRegistry
from atlantide.state import (
    NO_INPUT_HASH,
    NodeStatus,
    StateGraph,
    StateNode,
)


class Drift(Enum):
    """How one node's live state compares to what state records."""

    IN_SYNC = "in_sync"  # every value the read reported matches state
    DRIFTED = "drifted"  # see NodeDrift.changed
    MISSING = "missing"  # the provider could not find the resource


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
    def has_drift(self) -> bool:
        return any(n.kind is not Drift.IN_SYNC for n in self.nodes)


async def refresh(
    *,
    prior: StateGraph,
    env: ApplyEnv,
    write: bool = False,
    prune: bool = False,
    progress: RefreshProgress | None = None,
) -> DriftReport:
    """Read every recorded resource's live state and report drift vs. persisted state.

    Reads run concurrently (bounded by ``env.parallelism``) and never mutate the
    provider. With ``write=True`` the live read is folded back into state (see
    :func:`_folded`).

    A MISSING node is reported but removed only with ``prune=True``: the read can
    be wrong for reasons unrelated to the resource (an unpaginated listing, a
    missing permission, an eventually-consistent view), and deleting the row
    drops the only record of the resource, so the next apply creates a duplicate.

    Only a row the read changes is written, through the installed state writer
    (off the event loop for a network backend) and after ``env.lease.check()``,
    as the executor's writes are.

    The report is sorted by node id.
    """
    on_progress = progress or no_refresh_progress
    ctx = Context()
    outputs = live_outputs(prior, env.secrets)
    semaphore = asyncio.Semaphore(env.parallelism)
    writer = writer_for(env.backend)

    def checked_put(row: StateNode) -> None:
        env.lease.check()  # a lost lease must not write state
        env.backend.put(row)

    def checked_delete(node_id: str) -> None:
        env.lease.check()
        env.backend.delete(node_id)

    async def sync(node: StateNode, row: StateNode | None) -> None:
        """Write ``row`` over ``node``, drop it when ``None``; skip it if unchanged."""
        if row is None:
            await writer.run(checked_delete, node.id, key=node.id)
        elif row is not node:
            await writer.run(checked_put, row, key=node.id)

    async def check(node: StateNode) -> NodeDrift:
        async with semaphore:
            on_progress(node.id, Phase.START)
            try:
                res = reconstruct(node, env, outputs)
                live = await provider_for(env.providers, node.provider).read(ctx, res)
            except Exception as exc:
                on_progress(node.id, Phase.FAIL)
                raise node_failure(node.id, "read", exc) from exc
            on_progress(node.id, Phase.FINISH)
        cls = env.types.get(node.type)
        # `res` is what the provider read, so its inputs are the comparable baseline.
        resolved = resolved_properties(node, res)
        if write:
            if live is None:
                await sync(node, _synced_missing(node, prune=prune))
            else:
                await sync(node, _folded(node, resolved, live, cls, env.secrets))
        sensitive = frozenset(sensitive_fields(cls)) if cls is not None else frozenset()
        return classify_drift(node, resolved, live, sensitive, env.secrets)

    # Sorted so the report and any state writes are deterministic. A TaskGroup
    # cancels and awaits the remaining checks when one read fails; otherwise a
    # check with `write=True` could write state after the lock is released.
    ordered = [node for _, node in sorted(prior.nodes.items())]
    async with asyncio.TaskGroup() as tg:
        tasks = [tg.create_task(check(node)) for node in ordered]
    return DriftReport(nodes=[task.result() for task in tasks])


def resolved_properties(node: StateNode, res: Resource) -> dict[str, Any]:
    """The node's input properties as values, keyed as they are stored.

    ``properties`` keeps the ``$ref`` / ``$secret_ref`` / ``$transform`` markers the
    symbolic diff needs, while a provider's ``read`` reports resolved values; this
    makes the two comparable.
    """
    inputs = res.input_values()
    return {key: inputs.get(key, node.properties[key]) for key in node.properties}


def _observed_drift(
    resolved: dict[str, Any], outputs: dict[str, Any], live: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Per-key (stored, live) for every value the provider observed that changed.

    Each key the provider's ``read`` reported is compared against stored inputs
    (``resolved``) and plaintext ``outputs``, so a provider that observes inputs
    (e.g. an S3 bucket's versioning or tags) detects in-place drift. Unreported
    keys are never flagged.

    Both sides go through :func:`~atlantide.core.markers.canonicalize` first: an
    input declared as a nested model (``SgRule``, ``Route``) is a model in config
    but a plain mapping from the provider, and would otherwise always differ.
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
    unreported input is unchecked, not in sync. Derived from the live read rather
    than a per-handler declaration, so it always matches what the handler reports.
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

    Sealed outputs are unsealed for the comparison, and values of ``sensitive``
    fields are reported as :data:`REDACTED`, so drift on a secret is flagged
    without echoing it. ``observed`` / ``unobserved`` record which inputs the read
    covered, so IN_SYNC applies only to the observed ones.
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


def _synced_missing(node: StateNode, *, prune: bool) -> StateNode | None:
    """The row recording that the provider could not find ``node``; ``None`` drops it.

    A write-ahead row carries no physical id, so ``read`` reports MISSING whether
    or not the create leaked; the row is kept as it is for the next apply to reclaim.

    A confirmed row is dropped only with ``prune`` (see :func:`refresh`); otherwise
    its hash is cleared so the next plan re-checks the node.
    """
    if node.status != NodeStatus.CREATED:
        return node
    if prune:
        return None
    if node.input_hash == NO_INPUT_HASH:
        return node  # already marked
    return replace(node, input_hash=NO_INPUT_HASH)


def _folded(
    node: StateNode,
    resolved: dict[str, Any],
    live: dict[str, Any],
    cls: type[Resource] | None,
    secrets: SecretsRegistry,
) -> StateNode:
    """``node`` with the live read folded in: inputs into ``properties``, the rest
    into ``outputs``.

    Outputs are unsealed, merged, then re-sealed so a sensitive value is never
    written in the clear. In addition:

    * A property stored as a marker keeps it. Replacing a ``$ref`` with its value
      erases the dependency, and the next plan would diff the config's marker
      against a literal (a REPLACE on an ``immutable()`` field).
    * Input drift clears ``input_hash``. The diff is symbolic, so config and state
      hash identically after drift; :data:`NO_INPUT_HASH` makes the next plan
      re-check the node.
    * A read that changes nothing returns ``node`` itself, so the caller can skip
      the write. Compared in plaintext: sealing need not be deterministic.
    """
    properties = dict(node.properties)
    stored = unseal_outputs(node.outputs, secrets)
    outputs = dict(stored)
    drifted_inputs = False
    # Canonicalized on both sides, as in `_observed_drift`: a spurious difference
    # here would clear `input_hash` in state.
    comparable = canonicalize(resolved)
    for key, value in canonicalize(live).items():
        if key not in properties:
            outputs[key] = value
        elif comparable.get(key) != value:
            drifted_inputs = True
            if properties[key] == comparable.get(key):  # a literal, safe to record
                properties[key] = value

    if outputs == stored:
        sealed = node.outputs
    else:
        sealed = outputs if cls is None else seal_outputs(outputs, cls, secrets)
    row = replace(
        node,
        properties=properties,
        outputs=sealed,
        input_hash=NO_INPUT_HASH if drifted_inputs else node.input_hash,
    )
    return node if row == node else row
