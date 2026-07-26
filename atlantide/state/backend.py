"""State model and the storage-agnostic backend interface.

The engine talks only to :class:`StateBackend`; :class:`StateGraph` and
:class:`StateNode` are storage-independent value types.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Set
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from returns.result import Result

from atlantide.core.check import SKIP, Check
from atlantide.core.errors import FencedWriteError, LeaseLostError, LockError

#: An injectable wall-clock source (epoch seconds); overridable in tests.
Clock = Callable[[], float]

#: A node fully created and confirmed (outputs recorded).
STATUS_CREATED = "created"
#: A write-ahead row: a create was started but not confirmed. Re-created on the
#: next plan, and reclaimable by destroy/refresh even if the create leaked.
STATUS_CREATING = "creating"

#: ``input_hash`` for a node whose live inputs have drifted from config. No sha256
#: digest equals it, so the diff's Merkle skip cannot fire and the node is
#: re-planned. Written by ``refresh --write``, the only channel from a provider
#: read to the next plan — a symbolic diff cannot see drift.
NO_INPUT_HASH = ""


@dataclass(frozen=True, slots=True)
class StateNode:
    """A single persisted resource: desired inputs' hash + realised outputs."""

    id: str
    type: str
    provider: str
    provider_version: str
    input_hash: str
    outputs: dict[str, Any] = field(default_factory=dict)
    properties: dict[str, Any] = field(default_factory=dict)
    dependencies: tuple[str, ...] = ()
    prevent_destroy: bool = False
    status: str = "created"
    #: field name -> hex digest of the last-resolved secret value, for rotation
    #: detection. ``properties`` carries only the ``{"$secret_ref": ...}`` handle;
    #: the value itself is never stored.
    secret_digests: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class StateGraph:
    """The committed state as an id-keyed set of nodes."""

    nodes: dict[str, StateNode] = field(default_factory=dict)

    def get(self, node_id: str) -> StateNode | None:
        return self.nodes.get(node_id)

    def __contains__(self, node_id: str) -> bool:
        return node_id in self.nodes

    def __len__(self) -> int:
        return len(self.nodes)


@dataclass(frozen=True, slots=True)
class Lease:
    """A held lock over a set of node ids: owner + absolute expiry (epoch seconds).

    A lease covers only ``scope`` (the changeset's node ids plus their dependency
    closure), so applies touching disjoint subgraphs run concurrently.
    """

    owner: str
    expires_at: float
    scope: frozenset[str] = frozenset()
    #: Monotonic epoch minted when this lease was taken, answering "is my lease
    #: still the one holding these nodes". Distinct from ``serial``, which is a
    #: content version (see :meth:`StateBackend.serial`). ``0`` means unfenced: a
    #: store predating fencing, or a write made outside any run.
    fence: int = 0

    def blocks(self, owner: str, now: float) -> bool:
        """True if this lease bars ``owner`` from taking a node right now."""
        return self.owner != owner and self.expires_at > now


#: Default lease time-to-live, in seconds. A live run renews, so this bounds how
#: long a *dead* run blocks others; it need not cover the run's duration.
LOCK_TTL = 300.0


@dataclass(frozen=True, slots=True)
class LockPolicy:
    """How long a lease lasts and how often it is pushed out.

    A lease taken once and never renewed has to outlive the whole run, which is
    unknowable in advance: a single CloudFront distribution can take half an hour
    to settle, and a TTL long enough to cover that leaves a *dead* run's lock in
    place for the same half hour. Renewal decouples the two — the TTL then only
    has to outlive one renewal interval.
    """

    #: Lease duration requested from the backend.
    ttl: float = LOCK_TTL
    #: How often to push the expiry out. Well under the TTL, so a single slow or
    #: failed renewal is survivable.
    renew_interval: float = LOCK_TTL / 3
    #: Refuse a state write this close to expiry, covering the window between a
    #: renewal failing and the run being told about it.
    renew_grace: float = 30.0

    def validate(self) -> None:
        """Reject a policy that cannot keep a lease alive."""
        if self.ttl <= 0:
            raise LockError(f"[state].lock_ttl must be positive, got {self.ttl}")
        if self.renew_interval <= 0:
            raise LockError(
                f"[state].lock_renew_interval must be positive, got {self.renew_interval}"
            )
        if self.renew_interval >= self.ttl:
            raise LockError(
                f"[state].lock_renew_interval ({self.renew_interval}s) must be shorter "
                f"than lock_ttl ({self.ttl}s) — otherwise the lease expires before it "
                f"is ever renewed"
            )
        if self.renew_grace >= self.ttl:
            raise LockError(
                f"lock renew grace ({self.renew_grace}s) must be shorter than "
                f"[state].lock_ttl ({self.ttl}s) — otherwise every write is refused as "
                f"too close to expiry"
            )


