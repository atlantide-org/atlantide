"""Regressions for the S3 backend fix round: lost responses, fsck races, backoff,
chunked outputs, release, wrapped scan errors, the lifecycle check."""

from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from atlantide.cli.views.state import render_fsck
from atlantide.core.check import OK, WARN
from atlantide.core.errors import StateError
from atlantide.state.codec import EntryKind
from atlantide.state.s3 import dynamo, limits
from atlantide.state.s3.dynamo import parallel, transact
from atlantide.state.s3.maintenance import FsckReport
from atlantide.state.s3.preflight import _check_lifecycle

from ..conftest import node
from .support import (
    KEY,
    before,
    head_of,
    journal_keys,
    lease_items,
    lock_rows,
    new_backend,
    spy,
)


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff sleeps instead of sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(dynamo, "time", SimpleNamespace(sleep=slept.append))
    return slept


def _error(code: str, operation: str, **extra: Any) -> ClientError:
    response: Any = {"Error": {"Code": code}, **extra}
    return ClientError(response, operation)


def _transient() -> ClientError:
    return _error(
        "TransactionCanceledException",
        "TransactWriteItems",
        CancellationReasons=[{"Code": "TransactionConflict"}],
    )


# -- 1, 2: a write whose response was lost is retried by botocore -----------


