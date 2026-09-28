"""The storage-agnostic backend interface every state store implements.

The engine talks only to :class:`StateBackend`; the value types it trades in live
in :mod:`atlantide.state.model` and :mod:`atlantide.state.leases`.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Set
from typing import Any, ClassVar

from returns.result import Result

from atlantide.core.check import SKIP, Check
from atlantide.core.errors import LockError
from atlantide.state.fencing import fence_violation
from atlantide.state.leases import Clock, Lease
from atlantide.state.model import StateGraph, StateNode

__all__ = ["StateBackend", "merge_outputs"]


def merge_outputs(
    current: Mapping[str, Any], outputs: Mapping[str, Any], remove: Iterable[str]
) -> dict[str, Any]:
    """Merge committed outputs: drop ``remove``, then overlay ``outputs``.

    Shared so every backend applies the same semantics (removal before overlay,
    overlay wins).
    """
    dropped = set(remove)
    kept = {key: value for key, value in current.items() if key not in dropped}
    return {**kept, **outputs}


class StateBackend(ABC):
    """Storage-agnostic state store. Mutations bump ``serial`` (optimistic token)."""

    #: Whether an apply should run this backend's calls on a dedicated writer
    #: thread (see ``atlantide.reconcile.writer.StateWriter``) instead of on the
    #: event loop. Opting in requires the instance to accept calls from a thread
    #: other than its creator, including a loop-thread call while the writer
    #: thread is mid-call.
    offload_writes: ClassVar[bool] = False

    #: How many state writes (``put``/``delete``) to *different* nodes the
    #: executor may have in flight on this backend at once. ``1`` means a
    #: single-writer FIFO; a higher value requires concurrent writes to distinct
    #: nodes to be safe from several threads. Lock operations
    #: (acquire, renew, ``bind_lease``, release) are always called exclusively,
    #: with no write in flight. The executor caps it by ``--parallelism``.
    write_concurrency: int = 1

    def checkpoint(self) -> None:  # noqa: B027 - optional hook, intentionally non-abstract
        """Best-effort housekeeping after a locked run (no-op by default).

        Called once a run that held a lock has finished writing. A backend that
        accumulates per-write artifacts (the S3 journal) compacts them here. It
        must not lose a committed write and must not raise for a transient
        failure: the run's writes are already durable, so a failed checkpoint is
        only deferred work.
        """

    @abstractmethod
    def load(self) -> StateGraph:
        """Return the full committed state graph."""

    @abstractmethod
    def put(self, node: StateNode) -> None:
        """Upsert one node (incremental, crash-safe persist)."""

    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert several nodes as one unit where the backend can.

        The default loops over :meth:`put` and leaves a partial write if
        interrupted. Backends with transactions (or that rewrite a whole
        document) override it so a bulk write (a migration, an alias rekey, a
        rollback) is atomic and costs one round trip.
        """
        for node in nodes:
            self.put(node)

    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Delete and upsert as one unit where the backend can.

        Used by the alias rekey, which moves a resource to a new id. Separate
        deletes and writes leave a window where state holds neither id, from
        which a re-run cannot recover. The default deletes in a loop, then calls
        :meth:`put_many`; backends with transactions override it.
        """
        for node_id in delete_ids:
            self.delete(node_id)
        self.put_many(nodes)

    @abstractmethod
    def delete(self, node_id: str) -> None:
        """Remove one node if present."""

    @abstractmethod
    def serial(self) -> int:
        """Monotonic version, advanced whenever stored state changes.

        A backend may leave it unchanged for a no-op write (an upsert of a node
        already stored verbatim), so compare serials for difference, not as a
        count of calls.
        """

    @abstractmethod
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        """Lock every node id in ``scope`` for ``owner``.

        Fails if any node is already held by a different, unexpired owner.
        Reentrant for the same owner (re-locks/renews); reclaims expired holds.
        An empty ``scope`` is a no-op success.
        """

    #: The lease writes are fenced against, or ``None`` when unbound. Declared on
    #: the base class so :meth:`bind_lease` has one implementation.
    _lease: Lease | None = None

    #: Injectable clock; constructors override it per instance for deterministic
    #: lock-expiry tests.
    _now: Clock = time.time

    def _refuse_unfenced(self, touched: Set[str], held: Mapping[str, Lease]) -> None:
        """Refuse a write the bound lease no longer covers.

        The shared pre-write fencing check; each backend supplies only ``held``,
        the current holds over ``touched`` as read from its store.
        """
        violation = fence_violation(self._lease, held, self._now(), set(touched))
        if violation is not None:
            raise violation

    @staticmethod
    def _minted_lease(owner: str, expires_at: float, scope: Set[str], fence: int) -> Lease:
        """The lease a successful acquisition hands back."""
        return Lease(owner=owner, expires_at=expires_at, scope=frozenset(scope), fence=fence)

    def bind_lease(self, lease: Lease | None) -> None:
        """Fence every subsequent mutation on this backend against ``lease``.

        While bound, a write is refused unless the store still records the lease
        as the holder of the node being written. This is the authoritative
        concurrency check; :class:`LeaseGuard` is only a local clock check.

        ``with_lock`` and ``held_lock`` bind on acquisition and unbind on
        release; nothing else calls this. It is backend state rather than a
        ``put``/``delete`` parameter because those are called from the executor,
        refresh and the migration helpers, not only from the lock holders.

        Unbound (``None``) writes are unfenced, as ``state restore`` and
        ``state migrate`` require: they write outside any run, under their own
        lock.

        This only records the lease. A backend with no notion of holds, or whose
        store enforces fencing another way (S3 uses conditional writes), needs no
        override.
        """
        self._lease = lease

    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        """Extend a hold ``owner`` already has, pushing its expiry out by ``ttl_seconds``.

        Acquiring is reentrant for the same owner, so the default acquires again.
        The methods are separate because an acquire marks where the run's view of
        state begins and may drop caches, while a renewal happens during a run
        and must leave that view intact. A backend that does anything on acquire
        beyond taking the lock must override this.

        A ``Failure`` means another owner took the hold after it lapsed; an
        unreachable store raises.
        """
        return self.acquire_lock(owner, ttl_seconds, scope)

    @abstractmethod
    def release_lock(self, owner: str) -> Result[None, LockError]:
        """Release every node held by ``owner``."""

    # -- lock administration ----------------------------------------------
    # A lease outlives a killed run, so holds must be inspectable and breakable.
    # Abstract: every backend implements `acquire_lock`, so it keeps this record.

    @abstractmethod
    def locks(self) -> dict[str, Lease]:
        """Every currently recorded hold, node id -> lease (expired ones included).

        Expired leases are reported, not filtered, so a caller deciding whether
        to break a lock can see that it lapsed.
        """

    @abstractmethod
    def force_unlock(self, node_ids: Set[str]) -> int:
        """Drop the holds on ``node_ids`` regardless of owner; return how many went.

        Backs ``atlantide state unlock``, for when the run that took a lease died
        without releasing it. Callers display the holder and confirm first.
        """

    # -- preflight ---------------------------------------------------------

    def check(self) -> list[Check]:
        """Verify this backend is usable and safely configured.

        Backends whose trust root is external (a bucket that requires
        versioning, a lock table that requires a specific key) override this to
        report every problem at once.
        """
        return []

    def probe(self) -> Check:
        """Actively verify the store's concurrency guarantee, by writing to it.

        Separate from :meth:`check` because it mutates (scratch space only,
        never state), so the CLI can offer to skip it.
        """
        return Check("conditional writes", SKIP, "not applicable to this backend")

    # -- committed stack outputs (keyed ``{stack}:{name}``) ----------------
    # Declared ``output()`` exports, persisted so another config's StackReference
    # can resolve them.
    #
    # Abstract because an inert default would drop outputs without error, and a
    # dependent `StackReference` would resolve to a stale or missing value. A
    # backend that cannot persist them must raise.

    @abstractmethod
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        """Merge declared stack outputs into the store (later applies win).

        ``remove`` drops keys this run no longer declares. Without it the store
        is append-only: the last value of an ``output()`` removed from config,
        or of a destroyed stack, stays committed and resolvable by a dependent
        ``StackReference``.
        """

    @abstractmethod
    def outputs(self) -> dict[str, Any]:
        """All committed stack outputs, keyed ``{stack}:{name}``."""

    def close(self) -> None:  # noqa: B027 - optional hook, intentionally non-abstract
        """Release any underlying resources (no-op by default)."""
