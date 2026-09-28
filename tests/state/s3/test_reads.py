"""Reads of S3 state: the snapshot, the journal LIST, heads, and restarts."""

from __future__ import annotations

import json
from typing import Any

import pytest

from atlantide.core.errors import StateError
from atlantide.state.codec import SNAPSHOT_VERSION
from atlantide.state.s3 import S3StateBackend, limits
from atlantide.state.s3.journal import Layout

from ..conftest import BUCKET, LOCK_TABLE, REGION, node
from .support import (
    KEY,
    before,
    new_backend,
    s3_client,
    spy,
)


def test_state_is_visible_to_a_second_process(aws: None) -> None:
    """Another run reads what this one wrote."""
    writer = new_backend()
    writer.put(node("a", input_hash="h1"))
    writer.set_outputs({"dev:url": "https://example.test"})

    reader = new_backend()  # a fresh instance = a different machine
    assert reader.load().get("a").input_hash == "h1"
    assert reader.outputs() == {"dev:url": "https://example.test"}
    assert reader.serial() == 2


def test_the_snapshot_is_canonical_json_at_the_current_version(aws: None) -> None:
    new_backend().put(node("b"))
    raw = s3_client().get_object(Bucket=BUCKET, Key=KEY)["Body"].read()
    assert raw == json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()
    assert json.loads(raw)["version"] == SNAPSHOT_VERSION


def test_an_older_state_format_is_refused_with_the_migration_path(aws: None) -> None:
    s3_client().put_object(
        Bucket=BUCKET, Key=KEY, Body=json.dumps({"version": 2, "serial": 1, "nodes": {}}).encode()
    )
    with pytest.raises(StateError, match="state migrate"):
        new_backend().load()


def test_a_read_creates_nothing(aws: None) -> None:
    """`plan` is read-only: reading an empty state must not create the snapshot."""
    assert new_backend().load().nodes == {}
    assert "Contents" not in s3_client().list_objects_v2(Bucket=BUCKET)


def test_missing_bucket_is_reported_with_a_hint(aws: None) -> None:
    backend = S3StateBackend("no-such-bucket", KEY, lock_table=LOCK_TABLE, region=REGION)
    with pytest.raises(StateError) as exc:
        backend.load()
    assert "does not exist" in str(exc.value)


def test_listing_is_paginated(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "LIST_PAGE", 2)
    writer = new_backend()
    for i in range(5):
        writer.put(node(f"n{i}"))
    reader = new_backend()
    lists = spy(monkeypatch, reader._s3, "list_objects_v2")
    assert set(reader.load().nodes) == {f"n{i}" for i in range(5)}
    assert len(lists) == 3


def test_a_compacted_state_is_read_without_touching_the_heads(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = new_backend()
    writer.put_many([node("a"), node("b")])
    writer.compact()
    reader = new_backend()
    batch_gets = spy(monkeypatch, reader._ddb, "batch_get_item")
    assert set(reader.load().nodes) == {"a", "b"}
    assert batch_gets == []


def test_an_unchanged_snapshot_is_revalidated_not_refetched(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    gets = spy(monkeypatch, backend._s3, "get_object")
    backend._views.view = None
    backend.load()
    assert any(call.get("Key") == KEY and "IfNoneMatch" in call for call in gets)


def test_a_head_committed_after_the_listing_forces_a_relist(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Causality: the reader sees `y`'s newest commit, which happened after its
    LIST; `x` was committed before that and must be visible too."""
    writer = new_backend()
    writer.put(node("y", input_hash="1"))  # y is dirty: its head will be read
    reader = new_backend()
    original = reader._reads.list_entries
    calls: list[int] = []

    def racing_list(layout: Layout) -> Any:
        listed = original(layout)
        if not calls:
            writer.put(node("x"))  # committed first...
            writer.put(node("y", input_hash="2"))  # ...then its dependent
        calls.append(1)
        return listed

    monkeypatch.setattr(reader._reads, "list_entries", racing_list)
    loaded = reader.load().nodes
    assert loaded["y"].input_hash == "2"
    assert "x" in loaded, "a dependent is never visible without its dependency"
    assert len(calls) == 2


def test_an_entry_compacted_away_mid_read_restarts_the_read(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = new_backend()
    writer.put(node("a"))
    reader = new_backend()
    before(monkeypatch, reader._reads, "get_entry", lambda: new_backend().compact())
    snapshots = spy(monkeypatch, reader._s3, "head_object")
    # The first fetch meets a compacted (deleted) entry; the read restarts from
    # the new snapshot, which now holds the node.
    assert reader.load().nodes["a"] == node("a")
    assert len(snapshots) == 1, "only the restarted read got as far as its final check"


def test_a_snapshot_replaced_mid_read_restarts_the_read(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer = new_backend()
    writer.put(node("a"))
    reader = new_backend()
    original = reader._snapshots.etag
    fired: list[bool] = []

    def bulk_first() -> Any:
        if not fired:
            fired.append(True)
            new_backend().put_many([node("b"), node("c")])
        return original()

    monkeypatch.setattr(reader._snapshots, "etag", bulk_first)
    assert set(reader.load().nodes) == {"a", "b", "c"}


def test_a_read_that_never_settles_fails_loudly(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    new_backend().put(node("a"))
    reader = new_backend()
    monkeypatch.setattr(reader._snapshots, "etag", lambda: "moving")
    with pytest.raises(StateError, match="kept being rewritten"):
        reader.load()


def test_the_backend_repr_names_the_state_and_its_tables(aws: None) -> None:
    backend = new_backend(journal_table="heads")
    assert repr(backend) == (
        f"S3StateBackend('s3://{BUCKET}/{KEY}', lock_table={LOCK_TABLE!r}, journal_table='heads')"
    )


def test_the_view_repr_holds_counts_not_values(aws: None) -> None:
    backend = new_backend()
    backend.put(node("a", outputs={"password": "hunter2"}))
    view = backend._views.ensure()
    assert "hunter2" not in repr(view)
    assert repr(view) == f"View(epoch={view.epoch!r}, serial=1, nodes=1, outputs=0)"
