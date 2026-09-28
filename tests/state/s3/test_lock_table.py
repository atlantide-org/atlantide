"""Leases in the DynamoDB lock table: acquire, renew, takeover, release, skew."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from botocore.exceptions import ClientError

from atlantide.core import is_successful
from atlantide.core.errors import StateError
from atlantide.state.s3 import S3StateBackend, limits
from atlantide.state.s3.fences import FENCE_ITEM
from tests.support import FakeClock

from ..conftest import BUCKET, LOCK_TABLE, REGION, node
from .harness import in_thread, joined, stepping
from .support import (
    KEY,
    TAKEOVER,
    bound_lease,
    counting,
    ddb_client,
    head_of,
    lease_items,
    lock_rows,
    new_backend,
    spy,
    stored_snapshot,
)


def test_missing_lock_table_is_reported_with_a_hint(aws: None) -> None:
    backend = new_backend(lock_table="no-such-table")
    with pytest.raises(StateError) as exc:
        backend.acquire_lock("alice", 30, {"a"})
    assert "node_id" in str(exc.value)


def test_a_renewal_keeps_the_fence_and_writes_no_object(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    lease = bound_lease(backend, "me", {"a"})
    puts = spy(monkeypatch, backend._s3, "put_object")
    renewed = backend.renew_lock("me", 60.0, frozenset({"a"})).unwrap()
    assert renewed.fence == lease.fence
    assert puts == []
    backend.bind_lease(renewed)
    backend.put(node("a"))


def test_a_renewal_after_being_superseded_fails(aws: None) -> None:
    """Lapsed, taken, released — the lock rows then look free, but the head
    remembers the newer lease, and the renewal must report the loss."""
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a"})
    clock.advance(TAKEOVER)
    run_b.acquire_lock("run-b", 60.0, frozenset({"a"})).unwrap()
    run_b.release_lock("run-b")

    renewed = run_a.renew_lock("run-a", 60.0, frozenset({"a"}))
    assert not is_successful(renewed)
    assert "newer lease" in str(renewed.failure())
    assert run_a.locks() == {}, "a failed renewal takes nothing"


def test_an_unbound_renewal_mints_and_raises_a_fence(aws: None) -> None:
    backend = new_backend()
    lease = backend.renew_lock("me", 60.0, frozenset({"a"})).unwrap()
    assert head_of("a").fence == lease.fence


def test_an_acquire_refused_at_a_head_releases_its_rows(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.acquire_lock("old", 60.0, frozenset({"a"})).unwrap()
    backend.release_lock("old")
    monkeypatch.setattr(backend._fences, "next_fence", lambda *, above: 0)  # below the head
    refused = backend.acquire_lock("me", 60.0, frozenset({"a", "b"}))
    assert not is_successful(refused)
    assert "newer lease" in str(refused.failure())
    assert backend.locks() == {}, "no half-taken scope is left behind"


def test_a_recreated_lock_table_reseeds_fences_far_above_the_snapshot(aws: None) -> None:
    """Fences minted after the last fold are unknown once the counter is gone;
    a paused run may hold one, so the counter restarts well past them."""
    backend = new_backend()
    first = backend.acquire_lock("me", 60.0, frozenset({"a"})).unwrap()
    backend.release_lock("me")
    backend.compact()  # records max_fence in the snapshot
    assert stored_snapshot().max_fence >= first.fence
    ddb_client().delete_item(TableName=LOCK_TABLE, Key={"node_id": {"S": FENCE_ITEM}})
    second = bound_lease(backend, "me", {"a"})
    assert second.fence > first.fence + limits.RESEED_GAP - 1
    backend.put(node("a"))


def test_contended_lock_names_the_holder(aws: None) -> None:
    backend = new_backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"a", "b"})
    contended = backend.acquire_lock("bob", 30, {"b", "c"})
    assert not is_successful(contended)
    assert "alice" in str(contended.failure())


def test_partial_lock_is_rolled_back(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope too big for one transaction must not leave half the nodes held."""
    monkeypatch.setattr(limits, "TRANSACT_MAX", 1)  # one node per transaction
    backend = new_backend(clock=FakeClock())
    backend.acquire_lock("alice", 30, {"c"})

    refused = backend.acquire_lock("bob", 30, {"a", "b", "c"})  # takes a, b, then loses c
    assert not is_successful(refused)
    assert is_successful(backend.acquire_lock("carol", 30, {"a", "b"}))
    assert not is_successful(backend.acquire_lock("carol", 30, {"c"}))


