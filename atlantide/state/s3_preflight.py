"""Preflight checks for the S3 state backend: bucket, lock table, CAS probe.

Split from :mod:`atlantide.state.s3_backend` because it shares nothing with the
storage path — these functions take the boto3 clients and names directly and
only ever produce :class:`~atlantide.core.check.Check` rows for ``atlantide
state check``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core.check import FAIL, OK, WARN, Check, Status

#: Error codes S3 returns when a conditional write loses the race. Mirrored by
#: the backend module; the probe below is their behavioural test.
CAS_CODES = frozenset({"PreconditionFailed", "ConditionalRequestConflict"})


def run_checks(s3: Any, ddb: Any, *, bucket: str, key: str, lock_table: str) -> list[Check]:
    """Report every way this bucket + lock table is unfit for shared state.

    Everything is reported, never short-circuited: an operator setting a
    backend up wants the whole list, not the first thing that happens to be
    wrong. The one exception is the lock table, whose key schema and TTL
    cannot be read at all if the table itself is missing.
    """
    checks = [_check_bucket(s3, bucket, key), _check_versioning(s3, bucket)]
    table = _describe_table(ddb, lock_table)
    if isinstance(table, Check):  # table unreadable: nothing further to inspect
        return [*checks, table]
    return [*checks, _check_key_schema(table, lock_table), _check_ttl(ddb, lock_table)]


def _checked(name: str, status: Status, read: Callable[[], Check]) -> Check:
    """Run ``read``, mapping a ClientError to a ``cannot read`` Check.

    The five per-property checks all share this try/except shape; the messages
    that make each check actionable stay with the check.
    """
    try:
        return read()
    except ClientError as exc:
        return Check(name, status, f"cannot read: {exc}")


def _check_bucket(s3: Any, bucket: str, key: str) -> Check:
    try:
        s3.head_bucket(Bucket=bucket)
    except ClientError as exc:
        return Check("bucket", FAIL, f"{bucket!r} unreachable: {exc}")
    return Check("bucket", OK, f"s3://{bucket}/{key}")


def _check_versioning(s3: Any, bucket: str) -> Check:
    """Versioning is what makes a bad state write recoverable."""

    def read() -> Check:
        status = s3.get_bucket_versioning(Bucket=bucket).get("Status")
        if status == "Enabled":
            return Check("bucket versioning", OK, "enabled")
        return Check(
            "bucket versioning",
            WARN,
            f"not enabled on {bucket!r} — a bad state write would be "
            f"unrecoverable; enable it with `aws s3api put-bucket-versioning "
            f"--bucket {bucket} --versioning-configuration Status=Enabled`",
        )

    return _checked("bucket versioning", WARN, read)


def _describe_table(ddb: Any, lock_table: str) -> dict[str, Any] | Check:
    """The lock table's description, or the failing check if it cannot be read."""
    try:
        table: dict[str, Any] = ddb.describe_table(TableName=lock_table)["Table"]
    except ClientError as exc:
        return Check(
            "lock table",
            FAIL,
            f"{lock_table!r} unreachable: {exc} — create it with a 'node_id' (S) hash key",
        )
    return table


def _check_key_schema(table: dict[str, Any], lock_table: str) -> Check:
    hash_keys = [k["AttributeName"] for k in table.get("KeySchema", []) if k["KeyType"] == "HASH"]
    if hash_keys != ["node_id"]:
        return Check(
            "lock table",
            FAIL,
            f"{lock_table!r} hash key is {hash_keys or 'missing'}, expected ['node_id']",
        )
    return Check("lock table", OK, lock_table)


def _check_ttl(ddb: Any, lock_table: str) -> Check:
    """Without a TTL on ``expires_at`` an abandoned lease is never reaped.

    An expired hold is already ignored, so this affects table growth rather
    than correctness.
    """

    def read() -> Check:
        spec = ddb.describe_time_to_live(TableName=lock_table)
        description = spec.get("TimeToLiveDescription", {})
        if description.get("TimeToLiveStatus") != "ENABLED":
            return Check(
                "lock table TTL",
                WARN,
                f"not enabled on {lock_table!r} — abandoned leases are ignored "
                f"once expired but never deleted; enable TTL on the 'expires_at' "
                f"attribute to self-evict them",
            )
        attribute = description.get("AttributeName")
        if attribute != "expires_at":
            return Check("lock table TTL", WARN, f"enabled on {attribute!r}, expected 'expires_at'")
        return Check("lock table TTL", OK, "enabled on expires_at")

    return _checked("lock table TTL", WARN, read)


def run_probe(s3: Any, *, bucket: str, key: str) -> Check:
    """Confirm the endpoint honours conditional writes, by trying to break one.

    A store that ignores ``If-None-Match`` accepts both writes below, which
    makes every compare-and-swap the backend relies on ineffective; some
    S3-compatible endpoints behave this way. Writes to a scratch key beside
    the state object and deletes it again; state itself is untouched.
    """
    scratch = f"{key}.atlantide-probe"
    try:
        s3.put_object(Bucket=bucket, Key=scratch, Body=b"1", IfNoneMatch="*")
    except ClientError as exc:
        if _code(exc) not in CAS_CODES:
            return Check("conditional writes", WARN, f"probe could not write: {exc}")
        # A probe that crashed between its put and its delete left the
        # scratch key behind, and every later probe would lose its first
        # conditional put to it forever. Clear it and retry once.
        try:
            s3.delete_object(Bucket=bucket, Key=scratch)
            s3.put_object(Bucket=bucket, Key=scratch, Body=b"1", IfNoneMatch="*")
        except ClientError as retry_exc:
            return Check("conditional writes", WARN, f"probe could not write: {retry_exc}")
    try:
        s3.put_object(Bucket=bucket, Key=scratch, Body=b"2", IfNoneMatch="*")
    except ClientError as exc:
        result = (
            Check("conditional writes", OK, "honoured (compare-and-swap works)")
            if _code(exc) in CAS_CODES
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
        s3.delete_object(Bucket=bucket, Key=scratch)
    return result


def _code(exc: ClientError) -> str:
    """The AWS error code (``""`` if the response shape is unexpected)."""
    return str(exc.response.get("Error", {}).get("Code", ""))
