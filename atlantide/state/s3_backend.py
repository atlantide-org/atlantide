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
from collections.abc import Iterable, Iterator, Mapping, Sequence, Set
from contextlib import suppress
from dataclasses import replace
from typing import Any

import boto3
from botocore.exceptions import ClientError
from returns.result import Failure, Result, Success

from atlantide.core.check import FAIL, OK, WARN, Check
from atlantide.core.errors import LockError, StateError
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
    scope_conflict,
)
from atlantide.state.codec import StateDocument, decode, encode

#: DynamoDB caps a transaction at 100 items; larger scopes are locked in chunks.
_TRANSACT_MAX = 100

#: Error codes S3 returns when a conditional write loses the race.
_CAS_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})

#: Take one node: unheld, held by the same owner, or expired — the same rule
#: :func:`scope_conflict` applies in the other backends.
_LOCK_CONDITION = "attribute_not_exists(node_id) OR #o = :owner OR expires_at < :now"


def _code(exc: ClientError) -> str:
    """The AWS error code, e.g. ``NoSuchKey`` (``""`` if the shape is unexpected)."""
    return str(exc.response.get("Error", {}).get("Code", ""))


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
        # dynamically alongside the provider's clients, so go through an untyped factory.
        make_client: Any = session.client
        self._s3: Any = make_client("s3", endpoint_url=endpoint_url)
        self._ddb: Any = make_client("dynamodb", endpoint_url=endpoint_url)
        #: Cached document + the ETag it was read/written at (``None`` = no object yet).
        self._doc: StateDocument | None = None
        self._etag: str | None = None
        #: owner -> node ids this process locked, so release targets exactly them.
        self._held: dict[str, set[str]] = {}

    @property
    def _uri(self) -> str:
        return f"s3://{self._bucket}/{self._key}"

    # -- state ------------------------------------------------------------

    def load(self) -> StateGraph:
        return StateGraph(nodes=dict(self._document().nodes))

    def put(self, node: StateNode) -> None:
        self.put_many((node,))

    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Store every node in one object write; nodes already stored are dropped.

        Re-storing a node byte-for-byte identical to the one on record is not a
        state change, so it triggers neither a serial bump nor a request; a
        re-apply that changes nothing issues no writes.
        """
        doc = self._document()
        fresh = {node.id: node for node in nodes if doc.nodes.get(node.id) != node}
        if not fresh:
            return
        self._write(replace(doc, serial=doc.serial + 1, nodes={**doc.nodes, **fresh}))

    def delete(self, node_id: str) -> None:
        doc = self._document()
        if node_id not in doc.nodes:
            return
        remaining = {nid: n for nid, n in doc.nodes.items() if nid != node_id}
        self._write(replace(doc, serial=doc.serial + 1, nodes=remaining))

    def serial(self) -> int:
        return self._document().serial

    def set_outputs(self, outputs: Mapping[str, Any]) -> None:
        doc = self._document()
        self._write(replace(doc, outputs={**doc.outputs, **outputs}))

    def outputs(self) -> dict[str, Any]:
        return dict(self._document().outputs)

    # -- object storage ---------------------------------------------------

    def _document(self) -> StateDocument:
        """The current document, fetched once then maintained by this backend's writes.

        Re-reading on every access would not make concurrent writes safe — the
        lease does that — and would cost a GET per node. A stale cache surfaces as
        a compare-and-swap failure on the next write.
        """
        if self._doc is None:
            self._doc, self._etag = self._fetch()
        return self._doc

    def _fetch(self) -> tuple[StateDocument, str | None]:
        try:
            response = self._s3.get_object(Bucket=self._bucket, Key=self._key)
        except ClientError as exc:
            if _code(exc) in ("NoSuchKey", "404"):
                return StateDocument(), None  # first run against this key
            raise self._read_error(exc) from exc
        return decode(response["Body"].read()), response.get("ETag")

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
                raise StateError(
                    f"remote state {self._uri} changed under this run (expected serial "
                    f"{doc.serial - 1}) — another apply is writing it, or this run's "
                    f"lease expired"
                ) from exc
            raise StateError(f"cannot write state {self._uri}: {exc}") from exc
        self._doc = doc
        self._etag = response.get("ETag")

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
        if self._etag is None:
            return {"IfNoneMatch": "*"}
        return {"IfMatch": self._etag}

    # -- locking ----------------------------------------------------------

    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        now = self._now()
        expires = now + ttl_seconds
        if not scope:
            return Success(Lease(owner=owner, expires_at=expires))
        taken: list[str] = []
        for chunk in _chunks(sorted(scope), _TRANSACT_MAX):
            try:
                self._ddb.transact_write_items(
                    TransactItems=[self._take(nid, owner, expires, now) for nid in chunk]
                )
            except ClientError as exc:
                # A later chunk was refused: release the nodes already taken, so a
                # failed acquire never leaves half the scope held.
                for node_id in taken:
                    self._release_one(node_id, owner)
                if _code(exc) != "TransactionCanceledException":
                    raise self._lock_error(exc) from exc
                return Failure(self._blocker(owner, now, scope, exc))
            taken.extend(chunk)
        self._held.setdefault(owner, set()).update(taken)
        return Success(Lease(owner=owner, expires_at=expires, scope=frozenset(scope)))

    def _take(self, node_id: str, owner: str, expires: float, now: float) -> dict[str, Any]:
        """A conditional Put claiming one node id for ``owner``."""
        return {
            "Put": {
                "TableName": self._lock_table,
                "Item": {
                    "node_id": {"S": node_id},
                    "owner": {"S": owner},
                    "expires_at": {"N": repr(expires)},
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

    def _blocker(
        self, owner: str, now: float, scope: Set[str], exc: ClientError
    ) -> LockError:
        """The error naming who holds the scope, in the wording shared by all backends."""
        error = scope_conflict(self._read_holds(scope), owner, now, scope)
        # The blocking hold may have expired between the cancel and the read-back;
        # report contention rather than claim a lease this run does not hold.
        return error or LockError(f"state lock contended by another run ({exc})")

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
            # The hold expired and was reclaimed by another owner: not this run's
            # to delete, and nothing to undo.
            if _code(exc) != "ConditionalCheckFailedException":
                raise StateError(f"release_lock failed: {exc}") from exc

    def _read_holds(self, scope: Set[str]) -> dict[str, Lease]:
        """Leases currently recorded over any node id in ``scope``."""
        holds: dict[str, Lease] = {}
        for chunk in _chunks(sorted(scope), _TRANSACT_MAX):
            response = self._ddb.batch_get_item(
                RequestItems={
                    self._lock_table: {
                        "Keys": [{"node_id": {"S": node_id}} for node_id in chunk],
                        "ConsistentRead": True,
                    }
                }
            )
            for item in response.get("Responses", {}).get(self._lock_table, []):
                holds[item["node_id"]["S"]] = Lease(
                    owner=item["owner"]["S"], expires_at=float(item["expires_at"]["N"])
                )
        return holds

    # -- lock administration ----------------------------------------------

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
                holds[item["node_id"]["S"]] = Lease(
                    owner=item["owner"]["S"], expires_at=float(item["expires_at"]["N"])
                )
        return holds

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

    def check(self) -> list[Check]:
        """Report every way this bucket + lock table is unfit for shared state.

        Everything is reported, never short-circuited: an operator setting a
        backend up wants the whole list, not the first thing that happens to be
        wrong. The one exception is the lock table, whose key schema and TTL
        cannot be read at all if the table itself is missing.
        """
        checks = [self._check_bucket(), self._check_versioning()]
        table = self._describe_table()
        if isinstance(table, Check):  # unreachable: nothing further to inspect
            return [*checks, table]
        return [*checks, self._check_key_schema(table), self._check_ttl()]

    def _check_bucket(self) -> Check:
        try:
            self._s3.head_bucket(Bucket=self._bucket)
        except ClientError as exc:
            return Check("bucket", FAIL, f"{self._bucket!r} unreachable: {exc}")
        return Check("bucket", OK, self._uri)

    def _check_versioning(self) -> Check:
        """Versioning is what makes a bad state write recoverable."""
        try:
            status = self._s3.get_bucket_versioning(Bucket=self._bucket).get("Status")
        except ClientError as exc:
            return Check("bucket versioning", WARN, f"cannot read: {exc}")
        if status == "Enabled":
            return Check("bucket versioning", OK, "enabled")
        return Check(
            "bucket versioning",
            WARN,
            f"not enabled on {self._bucket!r} — a bad state write would be "
            f"unrecoverable; enable it with `aws s3api put-bucket-versioning "
            f"--bucket {self._bucket} --versioning-configuration Status=Enabled`",
        )

    def _describe_table(self) -> dict[str, Any] | Check:
        """The lock table's description, or the failing check if it cannot be read."""
        try:
            table: dict[str, Any] = self._ddb.describe_table(TableName=self._lock_table)["Table"]
        except ClientError as exc:
            return Check(
                "lock table",
                FAIL,
                f"{self._lock_table!r} unreachable: {exc} — create it with a "
                f"'node_id' (S) hash key",
            )
        return table

    def _check_key_schema(self, table: dict[str, Any]) -> Check:
        hash_keys = [
            k["AttributeName"] for k in table.get("KeySchema", []) if k["KeyType"] == "HASH"
        ]
        if hash_keys != ["node_id"]:
            return Check(
                "lock table",
                FAIL,
                f"{self._lock_table!r} hash key is {hash_keys or 'missing'}, "
                f"expected ['node_id']",
            )
        return Check("lock table", OK, self._lock_table)

    def _check_ttl(self) -> Check:
        """Without a TTL on ``expires_at`` an abandoned lease is never reaped.

        An expired hold is already ignored, so this affects table growth rather
        than correctness.
        """
        try:
            spec = self._ddb.describe_time_to_live(TableName=self._lock_table)
            description = spec.get("TimeToLiveDescription", {})
        except ClientError as exc:
            return Check("lock table TTL", WARN, f"cannot read: {exc}")
        if description.get("TimeToLiveStatus") != "ENABLED":
            return Check(
                "lock table TTL",
                WARN,
                f"not enabled on {self._lock_table!r} — abandoned leases are ignored "
                f"once expired but never deleted; enable TTL on the 'expires_at' "
                f"attribute to self-evict them",
            )
        attribute = description.get("AttributeName")
        if attribute != "expires_at":
            return Check(
                "lock table TTL", WARN, f"enabled on {attribute!r}, expected 'expires_at'"
            )
        return Check("lock table TTL", OK, "enabled on expires_at")

    def probe(self) -> Check:
        """Confirm the endpoint honours conditional writes, by trying to break one.

        A store that ignores ``If-None-Match`` accepts both writes below, which
        makes every compare-and-swap this backend relies on ineffective; some
        S3-compatible endpoints behave this way. Writes to a scratch key beside
        the state object and deletes it again; state itself is untouched.
        """
        key = f"{self._key}.atlantide-probe"
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=b"1", IfNoneMatch="*")
        except ClientError as exc:
            return Check("conditional writes", WARN, f"probe could not write: {exc}")
        try:
            self._s3.put_object(Bucket=self._bucket, Key=key, Body=b"2", IfNoneMatch="*")
        except ClientError as exc:
            honoured = _code(exc) in _CAS_CODES
            result = (
                Check("conditional writes", OK, "honoured (compare-and-swap works)")
                if honoured
                else Check("conditional writes", WARN, f"unexpected refusal: {exc}")
            )
        else:
            result = Check(
                "conditional writes",
                FAIL,
                "the endpoint ignored If-None-Match — concurrent runs would "
                "silently overwrite each other's state; do not share this backend",
            )
        with suppress(ClientError):  # a leftover scratch object is harmless
            self._s3.delete_object(Bucket=self._bucket, Key=key)
        return result

    def close(self) -> None:
        self._doc = None
        self._etag = None


def _chunks(items: Sequence[str], size: int) -> Iterator[Sequence[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]
