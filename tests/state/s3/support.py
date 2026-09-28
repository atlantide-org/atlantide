"""Helpers shared by the S3 backend tests: a backend on the mocked store, raw
views of what it stored, and call spies."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import boto3
import pytest

from atlantide.state.codec import (
    EntryKind,
    decode,
)
from atlantide.state.leases import DEFAULT_SKEW_MARGIN, Lease
from atlantide.state.s3 import S3StateBackend
from atlantide.state.s3.journal import Head, Layout, head_of_item

from ..conftest import BUCKET, LOCK_TABLE, REGION
from .harness import CountingClient

KEY = "prod/atlantide.json"
TAKEOVER = 60.0 + DEFAULT_SKEW_MARGIN + 1.0


def new_backend(**kwargs: Any) -> S3StateBackend:
    kwargs.setdefault("lock_table", LOCK_TABLE)
    kwargs.setdefault("region", REGION)
    return S3StateBackend(BUCKET, KEY, **kwargs)


def s3_client() -> Any:
    return boto3.client("s3", region_name=REGION)


def ddb_client() -> Any:
    return boto3.client("dynamodb", region_name=REGION)


def stored_snapshot() -> Any:
    """The snapshot object as stored."""
    return decode(s3_client().get_object(Bucket=BUCKET, Key=KEY)["Body"].read())


def journal_layout() -> Layout:
    return Layout(KEY, f"s3://{BUCKET}/{KEY}", stored_snapshot().epoch)


def journal_keys() -> list[str]:
    listed = s3_client().list_objects_v2(Bucket=BUCKET, Prefix=f"{KEY}.d/").get("Contents", [])
    return sorted(obj["Key"] for obj in listed)


def head_of(node_id: str, kind: str = EntryKind.NODE, table: str = LOCK_TABLE) -> Head:
    key = journal_layout().head_key(kind, node_id)
    item = ddb_client().get_item(TableName=table, Key={"node_id": {"S": key}}, ConsistentRead=True)
    return head_of_item(item.get("Item"))


def bound_lease(backend: S3StateBackend, owner: str, scope: set[str], ttl: float = 60.0) -> Lease:
    lease = backend.acquire_lock(owner, ttl, frozenset(scope)).unwrap()
    backend.bind_lease(lease)
    return lease


def spy(monkeypatch: pytest.MonkeyPatch, client: Any, method: str) -> list[dict[str, Any]]:
    """Record the kwargs of every ``method`` call on ``client``."""
    calls: list[dict[str, Any]] = []
    original = getattr(client, method)

    def recorded(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(**kwargs)

    monkeypatch.setattr(client, method, recorded)
    return calls


def before(
    monkeypatch: pytest.MonkeyPatch, client: Any, method: str, hook: Callable[[], None]
) -> None:
    """Run ``hook`` once, just before the first ``method`` call on ``client``."""
    original = getattr(client, method)
    fired: list[bool] = []

    def hooked(*args: Any, **kwargs: Any) -> Any:
        if not fired:
            fired.append(True)
            hook()
        return original(*args, **kwargs)

    monkeypatch.setattr(client, method, hooked)


def spy_calls(monkeypatch: pytest.MonkeyPatch, target: Any, method: str) -> list[Any]:
    calls: list[Any] = []
    original = getattr(target, method)

    def recorded(*args: Any) -> Any:
        calls.append(args)
        return original(*args)

    monkeypatch.setattr(target, method, recorded)
    return calls


def lock_items() -> list[dict[str, Any]]:
    return ddb_client().scan(TableName=LOCK_TABLE, ConsistentRead=True)["Items"]


def lease_items() -> list[dict[str, Any]]:
    return [item for item in lock_items() if "lease_ns" in item]


def lock_rows() -> dict[str, dict[str, Any]]:
    return {item["node"]["S"]: item for item in lock_items() if "namespace" in item}


def counting(backend: S3StateBackend) -> list[str]:
    calls: list[str] = []
    backend._s3 = CountingClient(backend._s3, calls)
    backend._ddb = CountingClient(backend._ddb, calls)
    return calls
