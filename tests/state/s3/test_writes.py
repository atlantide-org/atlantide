"""Node writes on S3: journal entries, head commits and their fences, bulk writes."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from atlantide.core import is_successful
from atlantide.core.errors import FencedWriteError, StateError
from atlantide.state.codec import SNAPSHOT_VERSION, StateDocument, dumps, loads
from atlantide.state.leases import Lease
from atlantide.state.s3 import limits
from atlantide.state.s3.journal import Head
from atlantide.state.s3.writes import stamp
from tests.support import FakeClock

from ..conftest import BUCKET, node
from .support import (
    KEY,
    TAKEOVER,
    before,
    bound_lease,
    ddb_client,
    head_of,
    journal_keys,
    new_backend,
    s3_client,
    spy,
    spy_calls,
    stored_snapshot,
)


def test_a_write_is_one_entry_and_one_head_commit(aws: None) -> None:
    backend = new_backend()
    lease = bound_lease(backend, "me", {"a"})
    backend.put(node("a"))

    (entry,) = journal_keys()
    assert entry.startswith(f"{KEY}.d/{stored_snapshot().epoch}/log/a/000000000001-{lease.fence}-")
    assert head_of("a") == Head(fence=lease.fence, seq=1, ref=entry, op="put")
    assert stored_snapshot().nodes == {}, "the snapshot is untouched until a fold"


def test_kms_key_is_used_for_snapshot_and_entries(aws: None) -> None:
    backend = new_backend(kms_key_id="alias/atlantide")
    backend.put(node("a"))
    for key in (KEY, *journal_keys()):
        head = s3_client().head_object(Bucket=BUCKET, Key=key)
        assert head["ServerSideEncryption"] == "aws:kms"
        assert head["SSEKMSKeyId"] == "alias/atlantide"


def test_default_encryption_is_aes256(aws: None) -> None:
    new_backend().put(node("a"))
    for key in (KEY, *journal_keys()):
        assert s3_client().head_object(Bucket=BUCKET, Key=key)["ServerSideEncryption"] == "AES256"


def test_storing_an_unchanged_node_costs_no_request(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    puts = spy(monkeypatch, backend._s3, "put_object")
    backend.put(node("a"))
    backend.delete("absent")
    assert puts == []
    backend.put(node("a", input_hash="changed"))
    assert len(puts) == 1


def test_write_concurrency_is_configurable_and_validated(aws: None) -> None:
    assert new_backend().write_concurrency == limits.DEFAULT_WRITE_CONCURRENCY
    assert new_backend(write_concurrency=4).write_concurrency == 4
    with pytest.raises(StateError, match="write_concurrency"):
        new_backend(write_concurrency=0)


def test_concurrent_writes_to_different_nodes_all_land(aws: None) -> None:
    """P concurrent writers on one backend lose no update."""
    backend = new_backend()
    lease = bound_lease(backend, "me", {f"n{i}" for i in range(40)}, ttl=600.0)
    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda i: backend.put(node(f"n{i}")), range(40)))
    assert backend.serial() == 40
    reader = new_backend()
    assert set(reader.load().nodes) == {f"n{i}" for i in range(40)}
    assert reader.serial() == 40
    assert head_of("n7").fence == lease.fence


def test_a_commit_whose_response_was_lost_is_recognised_as_ours(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retried UpdateItem after a lost response fails its own condition; the
    head then already points at our entry, which is success, not a conflict."""
    backend = new_backend()
    bound_lease(backend, "me", {"a"})
    original = backend._ddb.update_item

    def lost_response(**kwargs: Any) -> Any:
        original(**kwargs)  # lands...
        return original(**kwargs)  # ...and the "retry" meets its own commit

    monkeypatch.setattr(backend._ddb, "update_item", lost_response)
    backend.put(node("a"))
    assert head_of("a").seq == 1
    assert new_backend().load().nodes["a"] == node("a")


def test_a_failed_condition_without_the_old_item_reads_the_head(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fallback for endpoints that do not return ALL_OLD on a failed condition."""
    backend = new_backend()
    bound_lease(backend, "me", {"a"})
    original = backend._ddb.update_item

    def no_item(**kwargs: Any) -> Any:
        try:
            return original(**kwargs)
        except ClientError as exc:
            exc.response.pop("Item", None)
            raise

    monkeypatch.setattr(backend._ddb, "update_item", no_item)
    new_backend().acquire_lock("me", 60.0, frozenset({"a"})).unwrap()  # newer fence
    with pytest.raises(FencedWriteError, match="superseded"):
        backend.put(node("a"))


def test_a_stale_unfenced_writer_rebases_onto_the_head(aws: None) -> None:
    """Two unlocked writers of one node: the stale one rebases, nothing is lost."""
    first, second = new_backend(), new_backend()
    first.put(node("a", input_hash="1"))
    second.load()
    first.put(node("a", input_hash="2"))
    second.put(node("a", input_hash="3"))  # its view says seq 1; the head is at 2
    assert head_of("a").seq == 3
    assert new_backend().load().nodes["a"].input_hash == "3"
    assert new_backend().serial() == 3


def test_a_head_that_never_stops_moving_surfaces(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    monkeypatch.setattr(backend._writes, "commit", lambda *args: 99)
    with pytest.raises(StateError, match="kept moving"):
        backend.put(node("a", input_hash="x"))


def test_a_lapsed_lease_is_refused_once_another_run_takes_over(aws: None) -> None:
    """Two machines on one state: A's lease lapses, B takes it, A writes late."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    run_a.put(node("a", input_hash="from-a"))

    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"a"})
    run_b.put(node("a", input_hash="from-b"))

    with pytest.raises(FencedWriteError, match="run-b"):
        run_a.put(node("a", input_hash="stale-from-a"))
    assert new_backend().load().nodes["a"].input_hash == "from-b"


