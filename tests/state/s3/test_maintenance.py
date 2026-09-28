"""Compaction and fsck of the S3 journal."""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest

from atlantide.core.errors import FencedWriteError, StateError
from atlantide.state.codec import (
    EntryKind,
)
from atlantide.state.s3.maintenance import CompactionReport
from tests.support import FakeClock

from ..conftest import BUCKET, LOCK_TABLE, node
from .support import (
    TAKEOVER,
    before,
    bound_lease,
    ddb_client,
    head_of,
    journal_keys,
    journal_layout,
    new_backend,
    s3_client,
    stored_snapshot,
)


def test_compaction_folds_the_journal_and_keeps_the_serial(aws: None) -> None:
    backend = new_backend()
    bound_lease(backend, "me", {"a", "b"})
    backend.put(node("a"))
    backend.put(node("b"))
    backend.put(node("a", input_hash="2"))
    backend.set_outputs({"dev:url": "u"})
    assert backend.serial() == 4

    report = backend.compact()
    stored = stored_snapshot()
    assert report.folded == 3 and report.deleted == 4
    assert stored.serial == 4 and stored.gen == 1
    assert stored.wm == {"a": 2, "b": 1} and stored.owm == {"dev": 1}
    assert stored.nodes["a"].input_hash == "2" and stored.outputs == {"dev:url": "u"}
    assert stored.fences["a"] == backend._lease.fence  # type: ignore[union-attr]
    assert journal_keys() == []
    assert new_backend().serial() == 4

    backend.put(node("b", input_hash="2"))  # writes continue above the watermark
    assert head_of("b").seq == 2
    assert new_backend().serial() == 5


def test_compaction_keeps_pending_entries_and_collects_refused_ones(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a", "p"})
    run_a.put(node("a"))
    run_a.put(node("p"))
    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    with pytest.raises(FencedWriteError):
        run_a.put(node("a", input_hash="stale"))  # refused: orphan at seq 2 > head 1
    refused = [k for k in journal_keys() if "/log/a/000000000002-" in k]

    # A writer paused between its entry PUT and its commit: a pending entry.
    monkeypatch.setattr(
        run_a._writes, "commit", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        run_a.put(node("p", input_hash="pending"))
    pending = [k for k in journal_keys() if "/log/p/000000000002-" in k]
    assert len(refused) == 1 and len(pending) == 1

    new_backend().compact()
    assert refused[0] in journal_keys(), "seq 2 is above the head (1): pending-shaped, kept"
    assert pending[0] in journal_keys(), "a pending entry is never collected"
    run_b.put(node("a", input_hash="from-b"))  # head moves to 2 (a different key)
    new_backend().compact()
    assert refused[0] not in journal_keys(), "now at or below the head: collected"
    assert pending[0] in journal_keys()
    assert new_backend().load().nodes["p"] == node("p")


def test_a_lost_compaction_race_changes_and_deletes_nothing(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    before(monkeypatch, backend._s3, "put_object", lambda: new_backend().compact())
    report = backend.compact()
    assert report.skipped
    assert new_backend().load().nodes["a"] == node("a")


def test_compaction_of_an_empty_state_is_a_no_op(aws: None) -> None:
    assert new_backend().compact() == CompactionReport()


def test_leftovers_from_a_crashed_compaction_are_deleted_next_time(aws: None) -> None:
    backend = new_backend()
    backend.put(node("a"))
    leftover = journal_keys()
    folded = new_backend()._reads.cut().fold()  # type: ignore[union-attr]
    snap = new_backend()._snapshots.ensure()
    backend._snapshots.put(folded, snap.etag)  # the PUT landed, the deletes did not
    assert journal_keys() == leftover
    report = new_backend().compact()
    assert report.deleted == 1 and journal_keys() == []
    assert new_backend().load().nodes["a"] == node("a")


def test_checkpoint_is_best_effort(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = new_backend()
    backend.put(node("a"))

    def broken() -> Any:
        raise StateError("s3 is down")

    monkeypatch.setattr(backend, "compact", broken)
    backend.checkpoint()  # logs, does not raise


def test_compaction_runs_in_the_background_every_k_commits(aws: None) -> None:
    backend = new_backend(compact_every=3)
    done = threading.Event()
    original = backend.checkpoint

    def signalled() -> None:
        original()
        done.set()

    backend.checkpoint = signalled  # type: ignore[method-assign]
    for i in range(3):
        backend.put(node(f"n{i}"))
    assert done.wait(10.0)
    backend.close()
    assert stored_snapshot().gen == 1


def test_fsck_reports_a_healthy_journal(aws: None) -> None:
    backend = new_backend()
    backend.put(node("a"))
    backend.put(node("a", input_hash="2"))
    report = backend.fsck()
    assert report.healthy and report.heads == 1 and report.entries == 2
    assert report.collectable == 1


def test_fsck_reports_and_rebuilds_a_lost_head(aws: None) -> None:
    backend = new_backend()
    lease = bound_lease(backend, "me", {"a"})
    backend.put(node("a", input_hash="lost"))
    ddb_client().delete_item(
        TableName=LOCK_TABLE, Key={"node_id": {"S": journal_layout().head_key(EntryKind.NODE, "a")}}
    )
    assert "a" not in new_backend().load().nodes, "the commit is lost with its head"

    report = new_backend().fsck()
    assert not report.healthy and report.headless == ["a"]
    rebuilt = new_backend().fsck(rebuild_heads=True)
    assert [name for name, _ in rebuilt.rebuilt] == ["a"]
    assert head_of("a").fence == lease.fence
    assert new_backend().load().nodes["a"].input_hash == "lost"
    assert new_backend().fsck().healthy


def test_fsck_reports_a_head_whose_entry_is_missing(aws: None) -> None:
    backend = new_backend()
    backend.put(node("a"))
    (entry,) = journal_keys()
    s3_client().delete_object(Bucket=BUCKET, Key=entry)
    report = backend.fsck()
    assert report.missing == [("a", entry)]
    assert not report.healthy


def test_fsck_counts_pending_entries(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = new_backend()
    backend.put(node("a"))
    monkeypatch.setattr(
        backend._writes, "commit", lambda *args: (_ for _ in ()).throw(KeyboardInterrupt)
    )
    with pytest.raises(KeyboardInterrupt):
        backend.put(node("a", input_hash="2"))
    assert len(new_backend().fsck().pending) == 1


def test_fsck_of_an_empty_state(aws: None) -> None:
    assert new_backend().fsck().healthy


def test_a_deferred_compaction_is_logged_under_the_backends_fixed_name(
    aws: None, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Operators filter on this logger; its name must not follow the module path."""
    backend = new_backend()

    def broken() -> Any:
        raise StateError("s3 is down")

    monkeypatch.setattr(backend, "compact", broken)
    with caplog.at_level(logging.WARNING, logger="atlantide.state.s3_backend"):
        backend.checkpoint()
    (record,) = caplog.records
    assert record.name == "atlantide.state.s3_backend"
    assert "deferred: s3 is down" in record.getMessage()
