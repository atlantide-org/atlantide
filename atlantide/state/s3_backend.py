"""Remote state backend: one S3 object for the graph, DynamoDB rows for leases.

The whole state is a single canonical JSON blob (see
:mod:`atlantide.state.codec`). Every mutation rewrites the object under a
compare-and-swap on its ETag, so a write from a run whose view of state is stale
is rejected rather than silently clobbering another run's work. Writes are
write-through — one ``PutObject`` per node — matching the sqlite backend's
crash-safety: a killed apply leaves ``status="creating"`` rows the next run
reclaims.

Write-through means an apply costs one ``PutObject`` per node, each carrying the
whole graph. Buffering would give up the crash-safety, so the cost is reduced
three ways instead: a write whose node is already stored verbatim is skipped,
:meth:`put_many` collapses a bulk write (migration, alias rekey, rollback) into
one request, and a large document is stored gzipped (see
:data:`~atlantide.state.codec.COMPRESS_OVER`).

Locking is per-node, like every other backend: one DynamoDB item per locked node
id holding ``owner`` + ``expires_at``, taken with conditional writes so runs over
disjoint subgraphs proceed concurrently across machines.

Neither the bucket nor the lock table is auto-created; they are the trust root
for shared state and are expected to exist (versioning and encryption enabled)
before atlantide is pointed at them.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence, Set
from dataclasses import dataclass, replace
from typing import Any

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError
from returns.result import Failure, Result, Success
from typing_extensions import override

from atlantide.core.check import Check
from atlantide.core.errors import LockError, StateError
from atlantide.core.tuning import CONNECT_TIMEOUT, MAX_ATTEMPTS, READ_TIMEOUT, RETRY_MODE
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
    merge_outputs,
    scope_conflict,
)
from atlantide.state.codec import StateDocument, decode, encode
from atlantide.state.s3_preflight import run_checks, run_probe

#: Bounded, retried clients. Without a read timeout a hung state call hangs the
#: whole run while holding its lease.
_CLIENT_CONFIG = BotoConfig(
    connect_timeout=CONNECT_TIMEOUT,
    read_timeout=READ_TIMEOUT,
    retries={"max_attempts": MAX_ATTEMPTS, "mode": RETRY_MODE},
)

#: Reserved key in the lock table holding the monotonic fence counter. Node ids
#: are ``{stack}:{type}:{name}`` with no empty part, so none can collide with it.
_FENCE_ITEM = "\x00atlantide-fence"

#: DynamoDB caps a transaction at 100 items; larger scopes are locked in chunks.
_TRANSACT_MAX = 100

#: Error codes S3 returns when a conditional write loses the race.
_CAS_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})

#: How many times a mutation is rebased onto a freshly-read document after a
#: lost compare-and-swap before giving up.
_CAS_ATTEMPTS = 5

#: Bounded retries for DynamoDB calls that can partially fail transiently.
_DDB_ATTEMPTS = 5


class _CasConflict(Exception):
    """Internal: the object changed between our read and our conditional write."""

    def __init__(self, error: ClientError) -> None:
        super().__init__(str(error))
        self.error = error


def _contended(exc: ClientError) -> bool:
    """Whether a cancelled lock transaction was refused by the lock *condition*.

    DynamoDB cancels transactions for transient reasons too (item-level
    conflicts, throttling); treating those as "another run holds the lease"
    would cancel a healthy run on a hiccup.
    """
    reasons = exc.response.get("CancellationReasons") or []
    if not reasons:
        return True  # older/unknown response shape: assume contention
    return any(reason.get("Code") == "ConditionalCheckFailed" for reason in reasons)


#: Take one node: unheld, held by the same owner, or expired — the same rule
#: :func:`scope_conflict` applies in the other backends.
_LOCK_CONDITION = "attribute_not_exists(node_id) OR #o = :owner OR expires_at < :now"


def _code(exc: ClientError) -> str:
    """The AWS error code, e.g. ``NoSuchKey`` (``""`` if the shape is unexpected)."""
    return str(exc.response.get("Error", {}).get("Code", ""))


def _lease_of_item(item: Mapping[str, Any]) -> Lease | None:
    """One DynamoDB lock-table item as a Lease; ``None`` for the fence counter."""
    if item["node_id"]["S"] == _FENCE_ITEM:
        return None
    return Lease(
        owner=item["owner"]["S"],
        expires_at=float(item["expires_at"]["N"]),
        fence=int(item.get("fence", {}).get("N", 0)),
    )


@dataclass(frozen=True, slots=True)
class _Snapshot:
    """A document and the ETag it was read or written at.

    The two are only ever meaningful together: an ETag names the version of the
    object the document came from, and writing under a precondition taken from
    one version while holding the bytes of another is exactly the lost update the
    compare-and-swap exists to prevent. Keeping them in one value makes an
    invalidation a single assignment rather than a pair that could be half-done.

    ``etag`` is ``None`` when there is no object yet — a first run against this
    key — which is what makes the write's precondition ``IfNoneMatch``.
    """

    doc: StateDocument
    etag: str | None


class S3StateBackend(StateBackend):
    """State in an S3 object, leases in a DynamoDB table."""

    def __init__(
        self,
        bucket: str,
        key: str,
        *,
        lock_table: str,
        region: str | None = None,
        profile: str | None = None,
        endpoint_url: str | None = None,
        kms_key_id: str | None = None,
        clock: Clock = time.time,
    ) -> None:
        self._bucket = bucket
        self._key = key
        self._lock_table = lock_table
        self._kms_key_id = kms_key_id
        self._now = clock
        session = boto3.Session(profile_name=profile, region_name=region)
        # boto3-stubs overloads client() per literal service name; these are built
        # dynamically, so go through an untyped factory.
        make_client: Any = session.client
        self._s3: Any = make_client("s3", endpoint_url=endpoint_url, config=_CLIENT_CONFIG)
        self._ddb: Any = make_client("dynamodb", endpoint_url=endpoint_url, config=_CLIENT_CONFIG)
        #: The document as this backend last saw it, or ``None`` when the cache is
        #: cold or has been invalidated.
        self._cached: _Snapshot | None = None
        #: owner -> node ids this process locked, so release targets exactly them.
        self._held: dict[str, set[str]] = {}

    @property
    def _uri(self) -> str:
        return f"s3://{self._bucket}/{self._key}"

    # -- state ------------------------------------------------------------

    @override
    def load(self) -> StateGraph:
        return StateGraph(nodes=dict(self._document().nodes))

    def _check(self, touched: Set[str]) -> None:
        """Refuse a write whose nodes this run no longer holds.

        The ETag compare-and-swap already refuses a writer whose *view* of the
        document is stale, and catches most of this. It does not catch the case
        where a lease lapsed, someone else took it, and has not written yet — the
        ETag is still current, so the write would land into state that is no
        longer this run's to touch. One consistent lock-table read closes that.
        """
        if self._lease is None or not touched:
            return
        self._refuse_unfenced(touched, self._read_holds(set(touched)))

    @override
    def put(self, node: StateNode) -> None:
        self.put_many((node,))

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Store every node in one object write; nodes already stored are dropped.

        Re-storing a node byte-for-byte identical to the one on record is not a
        state change, so it triggers neither a serial bump nor a request; a
        re-apply that changes nothing issues no writes.
        """
        stored = list(nodes)

        def mutate(doc: StateDocument) -> tuple[StateDocument, Set[str]] | None:
            fresh = {node.id: node for node in stored if doc.nodes.get(node.id) != node}
            if not fresh:
                return None
            return (
                replace(doc, serial=doc.serial + 1, nodes={**doc.nodes, **fresh}),
                set(fresh),
            )

        self._commit(mutate)

    @override
    def delete(self, node_id: str) -> None:
        def mutate(doc: StateDocument) -> tuple[StateDocument, Set[str]] | None:
            if node_id not in doc.nodes:
                return None
            remaining = {nid: n for nid, n in doc.nodes.items() if nid != node_id}
            return replace(doc, serial=doc.serial + 1, nodes=remaining), {node_id}

        self._commit(mutate)

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Deletes and upserts in one object write, so a rekey cannot half-land."""
        dropped = set(delete_ids)
        fresh = {node.id: node for node in nodes}

        def mutate(doc: StateDocument) -> tuple[StateDocument, Set[str]] | None:
            remaining = {nid: n for nid, n in doc.nodes.items() if nid not in dropped}
            merged = {**remaining, **fresh}
            if merged == doc.nodes:
                return None
            return replace(doc, serial=doc.serial + 1, nodes=merged), dropped | set(fresh)

        self._commit(mutate)

    @override
    def serial(self) -> int:
        return self._document().serial

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        dropped = set(remove)

        def mutate(doc: StateDocument) -> tuple[StateDocument, Set[str]] | None:
            return replace(doc, outputs=merge_outputs(doc.outputs, outputs, dropped)), frozenset()

        self._commit(mutate)

    def _commit(
        self, mutate: Callable[[StateDocument], tuple[StateDocument, Set[str]] | None]
    ) -> None:
        """Apply ``mutate`` to the current document and persist it, rebasing on conflict.

        A lost compare-and-swap does not mean *this* run's nodes were touched —
        with per-node leases, two runs over disjoint subgraphs legitimately share
        the one state object. The mutation is therefore re-applied to a freshly
        read document and retried; the fencing check (`_check`, re-run per
        attempt) is what refuses a write whose own nodes are no longer held.
        Aborting instead — the old behaviour — killed a healthy run mid-apply
        *after* its provider call succeeded, orphaning the resource it created.
        """
        last: _CasConflict | None = None
        for _ in range(_CAS_ATTEMPTS):
            doc = self._document()
            result = mutate(doc)
            if result is None:
                return
            updated, touched = result
            self._check(set(touched))
            try:
                self._write(updated)
                return
            except _CasConflict as conflict:
                last = conflict
                self._cached = None  # rebase: re-fetch and re-apply on the next loop
        raise StateError(
            f"remote state {self._uri} kept changing under this run "
            f"({_CAS_ATTEMPTS} compare-and-swap attempts) — another run is "
            f"rewriting the same object; re-run once it settles"
        ) from (last.error if last is not None else None)

    @override
    def outputs(self) -> dict[str, Any]:
        return dict(self._document().outputs)

    # -- object storage ---------------------------------------------------

    def _document(self) -> StateDocument:
        """The current document, fetched once then maintained by this backend's writes.

        Re-reading on every access would not make concurrent writes safe — the
        lease does that — and would cost a GET per node. A stale cache surfaces as
        a compare-and-swap failure on the next write. :meth:`acquire_lock` drops
        the cache, so a run reads what was committed when its lease was taken.
        """
        if self._cached is None:
            self._cached = self._fetch()
        return self._cached.doc

    def _fetch(self) -> _Snapshot:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=self._key)
        except ClientError as exc:
            if _code(exc) in ("NoSuchKey", "404"):
                return _Snapshot(StateDocument(), None)  # first run against this key
            raise self._read_error(exc) from exc
        return _Snapshot(decode(response["Body"].read()), response.get("ETag"))

    def _read_error(self, exc: ClientError) -> StateError:
        if _code(exc) == "NoSuchBucket":
            return StateError(
                f"state bucket {self._bucket!r} does not exist — create it "
                f"(with versioning enabled) before using the s3 state backend"
            )
        return StateError(f"cannot read state {self._uri}: {exc}")

    def _write(self, doc: StateDocument) -> None:
        """Persist ``doc``, refusing the write if the object changed concurrently."""
        body = encode(doc)
        try:
            response = self._s3.put_object(
                Bucket=self._bucket,
                Key=self._key,
                Body=body,
                ContentType="application/json",
                **self._compression(body),
                **self._encryption(),
                **self._precondition(),
            )
        except ClientError as exc:
            if _code(exc) in _CAS_CODES:
                raise _CasConflict(exc) from exc  # `_commit` rebases and retries
            raise StateError(f"cannot write state {self._uri}: {exc}") from exc
        self._cached = _Snapshot(doc, response.get("ETag"))

    def _compression(self, body: bytes) -> dict[str, str]:
        """Label a gzipped body for readers accessing the object out-of-band.

        The stored bytes are self-describing either way — :func:`decode` sniffs
        the magic number — so this header is informational, not a decoding
        contract.
        """
        return {"ContentEncoding": "gzip"} if body[:2] == b"\x1f\x8b" else {}

    def _encryption(self) -> dict[str, str]:
        if self._kms_key_id:
            return {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": self._kms_key_id}
        return {"ServerSideEncryption": "AES256"}

    def _precondition(self) -> dict[str, str]:
        """Replace exactly the version last read, or create the object if there is none.

        S3 enforces the precondition: a lost race fails the request outright.
        """
        if self._cached is None or self._cached.etag is None:
            return {"IfNoneMatch": "*"}
        return {"IfMatch": self._cached.etag}

    # -- locking ----------------------------------------------------------

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        return self._take_scope(owner, ttl_seconds, scope, invalidate=True)

    @override
    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        """Push the expiry out without disturbing this run's view of state.

        The cache drop an acquire performs is exactly wrong here: the cached ETag
        is what this run's writes compare against, and a renewal that cleared it
        would make the next write re-read the object and compare-and-swap against
        whatever it found — defeating the staleness check the ETag exists for.
        """
        return self._take_scope(owner, ttl_seconds, scope, invalidate=False)

    def _take_scope(
        self, owner: str, ttl_seconds: float, scope: Set[str], *, invalidate: bool
    ) -> Result[Lease, LockError]:
        now = self._now()
        expires = now + ttl_seconds
        if not scope:
            return Success(Lease(owner=owner, expires_at=expires))
        fence = self._next_fence()
        taken: list[str] = []
        for chunk in _chunks(sorted(scope), _TRANSACT_MAX):
            items = [self._take(nid, owner, expires, now, fence) for nid in chunk]
            for attempt in range(_DDB_ATTEMPTS):
                try:
                    self._ddb.transact_write_items(TransactItems=items)
                    break
                except ClientError as exc:
                    cancelled = _code(exc) == "TransactionCanceledException"
                    if cancelled and not _contended(exc) and attempt + 1 < _DDB_ATTEMPTS:
                        # Cancelled for a transient reason (item conflict,
                        # throttling), not by the lock condition: retry. On a
                        # *renew* especially, treating this as contention would
                        # cancel a healthy run mid-apply.
                        continue
                    # A failed *acquire* releases what it took, so no half-scope
                    # stays held. A failed *renew* must not: these rows are the
                    # live lease of a still-running run, and deleting them would
                    # release locks it validly holds.
                    if invalidate:
                        for node_id in taken:
                            self._release_one(node_id, owner)
                    if not cancelled:
                        raise self._lock_error(exc) from exc
                    return Failure(self._blocker(owner, now, scope, exc))
            taken.extend(chunk)
        self._held.setdefault(owner, set()).update(taken)
        if invalidate:
            # Drop the cached document now the lease is held: the plan was built
            # from a pre-lock read, so its ETag is already stale.
            self._cached = None
        return Success(self._minted_lease(owner, expires, scope, fence))

    def _next_fence(self) -> int:
        """Mint the next epoch with an atomic counter in the lock table.

        One extra round trip per *acquisition* — not per write — which is the
        cheap end of the trade: without it, two runs whose leases overlapped
        could not be told apart at write time.
        """
        try:
            response = self._ddb.update_item(
                TableName=self._lock_table,
                Key={"node_id": {"S": _FENCE_ITEM}},
                UpdateExpression="ADD fence :one",
                ExpressionAttributeValues={":one": {"N": "1"}},
                ReturnValues="UPDATED_NEW",
            )
        except ClientError as exc:
            # First call an acquire makes, so a missing or misconfigured lock
            # table surfaces here rather than at the lock write.
            raise self._lock_error(exc) from exc
        return int(response["Attributes"]["fence"]["N"])

    def _take(
        self, node_id: str, owner: str, expires: float, now: float, fence: int
    ) -> dict[str, Any]:
        """A conditional Put claiming one node id for ``owner``."""
        return {
            "Put": {
                "TableName": self._lock_table,
                "Item": {
                    "node_id": {"S": node_id},
                    "owner": {"S": owner},
                    "expires_at": {"N": repr(expires)},
                    "fence": {"N": str(fence)},
                },
                "ConditionExpression": _LOCK_CONDITION,
                "ExpressionAttributeNames": {"#o": "owner"},
                "ExpressionAttributeValues": {
                    ":owner": {"S": owner},
                    ":now": {"N": repr(now)},
                },
            }
        }

    def _lock_error(self, exc: ClientError) -> StateError:
        """An infrastructure failure (as opposed to ordinary contention)."""
        if _code(exc) == "ResourceNotFoundException":
            return StateError(
                f"lock table {self._lock_table!r} does not exist — create it with a "
                f"'node_id' (S) hash key before using the s3 state backend"
            )
        return StateError(f"acquire_lock failed: {exc}")

    def _blocker(self, owner: str, now: float, scope: Set[str], exc: ClientError) -> LockError:
        """The error naming who holds the scope, in the wording shared by all backends."""
        error = scope_conflict(self._read_holds(scope), owner, now, scope)
        # The blocking hold may have expired between the cancel and the read-back;
        # report contention rather than name a holder that is gone.
        return error or LockError(f"state lock contended by another run ({exc})")

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        for node_id in sorted(self._held.pop(owner, set())):
            self._release_one(node_id, owner)
        return Success(None)

    def _release_one(self, node_id: str, owner: str) -> None:
        try:
            self._ddb.delete_item(
                TableName=self._lock_table,
                Key={"node_id": {"S": node_id}},
                ConditionExpression="#o = :owner",
                ExpressionAttributeNames={"#o": "owner"},
                ExpressionAttributeValues={":owner": {"S": owner}},
            )
        except ClientError as exc:
            # The hold expired and was reclaimed by another owner: not ours to
            # delete, and nothing to undo.
            if _code(exc) != "ConditionalCheckFailedException":
                raise StateError(f"release_lock failed: {exc}") from exc

    def _read_holds(self, scope: Set[str]) -> dict[str, Lease]:
        """Leases currently recorded over any node id in ``scope``.

        ``UnprocessedKeys`` are re-requested until the read is complete, and a
        read that stays incomplete fails closed: a hold silently dropped under
        throttling would read as "unheld", and the fencing check this feeds
        would wave through a write to a node another run owns.
        """
        holds: dict[str, Lease] = {}
        for chunk in _chunks(sorted(scope), _TRANSACT_MAX):
            request: dict[str, Any] = {
                self._lock_table: {
                    "Keys": [{"node_id": {"S": node_id}} for node_id in chunk],
                    "ConsistentRead": True,
                }
            }
            for _ in range(_DDB_ATTEMPTS):
                response = self._ddb.batch_get_item(RequestItems=request)
                for item in response.get("Responses", {}).get(self._lock_table, []):
                    if (hold := _lease_of_item(item)) is not None:
                        holds[item["node_id"]["S"]] = hold
                request = response.get("UnprocessedKeys") or {}
                if not request:
                    break
            if request:
                raise StateError(
                    "could not read the lock table completely (DynamoDB kept "
                    "returning unprocessed keys) — refusing to write unfenced"
                )
        return holds

    # -- lock administration ----------------------------------------------

    @override
    def locks(self) -> dict[str, Lease]:
        """Every hold in the lock table.

        The table is keyed by node id alone, so a table shared between projects
        reports all of them — which is the honest answer, and why the CLI shows
        the holders before it breaks anything.
        """
        holds: dict[str, Lease] = {}
        paginator = self._ddb.get_paginator("scan")
        for page in paginator.paginate(TableName=self._lock_table, ConsistentRead=True):
            for item in page.get("Items", []):
                if (hold := _lease_of_item(item)) is not None:
                    holds[item["node_id"]["S"]] = hold
        return holds

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        broken = 0
        for node_id in sorted(node_ids):
            # ALL_OLD so the count is holds actually broken, not delete calls made.
            response = self._ddb.delete_item(
                TableName=self._lock_table,
                Key={"node_id": {"S": node_id}},
                ReturnValues="ALL_OLD",
            )
            broken += 1 if response.get("Attributes") else 0
        return broken

    # -- preflight ---------------------------------------------------------
    #
    # The checks share nothing with the storage path; see state/s3_preflight.

    @override
    def check(self) -> list[Check]:
        return run_checks(
            self._s3, self._ddb, bucket=self._bucket, key=self._key, lock_table=self._lock_table
        )

    @override
    def probe(self) -> Check:
        return run_probe(self._s3, bucket=self._bucket, key=self._key)

    @override
    def close(self) -> None:
        self._cached = None


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
