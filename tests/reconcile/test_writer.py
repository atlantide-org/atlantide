"""State writes off the event loop: same ordering, as concurrent as the backend allows.

A synchronous ``put`` against a network backend stalls every in-flight node for
its duration. :class:`StateWriter` moves those calls onto writer threads — one
unless the backend declares ``write_concurrency``, then up to ``--parallelism``.
Lock operations (:meth:`StateWriter.call`) run alone: after every admitted write,
before any new one.
What must *not* change is the order the executor relies on: the write-ahead row
is durable before the provider call it guards, a node's row is persisted before
any dependent starts, and a started write is never abandoned by a cancellation.
"""

from __future__ import annotations

import asyncio
import threading
import time
from typing import Any

import pytest

from atlantide.core import Context, Resource
from atlantide.core.tuning import DEFAULT_PARALLELISM
from atlantide.reconcile.writer import (
    StateWriter,
    offloads_writes,
    state_writer,
    writer_for,
    writes_in_flight,
)
from atlantide.state import (
    MemoryStateBackend,
    NodeStatus,
    SqliteStateBackend,
    StateNode,
)
from tests.support import FakeProvider, SpyBackend

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


class ThreadSpy(SpyBackend):
    """Records the thread of every write and how many ran at once."""

    offload_writes = True

    def __init__(self, *, delay: float = 0.0) -> None:
        super().__init__()
        self.delay = delay
        self.threads: set[str] = set()
        self._active = 0
        self.max_active = 0
        self._guard = threading.Lock()

    def put(self, node: StateNode) -> None:
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            self.threads.add(threading.current_thread().name)
            if self.delay:
                time.sleep(self.delay)
            super().put(node)
        finally:
            with self._guard:
                self._active -= 1


def test_offload_is_the_backends_declared_attribute(tmp_path: Any) -> None:
    from atlantide.state.s3 import S3StateBackend
    from atlantide.state.sql.postgres import PostgresStateBackend

    assert not offloads_writes(MemoryStateBackend())
    sqlite = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        # Thread-safe, but a local commit is cheaper than the thread hop.
        assert not offloads_writes(sqlite)
    finally:
        sqlite.close()
    # Read off the class: no connection needed to decide.
    assert S3StateBackend.offload_writes and PostgresStateBackend.offload_writes
    assert offloads_writes(ThreadSpy())  # declared by a subclass


def test_offload_is_not_inferred_from_the_class_name() -> None:
    class S3StateBackend(MemoryStateBackend):  # a name, not a declaration
        pass

    assert not offloads_writes(S3StateBackend())


def test_a_backend_without_the_attribute_stays_inline() -> None:
    """A third-party backend predating the attribute, or declaring it loosely."""

    class Undeclared:
        pass

    class Truthy(MemoryStateBackend):
        offload_writes = 1  # type: ignore[assignment]

    assert not offloads_writes(Undeclared())  # type: ignore[arg-type]
    assert not offloads_writes(Truthy())


def test_writes_run_off_the_loop_one_at_a_time() -> None:
    backend = ThreadSpy(delay=0.005)
    h = Harness(backend)
    h.parallelism = 8
    with state_writer(backend) as writer:
        assert writer.offloaded
        report = h.apply("".join(f"Box('n{i}', size={i})\n" for i in range(12)))
    assert len(report.created) == 12
    assert backend.threads and all(name.startswith("atlantide-state") for name in backend.threads)
    assert backend.max_active == 1
    assert all(node.status == NodeStatus.CREATED for node in backend.load().nodes.values())


def test_without_an_installed_writer_writes_stay_inline() -> None:
    backend = ThreadSpy()
    assert not writer_for(backend).offloaded
    Harness(backend).apply("Box('a', size=1)\n")
    assert backend.threads == {threading.main_thread().name}


