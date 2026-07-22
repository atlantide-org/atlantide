"""In-process state backend."""

from __future__ import annotations

import time
from collections.abc import Mapping, Set
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core.errors import LockError
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
    scope_conflict,
)


class MemoryStateBackend(StateBackend):
    """Volatile backend. Same semantics as sqlite, no persistence."""

    def __init__(self, *, clock: Clock = time.time) -> None:
        self._nodes: dict[str, StateNode] = {}
        self._outputs: dict[str, Any] = {}
        self._serial = 0
        self._holds: dict[str, Lease] = {}  # node id -> the lease holding it
        self._now = clock  # injectable for deterministic lock-expiry tests

    def load(self) -> StateGraph:
        return StateGraph(nodes=dict(self._nodes))

    def set_outputs(self, outputs: Mapping[str, Any]) -> None:
        self._outputs.update(outputs)

    def outputs(self) -> dict[str, Any]:
        return dict(self._outputs)

    def put(self, node: StateNode) -> None:
        self._nodes[node.id] = node
        self._serial += 1

    def delete(self, node_id: str) -> None:
        if node_id in self._nodes:
            del self._nodes[node_id]
            self._serial += 1

    def serial(self) -> int:
        return self._serial

    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        now = self._now()
        if err := scope_conflict(self._holds, owner, now, scope):
            return Failure(err)
        lease = Lease(owner=owner, expires_at=now + ttl_seconds, scope=frozenset(scope))
        for node_id in scope:
            self._holds[node_id] = lease
        return Success(lease)

    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._holds = {nid: lease for nid, lease in self._holds.items() if lease.owner != owner}
        return Success(None)

    def locks(self) -> dict[str, Lease]:
        return dict(self._holds)

    def force_unlock(self, node_ids: Set[str]) -> int:
        broken = [nid for nid in node_ids if nid in self._holds]
        for node_id in broken:
            del self._holds[node_id]
        return len(broken)
