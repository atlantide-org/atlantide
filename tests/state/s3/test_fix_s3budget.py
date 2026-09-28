"""Regressions: a lock chunk's transient and contended cancellations share one
budget of ``TransactWriteItems`` calls, instead of nesting two retry loops."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from botocore.exceptions import ClientError

from atlantide.state.s3 import dynamo, limits

from .support import new_backend

_TRANSIENT = [{"Code": "TransactionConflict"}]
#: The lock row (item 0) refused by its condition; the head (item 1) not.
_CONTENDED = [{"Code": "ConditionalCheckFailed"}, {"Code": "None"}]


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the backoff sleeps instead of sleeping."""
    slept: list[float] = []
    monkeypatch.setattr(dynamo, "time", SimpleNamespace(sleep=slept.append))
    return slept


def _cancelled(reasons: list[dict[str, str]]) -> ClientError:
    response: Any = {
        "Error": {"Code": "TransactionCanceledException"},
        "CancellationReasons": reasons,
    }
    return ClientError(response, "TransactWriteItems")


def _acquire(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[list[dict[str, str]]]
) -> tuple[str, int]:
    """Acquire one node against cancellations cycling through ``outcomes``:
    the failure message and the number of ``TransactWriteItems`` calls."""
    backend = new_backend()
    calls: list[int] = []

    def cancelled(**_: Any) -> Any:
        calls.append(1)
        raise _cancelled(outcomes[(len(calls) - 1) % len(outcomes)])

    monkeypatch.setattr(backend._ddb, "transact_write_items", cancelled)
    message = str(backend.acquire_lock("me", 60.0, frozenset({"a"})).failure())
    return message, len(calls)


def test_a_chunk_cancelled_transiently_every_time_spends_one_budget(
    aws: None, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    message, calls = _acquire(monkeypatch, [_TRANSIENT])
    assert calls <= limits.DDB_ATTEMPTS
    assert message == (
        f"state lock contended: DynamoDB cancelled the transaction "
        f"{limits.DDB_ATTEMPTS} times (TransactionConflict); retry"
    )
    assert len(sleeps) == limits.DDB_ATTEMPTS - 1


def test_mixed_cancellations_ending_transient_share_the_budget(
    aws: None, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    message, calls = _acquire(monkeypatch, [_TRANSIENT, _CONTENDED])
    assert calls == limits.DDB_ATTEMPTS  # T, C, T, C, T
    assert message == (
        f"state lock contended: DynamoDB cancelled the transaction "
        f"{limits.DDB_ATTEMPTS} times (TransactionConflict); retry"
    )
    # Backoff only before a transient retry, its exponent the transient count.
    assert len(sleeps) == 2
    assert 0 <= sleeps[0] <= limits.BACKOFF_BASE
    assert 0 <= sleeps[1] <= 2 * limits.BACKOFF_BASE


def test_mixed_cancellations_ending_contended_share_the_budget(
    aws: None, monkeypatch: pytest.MonkeyPatch, sleeps: list[float]
) -> None:
    message, calls = _acquire(monkeypatch, [_CONTENDED, _TRANSIENT])
    assert calls == limits.DDB_ATTEMPTS  # C, T, C, T, C
    assert message == (
        f"the state lock over 'a' kept changing hands while it was "
        f"taken ({limits.DDB_ATTEMPTS} attempts) — retry"
    )


def test_transact_draws_on_a_shared_budget(sleeps: list[float]) -> None:
    calls: list[int] = []

    def flaky(**_: Any) -> Any:
        calls.append(1)
        raise _cancelled(_TRANSIENT if len(calls) < 3 else _CONTENDED)

    ddb = SimpleNamespace(transact_write_items=flaky)
    budget = dynamo.Budget()
    assert dynamo.transact(ddb, [], budget) is not None  # T, T, C
    assert budget.left == limits.DDB_ATTEMPTS - 3
    calls.clear()  # transient again: only the two attempts left are made
    with pytest.raises(ClientError):
        dynamo.transact(ddb, [], budget)
    assert len(calls) == 2 and budget.left == 0
