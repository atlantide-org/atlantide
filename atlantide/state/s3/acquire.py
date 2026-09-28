"""Taking a scope's lock rows for a new lease, and raising its heads' fences.

Rows are written in parallel transactions of up to ``TRANSACT_MAX // 2`` nodes
(a lock row and a head fence each). Each chunk is atomic but the scope is not,
so any failed chunk undoes the whole acquire. A row refused by its condition is
judged by its lease item: a lease that is gone, revoked, this owner's own, or
lapsed ``lock_skew_margin`` ago may be taken from. A lapsed lease is revoked
first, so its holder's next renewal fails even if this run releases the node
before then. A head refusing the raise means a newer lease holds the node; that
always refuses.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence, Set
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from itertools import batched
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core.errors import LockError
from atlantide.state.codec import (
    EntryKind,
)
from atlantide.state.fencing import scope_conflict
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context, backend_log
from atlantide.state.s3.dynamo import (
    Budget,
    cancellation_codes,
    ddb_num,
    ddb_str,
    failed_items,
    transact,
)
from atlantide.state.s3.journal import Layout
from atlantide.state.s3.lease_items import LEASE_EXPIRY, Grant, LeaseItems, lease_from_item
from atlantide.state.s3.reads import Reads
from atlantide.util.aws import error_code

__all__ = ["Acquirer", "ChunkOutcome", "Takeover"]


@dataclass(frozen=True, slots=True)
class ChunkOutcome:
    """How one lock chunk of an acquire ended; a raised exception is returned in its place."""

    skipped: bool = False
    refusal: LockError | None = None


@dataclass(slots=True)
class Takeover:
    """One acquire in progress, shared by its parallel chunks.

    ``dead`` holds the leases found takeable: gone, revoked, or this owner's
    own. Each stays takeable permanently (a lease id is never reused, a revoke is
    never undone, an owner never changes), so a chunk may pass another chunk's
    findings into its row conditions.
    """

    owner: str
    grant: Grant
    now: float
    #: node id -> the takeable lease its row was read pointing at.
    pointed: dict[str, str] = field(default_factory=dict)
    dead: dict[str, None] = field(default_factory=dict)
    judges: dict[str, threading.Lock] = field(default_factory=dict)
    mutex: threading.Lock = field(default_factory=threading.Lock)

    def allows(self, lease_id: str) -> bool:
        with self.mutex:
            return lease_id == self.grant.lease_id or lease_id in self.dead

    def kill(self, lease_id: str) -> None:
        """Record ``lease_id`` as takeable."""
        with self.mutex:
            self.dead[lease_id] = None

    def allow(self, node_id: str, lease_id: str) -> None:
        """Record that ``node_id``'s row points at the takeable ``lease_id``."""
        with self.mutex:
            self.pointed[node_id] = lease_id
            self.dead[lease_id] = None

    def judging(self, lease_id: str) -> threading.Lock:
        """The lock serializing the judgement of one lease across chunks."""
        with self.mutex:
            return self.judges.setdefault(lease_id, threading.Lock())

    def allowed_for(self, node_id: str) -> list[str]:
        """The lease ids ``node_id``'s row may point at for this acquire to take it."""
        with self.mutex:
            if node_id in self.pointed:
                return [self.grant.lease_id, self.pointed[node_id]]
            return [self.grant.lease_id, *list(self.dead)[-limits.SHARED_ALLOWED :]]