DEFAULT_LOCK_POLICY = LockPolicy()


@dataclass(slots=True)
class LeaseGuard:
    """Whether the current run still holds its lease, checkable before a write.

    Two things can end a lease mid-run: another owner takes it after it lapsed,
    or the clock simply passes its expiry because renewal stopped. The renewal
    task learns about the first and calls :meth:`fail`; :meth:`check` catches
    both, and is called before every state write.

    This is the *advisory* half of the guarantee — it is a local clock check, so
    it can be wrong about a lease that expired a moment ago. The authoritative
    half belongs at the store (a conditional write, or a fencing token). Both
    exist because they fail differently: the guard is free and catches the common
    case early, before a write is attempted at all.
    """

    #: Refuse a write this close to expiry, rather than racing the last moment.
    grace: float = 30.0
    clock: Clock = time.time
    lease: Lease | None = None
    lost: LeaseLostError | None = None

    def renewed(self, lease: Lease) -> None:
        """Record a freshly acquired or renewed lease."""
        self.lease = lease

    def fail(self, error: LeaseLostError) -> None:
        """Record that the lease is gone; every later :meth:`check` raises."""
        self.lost = error

    def check(self) -> None:
        """Raise :class:`LeaseLostError` if the lease is gone or about to lapse."""
        if self.lost is not None:
            raise self.lost
        if self.lease is None:  # never acquired: nothing claimed, nothing to guard
            return
        remaining = self.lease.expires_at - self.clock()
        if remaining <= self.grace:
            self.lost = LeaseLostError(
                f"the state lock expired {-remaining:.0f}s ago (or is within "
                f"{self.grace:.0f}s of doing so) and could not be renewed — refusing "
                f"to write state another run may now own. Resources created before "
                f"this point exist but are not recorded; run `atlantide refresh` "
                f"before applying again"
            )
            raise self.lost


def close_quietly(backend: Any) -> None:
    """Best-effort ``_conn`` close for ``__del__`` finalizers.

    A finalizer runs on whichever thread the collector happens to be on, and
    some drivers (sqlite) refuse cross-thread closes with a raised error. The
    process is ending or the object is unreachable either way, so the failure
    is noise standing in for nothing — the connection is released regardless.
    """
    conn = getattr(backend, "_conn", None)
    if conn is not None:
        with suppress(Exception):
            conn.close()


def merge_outputs(
    current: Mapping[str, Any], outputs: Mapping[str, Any], remove: Iterable[str]
) -> dict[str, Any]:
    """The committed-outputs merge every backend performs.

    Drop ``remove``, overlay ``outputs``; written once so the four backends
    cannot drift on the semantics (removal before overlay, overlay wins).
    """
    dropped = set(remove)
    kept = {key: value for key, value in current.items() if key not in dropped}
    return {**kept, **outputs}


