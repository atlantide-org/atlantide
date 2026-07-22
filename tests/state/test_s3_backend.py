"""S3 + DynamoDB specifics: the compare-and-swap, lock rollback, and setup errors.

The shared behaviour (roundtrip, serial, lock semantics) is covered once for
every backend in :mod:`tests.state.test_backend`; this suite covers only what is
particular to talking to S3.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from atlantide.core import is_successful
from atlantide.core.errors import StateError
from atlantide.state import s3_backend as s3_module
from atlantide.state.codec import loads
from atlantide.state.s3_backend import S3StateBackend
from tests.support import FakeClock, create_state_store, fake_aws_credentials

from .conftest import BUCKET, LOCK_TABLE, REGION, node

KEY = "prod/atlantide.json"


@pytest.fixture
def aws(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Credentials, a mocked AWS, and the bucket + lock table already created."""
    fake_aws_credentials(monkeypatch, region=REGION)
    with mock_aws():
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
        yield


def _backend(**kwargs: Any) -> S3StateBackend:
    kwargs.setdefault("lock_table", LOCK_TABLE)
    kwargs.setdefault("region", REGION)
    return S3StateBackend(BUCKET, KEY, **kwargs)


def _head() -> Any:
    return boto3.client("s3", region_name=REGION).head_object(Bucket=BUCKET, Key=KEY)


def test_state_is_visible_to_a_second_process(aws: None) -> None:
    """The point of remote state: another run reads what this one wrote."""
    writer = _backend()
    writer.put(node("a", input_hash="h1"))
    writer.set_outputs({"dev:url": "https://example.test"})

    reader = _backend()  # a fresh instance = a different machine
    assert reader.load().get("a").input_hash == "h1"
    assert reader.outputs() == {"dev:url": "https://example.test"}
    assert reader.serial() == 1


def test_object_is_canonical_json(aws: None) -> None:
    backend = _backend()
    backend.put(node("b"))
    raw = boto3.client("s3", region_name=REGION).get_object(Bucket=BUCKET, Key=KEY)[
        "Body"
    ].read()
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    assert "b" in loads(raw).nodes


def test_stale_write_is_refused(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A lost compare-and-swap must surface, never silently clobber the winner."""
    backend = _backend()

    def _precondition_failed(**_: Any) -> None:
        raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")

    monkeypatch.setattr(backend._s3, "put_object", _precondition_failed)
    with pytest.raises(StateError) as exc:
        backend.put(node("a"))
    assert "changed under this run" in str(exc.value)


def test_missing_bucket_is_reported_with_a_hint(aws: None) -> None:
    backend = S3StateBackend("no-such-bucket", KEY, lock_table=LOCK_TABLE, region=REGION)
    with pytest.raises(StateError) as exc:
        backend.load()
    assert "does not exist" in str(exc.value)


def test_missing_lock_table_is_reported_with_a_hint(aws: None) -> None:
    backend = _backend(lock_table="no-such-table")
    with pytest.raises(StateError) as exc:
        backend.acquire_lock("alice", 30, {"a"})
    assert "node_id" in str(exc.value)


def test_contended_lock_names_the_holder(aws: None) -> None:
    backend = _backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"a", "b"})
    contended = backend.acquire_lock("bob", 30, {"b", "c"})
    assert not is_successful(contended)
    assert "alice" in str(contended.failure())


def test_partial_lock_is_rolled_back(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope too big for one transaction must not leave half the nodes held."""
    monkeypatch.setattr(s3_module, "_TRANSACT_MAX", 1)  # one node per transaction
    backend = _backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"c"})

    refused = backend.acquire_lock("bob", 30, {"a", "b", "c"})  # takes a, b, then loses c
    assert not is_successful(refused)
    # a and b are free again; c is still alice's.
    assert is_successful(backend.acquire_lock("carol", 30, {"a", "b"}))
    assert not is_successful(backend.acquire_lock("carol", 30, {"c"}))


def test_kms_key_is_used_when_configured(aws: None) -> None:
    backend = _backend(kms_key_id="alias/atlantide")
    backend.put(node("a"))
    head = _head()
    assert head["ServerSideEncryption"] == "aws:kms"
    assert head["SSEKMSKeyId"] == "alias/atlantide"


