"""Lock calls against in-flight writes: :meth:`StateWriter.call` and its ``ReadWriteGate``.

State writes share the writer; a lock operation (renew, rebind) takes it
exclusively — after every admitted write and before any new one — so a renewal
and the rebinding that follows it form one window no write can land inside.
A waiting exclusive caller is preferred, so a stream of writes cannot starve it.
"""

from __future__ import annotations

import asyncio
import threading
import time

from atlantide.reconcile.writer import ReadWriteGate, StateWriter
from tests.support import debug_records


async def test_a_lock_call_drains_in_flight_writes_first() -> None:
    """Started *and* submitted-but-queued writes: two workers, four writes. The
    queued pair must land before the lock call too, or one could slip in between
    a renewal and the rebinding that follows it."""
    writer = StateWriter(offload=True, workers=2)
    started = threading.Semaphore(0)
    active = 0
    landed: list[int] = []
    guard = threading.Lock()
    seen_active: list[int] = []

    def write(n: int) -> None:
        nonlocal active
        with guard:
            active += 1
        started.release()
        time.sleep(0.05)
        with guard:
            active -= 1
            landed.append(n)

    def lock_op() -> str:
        with guard:
            seen_active.append(active)
        return "renewed"

    try:
        tasks = [asyncio.ensure_future(writer.run(write, n)) for n in range(4)]
        await asyncio.sleep(0)
        for _ in range(2):
            await asyncio.to_thread(started.acquire)
        # Blocking, on the loop thread — as the heartbeat does.
        assert writer.call(lock_op) == "renewed"
        assert seen_active == [0]
        assert sorted(landed) == [0, 1, 2, 3]
        await asyncio.gather(*tasks)
    finally:
        writer.close()


def test_the_gate_prefers_a_waiting_exclusive_caller() -> None:
    """Shared callers arriving behind a waiting exclusive one wait for it: a
    continuous stream of writes cannot starve the lease renewal."""
    gate = ReadWriteGate()
    order: list[str] = []
    gate.acquire_shared()  # a write in flight

    def exclusive() -> None:
        gate.acquire_exclusive()
        order.append("exclusive")
        gate.release_exclusive()

    def shared() -> None:
        gate.acquire_shared()
        order.append("shared")
        gate.release_shared()

    ex = threading.Thread(target=exclusive)
    ex.start()
    while not gate._exclusive_waiting:
        time.sleep(0.001)
    late = threading.Thread(target=shared)
    late.start()
    time.sleep(0.02)
    assert order == []  # the late write is held back, the renewal still waiting
    gate.release_shared()
    ex.join(1)
    late.join(1)
    assert order == ["exclusive", "shared"]


def test_the_gate_admits_shared_callers_together() -> None:
    gate = ReadWriteGate()
    gate.acquire_shared()
    done = threading.Event()

    def second() -> None:
        gate.acquire_shared()
        done.set()
        gate.release_shared()

    threading.Thread(target=second).start()
    assert done.wait(1)
    gate.release_shared()


async def test_lock_calls_are_not_starved_by_a_stream_of_writes() -> None:
    writer = StateWriter(offload=True, workers=4)
    stop = asyncio.Event()
    calls = 0

    def write() -> None:
        time.sleep(0.002)

    async def stream() -> None:
        while not stop.is_set():
            await writer.run(write)

    try:
        streams = [asyncio.ensure_future(stream()) for _ in range(8)]
        deadline = time.perf_counter() + 0.2
        while time.perf_counter() < deadline:
            await asyncio.sleep(0.01)
            writer.call(lambda: None)
            calls += 1
        stop.set()
        await asyncio.gather(*streams)
    finally:
        writer.close()
    assert calls >= 5


def test_an_exclusive_call_is_logged_at_debug_by_name_and_duration() -> None:
    writer = StateWriter(offload=True)

    def renew_lock() -> str:
        return "a value that must not be logged"

    try:
        with debug_records("atlantide.reconcile.writer") as records:
            assert writer.call(renew_lock) == "a value that must not be logged"
    finally:
        writer.close()
    [record] = records
    message = record.getMessage()
    assert "renew_lock" in message and "waited" in message
    assert "must not be logged" not in message