def fence_violation(
    lease: Lease | None, held: Mapping[str, Lease], now: float, touched: Set[str]
) -> FencedWriteError | None:
    """The error barring ``lease`` from writing ``touched``, or ``None`` if it may.

    ``held`` maps node id -> the lease currently holding it, read from the store
    inside the same transaction as the write. Three ways to fail, and each is a
    genuinely different situation worth naming separately:

    * the hold is gone or belongs to someone else — another run took over;
    * the hold is this owner's but at an older fence — *this process* re-acquired,
      so the earlier run's in-flight writes must not land;
    * the node was never in this lease's scope — a bug rather than a race, but a
      write outside the scope is exactly what the lock failed to protect.

    Nodes with no hold at all are allowed: a delete removes the row and the lock
    together, and a scope covering a node that never existed is normal.
    """
    if lease is None:
        return None
    for node_id in sorted(touched):
        current = held.get(node_id)
        if current is None:
            if node_id not in lease.scope:
                return FencedWriteError(
                    f"refusing to write {node_id!r}: it is outside this run's lock "
                    f"scope, so nothing was protecting it"
                )
            continue
        if current.owner != lease.owner:
            return FencedWriteError(
                f"refusing to write {node_id!r}: the state lock is now held by "
                f"{current.owner!r}, not by this run. Resources this run created "
                f"exist but are not recorded; run `atlantide refresh` before "
                f"applying again"
            )
        if current.fence > lease.fence:
            return FencedWriteError(
                f"refusing to write {node_id!r}: this run's lease (fence "
                f"{lease.fence}) was superseded by a newer one (fence "
                f"{current.fence}) taken by the same owner"
            )
        if current.expires_at <= now:
            return FencedWriteError(
                f"refusing to write {node_id!r}: this run's lease expired. "
                f"Run `atlantide refresh` before applying again"
            )
    return None


def scope_conflict(
    held: Mapping[str, Lease], owner: str, now: float, scope: Set[str]
) -> LockError | None:
    """The error barring ``owner`` from locking ``scope``, or ``None`` if it may.

    ``held`` maps an already-locked node id to the lease holding it. A conflict is
    the first requested node held by a *different*, unexpired owner.
    """
    for node_id in sorted(scope):
        current = held.get(node_id)
        if current is not None and current.blocks(owner, now):
            return LockError(
                f"node {node_id!r} is locked by {current.owner!r} until {current.expires_at}"
            )
    return None


