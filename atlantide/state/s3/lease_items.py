"""The lock table's lease items and lock rows: their keys, writes, and reads.

One lease item per acquisition (``{owner, fence, lease_expires_at}``), one lock
row per node pointing at it by ``lease_id``. Liveness is the lease item's
alone: a row whose lease item is gone or revoked holds nothing. Every key here
is namespaced by the state, and lease items (like the fence counter) start with
a NUL no node id holds, so neither is ever read or broken as a lock row.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from dataclasses import dataclass
from itertools import batched
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core.errors import StateError
from atlantide.state.leases import Lease
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import ddb_num, ddb_str, parallel
from atlantide.state.s3.reads import Reads
from atlantide.util.aws import error_code

__all__ = [
    "LEASE_EXPIRY",
    "LEASE_TAG",
    "NS_SEP",
    "Grant",
    "LeaseItems",
    "lease_from_item",
]

#: Tags a lease item's key in the lock table: ``\x00l\x00{namespace}\x00{lease id}``.
#: Like the fence counter and the heads, it starts with a NUL no node id holds.
LEASE_TAG = "\x00l\x00"

#: A lease item's expiry (liveness). Its ``expires_at`` is the TTL attribute,
#: set ``lock_skew_margin`` later, so DynamoDB's reaper frees nothing a taker
#: could not.
LEASE_EXPIRY = "lease_expires_at"

#: Separates the namespace from the node id in a lock-table key. Node ids start
#: with a stack identifier (``[A-Za-z][A-Za-z0-9_-]*``), which cannot hold it.
NS_SEP = "#"


def lease_from_item(item: Mapping[str, Any], *, expires_at: float | None = None) -> Lease:
    """A lease item, or a lock row with its lease's expiry, as the :class:`Lease` it grants."""
    return Lease(
        owner=item["owner"]["S"],
        expires_at=float(item[LEASE_EXPIRY]["N"]) if expires_at is None else expires_at,
        fence=int(item.get("fence", {}).get("N", 0)),
    )


@dataclass(frozen=True, slots=True)
class Grant:
    """A lease item this process created: its id, and the fence it was minted at."""

    lease_id: str
    fence: int


