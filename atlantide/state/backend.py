"""State model and the storage-agnostic backend interface.

The engine talks only to :class:`StateBackend`; :class:`StateGraph` and
:class:`StateNode` are storage-independent value types.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass, field
from typing import Any

from returns.result import Result

from atlantide.core.check import SKIP, Check
from atlantide.core.errors import LockError

#: An injectable wall-clock source (epoch seconds); overridable in tests.
Clock = Callable[[], float]

#: A node fully created and confirmed (outputs recorded).
STATUS_CREATED = "created"
#: A write-ahead row: a create was started but not confirmed. Re-created on the
#: next plan; reclaimable by destroy/refresh even if the create leaked or was
#: cancelled before its state row was finalised.
STATUS_CREATING = "creating"


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
    #: field name -> hex digest of the last-resolved secret value (rotation
    #: detection). ``properties`` carries only the ``{"$secret_ref": ...}`` handle;
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

    def blocks(self, owner: str, now: float) -> bool:
        """True if this lease bars ``owner`` from taking a node right now."""
        return self.owner != owner and self.expires_at > now


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

    @abstractmethod
    def release_lock(self, owner: str) -> Result[None, LockError]:
        """Release every node held by ``owner``."""

    # -- lock administration ----------------------------------------------
    # A lease outlives the run that took it if that run is killed, so operators
    # need to inspect and break holds. The defaults report no holds, which is
    # accurate for a backend that records none.

    def locks(self) -> dict[str, Lease]:
        """Every currently recorded hold, node id -> lease (expired ones included).

        Expired leases are reported rather than filtered: an operator deciding
        whether to break a lock needs to see that it has already lapsed.
        """
        return {}

    def force_unlock(self, node_ids: Set[str]) -> int:
        """Drop the holds on ``node_ids`` regardless of owner; return how many went.

        Backs ``atlantide state unlock``, for when the run that took a lease died
        without releasing it. Callers are expected to display the holder and
        confirm before calling.
        """
        return 0

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
    # Declared ``output()`` exports, persisted so a StackReference in another
    # config can resolve them. The defaults are inert; storage backends override.

    def set_outputs(self, outputs: Mapping[str, Any]) -> None:  # noqa: B027
        """Merge declared stack outputs into the store (later applies win)."""

    def outputs(self) -> dict[str, Any]:
        """All committed stack outputs, keyed ``{stack}:{name}``."""
        return {}

    def close(self) -> None:  # noqa: B027 - optional hook, intentionally non-abstract
        """Release any underlying resources (no-op by default)."""
