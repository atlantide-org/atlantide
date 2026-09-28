"""Leases: what a lock hands back, how long it lasts, and the run-side guard."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from atlantide.core.errors import LeaseLostError, LockError, StateError

__all__ = [
    "DEFAULT_LOCK_POLICY",
    "DEFAULT_SKEW_MARGIN",
    "LOCK_TTL",
    "Clock",
    "Lease",
    "LeaseGuard",
    "LockPolicy",
    "require_skew_margin",
]

#: An injectable wall-clock source (epoch seconds); overridable in tests.
type Clock = Callable[[], float]


@dataclass(frozen=True, slots=True)
class Lease:
    """A held lock over a set of node ids: owner + absolute expiry (epoch seconds).

    A lease covers only ``scope`` (the changeset's node ids plus their dependency
    closure), so applies touching disjoint subgraphs run concurrently.
    """

    owner: str
    expires_at: float
    scope: frozenset[str] = frozenset()
    #: Monotonic epoch minted at acquisition; identifies whether this lease is
    #: still the one holding its nodes. Distinct from ``serial``, a content
    #: version (see :meth:`StateBackend.serial`). ``0`` means unfenced: no fence
    #: recorded, or a write made outside any run.
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

    With renewal the TTL only has to outlive one renewal interval, not the whole
    run (whose length is unknown in advance), so a dead run's lock lapses after
    one TTL.
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

#: How far past a hold's recorded expiry it must be before another owner takes
#: it over. On S3 expiry is judged by the taker's clock, and the margin absorbs
#: that much clock skew between hosts. Postgres judges expiry by the server's
#: clock, where the margin is grace for a late renewal. Configurable as
#: ``[state].lock_skew_margin``.
DEFAULT_SKEW_MARGIN = 30.0


def require_skew_margin(margin: float) -> float:
    """``margin``, refused with :class:`StateError` when negative."""
    if margin < 0:
        raise StateError(f"[state].lock_skew_margin must not be negative, got {margin}")
    return margin


@dataclass(slots=True)
class LeaseGuard:
    """Whether the current run still holds its lease, checkable before a write.

    Two things can end a lease mid-run: another owner takes it after it lapsed,
    or the clock passes its expiry because renewal stopped. The renewal task
    detects the first and calls :meth:`fail`; :meth:`check` catches both and is
    called before every state write.

    This check is advisory: it uses the local clock and can misjudge a lease
    that expired a moment ago. The authoritative check is at the store (a
    conditional write or a fencing token). The guard needs no round trip and
    catches the common case before a write is attempted.
    """

    #: Seconds before expiry within which a write is refused.
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
        if self.lease is None:  # never acquired: nothing to guard
            return
        remaining = self.lease.expires_at - self.clock()
        if remaining <= self.grace:
            when = (
                f"expired {-remaining:.0f}s ago"
                if remaining < 0
                else f"expires in {remaining:.0f}s (within the {self.grace:.0f}s grace window)"
            )
            self.lost = LeaseLostError(
                f"the state lock {when} and could not be renewed — refusing "
                f"to write state another run may now own. Resources created before "
                f"this point exist but are not recorded; run `atlantide refresh` "
                f"before applying again"
            )
            raise self.lost