def test_write_ahead_lands_before_create_and_persist_before_dependents() -> None:
    backend = ThreadSpy(delay=0.01)
    seen: dict[str, str | None] = {}

    def on_create(ctx: Context, res: Resource) -> dict[str, Any]:
        own = backend.inner.load().get(res.node_id)
        seen[res.logical_name] = own.status if own is not None else None
        if res.logical_name == "b":
            upstream = backend.inner.load().get(A)
            seen["a-when-b-starts"] = upstream.status if upstream is not None else None
        return {"out": f"{res.logical_name}:1"}

    h = Harness(backend, provider=FakeProvider(on_create=on_create))
    with state_writer(backend):
        h.apply("a = Box('a', size=1)\nBox('b', size=1, ref=a.out)\n")

    assert seen["a"] == NodeStatus.CREATING
    assert seen["b"] == NodeStatus.CREATING
    assert seen["a-when-b-starts"] == NodeStatus.CREATED


def test_the_loop_keeps_running_while_a_write_is_in_flight() -> None:
    backend = ThreadSpy(delay=0.05)
    h = Harness(backend)
    h.parallelism = 4
    gaps: list[float] = []

    async def run() -> None:
        stop = asyncio.Event()

        async def tick() -> None:
            last = time.perf_counter()
            while not stop.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        ticker = asyncio.ensure_future(tick())
        try:
            await h.apply_async("".join(f"Box('n{i}', size={i})\n" for i in range(4)))
        finally:
            stop.set()
            await ticker

    with state_writer(backend):
        asyncio.run(run())
    # Inline, each 50 ms put would freeze the loop for its whole duration.
    assert max(gaps) < 0.04, max(gaps)


async def test_a_cancelled_write_still_lands_before_the_cancellation() -> None:
    writer = StateWriter(offload=True)
    started = threading.Event()
    landed: list[str] = []

    def slow_write() -> None:
        started.set()
        time.sleep(0.05)
        landed.append("written")

    try:
        task = asyncio.ensure_future(writer.run(slow_write))
        while not started.is_set():
            await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert landed == ["written"]
    finally:
        writer.close()


async def test_a_failing_write_raises_its_own_error() -> None:
    writer = StateWriter(offload=True)

    def boom() -> None:
        raise RuntimeError("store refused")

    try:
        with pytest.raises(RuntimeError, match="store refused"):
            await writer.run(boom)
    finally:
        writer.close()


def test_a_nested_install_reuses_the_outer_writer() -> None:
    backend = ThreadSpy()
    with state_writer(backend) as outer, state_writer(backend) as inner:
        assert inner is outer
        assert writer_for(backend) is outer
    assert not writer_for(backend).offloaded


# -- the writer pool -------------------------------------------------------


class PooledSpy(ThreadSpy):
    """A spy declaring concurrent writes to distinct nodes safe."""

    write_concurrency = 4


def test_write_concurrency_is_the_declared_attribute_capped_by_parallelism() -> None:
    class Undeclared:
        pass

    class Loose(MemoryStateBackend):
        write_concurrency = True  # a bool is not a count

    class Zero(MemoryStateBackend):
        write_concurrency = 0

    assert writes_in_flight(MemoryStateBackend()) == 1
    assert writes_in_flight(Undeclared()) == 1  # type: ignore[arg-type]
    assert writes_in_flight(Loose()) == 1
    assert writes_in_flight(Zero()) == 1
    assert writes_in_flight(PooledSpy(), 16) == 4
    assert writes_in_flight(PooledSpy(), 2) == 2
    assert writes_in_flight(PooledSpy()) == min(4, DEFAULT_PARALLELISM)


def test_a_pooled_backend_overlaps_up_to_its_declared_concurrency() -> None:
    backend = PooledSpy(delay=0.02)
    h = Harness(backend)
    h.parallelism = 8
    with state_writer(backend, parallelism=8) as writer:
        assert writer.workers == 4
        report = h.apply("".join(f"Box('n{i}', size={i})\n" for i in range(24)))
    assert len(report.created) == 24
    assert backend.max_active == 4
    assert len(backend.threads) == 4
    assert all(node.status == NodeStatus.CREATED for node in backend.load().nodes.values())


def test_parallelism_caps_the_pool() -> None:
    backend = PooledSpy(delay=0.02)
    h = Harness(backend)
    h.parallelism = 2
    with state_writer(backend, parallelism=2) as writer:
        assert writer.workers == 2
        h.apply("".join(f"Box('n{i}', size={i})\n" for i in range(8)))
    assert backend.max_active == 2