class Acquirer:
    """Claims a scope's lock rows for a lease item already written."""

    def __init__(self, ctx: S3Context, lease_items: LeaseItems, reads: Reads) -> None:
        self._ctx = ctx
        self._lease_items = lease_items
        self._reads = reads

    def claim(self, layout: Layout, takeover: Takeover, scope: Set[str]) -> LockError | None:
        """Take every node of ``scope`` for ``takeover``; ``None`` on success."""
        batches = list(batched(sorted(scope), max(1, limits.TRANSACT_MAX // 2)))
        return self.take_chunks(layout, takeover, batches)

    def take_chunks(
        self, layout: Layout, takeover: Takeover, batches: Sequence[Sequence[str]]
    ) -> LockError | None:
        """Take every chunk in parallel; on any failure, undo the whole acquire.

        A failing chunk stops chunks not yet started; those in flight finish.
        The lease item is deleted first, which frees every row pointing at it,
        then the rows this acquire may have written. Fences already raised stay:
        a raised fence on a free node blocks only superseded runs.
        """
        outcomes = self.run_chunks(layout, takeover, batches)
        errors = [o for o in outcomes if isinstance(o, BaseException)]
        refusals = [o.refusal for o in outcomes if isinstance(o, ChunkOutcome) and o.refusal]
        if not errors and not refusals:
            return None
        written = [
            nid
            for batch, outcome in zip(batches, outcomes, strict=True)
            if not (isinstance(outcome, ChunkOutcome) and (outcome.skipped or outcome.refusal))
            for nid in batch
        ]
        crashed = [exc for exc in errors if not isinstance(exc, Exception)]
        if crashed:
            raise crashed[0]  # process is exiting: skip the cleanup calls
        try:
            self._lease_items.drop_grant(takeover.owner, takeover.grant, written)
        except Exception as cleanup:  # report the acquire's own failure instead
            backend_log.warning("could not release a failed acquire's lock rows: %s", cleanup)
        if errors:
            raise errors[0]
        return refusals[0]

    def run_chunks(
        self, layout: Layout, takeover: Takeover, batches: Sequence[Sequence[str]]
    ) -> list[ChunkOutcome | BaseException]:
        """Each chunk's outcome, in order: the first to fail skips those not started."""
        abort = threading.Event()

        def take(batch: Sequence[str]) -> ChunkOutcome:
            if abort.is_set():
                return ChunkOutcome(skipped=True)
            try:
                refusal = self.take_chunk(layout, takeover, batch)
            except BaseException:
                abort.set()
                raise
            if refusal is not None:
                abort.set()
            return ChunkOutcome(refusal=refusal)

        if len(batches) == 1:  # run inline, without a thread pool
            return [_outcome(lambda: take(batches[0]))]
        with ThreadPoolExecutor(max_workers=min(limits.LOCK_FANOUT, len(batches))) as pool:
            futures = [pool.submit(take, batch) for batch in batches]
            return [_outcome(future.result) for future in futures]

    def take_chunk(
        self, layout: Layout, takeover: Takeover, batch: Sequence[str]
    ) -> LockError | None:
        """Take one chunk in one transaction; ``None`` on success, else the refusal.

        The first attempt expects each row free, already this lease's, or
        pointing at a lease another chunk found dead. A row refused by its
        condition is read and its lease judged (and revoked if it lapsed past
        the skew margin); the chunk is then retried with that lease allowed.
        Those retries and transient ones share one budget of attempts.
        """
        budget = Budget()
        while budget.left > 0:
            items = [item for nid in batch for item in self.claim_items(layout, nid, takeover)]
            try:
                exc = transact(self._ctx.clients.ddb, items, budget)
            except ClientError as error:
                if error_code(error) != "TransactionCanceledException":
                    raise self._ctx.lock_error(error) from error
                # The budget ran out on a transient cancellation, not a condition.
                codes = ", ".join(cancellation_codes(error)) or "no reason given"
                return LockError(
                    f"state lock contended: DynamoDB cancelled the transaction "
                    f"{limits.DDB_ATTEMPTS} times ({codes}); retry"
                )
            if exc is None:
                return None
            failed = failed_items(exc)
            heads = [batch[i // 2] for i in failed if i % 2 == 1 and i // 2 < len(batch)]
            if heads:
                return self.newer_fence(layout, heads[0], takeover.grant.fence)
            rows = [batch[i // 2] for i in failed if i % 2 == 0 and i // 2 < len(batch)]
            blocked = self.judge_holders(takeover, rows or batch, exc)
            if blocked is not None:
                return blocked
        return LockError(
            f"the state lock over {batch[0]!r} kept changing hands while it was "
            f"taken ({limits.DDB_ATTEMPTS} attempts) — retry"
        )

    def claim_items(self, layout: Layout, node_id: str, takeover: Takeover) -> list[dict[str, Any]]:
        """The transaction items claiming one node id for the new lease.

        A conditional Put of the lock row (free, or pointing at a lease this
        acquire may take from) and a raise of the head's fence to the new
        lease's, never lowering it. :meth:`take_chunk` decodes failures by this
        order: row, then head.
        """
        ctx = self._ctx
        allowed = takeover.allowed_for(node_id)
        names = {f":l{i}": ddb_str(lease_id) for i, lease_id in enumerate(allowed)}
        return [
            {
                "Put": {
                    "TableName": ctx.lock_table,
                    "Item": {
                        **self._lease_items.row_key(node_id),
                        "namespace": ddb_str(ctx.namespace),
                        "node": ddb_str(node_id),
                        "owner": ddb_str(takeover.owner),
                        "fence": ddb_num(takeover.grant.fence),
                        "lease_id": ddb_str(takeover.grant.lease_id),
                    },
                    "ConditionExpression": (
                        f"attribute_not_exists(lease_id) OR lease_id IN ({', '.join(names)})"
                    ),
                    "ExpressionAttributeValues": names,
                }
            },
            {
                "Update": {
                    "TableName": ctx.heads_table,
                    "Key": {"node_id": ddb_str(layout.head_key(EntryKind.NODE, node_id))},
                    "UpdateExpression": "SET fence = :f, state_ns = :ns",
                    "ConditionExpression": "attribute_not_exists(fence) OR fence <= :f",
                    "ExpressionAttributeValues": {
                        ":f": ddb_num(takeover.grant.fence),
                        ":ns": ddb_str(ctx.namespace),
                    },
                }
            },
        ]

    def judge_holders(
        self, takeover: Takeover, node_ids: Sequence[str], exc: ClientError
    ) -> LockError | None:
        """Judge the leases holding ``node_ids``: allow the takeable, report a live one.

        A lease is takeable when it is gone (released or reaped), revoked, this
        owner's own, or lapsed ``lock_skew_margin`` ago. A lapsed lease is
        revoked first, conditionally, so its holder's next renewal fails even if
        this run releases the node before then. Gone and revoked are permanent
        (lease ids are never reused), so revoking outside the transaction that
        takes the rows is safe.
        """
        rows = self._reads.batch_get(
            self._ctx.lock_table, [self._lease_items.row_name(nid) for nid in node_ids]
        )
        pointed = {
            item["node"]["S"]: item["lease_id"]["S"]
            for item in rows
            if "lease_id" in item and "node" in item
        }
        unknown = {lease_id for lease_id in pointed.values() if not takeover.allows(lease_id)}
        leases = self._lease_items.read_leases(unknown)
        stale = takeover.now - self._ctx.skew_margin
        live: dict[str, dict[str, Any]] = {}
        for lease_id in sorted(unknown):
            # One judgement and revoke per lease across chunks: a chunk that
            # waits here then finds the lease already allowed.
            with takeover.judging(lease_id):
                if takeover.allows(lease_id):
                    continue
                if (
                    item := self.judge_lease(lease_id, leases.get(lease_id), takeover.owner, stale)
                ) is not None:
                    live[lease_id] = item
                    continue
                takeover.kill(lease_id)
        blocking = {nid: lease_from_item(live[lid]) for nid, lid in pointed.items() if lid in live}
        for node_id, lease_id in pointed.items():
            if lease_id not in live:
                takeover.allow(node_id, lease_id)
        if blocking:
            error = scope_conflict(blocking, takeover.owner, stale, set(blocking))
            return error or LockError(f"state lock contended by another run ({exc})")
        return None

    def judge_lease(
        self, lease_id: str, item: dict[str, Any] | None, owner: str, stale: float
    ) -> dict[str, Any] | None:
        """``None`` if the lease may be taken from (revoking it if lapsed), else its live item."""
        if item is None or "revoked" in item or item["owner"]["S"] == owner:
            return None
        if float(item[LEASE_EXPIRY]["N"]) >= stale:
            return item
        return self.revoke(lease_id, owner, stale)

    def revoke(self, lease_id: str, owner: str, stale: float) -> dict[str, Any] | None:
        """Mark a lapsed lease revoked; ``None`` once it is dead, else its live item."""
        ctx = self._ctx
        try:
            ctx.clients.ddb.update_item(
                TableName=ctx.lock_table,
                Key=self._lease_items.lease_key(lease_id),
                UpdateExpression="SET revoked = :t",
                ConditionExpression=(
                    "attribute_exists(#o) AND (#o = :owner OR attribute_exists(revoked) "
                    "OR #l < :stale)"
                ),
                ExpressionAttributeNames={"#o": "owner", "#l": LEASE_EXPIRY},
                ExpressionAttributeValues={
                    ":t": {"BOOL": True},
                    ":owner": ddb_str(owner),
                    ":stale": ddb_num(float(stale)),
                },
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as exc:
            if error_code(exc) != "ConditionalCheckFailedException":
                raise ctx.lock_error(exc) from exc
            old: Any = exc.response.get("Item")
            # No item: the lease is gone. Otherwise it was renewed concurrently.
            return dict(old) if old is not None else None
        return None

    def newer_fence(self, layout: Layout, node_id: str, fence: int) -> LockError:
        recorded = self._reads.get_head(layout.head_key(EntryKind.NODE, node_id)).fence
        return LockError(
            f"node {node_id!r} was granted to a newer lease (fence {recorded}) than "
            f"this one (fence {fence}) — another run took it over"
        )


def _outcome(result: Callable[[], ChunkOutcome]) -> ChunkOutcome | BaseException:
    """``result()``, or the exception it raised, returned so cleanup can run first."""
    try:
        return result()
    except BaseException as exc:
        return exc