class LeaseItems:
    """Keys, single-item writes and consistent reads of lease items and lock rows."""

    def __init__(self, ctx: S3Context, reads: Reads) -> None:
        self._ctx = ctx
        self._reads = reads

    # -- keys --------------------------------------------------------------

    def row_name(self, node_id: str) -> str:
        return f"{self._ctx.namespace}{NS_SEP}{node_id}"

    def lease_name(self, lease_id: str) -> str:
        return f"{LEASE_TAG}{self._ctx.namespace}\x00{lease_id}"

    def row_key(self, node_id: str) -> dict[str, Any]:
        return {"node_id": ddb_str(self.row_name(node_id))}

    def lease_key(self, lease_id: str) -> dict[str, Any]:
        return {"node_id": ddb_str(self.lease_name(lease_id))}

    def reap_at(self, expires: float) -> float:
        """When DynamoDB's TTL may delete a lease item: after the skew margin.

        The reaper frees every row pointing at the lease, so it must not act
        sooner than a taker may.
        """
        return float(expires + self._ctx.skew_margin)

    # -- writes ------------------------------------------------------------

    def put_lease(self, owner: str, grant: Grant, expires: float) -> None:
        ctx = self._ctx
        try:
            ctx.clients.ddb.put_item(
                TableName=ctx.lock_table,
                Item={
                    **self.lease_key(grant.lease_id),
                    "lease_id": ddb_str(grant.lease_id),
                    "lease_ns": ddb_str(ctx.namespace),
                    "owner": ddb_str(owner),
                    "fence": ddb_num(grant.fence),
                    LEASE_EXPIRY: ddb_num(float(expires)),
                    "expires_at": ddb_num(self.reap_at(expires)),
                },
                ConditionExpression="attribute_not_exists(node_id)",
            )
        except ClientError as exc:
            # The lease id is fresh, so only this call's own earlier attempt
            # (retried by botocore after a lost response) can have written it.
            if error_code(exc) == "ConditionalCheckFailedException":
                return
            raise ctx.lock_error(exc) from exc

    def drop_grant(self, owner: str, grant: Grant, node_ids: Sequence[str]) -> None:
        """Undo a failed acquire: its lease item, then the rows it may have written."""
        self.delete_lease(grant, owner)
        parallel(lambda nid: self.release_row(nid, owner, lease_id=grant.lease_id), list(node_ids))

    def delete_lease(self, grant: Grant, owner: str) -> None:
        self.delete_owned(self.lease_key(grant.lease_id), owner)

    def release_row(self, node_id: str, owner: str, *, lease_id: str | None = None) -> None:
        self.delete_owned(self.row_key(node_id), owner, lease_id=lease_id)

    def delete_owned(self, key: dict[str, Any], owner: str, *, lease_id: str | None = None) -> None:
        """Delete a lease item or lock row only while it is still ``owner``'s.

        A row must also still point at ``lease_id``. An item no longer owned is
        left in place.
        """
        ctx = self._ctx
        condition, values = "#o = :owner", {":owner": ddb_str(owner)}
        if lease_id is not None:
            condition += " AND lease_id = :lease"
            values[":lease"] = ddb_str(lease_id)
        try:
            ctx.clients.ddb.delete_item(
                TableName=ctx.lock_table,
                Key=key,
                ConditionExpression=condition,
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues=values,
            )
        except ClientError as exc:
            # Already gone, or a row whose lease lapsed and was taken over by
            # another lease: nothing to delete.
            if error_code(exc) != "ConditionalCheckFailedException":
                raise StateError(f"release_lock failed: {exc}") from exc

    # -- reads -------------------------------------------------------------

    def read_leases(self, lease_ids: Iterable[str]) -> dict[str, dict[str, Any]]:
        """The lease items for ``lease_ids`` that exist, by id (consistent reads)."""
        keys = sorted(self.lease_name(lease_id) for lease_id in set(lease_ids))
        found: dict[str, dict[str, Any]] = {}
        for item in self.read_items(keys):
            found[item["lease_id"]["S"]] = item
        return found

    def read_rows(self, node_ids: Iterable[str]) -> list[dict[str, Any]]:
        """The lock rows recorded over ``node_ids`` (consistent reads)."""
        return self.read_items(sorted(self.row_name(nid) for nid in node_ids))

    def read_items(self, keys: Sequence[str]) -> list[dict[str, Any]]:
        return [
            item
            for batch in batched(keys, limits.BATCH_GET_MAX)
            for item in self._reads.batch_get(self._ctx.lock_table, batch)
        ]

    def holds(self, rows: Iterable[Mapping[str, Any]]) -> dict[str, Lease]:
        """Lock rows as node id -> the lease holding it, judged by its lease item.

        A row whose lease is gone or revoked is reported with the expiry ``0``:
        it holds nothing, and any run may take it.
        """
        rows = [row for row in rows if "node" in row and "owner" in row]
        leases = self.read_leases(row["lease_id"]["S"] for row in rows if "lease_id" in row)
        holds: dict[str, Lease] = {}
        for row in rows:
            lease = leases.get(row.get("lease_id", {}).get("S", ""))
            expires = 0.0
            if lease is not None and "revoked" not in lease:
                expires = float(lease[LEASE_EXPIRY]["N"])
            holds[row["node"]["S"]] = lease_from_item(row, expires_at=expires)
        return holds

    def read_holds(self, scope: Set[str]) -> dict[str, Lease]:
        """Leases currently recorded over any node id in ``scope``.

        Used to name holders and for lock administration, never to decide
        whether a write lands (the heads' fences decide that). Fails closed.
        """
        return self.holds(self.read_rows(scope))
