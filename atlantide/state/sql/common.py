"""What the sqlite and postgres backends share beyond the row codec."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from atlantide.core.errors import LockError
from atlantide.state.codec import Row
from atlantide.state.leases import Lease

__all__ = ["Contended", "holds_from_rows", "lease_from_row"]


def lease_from_row(row: Row, *, expires_at: float | None = None) -> Lease:
    """One ``locks`` row (``owner``, ``expires_at``, ``fence``) as a :class:`Lease`.

    ``expires_at`` overrides the stored expiry, for a backend that reports it on
    another clock than the one it was written with.
    """
    return Lease(
        owner=row["owner"],
        expires_at=float(row["expires_at"]) if expires_at is None else expires_at,
        fence=int(row["fence"]),
    )


def holds_from_rows(
    rows: Iterable[Row], *, local: Callable[[float], float] | None = None
) -> dict[str, Lease]:
    """``locks`` rows (with ``node_id``) as the lease held over each node.

    ``local`` maps each stored expiry onto the caller's clock (see
    :func:`lease_from_row`); by default the stored expiry is kept.
    """
    return {
        row["node_id"]: lease_from_row(
            row, expires_at=None if local is None else local(float(row["expires_at"]))
        )
        for row in rows
    }


class Contended(Exception):
    """Internal: abort the lock transaction so no partial holds are committed."""

    def __init__(self, error: LockError) -> None:
        self.error = error
