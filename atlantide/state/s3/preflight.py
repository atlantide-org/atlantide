"""Preflight checks for the S3 state backend: bucket, tables, journal, CAS probe.

Independent of the storage path in :mod:`atlantide.state.s3.backend`: these
functions take the boto3 clients and names directly and only produce
:class:`~atlantide.core.check.Check` rows for ``atlantide state check``.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import suppress
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core.check import FAIL, OK, WARN, Check, Status
from atlantide.state.s3.dynamo import CAS_CODES
from atlantide.util.aws import error_code

__all__ = ["run_checks", "run_probe"]


def run_checks(
    s3: Any,
    ddb: Any,
    *,
    bucket: str,
    key: str,
    lock_table: str,
    journal_table: str | None = None,
) -> list[Check]:
    """Report every way this bucket + tables are unfit for shared state.

    Reports every problem, not only the first. For a missing table, the key
    schema, TTL and backup checks are skipped because they cannot be read.

    ``journal_table`` holds the journal heads (the commit pointers and fences);
    it defaults to the lock table.
    """
    heads = journal_table or lock_table
    checks = [
        _check_bucket(s3, bucket, key),
        _check_versioning(s3, bucket),
        _check_lifecycle(s3, bucket, key),
        _check_journal_listing(s3, bucket, key),
    ]
    table = _describe_table(ddb, lock_table)
    if isinstance(table, Check):  # table unreadable: nothing further to inspect
        checks.append(table)
    else:
        checks += [_check_key_schema(table, lock_table), _check_ttl(ddb, lock_table)]
    if heads != lock_table:
        described = _describe_table(ddb, heads, name="journal table")
        if isinstance(described, Check):
            return [*checks, described]
        checks.append(_check_key_schema(described, heads, name="journal table"))
    elif isinstance(table, Check):
        return checks
    return [*checks, _check_pitr(ddb, heads), _check_head_reads(ddb, heads)]


def _checked(name: str, status: Status, read: Callable[[], Check]) -> Check:
    """Run ``read``, mapping a ClientError to a ``cannot read`` Check."""
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


def _check_lifecycle(s3: Any, bucket: str, key: str) -> Check:
    """Compaction deletes superseded journal entries; on a versioned bucket each
    delete leaves a noncurrent version behind, which only a lifecycle rule reaps."""
    prefix = f"{key}.d/"
    hint = (
        f"no enabled lifecycle rule with NoncurrentVersionExpiration covers "
        f"{prefix!r} — on a versioned bucket, "
        f"compacted journal entries are kept forever as noncurrent versions; add "
        f"a rule on that prefix with NoncurrentVersionExpiration (e.g. 7 days) "
        f"and ExpiredObjectDeleteMarker"
    )

    def read() -> Check:
        try:
            rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket).get("Rules", [])
        except ClientError as exc:
            if error_code(exc) == "NoSuchLifecycleConfiguration":
                return Check("journal lifecycle", WARN, hint)
            raise
        for rule in rules:
            if rule.get("Status") != "Enabled" or "NoncurrentVersionExpiration" not in rule:
                continue
            rule_filter = rule.get("Filter")
            if rule_filter is None:  # the legacy top-level prefix
                rule_prefix = rule.get("Prefix")
            elif not rule_filter:
                rule_prefix = ""  # an empty filter applies to every object
            elif "And" in rule_filter:
                rule_prefix = rule_filter["And"].get("Prefix")
            else:
                rule_prefix = rule_filter.get("Prefix")
            if rule_prefix is not None and prefix.startswith(rule_prefix):
                return Check("journal lifecycle", OK, f"rule {rule.get('ID', '?')!r}")
        return Check("journal lifecycle", WARN, hint)

    return _checked("journal lifecycle", WARN, read)


def _check_journal_listing(s3: Any, bucket: str, key: str) -> Check:
    """Every reader LISTs the journal, so a read-only role needs s3:ListBucket."""
    try:
        s3.list_objects_v2(Bucket=bucket, Prefix=f"{key}.d/", MaxKeys=1)
    except ClientError as exc:
        return Check(
            "journal listing",
            FAIL,
            f"cannot list s3://{bucket}/{key}.d/: {exc} — every state read needs "
            f"s3:ListBucket on that prefix",
        )
    return Check("journal listing", OK, "s3:ListBucket allowed")


def _check_pitr(ddb: Any, table: str) -> Check:
    """The heads are the commit pointers: without point-in-time recovery, losing
    the table loses every commit since the last compaction."""

    def read() -> Check:
        description = ddb.describe_continuous_backups(TableName=table)
        status = (
            description.get("ContinuousBackupsDescription", {})
            .get("PointInTimeRecoveryDescription", {})
            .get("PointInTimeRecoveryStatus")
        )
        if status == "ENABLED":
            return Check("journal heads PITR", OK, f"enabled on {table!r}")
        return Check(
            "journal heads PITR",
            WARN,
            f"point-in-time recovery is off on {table!r}, which holds the state "
            f"journal's heads — losing the table loses every write since the last "
            f"compaction; enable it with `aws dynamodb update-continuous-backups "
            f"--table-name {table} --point-in-time-recovery-specification "
            f"PointInTimeRecoveryEnabled=true`",
        )

    return _checked("journal heads PITR", WARN, read)


def _check_head_reads(ddb: Any, table: str) -> Check:
    """Every reader batch-reads heads, so a read-only role needs BatchGetItem."""
    try:
        ddb.batch_get_item(
            RequestItems={
                table: {"Keys": [{"node_id": {"S": _PROBE_HEAD}}], "ConsistentRead": True}
            }
        )
    except ClientError as exc:
        return Check(
            "journal head reads",
            FAIL,
            f"cannot read {table!r}: {exc} — every state read needs dynamodb:BatchGetItem",
        )
    return Check("journal head reads", OK, "dynamodb:BatchGetItem allowed")


def _describe_table(
    ddb: Any, table_name: str, *, name: str = "lock table"
) -> dict[str, Any] | Check:
    """A table's description, or the failing check if it cannot be read."""
    try:
        table: dict[str, Any] = ddb.describe_table(TableName=table_name)["Table"]
    except ClientError as exc:
        return Check(
            name,
            FAIL,
            f"{table_name!r} unreachable: {exc} — create it with a 'node_id' (S) hash key",
        )
    return table


