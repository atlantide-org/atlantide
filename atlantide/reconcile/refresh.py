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
from atlantide.core.resource import Resource
from atlantide.reconcile.context import (
    PHASE_FAIL,
    PHASE_FINISH,
    PHASE_START,
    ApplyEnv,
    LiveOutputs,
    RefreshProgress,
    node_failure,
    provider_for,
)
from atlantide.reconcile.resolve import reconstruct, seal_outputs, unseal_outputs
from atlantide.secrets import SecretsRegistry
from atlantide.state.backend import StateBackend, StateGraph, StateNode


class Drift(Enum):
    """How one node's live state compares to what state records."""

    IN_SYNC = "in_sync"   # live outputs match the persisted ones
    DRIFTED = "drifted"   # live outputs differ (see NodeDrift.changed)
    MISSING = "missing"   # the resource no longer exists at the provider


@dataclass(frozen=True, slots=True)
class NodeDrift:
    node_id: str
    kind: Drift
    #: For DRIFTED: field -> (persisted, live). Empty otherwise.
    changed: dict[str, tuple[Any, Any]] = field(default_factory=dict)


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
    progress: RefreshProgress | None = None,
) -> DriftReport:
    """Read every recorded resource's live state and report drift vs. persisted state.

    Reads run concurrently (bounded by ``env.parallelism``); they never mutate the
    provider. With ``write=True`` the detected drift is synced back into state —
    a DRIFTED node's outputs are overwritten with the live ones, a MISSING node
    is deleted. Report order is deterministic (sorted by node id).
    """
    on_progress = progress or _noop_refresh_progress
    ctx = Context()
    # Plaintext seed for ref resolution; sensitive outputs are sealed at rest.
    outputs: LiveOutputs = {
        nid: unseal_outputs(node.outputs, env.secrets) for nid, node in prior.nodes.items()
    }
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
        if write:
            _sync_state(node, live, env.backend, cls, env.secrets)
        sensitive = frozenset(sensitive_fields(cls)) if cls is not None else frozenset()
        return _classify_drift(node, live, sensitive, env.secrets)

    # Sorted so the report (and any state writes) are deterministic.
    ordered = [node for _, node in sorted(prior.nodes.items())]
    return DriftReport(nodes=list(await asyncio.gather(*map(check, ordered))))


def _observed_drift(
    node: StateNode, outputs: dict[str, Any], live: dict[str, Any]
) -> dict[str, tuple[Any, Any]]:
    """Per-key (stored, live) for every value the provider observed that changed.

    Compares each key the provider's ``read`` reported against stored state —
    inputs (``properties``) and computed ``outputs`` (plaintext) alike — so a
    provider that observes input fields (e.g. an S3 bucket's versioning/tags)
    detects in-place drift, not just output drift. Keys the provider did not
    report are unobserved, hence never flagged.
    """
    baseline = {**node.properties, **outputs}
    return {
        key: (baseline.get(key), value)
        for key, value in sorted(live.items())
        if baseline.get(key) != value
    }


#: Stands in for both sides of a drifted value on a ``sensitive`` field.
REDACTED = "(sensitive)"


def _classify_drift(
    node: StateNode,
    live: dict[str, Any] | None,
    sensitive: frozenset[str],
    secrets: SecretsRegistry,
) -> NodeDrift:
    """Pure comparison of a node's persisted state to what its provider observed.

    Persisted sensitive outputs are unsealed for the comparison, then values of
    ``sensitive`` fields are replaced with :data:`REDACTED` in the report — drift
    on a generated secret is flagged without echoing it.
    """
    if live is None:
        return NodeDrift(node.id, Drift.MISSING)
    outputs = unseal_outputs(node.outputs, secrets)
    changed = {
        key: ((REDACTED, REDACTED) if key in sensitive else pair)
        for key, pair in _observed_drift(node, outputs, live).items()
    }
    return NodeDrift(node.id, Drift.DRIFTED if changed else Drift.IN_SYNC, changed)


def _sync_state(
    node: StateNode,
    live: dict[str, Any] | None,
    backend: StateBackend,
    cls: type[Resource] | None,
    secrets: SecretsRegistry,
) -> None:
    """Reconcile state to the live read: drop a gone node, else fold observed values
    back into the right column — inputs into ``properties``, the rest into ``outputs``.

    Outputs are unsealed, merged with the live read, then re-sealed so a synced
    sensitive value is never written back in the clear."""
    if live is None:
        backend.delete(node.id)
        return
    properties = dict(node.properties)
    outputs = unseal_outputs(node.outputs, secrets)
    for key, value in live.items():
        (properties if key in properties else outputs)[key] = value
    if cls is not None:
        outputs = seal_outputs(outputs, cls, secrets)
    backend.put(replace(node, properties=properties, outputs=outputs))
