"""Interrupting an apply runs the saga instead of walking away from it.

`asyncio.CancelledError` is a `BaseException`, so an `except Exception` around the
scheduler silently skips the compensation in the one case an operator most expects
it: Ctrl-C part-way through an apply that has already created resources. These
tests pin the consequence — what was created gets undone — rather than the clause.

The interrupt is delivered the way a real one is: by cancelling the task that is
*awaiting* the apply. Raising `CancelledError` inside a provider call would not do
it — `TaskGroup` reads that as the child being cancelled rather than as a failure,
so it never reaches the handler under test.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from atlantide.cli.errors import flatten_group
from atlantide.core.errors import LeaseLostError, ProviderError
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import LeaseGuard
from tests.support import FakeProvider

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"

SOURCE = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"


class BlocksOnB(FakeProvider):
    """Creates `a` normally, then parks on `b` until the run is cancelled.

    By the time `b` is reached, `a` exists at the provider and in state — so `a`
    is what the saga has to compensate.
    """

    def __init__(self, **kw: Any) -> None:
        super().__init__(**kw)
        self.reached_b = asyncio.Event()

    async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
        if res.node_id == B:
            self.reached_b.set()
            await asyncio.sleep(3600)  # cancelled from outside
        return await super().create(ctx, res)


async def _interrupt(h: Harness, on_failure: str = "rollback", **kw: Any) -> BaseException:
    """Start the apply, wait until `a` is done and `b` is in flight, then cancel."""
    provider = h.provider
    assert isinstance(provider, BlocksOnB)
    task = asyncio.ensure_future(h.apply_async(SOURCE, on_failure, **kw))
    await asyncio.wait_for(provider.reached_b.wait(), timeout=5)
    task.cancel()
    with pytest.raises(BaseException) as caught:
        await task
    return caught.value


async def test_an_interrupt_still_compensates_what_was_already_created() -> None:
    """The whole point. Before this, Ctrl-C left every created resource in place
    while reporting nothing about them."""
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())

    await _interrupt(h)

    assert A not in h.backend.load().nodes, "the completed create was undone"
    assert h.fake().deleted == ["a"], "and undone at the provider, not just in state"


async def test_a_cancellation_is_never_reported_as_a_provider_failure() -> None:
    """Wrapping it in a `ProviderError` would stop the unwind and invent a fault
    that never happened."""
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())

    error = await _interrupt(h, on_failure="halt")

    leaves = flatten_group(error)
    assert any(isinstance(e, asyncio.CancelledError) for e in leaves)
    assert not any(isinstance(e, ProviderError) for e in leaves)


async def test_halt_leaves_an_interrupted_apply_alone() -> None:
    """`--on-failure halt` means what it says for an interrupt too: state keeps
    the completed node, so the next run resumes rather than rebuilds."""
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())

    await _interrupt(h, on_failure="halt")

    assert A in h.backend.load().nodes
    assert h.fake().deleted == []


async def test_a_lost_lease_skips_the_saga_and_says_so() -> None:
    """A run that no longer holds the lock must not compensate: another run may
    now own these resources, and undoing "its" creates could destroy theirs.

    Leaving them is the lesser harm — but silently leaving them is not, so the
    reason has to reach the report.
    """
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())
    provider = h.fake()
    assert isinstance(provider, BlocksOnB)

    task = asyncio.ensure_future(h.apply_async(SOURCE, "rollback"))
    await asyncio.wait_for(provider.reached_b.wait(), timeout=5)
    # The renewal task fails mid-run and cancels the apply — in that order.
    h.lease.fail(LeaseLostError("another run took the lock"))
    task.cancel()
    with pytest.raises(BaseException):  # noqa: B017
        await task

    assert A in h.backend.load().nodes, "left in place rather than destroyed blind"
    assert provider.deleted == []


async def test_a_run_that_kept_its_lease_still_rolls_back() -> None:
    """The counterpart, so the skip cannot quietly become unconditional."""
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())
    assert h.lease.lost is None

    await _interrupt(h)

    assert A not in h.backend.load().nodes


async def test_the_progress_display_is_told_when_a_node_is_cancelled() -> None:
    """A node cancelled mid-CRUD would otherwise spin forever in the TUI: its
    `start` was never matched by a `finish` or a `fail`."""
    phases: list[tuple[str, str]] = []
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())

    await _interrupt(
        h,
        on_failure="halt",
        on_progress=lambda node_id, _action, phase: phases.append((node_id, phase)),
    )

    assert (B, "fail") in phases


async def test_a_second_interrupt_does_not_abandon_the_saga_half_done() -> None:
    """The shield. A compensation cancelled between its provider call and its
    state write is the exact window that leaves state lying, so the saga finishes
    even while the caller is being cancelled again.
    """
    h = Harness(MemoryStateBackend(), provider=BlocksOnB())
    provider = h.fake()
    assert isinstance(provider, BlocksOnB)

    task = asyncio.ensure_future(h.apply_async(SOURCE, "rollback"))
    await asyncio.wait_for(provider.reached_b.wait(), timeout=5)
    task.cancel()
    await asyncio.sleep(0)  # let the saga start
    task.cancel()  # ...and interrupt it again
    with pytest.raises(BaseException):  # noqa: B017
        await task

    assert A not in h.backend.load().nodes, "the compensation still completed"


def test_a_guard_with_no_lease_does_not_block_the_saga() -> None:
    """The default guard holds nothing, which must read as "may roll back" rather
    than "lease unknown, refuse"."""
    assert LeaseGuard().lost is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
