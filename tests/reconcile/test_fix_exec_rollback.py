"""Regressions for the rollback saga and for ``depends_on`` surviving into state.

- A ``depends_on`` edge is recorded in state, so a destroy (whose graph comes from
  state alone) still honours it.
- A state read failing mid-rollback does not abandon the remaining compensations.
- A rollback reads state once, not once per compensated node.
- A third cancellation does not abandon a rollback that is already running.
- A lease lost part-way through a rollback stops the remaining undos, and the
  undo's own state write is lease-checked.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from atlantide.cli.errors import flatten_group
from atlantide.core.errors import LeaseLostError, RollbackError
from atlantide.engine.selection import destroy_selection
from atlantide.state import NO_INPUT_HASH, MemoryStateBackend, StateGraph
from tests.support import FakeProvider

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"
C = "default:test.Box:c"


# -- depends_on in state -------------------------------------------------------

ORDERED = "a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n"


def test_a_depends_on_edge_is_recorded_in_state() -> None:
    h = Harness(MemoryStateBackend())
    h.apply(ORDERED)

    assert h.backend.load().nodes[B].depends_on == (A,)


def test_a_destroy_honours_a_depends_on_edge() -> None:
    """The desired graph is empty on a destroy; the order comes from state. Without
    the edge stored, `a` and `b` read as unrelated and `a` could go first."""
    h = Harness(MemoryStateBackend())
    h.apply(ORDERED)
    h.fake().reset()

    h.apply("")

    assert h.fake().deleted == ["b", "a"]


def test_destroy_target_closure_follows_a_depends_on_edge() -> None:
    h = Harness(MemoryStateBackend())
    h.apply(ORDERED)

    assert destroy_selection(h.backend.load(), [A]) == {A, B}


# -- rollback --------------------------------------------------------------------

#: `c` fails; `a` and `b` are compensated, `b` (the dependent) first.
CHAIN = "a = Box('a', size=1)\nb = Box('b', size=2, ref=a.out)\nBox('c', size=3, ref=b.out)\n"


class CountingBackend(MemoryStateBackend):
    """Counts `load()` calls and can be made to fail them."""

    def __init__(self) -> None:
        super().__init__()
        self.loads = 0
        self.broken = False

    def load(self) -> StateGraph:
        self.loads += 1
        if self.broken:
            raise OSError("state backend unreachable")
        return super().load()


class BreaksStateOnC(FakeProvider):
    """Fails `c`'s create after taking the state backend down with it."""

    def __init__(self, backend: CountingBackend, **kw: Any) -> None:
        super().__init__(fail_create={"c"}, **kw)
        self.backend = backend

    async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
        if res.logical_name == "c":
            self.backend.broken = True
        return await super().create(ctx, res)


def test_a_failed_state_read_does_not_abandon_the_rollback() -> None:
    """The outage that failed the forward pass must not skip every compensation
    and replace the original error."""
    backend = CountingBackend()
    h = Harness(backend, provider=BreaksStateOnC(backend))

    with pytest.raises(ExceptionGroup) as caught:
        h.apply(CHAIN, "rollback")

    assert h.fake().deleted == ["b", "a"], "every compensation still ran"
    leaves = flatten_group(caught.value)
    assert not any(isinstance(e, OSError) for e in leaves), "the original error is kept"
    backend.broken = False
    assert A not in backend.load().nodes and B not in backend.load().nodes


def test_a_rollback_reads_state_once() -> None:
    """Loading the whole state per compensated node is quadratic in the rollback."""
    backend = CountingBackend()
    many = "".join(f"Box('n{i}', size={i})\n" for i in range(8))
    before = 0

    class Mark(FakeProvider):
        async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
            nonlocal before
            if res.logical_name == "c":
                before = backend.loads
            return await super().create(ctx, res)

    h = Harness(backend, provider=Mark(fail_create={"c"}))
    with pytest.raises(ExceptionGroup):
        h.apply(many + CHAIN, "rollback")

    assert len(h.fake().deleted) == 10
    assert backend.loads - before <= 1


class ParksOnDeleteOfA(FakeProvider):
    """Fails `c`, then holds `a`'s compensating delete until released."""

    def __init__(self, **kw: Any) -> None:
        super().__init__(fail_create={"c"}, **kw)
        self.reached_c = asyncio.Event()
        self.deleting = asyncio.Event()
        self.release = asyncio.Event()

    async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
        if res.logical_name == "c":
            self.reached_c.set()
            await asyncio.sleep(3600)  # cancelled from outside
        return await super().create(ctx, res)

    async def delete(self, ctx: Any, res: Any) -> None:
        if res.logical_name == "a":
            self.deleting.set()
            await self.release.wait()
        await super().delete(ctx, res)


async def test_a_third_cancellation_does_not_abandon_the_rollback() -> None:
    """E.g. a lease-loss cancel landing after two Ctrl-Cs."""
    h = Harness(MemoryStateBackend(), provider=ParksOnDeleteOfA())
    provider = h.fake()
    assert isinstance(provider, ParksOnDeleteOfA)

    task = asyncio.ensure_future(h.apply_async(CHAIN, "rollback"))
    await asyncio.wait_for(provider.reached_c.wait(), timeout=5)  # a and b created
    task.cancel()
    await asyncio.wait_for(provider.deleting.wait(), timeout=5)
    for _ in range(3):
        task.cancel()
        await asyncio.sleep(0)
    provider.release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert provider.deleted == ["b", "a"]
    assert A not in h.backend.load().nodes, "the compensation's state write still landed"


class LosesLeaseDeletingB(FakeProvider):
    def __init__(self, harness: Any, **kw: Any) -> None:
        super().__init__(fail_create={"c"}, **kw)
        self.harness = harness

    async def delete(self, ctx: Any, res: Any) -> None:
        await super().delete(ctx, res)
        if res.logical_name == "b":
            self.harness.lease.fail(LeaseLostError("another run took the lock"))


def test_a_lease_lost_mid_rollback_stops_the_remaining_undos() -> None:
    h = Harness(MemoryStateBackend())
    h.provider = LosesLeaseDeletingB(h)

    with pytest.raises(ExceptionGroup) as caught:
        h.apply(CHAIN, "rollback")

    assert h.fake().deleted == ["b"], "a's undo was not attempted"
    failed = {e.node_id for e in flatten_group(caught.value) if isinstance(e, RollbackError)}
    assert failed == {A, B}, "both reported: b's write was refused, a was not attempted"
    rows = h.backend.load().nodes
    assert A in rows, "left in place"
    assert rows[B].input_hash == NO_INPUT_HASH, "b's refused write left the stale mark"
