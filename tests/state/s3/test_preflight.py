"""`state check` and the conditional-write probe for the S3 backend."""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.state.s3 import S3StateBackend

from ..conftest import BUCKET, LOCK_TABLE, REGION
from .support import (
    KEY,
    ddb_client,
    new_backend,
    s3_client,
)


def test_check_reports_a_healthy_store(aws: None) -> None:
    results = _checks(new_backend())
    assert results["bucket"].status == "ok"
    assert results["lock table"].status == "ok"
    assert results["journal listing"].status == "ok"
    assert results["journal head reads"].status == "ok"


def test_check_warns_when_versioning_is_off(aws: None) -> None:
    versioning = _checks(new_backend())["bucket versioning"]
    assert versioning.status == "warn"
    assert "put-bucket-versioning" in versioning.detail


def test_check_is_ok_once_versioning_is_on(aws: None) -> None:
    s3_client().put_bucket_versioning(Bucket=BUCKET, VersioningConfiguration={"Status": "Enabled"})
    assert _checks(new_backend())["bucket versioning"].status == "ok"


def test_check_warns_until_a_lifecycle_rule_covers_the_journal(aws: None) -> None:
    assert _checks(new_backend())["journal lifecycle"].status == "warn"
    s3_client().put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "other",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "elsewhere/"},
                    "Expiration": {"Days": 1},
                },
                {
                    "ID": "off",
                    "Status": "Disabled",
                    "Filter": {"Prefix": ""},
                    "Expiration": {"Days": 1},
                },
            ]
        },
    )
    assert _checks(new_backend())["journal lifecycle"].status == "warn"
    s3_client().put_bucket_lifecycle_configuration(
        Bucket=BUCKET,
        LifecycleConfiguration={
            "Rules": [
                {
                    "ID": "journal",
                    "Status": "Enabled",
                    "Filter": {"Prefix": "prod/"},
                    "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
                }
            ]
        },
    )
    assert _checks(new_backend())["journal lifecycle"].status == "ok"


def test_check_warns_when_the_heads_table_has_no_pitr(aws: None) -> None:
    pitr = _checks(new_backend())["journal heads PITR"]
    assert pitr.status == "warn" and "update-continuous-backups" in pitr.detail
    ddb_client().update_continuous_backups(
        TableName=LOCK_TABLE,
        PointInTimeRecoverySpecification={"PointInTimeRecoveryEnabled": True},
    )
    assert _checks(new_backend())["journal heads PITR"].status == "ok"


def test_check_covers_a_separate_journal_table(aws: None) -> None:
    results = _checks(new_backend(journal_table="absent"))
    assert results["journal table"].status == "fail"
    assert results["lock table"].status == "ok"


def test_check_warns_when_the_lock_table_has_no_ttl(aws: None) -> None:
    ttl = _checks(new_backend())["lock table TTL"]
    assert ttl.status == "warn"
    assert "expires_at" in ttl.detail


def test_check_fails_on_a_missing_bucket(aws: None) -> None:
    backend = S3StateBackend("absent", KEY, lock_table=LOCK_TABLE, region=REGION)
    results = _checks(backend)
    assert results["bucket"].status == "fail"
    assert results["journal listing"].status == "fail"


def test_check_fails_on_a_missing_lock_table(aws: None) -> None:
    backend = S3StateBackend(BUCKET, KEY, lock_table="absent", region=REGION)
    table = _checks(backend)["lock table"]
    assert table.status == "fail"
    assert "node_id" in table.detail


def test_probe_confirms_conditional_writes_and_cleans_up(aws: None) -> None:
    result = new_backend().probe()
    assert result.status == "ok"
    assert "DynamoDB" in result.detail
    keys = {o["Key"] for o in s3_client().list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert f"{KEY}.atlantide-probe" not in keys
    assert ddb_client().scan(TableName=LOCK_TABLE)["Items"] == []


def test_probe_recovers_from_a_leftover_scratch_key(aws: None) -> None:
    s3_client().put_object(Bucket=BUCKET, Key=f"{KEY}.atlantide-probe", Body=b"stale")
    assert new_backend().probe().status == "ok"
    keys = {o["Key"] for o in s3_client().list_objects_v2(Bucket=BUCKET).get("Contents", [])}
    assert f"{KEY}.atlantide-probe" not in keys


def test_probe_fails_when_the_endpoint_ignores_preconditions(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    original = backend._s3.put_object
    monkeypatch.setattr(
        backend._s3,
        "put_object",
        lambda **kwargs: original(**{k: v for k, v in kwargs.items() if k != "IfNoneMatch"}),
    )
    result = backend.probe()
    assert result.status == "fail"
    assert "If-None-Match" in result.detail


def test_probe_fails_when_the_table_ignores_conditions(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    original = backend._ddb.update_item
    monkeypatch.setattr(
        backend._ddb,
        "update_item",
        lambda **kwargs: original(
            **{k: v for k, v in kwargs.items() if k != "ConditionExpression"}
        ),
    )
    result = backend.probe()
    assert result.status == "fail"
    assert "condition" in result.detail


def test_probe_fails_without_update_permission_on_the_heads(aws: None) -> None:
    result = new_backend(journal_table="absent").probe()
    assert result.status == "fail"
    assert "UpdateItem" in result.detail


def _checks(backend: S3StateBackend) -> dict[str, Any]:
    return {check.name: check for check in backend.check()}
