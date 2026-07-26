"""A state backend that records what it was asked to do, and can refuse to do it.

Several correctness properties are about *which backend calls happen and in what
order* rather than about the state that results — "no write landed after the
lease was lost", "the lock was released", "the row was marked stale before the
compensation ran". Asserting those needs a backend that remembers.

:class:`SpyBackend` wraps a real one (usually :class:`MemoryStateBackend`) so the
stored state stays genuine and only the observation is added.
"""

from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import dataclass
from typing import Any

from returns.result import Failure, Result

from atlantide.core.errors import LockError
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import Lease, StateBackend, StateGraph, StateNode


@dataclass
class Call:
    """One recorded backend call: the method name and its salient argument."""

    method: str
    detail: Any = None


class SpyBackend(StateBackend):
    """Records every call, and can be told to fail specific ones.

    ``fail_lock_after`` is the lease-loss lever: the first N ``acquire_lock`` /
    ``renew_lock`` calls succeed and every one after that returns a ``Failure``,
    which is exactly what a run whose hold lapsed and was taken by someone else
    observes.
    """

    def __init__(
        self,
        inner: StateBackend | None = None,
        *,
        fail_lock_after: int | None = None,
        fail_writes: bool = False,
    ) -> None:
        self.inner = inner if inner is not None else MemoryStateBackend()
        self.calls: list[Call] = []
        self.fail_lock_after = fail_lock_after
        self.fail_writes = fail_writes
        self._locks = 0

    # -- observation ------------------------------------------------------

    def _record(self, method: str, detail: Any = None) -> None:
        self.calls.append(Call(method, detail))

    def names(self) -> list[str]:
        """Just the method names, in order."""
        return [call.method for call in self.calls]

    def count(self, method: str) -> int:
        return sum(1 for call in self.calls if call.method == method)

    def writes_after(self, method: str) -> list[Call]:
        """Every mutating call recorded after the last occurrence of ``method``.

        The assertion this exists for is "nothing was written once the lease was
        gone", which is about ordering, not totals.
        """
        mutations = {"put", "put_many", "delete", "replace_many", "set_outputs"}
        indexes = [i for i, call in enumerate(self.calls) if call.method == method]
        if not indexes:
            return []
        return [call for call in self.calls[indexes[-1] + 1 :] if call.method in mutations]

    # -- state ------------------------------------------------------------

    def load(self) -> StateGraph:
        self._record("load")
        return self.inner.load()

    def put(self, node: StateNode) -> None:
        self._record("put", node.id)
        self._refuse_write()
        self.inner.put(node)

    def put_many(self, nodes: Iterable[StateNode]) -> None:
        listed = list(nodes)
        self._record("put_many", [n.id for n in listed])
        self._refuse_write()
        self.inner.put_many(listed)

    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        listed = list(nodes)
        self._record("replace_many", [n.id for n in listed])
        self._refuse_write()
        self.inner.replace_many(delete_ids, listed)

    def delete(self, node_id: str) -> None:
        self._record("delete", node_id)
        self._refuse_write()
        self.inner.delete(node_id)

    def serial(self) -> int:
        return self.inner.serial()

    def _refuse_write(self) -> None:
        if self.fail_writes:
            raise StateWriteRefused("state write refused by the spy")

    # -- locking ----------------------------------------------------------

    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        self._record("acquire_lock", owner)
        return self._lock_result(owner, ttl_seconds, scope)

    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        self._record("renew_lock", owner)
        return self._lock_result(owner, ttl_seconds, scope)

    def _lock_result(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        self._locks += 1
        if self.fail_lock_after is not None and self._locks > self.fail_lock_after:
            self._record("lock_refused", owner)
            return Failure(LockError("held by another-run"))
        return self.inner.acquire_lock(owner, ttl_seconds, scope)

    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._record("release_lock", owner)
        return self.inner.release_lock(owner)

    def locks(self) -> dict[str, Lease]:
        return self.inner.locks()

    def force_unlock(self, node_ids: Set[str]) -> int:
        return self.inner.force_unlock(node_ids)

    # -- outputs ----------------------------------------------------------

    def set_outputs(self, outputs: Any, *, remove: Iterable[str] = ()) -> None:
        self._record("set_outputs", dict(outputs))
        self._refuse_write()
        self.inner.set_outputs(outputs, remove=remove)

    def outputs(self) -> dict[str, Any]:
        return self.inner.outputs()

    def close(self) -> None:
        self._record("close")
        self.inner.close()


class StateWriteRefused(Exception):
    """Raised by :class:`SpyBackend` when ``fail_writes`` is set."""


__all__ = ["Call", "SpyBackend", "StateWriteRefused"]