def test_a_transient_transaction_cancel_during_an_acquire_is_retried(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    original = backend._ddb.transact_write_items
    calls = {"n": 0}

    def flaky(**kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "TransactionConflict"}],
                },
                "TransactWriteItems",
            )
        return original(**kwargs)

    monkeypatch.setattr(backend._ddb, "transact_write_items", flaky)
    assert is_successful(backend.acquire_lock("me", 300.0, frozenset({"a"})))
    assert calls["n"] == 2


def test_a_transaction_cancelled_for_transient_reasons_every_time_is_contention(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()

    def conflicted(**kwargs: Any) -> Any:
        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "TransactionConflict"}],
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(backend._ddb, "transact_write_items", conflicted)
    refused = backend.acquire_lock("me", 300.0, frozenset({"a"}))
    assert "contended" in str(refused.failure())
    assert lease_items() == [], "the failed acquire deleted its lease item"


def test_a_throttled_acquire_names_the_cancellation_reason(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Throttling is not another run: the refusal carries DynamoDB's reason codes."""
    backend = new_backend()

    def throttled(**kwargs: Any) -> Any:
        raise ClientError(
            {
                "Error": {"Code": "TransactionCanceledException"},
                "CancellationReasons": [{"Code": "None"}, {"Code": "ThrottlingError"}],
            },
            "TransactWriteItems",
        )

    monkeypatch.setattr(backend._ddb, "transact_write_items", throttled)
    message = str(backend.acquire_lock("me", 300.0, frozenset({"a"})).failure())
    assert "(ThrottlingError)" in message
    assert "another run" not in message


def test_a_renewal_the_store_cannot_answer_raises_rather_than_reporting_a_loss(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed renewal means the hold is gone; an unreachable store is not that."""
    backend = new_backend()
    bound_lease(backend, "me", {"a"})

    def throttled(**kwargs: Any) -> Any:
        raise ClientError({"Error": {"Code": "ThrottlingException"}}, "UpdateItem")

    monkeypatch.setattr(backend._ddb, "update_item", throttled)
    with pytest.raises(StateError, match="Throttling"):
        backend.renew_lock("me", 300.0, frozenset({"a"}))


def test_locks_are_listed_and_heads_are_never_reported_as_locks(aws: None) -> None:
    backend = new_backend(clock=FakeClock())
    bound_lease(backend, "alice", {"a", "b"})
    backend.put(node("a"))
    held = backend.locks()
    assert set(held) == {"a", "b"}
    assert backend.force_unlock({"a"}) == 1
    assert set(backend.locks()) == {"b"}
    assert head_of("a").seq == 1, "breaking a lock never touches the head"
    assert is_successful(new_backend().acquire_lock("bob", 30, {"a"}))


def test_acquiring_a_lock_drops_a_stale_view(aws: None) -> None:
    backend = new_backend()
    backend.load()
    new_backend().put(node("a"))
    assert is_successful(backend.acquire_lock("me", 300.0, frozenset({"a"})))
    assert set(backend.load().nodes) == {"a"}, "the pre-lock view was discarded"


@pytest.mark.parametrize("size", [1, 120])
def test_a_renewal_is_one_dynamodb_call_whatever_the_scope(aws: None, size: int) -> None:
    backend = new_backend()
    scope = {f"n{i:03d}" for i in range(size)}
    lease = bound_lease(backend, "me", scope)
    calls = counting(backend)
    renewed = backend.renew_lock("me", 60.0, frozenset(scope)).unwrap()
    assert calls == ["update_item"], "one UpdateItem of the lease item, nothing else"
    assert renewed.fence == lease.fence
    (item,) = lease_items()
    assert float(item["lease_expires_at"]["N"]) == renewed.expires_at


def test_an_acquire_is_one_lease_item_and_rows_pointing_at_it(aws: None) -> None:
    backend = new_backend()
    lease = backend.acquire_lock("me", 60.0, frozenset({"a", "b"})).unwrap()
    (item,) = lease_items()
    assert item["owner"]["S"] == "me"
    assert int(item["fence"]["N"]) == lease.fence
    rows = lock_rows()
    assert set(rows) == {"a", "b"}
    assert {row["lease_id"]["S"] for row in rows.values()} == {item["lease_id"]["S"]}


def test_lock_rows_are_never_reaped_while_their_lease_lives(aws: None) -> None:
    """The table's TTL is on ``expires_at``. Rows have none, so a long run's rows
    are never deleted under it; the lease item's is set a skew margin past its
    expiry and pushed out by every renewal, so the reaper frees nothing a taker
    could not."""
    clock = FakeClock()
    backend = new_backend(clock=clock, lock_skew_margin=30.0)
    bound_lease(backend, "me", {"a", "b"})
    assert all("expires_at" not in row for row in lock_rows().values())
    (item,) = lease_items()
    assert float(item["expires_at"]["N"]) == float(item["lease_expires_at"]["N"]) + 30.0
    clock.advance(50.0)
    renewed = backend.renew_lock("me", 60.0, frozenset({"a", "b"})).unwrap()
    (item,) = lease_items()
    assert float(item["expires_at"]["N"]) == renewed.expires_at + 30.0
    assert all("expires_at" not in row for row in lock_rows().values())


def test_a_takeover_revokes_the_old_lease_and_its_renewal_fails_after_a_release(
    aws: None,
) -> None:
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"a", "b"})
    clock.advance(TAKEOVER)
    run_b.acquire_lock("run-b", 60.0, frozenset({"a"})).unwrap()
    (old,) = [item for item in lease_items() if item["owner"]["S"] == "run-a"]
    assert old["revoked"] == {"BOOL": True}
    assert lock_rows()["b"]["lease_id"] == old["lease_id"], "b is free, but still points at a"
    assert run_a.locks()["b"].expires_at == 0, "a revoked lease holds nothing"

    run_b.release_lock("run-b")
    renewed = run_a.renew_lock("run-a", 60.0, frozenset({"a", "b"}))
    assert "revoked" in str(renewed.failure())
    assert is_successful(new_backend(clock=clock).acquire_lock("run-c", 60.0, frozenset({"b"})))


def test_a_lease_that_merely_lapsed_can_still_be_renewed(aws: None) -> None:
    """Nobody took anything: the renewal extends the same lease and fence."""
    clock = FakeClock()
    backend = new_backend(clock=clock)
    lease = bound_lease(backend, "me", {"a"})
    clock.advance(TAKEOVER)
    renewed = backend.renew_lock("me", 60.0, frozenset({"a"})).unwrap()
    assert renewed.fence == lease.fence
    assert renewed.expires_at == clock() + 60.0


def test_a_renewal_of_a_released_lease_fails(aws: None) -> None:
    backend = new_backend()
    bound_lease(backend, "me", {"a"})
    grants = list(backend._locks.grants["me"])
    backend.release_lock("me")
    backend._locks.grants["me"] = grants  # renewing a lease this process no longer holds
    renewed = backend.renew_lock("me", 60.0, frozenset({"a"}))
    assert "no longer exists" in str(renewed.failure())


def test_a_lease_within_the_skew_margin_is_neither_taken_nor_revoked(aws: None) -> None:
    clock = FakeClock()
    holder, taker = new_backend(clock=clock), new_backend(clock=clock, lock_skew_margin=30.0)
    lease = bound_lease(holder, "holder", {"a"})
    clock.advance(60.0 + 29.0)  # lapsed, but within the margin
    refused = taker.acquire_lock("taker", 60.0, frozenset({"a"}))
    assert "holder" in str(refused.failure())
    (item,) = lease_items()
    assert "revoked" not in item, "a lease within the margin is left alone"
    assert holder.renew_lock("holder", 60.0, frozenset({"a"})).unwrap().fence == lease.fence


def test_a_lease_renewed_between_the_takers_read_and_its_revoke_is_left_alone(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = FakeClock()
    holder, taker = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(holder, "holder", {"a"})
    clock.advance(TAKEOVER)
    original = taker._ddb.update_item

    def renew_first(**kwargs: Any) -> Any:
        # The taker has read the lapsed lease; the holder renews just before
        # the revoke lands.
        if "revoked" in str(kwargs.get("UpdateExpression", "")):
            holder.renew_lock("holder", 60.0, frozenset({"a"})).unwrap()
        return original(**kwargs)

    monkeypatch.setattr(taker._ddb, "update_item", renew_first)
    refused = taker.acquire_lock("taker", 60.0, frozenset({"a"}))
    assert "holder" in str(refused.failure())
    (item,) = [item for item in lease_items() if item["owner"]["S"] == "holder"]
    assert "revoked" not in item


def test_an_old_lease_over_many_chunks_is_revoked_once(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "TRANSACT_MAX", 4)  # two nodes per transaction
    clock = FakeClock()
    old, new = new_backend(clock=clock), new_backend(clock=clock)
    scope = frozenset(f"n{i}" for i in range(8))
    bound_lease(old, "old", set(scope))
    clock.advance(TAKEOVER)
    updates = spy(monkeypatch, new._ddb, "update_item")
    lease = new.acquire_lock("new", 60.0, scope).unwrap()
    revokes = [call for call in updates if "revoked" in call["UpdateExpression"]]
    assert len(revokes) == 1, "one revoke per old lease, not one per chunk"
    assert {row["lease_id"]["S"] for row in lock_rows().values()} == {
        item["lease_id"]["S"] for item in lease_items() if item["owner"]["S"] == "new"
    }
    assert all(head_of(nid).fence == lease.fence for nid in scope)


def test_a_crashed_runs_rows_are_taken_over_once_its_lease_is_gone(aws: None) -> None:
    """A missing lease item (released first, or reaped) frees every row pointing
    at it, without the skew margin: nothing can renew it any more."""
    backend = new_backend(clock=FakeClock())
    bound_lease(backend, "dead", {"a", "b"})
    (item,) = lease_items()
    ddb_client().delete_item(TableName=LOCK_TABLE, Key={"node_id": item["node_id"]})
    assert backend.locks()["a"].expires_at == 0
    assert is_successful(new_backend(clock=FakeClock()).acquire_lock("next", 60.0, {"a", "b"}))


def test_the_same_owner_retakes_its_own_rows_without_revoking(aws: None) -> None:
    backend = new_backend()
    backend.acquire_lock("me", 60.0, frozenset({"a", "b"})).unwrap()
    backend.acquire_lock("me", 60.0, frozenset({"b", "c"})).unwrap()
    assert all("revoked" not in item for item in lease_items())
    assert not is_successful(new_backend().acquire_lock("other", 60.0, frozenset({"a"})))
    backend.release_lock("me")
    assert backend.locks() == {}
    assert lease_items() == []


def test_acquire_chunks_run_in_parallel(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """While one chunk's transaction is parked, another chunk's goes through."""
    monkeypatch.setattr(limits, "TRANSACT_MAX", 2)  # one node per transaction
    backend = new_backend()
    _, ddb = stepping(backend)

    def chunk_of(node_id: str) -> Callable[[dict[str, Any]], bool]:
        return lambda kwargs: f"#{node_id}" in str(kwargs["TransactItems"][0])

    parked = ddb.pause("transact_write_items", chunk_of("a"))
    other = ddb.pause("transact_write_items", chunk_of("b"), after=True)
    acquiring = in_thread(lambda: backend.acquire_lock("me", 60.0, frozenset({"a", "b"})), "acq")
    parked.wait()
    other.wait()  # b's transaction completed while a's is parked
    other.release()
    parked.release()
    assert is_successful(joined(*acquiring))
    assert set(lock_rows()) == {"a", "b"}


def test_a_failing_chunk_releases_every_chunk_of_the_acquire(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "TRANSACT_MAX", 2)  # one node per transaction
    backend = new_backend(clock=FakeClock())
    backend.acquire_lock("alice", 60.0, frozenset({"n3"})).unwrap()
    scope = frozenset(f"n{i}" for i in range(8))
    refused = backend.acquire_lock("bob", 60.0, scope)
    assert "alice" in str(refused.failure())
    assert {nid: hold.owner for nid, hold in backend.locks().items()} == {"n3": "alice"}
    assert [item["owner"]["S"] for item in lease_items()] == ["alice"]
    assert is_successful(backend.acquire_lock("carol", 60.0, scope - {"n3"}))


def test_a_chunk_failing_outright_releases_the_acquire_and_raises(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "TRANSACT_MAX", 2)
    backend = new_backend()
    original = backend._ddb.transact_write_items

    def broken(**kwargs: Any) -> Any:
        if "#n2" in str(kwargs["TransactItems"][0]):
            raise ClientError({"Error": {"Code": "InternalServerError"}}, "TransactWriteItems")
        return original(**kwargs)

    monkeypatch.setattr(backend._ddb, "transact_write_items", broken)
    with pytest.raises(StateError, match="InternalServerError"):
        backend.acquire_lock("me", 60.0, frozenset(f"n{i}" for i in range(4)))
    assert backend.locks() == {}
    assert lease_items() == []


def test_a_failed_cleanup_does_not_hide_why_the_acquire_failed(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend(clock=FakeClock())
    backend.acquire_lock("alice", 60.0, frozenset({"a"})).unwrap()

    def broken(**kwargs: Any) -> Any:
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "DeleteItem")

    monkeypatch.setattr(backend._ddb, "delete_item", broken)
    refused = backend.acquire_lock("bob", 60.0, frozenset({"a"}))
    assert "alice" in str(refused.failure())


def test_a_release_surfaces_a_store_failure(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = new_backend()
    backend.acquire_lock("me", 60.0, frozenset({"a"})).unwrap()

    def broken(**kwargs: Any) -> Any:
        raise ClientError({"Error": {"Code": "InternalServerError"}}, "DeleteItem")

    monkeypatch.setattr(backend._ddb, "delete_item", broken)
    with pytest.raises(StateError, match="release_lock failed"):
        backend.release_lock("me")


def test_a_fast_clock_cannot_take_a_lease_within_the_skew_margin(aws: None) -> None:
    holder_clock, fast_clock = FakeClock(), FakeClock()
    holder = new_backend(clock=holder_clock)
    fast = new_backend(clock=fast_clock, lock_skew_margin=30.0)
    holder.acquire_lock("holder", 60.0, frozenset({"a"})).unwrap()

    fast_clock.advance(60.0 + 29.0)
    refused = fast.acquire_lock("fast", 60.0, frozenset({"a"}))
    assert not is_successful(refused)
    assert "holder" in str(refused.failure())

    fast_clock.advance(2.0)
    assert is_successful(fast.acquire_lock("fast", 60.0, frozenset({"a"})))


def test_a_negative_skew_margin_is_refused() -> None:
    with pytest.raises(StateError, match="lock_skew_margin"):
        S3StateBackend(BUCKET, KEY, lock_table=LOCK_TABLE, region=REGION, lock_skew_margin=-1.0)


def _other_state(**kwargs: Any) -> S3StateBackend:
    kwargs.setdefault("lock_table", LOCK_TABLE)
    kwargs.setdefault("region", REGION)
    return S3StateBackend(BUCKET, "staging/atlantide.json", **kwargs)


def test_states_sharing_a_lock_table_do_not_contend(aws: None) -> None:
    ours, theirs = new_backend(), _other_state()
    bound_lease(ours, "alice", {"a"})
    bound_lease(theirs, "bob", {"a"})
    ours.put(node("a", input_hash="ours"))
    theirs.put(node("a", input_hash="theirs"))
    assert {nid: lease.owner for nid, lease in ours.locks().items()} == {"a": "alice"}
    assert {nid: lease.owner for nid, lease in theirs.locks().items()} == {"a": "bob"}
    assert new_backend().load().nodes["a"].input_hash == "ours"
    assert _other_state().load().nodes["a"].input_hash == "theirs"


def test_breaking_every_lock_only_breaks_this_states(aws: None) -> None:
    ours, theirs = new_backend(), _other_state()
    ours.acquire_lock("alice", 60.0, frozenset({"a", "b"})).unwrap()
    theirs.acquire_lock("bob", 60.0, frozenset({"a"})).unwrap()
    assert ours.force_unlock(set(ours.locks())) == 2
    assert ours.locks() == {}
    assert set(theirs.locks()) == {"a"}