def test_default_encryption_is_aes256(aws: None) -> None:
    backend = _backend()
    backend.put(node("a"))
    assert _head()["ServerSideEncryption"] == "AES256"


def _writes(monkeypatch: pytest.MonkeyPatch, backend: S3StateBackend) -> list[int]:
    """Record one entry per PutObject the backend issues against the state key."""
    calls: list[int] = []
    original = backend._s3.put_object

    def counted(**kwargs: Any) -> Any:
        if kwargs.get("Key") == KEY:
            calls.append(1)
        return original(**kwargs)

    monkeypatch.setattr(backend._s3, "put_object", counted)
    return calls


def test_storing_an_unchanged_node_costs_no_request(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every write rewrites the whole document, so a no-op write is worth skipping."""
    backend = _backend()
    backend.put(node("a"))
    writes = _writes(monkeypatch, backend)
    backend.put(node("a"))
    assert writes == []
    backend.put(node("a", input_hash="changed"))
    assert len(writes) == 1


def test_put_many_is_one_request(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _backend()
    writes = _writes(monkeypatch, backend)
    backend.put_many([node("a"), node("b"), node("c")])
    assert len(writes) == 1
    assert set(backend.load().nodes) == {"a", "b", "c"}


def test_put_many_of_only_known_nodes_writes_nothing(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = _backend()
    backend.put_many([node("a"), node("b")])
    writes = _writes(monkeypatch, backend)
    backend.put_many([node("a"), node("b")])
    assert writes == []


def test_locks_are_listed_and_can_be_broken(aws: None) -> None:
    backend = _backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"a", "b"})
    held = backend.locks()
    assert set(held) == {"a", "b"}
    assert held["a"].owner == "alice"

    assert backend.force_unlock({"a"}) == 1
    assert set(backend.locks()) == {"b"}
    # 'a' is free for someone else even though alice never released it.
    assert is_successful(_backend().acquire_lock("bob", 30, {"a"}))


def test_check_reports_a_healthy_store(aws: None) -> None:
    results = {check.name: check for check in _backend().check()}
    assert results["bucket"].status == "ok"
    assert results["lock table"].status == "ok"


def test_check_warns_when_versioning_is_off(aws: None) -> None:
    versioning = next(c for c in _backend().check() if c.name == "bucket versioning")
    assert versioning.status == "warn"
    assert "put-bucket-versioning" in versioning.detail


def test_check_is_ok_once_versioning_is_on(aws: None) -> None:
    boto3.client("s3", region_name=REGION).put_bucket_versioning(
        Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"}
    )
    versioning = next(c for c in _backend().check() if c.name == "bucket versioning")
    assert versioning.status == "ok"


def test_check_warns_when_the_lock_table_has_no_ttl(aws: None) -> None:
    ttl = next(c for c in _backend().check() if c.name == "lock table TTL")
    assert ttl.status == "warn"
    assert "expires_at" in ttl.detail


def test_check_fails_on_a_missing_bucket(aws: None) -> None:
    backend = S3StateBackend("absent", KEY, lock_table=LOCK_TABLE, region=REGION)
    results = {check.name: check for check in backend.check()}
    assert results["bucket"].status == "fail"


def test_check_fails_on_a_missing_lock_table(aws: None) -> None:
    backend = S3StateBackend(BUCKET, KEY, lock_table="absent", region=REGION)
    table = next(c for c in backend.check() if c.name == "lock table")
    assert table.status == "fail"
    assert "node_id" in table.detail


def test_probe_confirms_conditional_writes_and_cleans_up(aws: None) -> None:
    backend = _backend()
    result = backend.probe()
    assert result.status == "ok"
    client: Any = boto3.client("s3", region_name=REGION)
    keys = {o["Key"] for o in client.list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert f"{KEY}.atlantide-probe" not in keys


def test_probe_fails_when_the_endpoint_ignores_preconditions(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An S3-compatible store that drops If-None-Match makes every CAS decorative."""
    backend = _backend()
    original = backend._s3.put_object
    monkeypatch.setattr(
        backend._s3,
        "put_object",
        lambda **kwargs: original(**{k: v for k, v in kwargs.items() if k != "IfNoneMatch"}),
    )
    result = backend.probe()
    assert result.status == "fail"
    assert "If-None-Match" in result.detail
