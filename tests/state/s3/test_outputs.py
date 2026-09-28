"""Committed stack outputs on S3: fenced per stack, chunked, rebased."""

from __future__ import annotations

from typing import Any

import pytest
from botocore.exceptions import ClientError

from atlantide.core.errors import FencedWriteError, StateError
from atlantide.state.codec import (
    EntryKind,
)
from atlantide.state.s3 import limits
from tests.support import FakeClock

from .support import (
    TAKEOVER,
    bound_lease,
    head_of,
    new_backend,
    spy,
)


def test_outputs_are_fenced_on_the_leased_nodes_of_their_stack(aws: None) -> None:
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"dev:t:a", "prod:t:p"})
    run_a.set_outputs({"dev:url": "a"})

    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"dev:t:a"})
    run_b.set_outputs({"dev:url": "b"})

    with pytest.raises(FencedWriteError, match="run-b"):
        run_a.set_outputs({"dev:url": "stale"})
    with pytest.raises(FencedWriteError):
        run_a.set_outputs({}, remove=["dev:url"])  # a removal is a write too
    run_a.set_outputs({"prod:url": "p"})  # its own stack is still its own
    run_a.set_outputs({"loose:url": "x"})  # no leased node in the stack: unfenced
    assert new_backend().outputs() == {"dev:url": "b", "prod:url": "p", "loose:url": "x"}


def test_outputs_of_several_stacks_commit_in_one_transaction(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    txns = spy(monkeypatch, backend._ddb, "transact_write_items")
    backend.set_outputs({"a:x": 1, "b:x": 2, "c:x": 3})
    assert len(txns) == 1
    assert head_of("b", kind=EntryKind.OUTPUT).seq == 1
    backend.set_outputs({}, remove=["b:x"])
    assert head_of("b", kind=EntryKind.OUTPUT).op == "delete"
    assert new_backend().outputs() == {"a:x": 1, "c:x": 3}


def test_outputs_beyond_one_transaction_are_chunked_and_prechecked(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(limits, "TRANSACT_MAX", 2)
    clock = FakeClock()
    run_a, run_b = new_backend(clock=clock), new_backend(clock=clock)
    bound_lease(run_a, "run-a", {"s1:t:a", "s2:t:a"})
    txns = spy(monkeypatch, run_a._ddb, "transact_write_items")
    run_a.set_outputs({f"s{i}:x": i for i in range(5)})
    assert len(txns) == 3
    assert new_backend().outputs() == {f"s{i}:x": i for i in range(5)}

    clock.advance(TAKEOVER)
    bound_lease(run_b, "run-b", {"s2:t:a"})
    with pytest.raises(FencedWriteError, match="run-b"):
        run_a.set_outputs({f"s{i}:x": -i for i in range(1, 5)})
    assert new_backend().outputs()["s3:x"] == 3, "the pre-check refused before any chunk"


def test_a_transient_output_transaction_cancel_is_retried(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    original = backend._ddb.transact_write_items
    calls: list[int] = []

    def flaky(**kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise ClientError(
                {
                    "Error": {"Code": "TransactionCanceledException"},
                    "CancellationReasons": [{"Code": "TransactionConflict"}],
                },
                "TransactWriteItems",
            )
        return original(**kwargs)

    monkeypatch.setattr(backend._ddb, "transact_write_items", flaky)
    backend.set_outputs({"dev:a": 1})
    assert len(calls) == 2
    assert new_backend().outputs() == {"dev:a": 1}


def test_an_output_transaction_failing_outright_surfaces(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()

    def broken(**_: Any) -> Any:
        raise ClientError({"Error": {"Code": "AccessDeniedException"}}, "TransactWriteItems")

    monkeypatch.setattr(backend._ddb, "transact_write_items", broken)
    with pytest.raises(StateError, match="cannot commit outputs"):
        backend.set_outputs({"dev:a": 1})


def test_a_concurrent_output_merge_rebases_and_keeps_both(aws: None) -> None:
    first, second = new_backend(), new_backend()
    first.set_outputs({"dev:a": 1})
    second.load()
    first.set_outputs({"dev:b": 2})
    second.set_outputs({"dev:c": 3})  # stale view of stack dev: rebases on its head
    assert new_backend().outputs() == {"dev:a": 1, "dev:b": 2, "dev:c": 3}


def test_an_unchanged_output_write_costs_no_request(
    aws: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = new_backend()
    backend.set_outputs({"dev:url": "u"})
    puts = spy(monkeypatch, backend._s3, "put_object")
    backend.set_outputs({"dev:url": "u"})
    assert puts == []