def _check_key_schema(table: dict[str, Any], table_name: str, *, name: str = "lock table") -> Check:
    hash_keys = [k["AttributeName"] for k in table.get("KeySchema", []) if k["KeyType"] == "HASH"]
    if hash_keys != ["node_id"]:
        return Check(
            name,
            FAIL,
            f"{table_name!r} hash key is {hash_keys or 'missing'}, expected ['node_id']",
        )
    return Check(name, OK, table_name)


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


#: A head key no state uses (namespaces are ``s3://...`` URIs), for probes.
_PROBE_HEAD = "\x00h\x00atlantide-probe"


def run_probe(
    s3: Any,
    ddb: Any = None,
    *,
    bucket: str,
    key: str,
    journal_table: str | None = None,
    namespace: str = "",
) -> Check:
    """Confirm the store honours conditional writes, by trying to break them.

    A store that ignores ``If-None-Match`` (as some S3-compatible endpoints do)
    accepts both writes below, so no compare-and-swap is effective. Writes to a
    scratch key beside the state object and deletes it again; state itself is
    untouched. With a journal table, also tries a conditional ``UpdateItem`` on
    a scratch head, the permission and behaviour every commit depends on.
    """
    s3_result = _probe_s3(s3, bucket=bucket, key=key)
    if s3_result.status != OK or ddb is None or journal_table is None:
        return s3_result
    ddb_result = _probe_heads(ddb, journal_table, namespace)
    if ddb_result.status != OK:
        return ddb_result
    return Check("conditional writes", OK, "honoured by S3 and DynamoDB (compare-and-swap works)")


def _probe_heads(ddb: Any, table: str, namespace: str) -> Check:
    """A conditional update that must succeed, then one that must be refused."""
    scratch = {"node_id": {"S": f"{_PROBE_HEAD}\x00{namespace}"}}
    update = {
        "TableName": table,
        "Key": scratch,
        "UpdateExpression": "SET seq = :one",
        "ConditionExpression": "attribute_not_exists(seq)",
        "ExpressionAttributeValues": {":one": {"N": "1"}},
    }
    try:
        with suppress(ClientError):
            ddb.delete_item(TableName=table, Key=scratch)
        ddb.update_item(**update)
    except ClientError as exc:
        return Check(
            "conditional writes",
            FAIL,
            f"cannot commit to {table!r}: {exc} — every state write needs "
            f"dynamodb:UpdateItem on it",
        )
    try:
        ddb.update_item(**update)
    except ClientError as exc:
        result = (
            Check("conditional writes", OK, "honoured")
            if error_code(exc) == "ConditionalCheckFailedException"
            else Check("conditional writes", WARN, f"unexpected refusal: {exc}")
        )
    else:
        result = Check(
            "conditional writes",
            FAIL,
            f"{table!r} ignored a condition expression — journal commits would not be fenced",
        )
    with suppress(ClientError):
        ddb.delete_item(TableName=table, Key=scratch)
    return result


def _probe_s3(s3: Any, *, bucket: str, key: str) -> Check:
    scratch = f"{key}.atlantide-probe"
    try:
        s3.put_object(Bucket=bucket, Key=scratch, Body=b"1", IfNoneMatch="*")
    except ClientError as exc:
        if error_code(exc) not in CAS_CODES:
            return Check("conditional writes", WARN, f"probe could not write: {exc}")
        # A probe that crashed between its put and its delete leaves the scratch
        # key behind, failing the first conditional put: clear it and retry once.
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
            if error_code(exc) in CAS_CODES
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