class StateBackend(ABC):
    """Storage-agnostic state store. Mutations bump ``serial`` (optimistic token)."""

    @abstractmethod
    def load(self) -> StateGraph:
        """Return the full committed state graph."""

    @abstractmethod
    def put(self, node: StateNode) -> None:
        """Upsert one node (incremental, crash-safe persist)."""

    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert several nodes as one unit where the backend can.

        The default is a loop over :meth:`put`, which is correct everywhere but
        leaves a partial write behind if it is interrupted. Backends whose store
        has transactions (or that rewrite a whole document) override this so a
        bulk write — a migration, an alias rekey, a rollback — either lands
        completely or not at all, and costs one round trip instead of N.
        """
        for node in nodes:
            self.put(node)

    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Delete and upsert as one unit where the backend can.

        The alias rekey is a move: the same resource under a new id. Committed
        deletes followed by a separate bulk write leave a window where state holds
        neither id, and the alias no longer matches anything, so a re-run cannot
        recover. The default is a delete loop then :meth:`put_many`; backends with
        transactions override it.
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

        A backend may leave it alone for a write that changes nothing — an upsert
        of a node already stored verbatim — so compare serials for *difference*,
        never treat one as a count of calls.
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

    #: The lease writes are fenced against, or ``None`` when unbound. Declared
    #: here rather than per backend so :meth:`bind_lease` has one implementation;
    #: a backend that fences another way simply never reads it.
    _lease: Lease | None = None

    #: Injectable clock (same class-level pattern as ``_lease``): constructors
    #: overwrite it per instance so lock-expiry tests are deterministic.
    _now: Clock = time.time

    def _refuse_unfenced(self, touched: Set[str], held: Mapping[str, Lease]) -> None:
        """Refuse a write the bound lease no longer covers.

        The shared half of every backend's pre-write fencing check; each backend
        supplies only ``held`` — how the current holds over ``touched`` are read
        (which is the only part that genuinely differs between stores).
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

        While bound, a write is refused unless the lease is still, at the store,
        the holder of the node being written. That is the *authoritative* half of
        the concurrency guarantee — :class:`LeaseGuard` is a local clock check and
        can be wrong; this cannot, because the store decides.

        Binding is deliberately not a parameter on ``put``/``delete``: those are
        called from the executor, from refresh, and from the migration helpers,
        and threading a token through all of them would put the burden on every
        caller rather than on the two places that take a lock. ``with_lock`` and
        ``held_lock`` bind on acquisition and unbind on release; nothing else
        should call this.

        Unbound (``None``) writes are unfenced, which is correct for the
        administrative commands — ``state restore`` and ``state migrate``
        legitimately write outside any run, under their own lock.

        Recording the lease is all this does; whether a write consults it is up to
        the backend, so one with no notion of holds (or one whose store enforces
        this another way, as S3 does with a conditional write) needs no override.
        """
        self._lease = lease

    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        """Extend a hold ``owner`` already has, pushing its expiry out by ``ttl_seconds``.

        Acquiring is reentrant for the same owner, so the default is simply to
        acquire again. It is a separate method because the two differ in what
        else they may do: an acquire marks the boundary where this run's view of
        state begins and may drop caches accordingly, while a renewal happens
        *during* a run and must leave that view alone. A backend that does
        anything on acquire beyond taking the lock has to override this.

        A ``Failure`` means the hold is gone — another owner took it after it
        lapsed — not that the store is unreachable, which raises.
        """
        return self.acquire_lock(owner, ttl_seconds, scope)

    @abstractmethod
    def release_lock(self, owner: str) -> Result[None, LockError]:
        """Release every node held by ``owner``."""

    # -- lock administration ----------------------------------------------
    # A lease outlives a killed run, so operators need to inspect and break holds.
    # Abstract because a backend that takes locks at all can report and break them:
    # `acquire_lock` is abstract, so every implementer already keeps this record.

    @abstractmethod
    def locks(self) -> dict[str, Lease]:
        """Every currently recorded hold, node id -> lease (expired ones included).

        Expired leases are reported rather than filtered: an operator deciding
        whether to break a lock needs to see that it has already lapsed.
        """

    @abstractmethod
    def force_unlock(self, node_ids: Set[str]) -> int:
        """Drop the holds on ``node_ids`` regardless of owner; return how many went.

        Backs ``atlantide state unlock``, for when the run that took a lease died
        without releasing it. Callers are expected to display the holder and
        confirm before calling.
        """

    # -- preflight ---------------------------------------------------------

    def check(self) -> list[Check]:
        """Verify this backend is usable and safely configured.

        Backends whose trust root is external — a bucket that must have
        versioning, a lock table that must have the right key — override this to
        report every problem at once instead of one failed call at a time.
        """
        return []

    def probe(self) -> Check:
        """Actively verify the store's concurrency guarantee, by writing to it.

        Separate from :meth:`check` because it is the one preflight that mutates
        (to scratch space, never to state), so the CLI can offer to skip it.
        """
        return Check("conditional writes", SKIP, "not applicable to this backend")

    # -- committed stack outputs (keyed ``{stack}:{name}``) ----------------
    # Declared ``output()`` exports, persisted so another config's StackReference
    # can resolve them.
    #
    # Abstract, because the only alternative — an inert default — is a store that
    # accepts outputs and forgets them. Nothing fails at the time: the apply that
    # declared them succeeds, and the loss surfaces later and elsewhere, as a
    # dependent stack resolving a `StackReference` to a stale value (or to
    # nothing) with no error anywhere to connect it back to the backend. A
    # backend that genuinely cannot persist them should say so by raising.

    @abstractmethod
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        """Merge declared stack outputs into the store (later applies win).

        ``remove`` drops keys this run no longer declares, without which the store
        is append-only: an ``output()`` deleted from config, or a whole stack
        destroyed, leaves its last value committed and a dependent stack's
        ``StackReference`` still resolves to it.
        """

    @abstractmethod
    def outputs(self) -> dict[str, Any]:
        """All committed stack outputs, keyed ``{stack}:{name}``."""

    def close(self) -> None:  # noqa: B027 - optional hook, intentionally non-abstract
        """Release any underlying resources (no-op by default)."""
