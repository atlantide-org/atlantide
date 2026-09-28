"""Who may write or lock a node, and the wording of every refusal.

The judgements (:func:`fence_violation`, :func:`scope_conflict`) serve the
backends that read holds inside their own transaction. S3 decides at its heads
instead but words its refusals with the same builders, so each situation
produces the same message on every backend.
"""

from __future__ import annotations

from collections.abc import Mapping, Set

from atlantide.core.errors import FencedWriteError, LockError
from atlantide.state.leases import Lease

__all__ = [
    "fence_violation",
    "held_by_another",
    "lease_expired",
    "never_recorded",
    "outside_scope",
    "scope_conflict",
    "superseded",
]

_REFRESH = "Run `atlantide refresh` before applying again"


def outside_scope(node_id: str) -> str:
    return (
        f"refusing to write {node_id!r}: it is outside this run's lock "
        f"scope, so nothing was protecting it"
    )


def held_by_another(node_id: str, holder: str, *, detail: str = "") -> str:
    """``detail`` (e.g. ``" (fence 7 superseded 5)"``) follows "not by this run"."""
    return (
        f"refusing to write {node_id!r}: the state lock is now held by "
        f"{holder!r}, not by this run{detail}. Resources this run created "
        f"exist but are not recorded; run `atlantide refresh` before "
        f"applying again"
    )


def superseded(
    node_id: str, fence: int, newer: int, *, taker: str = "the same owner", refresh: bool = False
) -> str:
    text = (
        f"refusing to write {node_id!r}: this run's lease (fence "
        f"{fence}) was superseded by a newer one (fence "
        f"{newer}) taken by {taker}"
    )
    return f"{text}. {_REFRESH}" if refresh else text


def lease_expired(node_id: str) -> str:
    return f"refusing to write {node_id!r}: this run's lease expired. {_REFRESH}"


def never_recorded(node_id: str, fence: int, where: str, recorded: int) -> str:
    return (
        f"refusing to write {node_id!r}: this run's lease (fence "
        f"{fence}) was never recorded in {where} (its head is at "
        f"fence {recorded}), so it was never safe to write under"
    )


def fence_violation(
    lease: Lease | None, held: Mapping[str, Lease], now: float, touched: Set[str]
) -> FencedWriteError | None:
    """The error barring ``lease`` from writing ``touched``, or ``None`` if it may.

    ``held`` maps node id -> the lease currently holding it, read from the store
    inside the same transaction as the write. A write is refused when:

    * the hold belongs to another owner: another run took over;
    * the hold is this owner's but at a newer fence: this process re-acquired,
      so the earlier run's in-flight writes must not land;
    * the hold has expired;
    * the node has no hold and is outside this lease's scope: a bug rather than
      a race, and a write the lock did not protect.

    An unheld node inside the scope is allowed: a delete removes the row and the
    lock together, and a scope covering a node that does not exist is normal.
    """
    if lease is None:
        return None
    for node_id in sorted(touched):
        current = held.get(node_id)
        if current is None:
            if node_id not in lease.scope:
                return FencedWriteError(outside_scope(node_id))
            continue
        if current.owner != lease.owner:
            return FencedWriteError(held_by_another(node_id, current.owner))
        if current.fence > lease.fence:
            return FencedWriteError(superseded(node_id, lease.fence, current.fence))
        if current.expires_at <= now:
            return FencedWriteError(lease_expired(node_id))
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
