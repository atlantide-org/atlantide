"""In-process state backend."""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Set
from typing import Any

from returns.result import Failure, Result, Success
from typing_extensions import override

from atlantide.core.errors import LockError
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
    merge_outputs,
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
        self._fence = 0  # monotonic epoch handed to each acquisition

    def _check(self, *touched: str) -> None:
        self._refuse_unfenced(set(touched), self._holds)

    @override
    def load(self) -> StateGraph:
        return StateGraph(nodes=dict(self._nodes))

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        self._outputs = merge_outputs(self._outputs, outputs, remove)

    @override
    def outputs(self) -> dict[str, Any]:
        return dict(self._outputs)

    @override
    def put(self, node: StateNode) -> None:
        self._check(node.id)
        self._store(node)

    def _store(self, node: StateNode) -> None:
        self._nodes[node.id] = node
        self._serial += 1

    def _drop(self, node_id: str) -> None:
        if node_id in self._nodes:
            del self._nodes[node_id]
            self._serial += 1

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Check the whole batch before writing any of it.

        The base implementation loops over :meth:`put`, which would let the
        nodes before the first refused one land — a bulk write that half-applies
        is exactly what the other backends use a transaction to prevent. Writes
        go straight to the store rather than through :meth:`put`, which would
        re-run the fencing check per node.
        """
        listed = list(nodes)
        self._check(*(node.id for node in listed))
        for node in listed:
            self._store(node)

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        dropped = list(delete_ids)
        listed = list(nodes)
        self._check(*dropped, *(node.id for node in listed))
        for node_id in dropped:
            self._drop(node_id)
        for node in listed:
            self._store(node)

    @override
    def delete(self, node_id: str) -> None:
        self._check(node_id)
        self._drop(node_id)

    @override
    def serial(self) -> int:
        return self._serial

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        now = self._now()
        if err := scope_conflict(self._holds, owner, now, scope):
            return Failure(err)
        self._fence += 1
        lease = self._minted_lease(owner, now + ttl_seconds, scope, self._fence)
        for node_id in scope:
            self._holds[node_id] = lease
        return Success(lease)

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._holds = {nid: lease for nid, lease in self._holds.items() if lease.owner != owner}
        return Success(None)

    @override
    def locks(self) -> dict[str, Lease]:
        return dict(self._holds)

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        broken = [nid for nid in node_ids if nid in self._holds]
        for node_id in broken:
            del self._holds[node_id]
        return len(broken)