def test_a_takeover_between_entry_and_commit_is_refused(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race a lock-table read cannot close: A stores its entry, B takes the
    lease (raising the head's fence), *then* A commits. The commit is refused
    atomically and A's entry is left pending, never visible."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    run_a.put(node("a", input_hash="from-a"))
    clock.advance(TAKEOVER)

    before(monkeypatch, run_a._ddb, "update_item", lambda: bound_lease(run_b, "run-b", {"a"}))
    with pytest.raises(FencedWriteError, match="superseded"):
        run_a.put(node("a", input_hash="stale-from-a"))
    assert new_backend().load().nodes["a"].input_hash == "from-a"
    assert len(journal_keys()) == 2, "the refused entry is an orphan"
    assert head_of("a").fence > run_a._lease.fence  # type: ignore[union-attr]


def test_a_commit_before_the_takeover_is_seen_by_the_new_holder(aws: None) -> None:
    """The other ordering: A's write linearizes before B's grant, so it stands."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    clock.advance(TAKEOVER)
    run_a.put(node("a", input_hash="from-a"))  # lease lapsed, but not yet taken
    bound_lease(run_b, "run-b", {"a"})
    assert run_b.load().nodes["a"].input_hash == "from-a"


def test_a_fence_that_was_never_recorded_is_refused(aws: None) -> None:
    backend = new_backend()
    backend.bind_lease(Lease(owner="forged", expires_at=1e12, scope=frozenset({"a"}), fence=10**9))
    with pytest.raises(FencedWriteError, match="never recorded"):
        backend.put(node("a"))


def test_the_same_owner_reacquiring_supersedes_its_old_lease(aws: None) -> None:
    backend = new_backend()
    first = bound_lease(backend, "me", {"a"})
    new_backend().acquire_lock("me", 60.0, frozenset({"a"})).unwrap()
    backend.bind_lease(first)
    with pytest.raises(FencedWriteError, match="taken by the same owner"):
        backend.put(node("a"))


def test_fencing_does_not_depend_on_reading_the_lock_table(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The heads decide whether a write lands, so a lock table that keeps
    throttling reads neither blocks a legitimate write nor waves a stale one
    through."""
    backend = new_backend()
    bound_lease(backend, "me", {"a"})

    def throttled(**kwargs: Any) -> Any:
        return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}

    backend.load()
    monkeypatch.setattr(backend._ddb, "batch_get_item", throttled)
    backend.put(node("a"))  # no lock-table read on the write path
    assert "a" in new_backend().load().nodes

    assert is_successful(new_backend().acquire_lock("me", 300.0, frozenset({"a"})))  # newer fence
    with pytest.raises(StateError, match="unprocessed"):  # naming the holder fails closed
        backend.put(node("a", input_hash="stale"))
    assert new_backend().load().nodes["a"].input_hash == "h0"


def test_heads_can_live_in_a_separate_journal_table(aws: None) -> None:
    ddb_client().create_table(
        TableName="heads",
        KeySchema=[{"AttributeName": "node_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "node_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    backend = new_backend(journal_table="heads")
    lease = bound_lease(backend, "me", {"a"})
    backend.put(node("a"))
    assert head_of("a", table="heads") == Head(lease.fence, 1, journal_keys()[0], "put")
    assert head_of("a") == Head(), "nothing but lock rows in the lock table"
    assert new_backend(journal_table="heads").load().nodes["a"] == node("a")


def test_every_commit_adds_one_and_a_bulk_write_adds_one(aws: None) -> None:
    backend = new_backend()
    backend.put(node("a"))
    backend.put(node("b"))
    backend.delete("a")
    assert new_backend().serial() == 3
    backend.put_many([node("c"), node("d"), node("e")])
    assert backend.serial() == 4 and new_backend().serial() == 4
    backend.compact()
    assert new_backend().serial() == 4


def test_a_bulk_write_is_one_snapshot_write_that_folds_the_journal(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    puts = spy(monkeypatch, backend._s3, "put_object")
    backend.replace_many(["a"], [node("b"), node("c")])
    assert [call["Key"] for call in puts] == [KEY]
    stored = stored_snapshot()
    assert set(stored.nodes) == {"b", "c"} and stored.wm == {"a": 1}
    assert journal_keys() == [], "the folded entry was collected"
    assert set(new_backend().load().nodes) == {"b", "c"}


def test_a_bulk_write_of_one_change_is_an_ordinary_commit(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put_many([node("a"), node("b")])
    puts = spy(monkeypatch, backend._s3, "put_object")
    backend.put_many([node("a"), node("b", input_hash="2")])
    assert [call["Key"].startswith(f"{KEY}.d/") for call in puts] == [True]
    backend.put_many([node("a"), node("b", input_hash="2")])
    assert len(puts) == 1, "nothing changed, nothing written"


def test_a_bulk_write_is_refused_when_a_fence_moved_before_its_swap(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-check: a takeover between the bulk read and its PUT is caught by
    re-reading the heads just before the swap."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a", "b"})
    clock.advance(TAKEOVER)
    before(monkeypatch, run_a._writes, "heads_unmoved", lambda: bound_lease(run_b, "run-b", {"b"}))
    with pytest.raises(FencedWriteError, match="run-b"):
        run_a.put_many([node("a"), node("b")])
    assert new_backend().load().nodes == {}


def test_a_bulk_write_rebases_over_a_commit_that_raced_it(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    before(
        monkeypatch,
        backend._writes,
        "heads_unmoved",
        lambda: new_backend().put(node("a", input_hash="2")),
    )
    backend.put_many([node("b"), node("c")])
    loaded = new_backend().load().nodes
    assert loaded["a"].input_hash == "2" and {"b", "c"} <= set(loaded)


def test_a_bulk_write_rebases_when_a_touched_head_moved(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit to a node the bulk write touches, landing between its read and
    its swap, sends it round again — and the later bulk write then wins."""
    backend = new_backend()
    backend.put(node("a"))
    before(
        monkeypatch,
        backend._writes,
        "heads_unmoved",
        lambda: new_backend().put(node("a", input_hash="2")),
    )
    heads_checks = spy_calls(monkeypatch, backend._writes, "heads_unmoved")
    backend.put_many([node("a", input_hash="bulk"), node("b")])
    assert len(heads_checks) == 2
    assert new_backend().load().nodes["a"].input_hash == "bulk"
    assert new_backend().serial() == 3


def test_a_bulk_write_that_loses_its_swap_rebases(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("a"))
    before(monkeypatch, backend._s3, "put_object", lambda: new_backend().compact())
    backend.put_many([node("b"), node("c")])
    assert set(new_backend().load().nodes) == {"a", "b", "c"}


def test_a_bulk_write_that_turns_out_to_change_nothing_writes_nothing(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view said two nodes changed; the store already had them."""
    backend = new_backend()
    backend.load()
    new_backend().put_many([node("a"), node("b")])
    puts = spy(monkeypatch, backend._s3, "put_object")
    backend.put_many([node("a"), node("b")])
    assert puts == []
    assert set(backend.load().nodes) == {"a", "b"}


def test_a_bulk_write_that_keeps_losing_surfaces(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    monkeypatch.setattr(backend._writes, "heads_unmoved", lambda *args: False)
    with pytest.raises(StateError, match="kept changing"):
        backend.put_many([node("a"), node("b")])


def test_a_bulk_write_carries_every_field_of_the_fold_but_serial_and_nodes(aws: None) -> None:
    """``bulk`` builds the next snapshot as ``replace(folded, ...)``. That equals
    a field-by-field copy onto a fresh document because the one field such a
    copy would reset, ``version``, is always :data:`SNAPSHOT_VERSION` on a
    fold: ``loads`` reads no other format."""
    clock = FakeClock()
    backend = new_backend(clock=clock)
    bound_lease(backend, "me", {"a", "b"})
    backend.put(node("a"))
    backend.set_outputs({"dev:url": "u"})
    backend.compact()
    backend.put(node("b"))

    cut = backend._reads.cut()
    assert cut is not None
    folded = cut.fold()
    # Every bookkeeping field is set, so the comparison below is not vacuous.
    assert folded.fences and folded.max_fence and folded.wm and folded.owm
    assert folded.gen and folded.epoch
    assert folded.version == SNAPSHOT_VERSION

    nodes = {"c": node("c")}
    assert replace(folded, serial=folded.serial + 1, nodes=nodes) == StateDocument(
        serial=folded.serial + 1,
        nodes=nodes,
        outputs=folded.outputs,
        fences=folded.fences,
        max_fence=folded.max_fence,
        wm=folded.wm,
        owm=folded.owm,
        gen=folded.gen,
        epoch=folded.epoch,
    )
    with pytest.raises(StateError, match="format version"):
        loads(dumps(replace(folded, version=SNAPSHOT_VERSION - 1)))


def test_an_entry_records_the_lease_it_was_written_under() -> None:
    lease = Lease(owner="run-a", expires_at=1.0, scope=frozenset({"a"}), fence=7)
    assert stamp(lease) == (7, "run-a")
    assert stamp(None) == (0, ""), "an unfenced write records no lease"