def test_a_journal_put_refused_by_its_own_lost_attempt_is_stored_anew(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.put(node("seed"))
    original = backend._s3.put_object
    puts: list[str] = []

    def lost_response(**kwargs: Any) -> Any:
        response = original(**kwargs)
        if kwargs["Key"] != KEY and not puts:
            puts.append(kwargs["Key"])
            # The PUT landed, its response was lost, and botocore's retry met
            # the object it had just written.
            raise _error("PreconditionFailed", "PutObject")
        return response

    monkeypatch.setattr(backend._s3, "put_object", lost_response)
    backend.put(node("a"))
    assert new_backend().load().nodes["a"] == node("a")
    assert head_of("a").ref != puts[0], "the head names the entry stored anew"
    assert puts[0] in journal_keys()
    assert new_backend().fsck().healthy


def test_a_lease_put_refused_by_its_own_lost_attempt_succeeds(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    original = backend._ddb.put_item
    fired: list[bool] = []

    def lost_response(**kwargs: Any) -> Any:
        response = original(**kwargs)
        if "lease_ns" in kwargs["Item"] and not fired:
            fired.append(True)
            raise _error("ConditionalCheckFailedException", "PutItem")
        return response

    monkeypatch.setattr(backend._ddb, "put_item", lost_response)
    lease = backend.acquire_lock("me", 60.0, frozenset({"a"})).unwrap()
    assert fired and lease.owner == "me"
    assert set(lock_rows()) == {"a"}


# -- 3: fsck under a concurrent commit or compaction --------------------------


def test_fsck_does_not_report_a_commit_made_after_its_listing(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker, writer = new_backend(), new_backend()
    writer.put(node("a"))
    # The head scan runs after the LIST: a commit in between names an entry
    # the LIST never showed.
    before(monkeypatch, checker._ddb, "get_paginator", lambda: writer.put(node("b")))
    report = checker.fsck()
    assert report.missing == [] and report.healthy


def test_fsck_restarts_when_a_compaction_replaces_the_snapshot(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    checker, writer = new_backend(), new_backend()
    writer.put(node("a"))

    def commit_and_compact() -> None:
        writer.put(node("b"))
        writer.compact()  # folds b and deletes its entry

    before(monkeypatch, checker._ddb, "get_paginator", commit_and_compact)
    gets = spy(monkeypatch, checker._s3, "get_object")
    report = checker.fsck()
    assert report.missing == [] and report.healthy
    assert sum(1 for call in gets if call["Key"] == KEY) == 2, "read the new snapshot"


# -- 4: jittered backoff, one transaction helper -----------------------------


def test_transact_backs_off_between_transient_cancellations(sleeps: list[float]) -> None:
    calls: list[int] = []

    def flaky(**_: Any) -> Any:
        calls.append(1)
        if len(calls) < 3:
            raise _transient()
        return {}

    assert transact(SimpleNamespace(transact_write_items=flaky), []) is None
    assert len(calls) == 3 and len(sleeps) == 2
    assert 0 <= sleeps[0] <= limits.BACKOFF_BASE
    assert 0 <= sleeps[1] <= 2 * limits.BACKOFF_BASE


def test_transact_returns_a_contended_cancellation_and_raises_the_rest(
    sleeps: list[float],
) -> None:
    contended = _error(
        "TransactionCanceledException",
        "TransactWriteItems",
        CancellationReasons=[{"Code": "ConditionalCheckFailed"}],
    )

    def refused(**_: Any) -> Any:
        raise contended

    assert transact(SimpleNamespace(transact_write_items=refused), []) is contended

    def always(**_: Any) -> Any:
        raise _transient()

    with pytest.raises(ClientError):
        transact(SimpleNamespace(transact_write_items=always), [])
    assert len(sleeps) == limits.DDB_ATTEMPTS - 1


def test_an_acquire_backs_off_and_keeps_its_contention_message(
    aws: None, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    backend = new_backend()

    def conflicted(**_: Any) -> Any:
        raise _transient()

    monkeypatch.setattr(backend._ddb, "transact_write_items", conflicted)
    message = str(backend.acquire_lock("me", 60.0, frozenset({"a"})).failure())
    assert message == (
        f"state lock contended: DynamoDB cancelled the transaction "
        f"{limits.DDB_ATTEMPTS} times (TransactionConflict); retry"
    )
    assert len(sleeps) == limits.DDB_ATTEMPTS - 1


def test_unprocessed_keys_are_re_requested_after_a_backoff(
    aws: None, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    new_backend().put(node("a"))
    reader = new_backend()
    original = reader._ddb.batch_get_item
    calls: list[int] = []

    def partial(**kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            return {"Responses": {}, "UnprocessedKeys": kwargs["RequestItems"]}
        return original(**kwargs)

    monkeypatch.setattr(reader._ddb, "batch_get_item", partial)
    assert "a" in reader.load().nodes
    assert len(calls) == 2 and len(sleeps) == 1


# -- 5: chunked outputs record what each chunk committed ---------------------


def test_chunks_committed_before_a_rebase_are_not_committed_again(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "TRANSACT_MAX", 2)
    backend, other = new_backend(), new_backend()
    other.set_outputs({"seed:x": 0})  # the snapshot exists: backend's view is kept
    backend.load()
    other.set_outputs({"d:x": 0})  # moves stack d under backend's view
    txns = spy(monkeypatch, backend._ddb, "transact_write_items")
    backend.set_outputs({"a:x": 1, "b:x": 2, "c:x": 3, "d:x": 4})
    # (a, b) commits, (c, d) is refused on d; the rebase commits only (c, d).
    assert len(txns) == 3
    assert head_of("a", kind=EntryKind.OUTPUT).seq == 1
    assert new_backend().outputs() == {"seed:x": 0, "a:x": 1, "b:x": 2, "c:x": 3, "d:x": 4}
    assert backend.outputs() == new_backend().outputs()


# -- 6, 7: release and administration failures ------------------------------


def test_a_release_attempts_every_delete_and_a_second_release_retries(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.acquire_lock("me", 60.0, frozenset({"a", "b"})).unwrap()
    original = backend._ddb.delete_item
    failed: list[bool] = []

    def lease_fails_once(**kwargs: Any) -> Any:
        if not failed:
            failed.append(True)
            raise _error("InternalServerError", "DeleteItem")
        return original(**kwargs)

    monkeypatch.setattr(backend._ddb, "delete_item", lease_fails_once)
    with pytest.raises(StateError, match="release_lock failed"):
        backend.release_lock("me")
    assert lock_rows() == {}, "the rows were deleted despite the lease item's failure"
    assert len(lease_items()) == 1
    backend.release_lock("me")
    assert lease_items() == []


class _FailingPaginator:
    def paginate(self, **_: Any) -> Any:
        yield from ()  # lazy, like botocore: the scan fails on iteration
        raise _error("AccessDeniedException", "Scan")


def test_lock_administration_wraps_store_errors(aws: None, monkeypatch: pytest.MonkeyPatch) -> None:
    backend = new_backend()
    backend.put(node("a"))
    monkeypatch.setattr(backend._ddb, "get_paginator", lambda _: _FailingPaginator())
    with pytest.raises(StateError, match="AccessDenied"):
        backend.locks()
    with pytest.raises(StateError, match="AccessDenied"):
        backend.fsck()

    def denied(**_: Any) -> Any:
        raise _error("AccessDeniedException", "DeleteItem")

    monkeypatch.setattr(backend._ddb, "delete_item", denied)
    with pytest.raises(StateError, match="AccessDenied"):
        backend.force_unlock({"a"})


# -- 8: the lifecycle check -------------------------------------------------


def _lifecycle(*rules: dict[str, Any]) -> Any:
    return SimpleNamespace(get_bucket_lifecycle_configuration=lambda **_: {"Rules": list(rules)})


def test_a_lifecycle_rule_without_noncurrent_expiration_does_not_count() -> None:
    rule = {"ID": "r", "Status": "Enabled", "Filter": {"Prefix": ""}, "Expiration": {"Days": 1}}
    check = _check_lifecycle(_lifecycle(rule), "bucket", KEY)
    assert check.status == WARN and "NoncurrentVersionExpiration" in check.detail


def test_an_empty_lifecycle_filter_covers_the_journal() -> None:
    rule = {
        "ID": "all",
        "Status": "Enabled",
        "Filter": {},
        "NoncurrentVersionExpiration": {"NoncurrentDays": 7},
    }
    assert _check_lifecycle(_lifecycle(rule), "bucket", KEY).status == OK


# -- 9: parallel stops early -------------------------------------------------


def test_parallel_drops_queued_work_after_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "READ_FANOUT", 1)
    ran: list[int] = []
    lock = threading.Lock()

    def work(item: int) -> int:
        with lock:
            ran.append(item)
        if item == 0:
            raise RuntimeError("boom")
        return item

    with pytest.raises(RuntimeError):
        parallel(work, list(range(50)))
    assert len(ran) < 50


# -- 11: one health verdict ------------------------------------------------


def test_a_rebuilt_head_is_healthy_in_the_report_and_the_cli() -> None:
    report = FsckReport(headless=["a", "b"], rebuilt=[("a", "k")])
    assert report.unrepaired == ["b"] and not report.healthy
    assert render_fsck(report) is True
    repaired = FsckReport(headless=["a"], rebuilt=[("a", "k")])
    assert repaired.unrepaired == [] and repaired.healthy
    assert render_fsck(repaired) is False
