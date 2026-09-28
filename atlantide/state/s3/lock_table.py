"""This process's leases: acquire, renew, release, and lock administration.

One lease item per acquisition, one lock row per node pointing at it
(:mod:`~atlantide.state.s3.lease_items`). Liveness lives on the lease item, so
a renewal is one ``UpdateItem`` whatever the scope; an acquire mints a fence
(:mod:`~atlantide.state.s3.fences`), writes the lease item, then claims every
row and raises every head's fence (:mod:`~atlantide.state.s3.acquire`). A
takeover revokes the lease it takes from, so a renewal of a lease that was
taken over fails even once the taker has released the node again.
"""

from __future__ import annotations

import uuid
from collections.abc import Set
from typing import Any

from botocore.exceptions import ClientError
from returns.result import Failure, Result, Success

from atlantide.core.errors import LockError
from atlantide.state.leases import Lease
from atlantide.state.s3.acquire import Acquirer, Takeover
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import ddb_num, ddb_str, parallel
from atlantide.state.s3.fences import Fences
from atlantide.state.s3.lease_items import LEASE_EXPIRY, Grant, LeaseItems
from atlantide.state.s3.reads import Reads
from atlantide.state.s3.snapshots import Snapshots
from atlantide.state.s3.view import ViewCache
from atlantide.util.aws import error_code

__all__ = ["LockTable"]


def _extend_request(
    table: str, key: dict[str, Any], *, owner: str, fence: int, expires: float, reap_at: float
) -> dict[str, Any]:
    """The ``UpdateItem`` arguments extending a lease item's expiry and TTL.

    The update applies only while the item is ``owner``'s, at ``fence``, and not revoked.
    """
    return {
        "TableName": table,
        "Key": key,
        "UpdateExpression": "SET #l = :e, expires_at = :reap",
        "ConditionExpression": "#o = :owner AND fence = :f AND attribute_not_exists(revoked)",
        "ExpressionAttributeNames": {"#o": "owner", "#l": LEASE_EXPIRY},
        "ExpressionAttributeValues": {
            ":owner": ddb_str(owner),
            ":f": ddb_num(fence),
            ":e": ddb_num(float(expires)),
            ":reap": ddb_num(reap_at),
        },
    }


def _lost_lease_error(grant: Grant, old: Any) -> LockError:
    """Why a renewal of ``grant`` was refused, given its lease item ``old`` (``None`` if gone)."""
    if old is not None and "revoked" in old:
        return LockError(
            f"this run's lease (fence {grant.fence}) was revoked: a node in "
            f"it was granted to a newer lease after it lapsed — another run "
            f"took it over"
        )
    return LockError(
        f"this run's lease (fence {grant.fence}) no longer exists: it was "
        f"released, or reaped after it lapsed, and its nodes may since "
        f"have been granted to a newer lease"
    )