def test_pooled_writes_keep_write_ahead_and_dependency_order() -> None:
    backend = PooledSpy(delay=0.01)
    seen: dict[str, str | None] = {}

    def on_create(ctx: Context, res: Resource) -> dict[str, Any]:
        own = backend.inner.load().get(res.node_id)
        seen[res.logical_name] = own.status if own is not None else None
        if res.logical_name == "b":
            upstream = backend.inner.load().get(A)
            seen["a-when-b-starts"] = upstream.status if upstream is not None else None
        return {"out": f"{res.logical_name}:1"}

    h = Harness(backend, provider=FakeProvider(on_create=on_create))
    h.parallelism = 8
    with state_writer(backend, parallelism=8):
        h.apply(
            "a = Box('a', size=1)\nBox('b', size=1, ref=a.out)\n"
            + "".join(f"Box('x{i}', size={i})\n" for i in range(8))
        )
    assert seen["a"] == NodeStatus.CREATING
    assert seen["b"] == NodeStatus.CREATING
    assert seen["a-when-b-starts"] == NodeStatus.CREATED


async def test_writes_with_the_same_key_never_overlap_and_keep_their_order() -> None:
    writer = StateWriter(offload=True, workers=4)
    log: list[tuple[str, int]] = []
    active: dict[str, int] = {"a": 0, "b": 0}
    overlap: list[str] = []
    guard = threading.Lock()

    def write(key: str, n: int) -> None:
        with guard:
            active[key] += 1
            if active[key] > 1:
                overlap.append(key)
        time.sleep(0.005)
        with guard:
            log.append((key, n))
            active[key] -= 1

    try:
        await asyncio.gather(
            *(writer.run(write, key, n, key=key) for n in range(6) for key in ("a", "b"))
        )
    finally:
        writer.close()
    assert overlap == []
    assert [n for key, n in log if key == "a"] == list(range(6))
    assert [n for key, n in log if key == "b"] == list(range(6))
    assert writer._keys == {}  # nothing retained once idle


async def test_cancelled_pooled_writes_all_land_before_the_cancellation() -> None:
    writer = StateWriter(offload=True, workers=4)
    started = threading.Semaphore(0)
    landed: list[int] = []

    def slow_write(n: int) -> None:
        started.release()
        time.sleep(0.05)
        landed.append(n)

    try:
        tasks = [asyncio.ensure_future(writer.run(slow_write, n)) for n in range(4)]
        await asyncio.sleep(0)
        for _ in range(4):
            await asyncio.to_thread(started.acquire)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with pytest.raises(asyncio.CancelledError):
                await task
        assert sorted(landed) == [0, 1, 2, 3]
        # Every shared hold was released, so an exclusive call proceeds at once.
        assert writer.call(lambda: "free") == "free"
    finally:
        writer.close()


async def test_a_write_cancelled_while_queued_behind_its_key_never_runs() -> None:
    writer = StateWriter(offload=True, workers=2)
    release = threading.Event()
    ran: list[str] = []

    def first() -> None:
        release.wait(1)
        ran.append("first")

    def second() -> None:
        ran.append("second")

    try:
        head = asyncio.ensure_future(writer.run(first, key="n"))
        await asyncio.sleep(0.01)
        queued = asyncio.ensure_future(writer.run(second, key="n"))
        await asyncio.sleep(0.01)
        queued.cancel()
        with pytest.raises(asyncio.CancelledError):
            await queued
        release.set()
        await head
    finally:
        writer.close()
    assert ran == ["first"]
    assert writer._keys == {}


def test_a_writer_names_its_mode_and_pool_size() -> None:
    inline = StateWriter(offload=False, workers=4)
    pooled = StateWriter(offload=True, workers=3)
    try:
        assert repr(inline) == "StateWriter(offloaded=False, workers=1)"
        assert repr(pooled) == "StateWriter(offloaded=True, workers=3)"
    finally:
        pooled.close()