class LockTable:
    """Acquire, renew, release and administer this state's leases."""

    def __init__(
        self,
        ctx: S3Context,
        *,
        snapshots: Snapshots,
        views: ViewCache,
        reads: Reads,
        lease_items: LeaseItems,
        fences: Fences,
    ) -> None:
        self._ctx = ctx
        self._snapshots = snapshots
        self._views = views
        self._lease_items = lease_items
        self._fences = fences
        self._acquirer = Acquirer(ctx, lease_items, reads)
        #: owner -> node ids this process locked, so release targets exactly them.
        self.held: dict[str, set[str]] = {}
        #: owner -> the lease items this process created for it, newest last.
        self.grants: dict[str, list[Grant]] = {}

    # -- acquire and renew -------------------------------------------------

    def acquire(self, owner: str, ttl_seconds: float, scope: Set[str]) -> Result[Lease, LockError]:
        return self.take_scope(owner, ttl_seconds, scope, renewing=False)

    def renew(self, owner: str, ttl_seconds: float, scope: Set[str]) -> Result[Lease, LockError]:
        """Push the lease's expiry out, keeping the bound lease's fence.

        The renewal of the lease this backend is bound to (and minted) is one
        conditional ``UpdateItem`` on its lease item: still this owner's, still
        at this fence, and never revoked. Every takeover revokes the lease it
        takes a node from before taking it, so a hold that lapsed, was taken and
        was released again between two heartbeats still fails here. A renewal
        of a lease that lapsed without a takeover succeeds: no other lease was
        granted its nodes.

        Anything else (an unbound backend, a scope the bound lease does not
        cover) is an acquire: a new lease and a new fence.
        """
        grant = self.renewable(owner, scope)
        if grant is None:
            return self.take_scope(owner, ttl_seconds, scope, renewing=True)
        expires = self._ctx.clock() + ttl_seconds
        return self.extend(owner, grant, expires, scope)

    def renewable(self, owner: str, scope: Set[str]) -> Grant | None:
        """The lease item a renewal of ``scope`` extends, if it is the bound one."""
        bound = self._ctx.bound_lease()
        grants = self.grants.get(owner)
        if bound is None or not grants or not scope:
            return None
        grant = grants[-1]
        if bound.owner != owner or bound.fence != grant.fence or not scope <= bound.scope:
            return None
        return grant

    def extend(
        self, owner: str, grant: Grant, expires: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        ctx = self._ctx
        try:
            ctx.clients.ddb.update_item(
                **_extend_request(
                    ctx.lock_table,
                    self._lease_items.lease_key(grant.lease_id),
                    owner=owner,
                    fence=grant.fence,
                    expires=expires,
                    reap_at=self._lease_items.reap_at(expires),
                ),
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as exc:
            if error_code(exc) != "ConditionalCheckFailedException":
                raise ctx.lock_error(exc) from exc
            return Failure(_lost_lease_error(grant, exc.response.get("Item")))
        return Success(
            Lease(owner=owner, expires_at=expires, scope=frozenset(scope), fence=grant.fence)
        )

    def take_scope(
        self, owner: str, ttl_seconds: float, scope: Set[str], *, renewing: bool
    ) -> Result[Lease, LockError]:
        """A new lease over ``scope``: mint a fence, write the lease item, claim the rows."""
        ctx = self._ctx
        now = ctx.clock()
        expires = now + ttl_seconds
        if not scope:
            return Success(Lease(owner=owner, expires_at=expires))
        snap = self._snapshots.ensure()
        layout = ctx.layout(snap.doc.epoch)
        fence = self._fences.next_fence(above=snap.doc.max_fence)
        grant = Grant(uuid.uuid4().hex, fence)
        self._lease_items.put_lease(owner, grant, expires)
        takeover = Takeover(owner=owner, grant=grant, now=now)
        refusal = self._acquirer.claim(layout, takeover, scope)
        if refusal is not None:
            return Failure(refusal)
        self.grants.setdefault(owner, []).append(grant)
        self.held.setdefault(owner, set()).update(scope)
        if not renewing:
            # An acquire starts this run's view of state: drop the cache so the
            # next use re-reads every prior commit. No write is in flight here
            # because the lock scaffold runs alone.
            with ctx.mutex:
                self._views.drop()
                self._views.unseen.clear()
        return Success(Lease(owner=owner, expires_at=expires, scope=frozenset(scope), fence=fence))

    # -- release -----------------------------------------------------------

    def release(self, owner: str) -> Result[None, LockError]:
        """Delete this owner's lease items, then its lock rows.

        Deleting the lease items first frees every row pointing at them, so a
        crash part-way leaves only rows another run may take. The rows are
        deleted in parallel, each only while it is still this owner's.

        Every delete is attempted even when one fails; the first failure is then
        raised, and what failed is kept so a second release retries it.
        """
        grants = self.grants.pop(owner, [])
        node_ids = self.held.pop(owner, set())
        errors: list[Exception] = []
        kept: list[Grant] = []
        for grant in grants:
            try:
                self._lease_items.delete_lease(grant, owner)
            except Exception as exc:
                errors.append(exc)
                kept.append(grant)

        def release_row(node_id: str) -> Exception | None:
            try:
                self._lease_items.release_row(node_id, owner)
            except Exception as exc:
                return exc
            return None

        ordered = sorted(node_ids)
        failed = parallel(release_row, ordered)
        errors += [exc for exc in failed if exc is not None]
        if not errors:
            return Success(None)
        if kept:
            self.grants.setdefault(owner, [])[:0] = kept
        unreleased = {nid for nid, exc in zip(ordered, failed, strict=True) if exc is not None}
        if unreleased:
            self.held.setdefault(owner, set()).update(unreleased)
        raise errors[0]

    # -- administration ----------------------------------------------------

    def locks(self) -> dict[str, Lease]:
        """Every hold recorded for this state, i.e. in this state's namespace.

        The table may be shared between projects; rows of other namespaces are
        not this state's to report. Heads (``state_ns``) and lease items
        (``lease_ns``) never carry ``namespace``, so they are never listed or
        broken as locks. A row's expiry is its lease item's (``0`` once that is
        gone or revoked).
        """
        ctx = self._ctx
        rows: list[dict[str, Any]] = []
        paginator = ctx.clients.ddb.get_paginator("scan")
        pages = paginator.paginate(
            TableName=ctx.lock_table,
            ConsistentRead=True,
            FilterExpression="#ns = :ns",
            ExpressionAttributeNames={"#ns": "namespace"},
            ExpressionAttributeValues={":ns": ddb_str(ctx.namespace)},
        )
        try:
            for page in pages:
                rows.extend(page.get("Items", []))
        except ClientError as exc:
            raise ctx.lock_error(exc) from exc
        return self._lease_items.holds(rows)

    def force_unlock(self, node_ids: Set[str]) -> int:
        """Break this namespace's holds on ``node_ids``; heads and lease items stay.

        The lease item may still hold nodes that were not broken. The broken run
        keeps renewing; if another run then takes a broken node, that run raises
        the node's fence and the broken run's writes to it are refused.
        """
        ctx = self._ctx

        def unlock(node_id: str) -> int:
            # ALL_OLD so the count is holds broken, not delete calls made.
            try:
                response = ctx.clients.ddb.delete_item(
                    TableName=ctx.lock_table,
                    Key=self._lease_items.row_key(node_id),
                    ReturnValues="ALL_OLD",
                )
            except ClientError as exc:
                raise ctx.lock_error(exc) from exc
            return 1 if response.get("Attributes") else 0

        return sum(parallel(unlock, sorted(node_ids)))
