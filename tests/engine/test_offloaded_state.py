"""An engine run whose state writes are offloaded to the writer thread.

The lease heartbeat still runs on the event loop. If it called the backend
directly, a renewal could run *alongside* an in-flight write — two threads in one
client — and a write could land between a renewal minting a newer fence and the
rebinding of the lease that follows, where the store refuses it as superseded.
These tests hold the engine to: lock calls never overlap a write (writes may
overlap each other only as far as the backend's ``write_concurrency`` allows),
the run succeeds, and the backend's ``checkpoint`` runs after every locked run.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Set
from pathlib import Path
from typing import Any

import pytest
from returns.result import Result

from atlantide.core import is_successful
from atlantide.core.errors import LockError
from atlantide.state import Lease, LockPolicy, MemoryStateBackend, SqliteStateBackend, StateNode
from tests.support import Box, FakeProvider, engine_for, globals_of

#: Renews many times over a run of slow writes.
FAST = LockPolicy(ttl=5.0, renew_interval=0.01, renew_grace=0.0)


class SlowFencedMemory(MemoryStateBackend):
    """A fencing in-memory store with slow writes, declared safe to offload.

    Every renewal mints a newer fence (as the S3 and Postgres backends do), so a
    write carrying the pre-renewal lease is refused — the race a direct heartbeat
    call would open.
    """

    offload_writes = True

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self._guard = threading.Lock()
        self._active = 0
        self.max_active = 0
        self.renew_threads: set[str] = set()
        self.renewals = 0

    def _enter(self) -> None:
        with self._guard:
            self._active += 1
            self.max_active = max(self.max_active, self._active)

    def _exit(self) -> None:
        with self._guard:
            self._active -= 1

    def put(self, node: StateNode) -> None:
        self._enter()
        try:
            time.sleep(self.delay)
            super().put(node)
        finally:
            self._exit()

    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        self._enter()
        try:
            self.renew_threads.add(threading.current_thread().name)
            self.renewals += 1
            time.sleep(self.delay)
            return super().renew_lock(owner, ttl_seconds, scope)
        finally:
            self._exit()

    def bind_lease(self, lease: Lease | None) -> None:
        self._enter()
        try:
            super().bind_lease(lease)
        finally:
            self._exit()


async def test_heartbeat_and_offloaded_writes_never_overlap() -> None:
    backend = SlowFencedMemory(delay=0.003)
    engine = engine_for(
        Box, provider=FakeProvider(), backend=backend, lock_policy=FAST, parallelism=8
    )
    source = "".join(f"Box('n{i}', size={i})\n" for i in range(30))

    result = await engine.apply(source, extra_globals=globals_of(Box))

    assert is_successful(result), result
    assert len(result.unwrap().created) == 30
    assert backend.renewals > 0, "the run must be long enough to renew"
    assert backend.max_active == 1
    assert backend.renew_threads == {
        name for name in backend.renew_threads if name.startswith("atlantide-state")
    }


class ThreadedSqlite(SqliteStateBackend):
    """The real sqlite backend opted in to offloading, recording ``put`` threads."""

    offload_writes = True

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.put_threads: set[str] = set()

    def put(self, node: StateNode) -> None:
        self.put_threads.add(threading.current_thread().name)
        super().put(node)


async def test_offloaded_sqlite_survives_a_rollback(tmp_path: Path) -> None:
    """Writes on the writer thread; the heartbeat there too; the rollback's
    compensations and the outputs on the loop thread — all against one sqlite
    connection, which must serialize them itself."""
    backend = ThreadedSqlite(str(tmp_path / "s.db"))
    provider = FakeProvider(fail_create={"n39"})
    engine = engine_for(Box, provider=provider, backend=backend, lock_policy=FAST, parallelism=8)
    source = "".join(f"Box('n{i}', size={i})\n" for i in range(40))
    try:
        with pytest.raises(ExceptionGroup):  # the create failure, after the rollback
            await engine.apply(source, extra_globals=globals_of(Box))
        assert any(op == "delete" for op, _ in provider.calls), "rollback compensations ran"
        assert any(name.startswith("atlantide-state") for name in backend.put_threads)

        provider.fail_create.clear()
        result = await engine.apply(source, extra_globals=globals_of(Box))
        assert is_successful(result), result
        assert len(backend.load().nodes) == 40
    finally:
        backend.close()


async def test_apply_compiled_runs_the_plans_compilation() -> None:
    engine = engine_for(Box)
    source = "Box('a', size=1)\nBox('b', size=2)\n"
    planned = engine.plan(source, extra_globals=globals_of(Box)).unwrap()

    calls: list[Any] = []
    original = engine.compile

    def counting(*args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return original(*args, **kwargs)

    engine.compile = counting  # type: ignore[method-assign]
    result = await engine.apply_compiled(planned.compiled, expect=planned.changeset)

    assert is_successful(result), result
    assert sorted(result.unwrap().created) == ["default:test.Box:a", "default:test.Box:b"]
    assert calls == []


async def test_apply_compiled_still_rediffs_under_the_lock() -> None:
    """A node another run created after the plan is not created twice."""
    engine = engine_for(Box)
    source = "Box('a', size=1)\n"
    planned = engine.plan(source, extra_globals=globals_of(Box)).unwrap()
    first = await engine.apply(source, extra_globals=globals_of(Box))
    assert is_successful(first)

    again = await engine.apply_compiled(planned.compiled)

    assert is_successful(again), again
    assert again.unwrap().created == []


class PooledFencedMemory(MemoryStateBackend):
    """A fencing store declaring four concurrent writes safe; slow puts.

    Records how many puts overlapped, and whether any lock call ever ran while a
    put was in flight — the interleaving the exclusive gate rules out.
    """

    offload_writes = True
    write_concurrency = 4

    def __init__(self, delay: float) -> None:
        super().__init__()
        self.delay = delay
        self._guard = threading.Lock()
        self._puts = 0
        self.max_puts = 0
        self.overlapped: list[str] = []
        self.renewals = 0

    def put(self, node: StateNode) -> None:
        with self._guard:
            self._puts += 1
            self.max_puts = max(self.max_puts, self._puts)
        try:
            time.sleep(self.delay)
            with self._guard:
                super().put(node)
        finally:
            with self._guard:
                self._puts -= 1

    def _lock_op(self, name: str) -> None:
        with self._guard:
            if self._puts:
                self.overlapped.append(name)

    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        self._lock_op("renew")
        self.renewals += 1
        time.sleep(self.delay)
        with self._guard:
            return super().renew_lock(owner, ttl_seconds, scope)

    def bind_lease(self, lease: Lease | None) -> None:
        self._lock_op("bind")
        with self._guard:
            super().bind_lease(lease)


async def test_pooled_writes_overlap_but_never_a_lock_call() -> None:
    backend = PooledFencedMemory(delay=0.005)
    engine = engine_for(
        Box, provider=FakeProvider(), backend=backend, lock_policy=FAST, parallelism=8
    )
    source = "".join(f"Box('n{i}', size={i})\n" for i in range(60))

    result = await engine.apply(source, extra_globals=globals_of(Box))

    assert is_successful(result), result
    assert len(result.unwrap().created) == 60
    assert backend.renewals > 0, "the run must be long enough to renew"
    assert backend.max_puts == 4
    assert backend.overlapped == []


class Checkpointed(MemoryStateBackend):
    """Records each checkpoint: how many rows had landed, and whether the lease was held."""

    def __init__(self, *, fail: bool = False) -> None:
        super().__init__()
        self.fail = fail
        self.checkpoints: list[tuple[int, bool]] = []

    def checkpoint(self) -> None:
        self.checkpoints.append((len(self.load().nodes), bool(self.locks())))
        if self.fail:
            raise RuntimeError("compaction lost a race")


class OffloadedCheckpointed(Checkpointed):
    offload_writes = True
    write_concurrency = 4


@pytest.mark.parametrize("backend_cls", [Checkpointed, OffloadedCheckpointed])
async def test_checkpoint_runs_after_a_run_while_the_lease_is_held(
    backend_cls: type[Checkpointed],
) -> None:
    backend = backend_cls()
    engine = engine_for(Box, provider=FakeProvider(), backend=backend, parallelism=8)
    source = "".join(f"Box('n{i}', size={i})\n" for i in range(10))

    result = await engine.apply(source, extra_globals=globals_of(Box))

    assert is_successful(result), result
    assert backend.checkpoints == [(10, True)]  # every write landed, lock still held
    assert backend.locks() == {}  # and released after


async def test_checkpoint_runs_after_a_failed_run_without_masking_it() -> None:
    backend = OffloadedCheckpointed(fail=True)
    provider = FakeProvider(fail_create={"n3"})
    engine = engine_for(Box, provider=provider, backend=backend, parallelism=8)
    source = "".join(f"Box('n{i}', size={i})\n" for i in range(6))

    with pytest.raises(ExceptionGroup) as raised:
        await engine.apply(source, extra_globals=globals_of(Box))

    assert "n3" in str(raised.value.exceptions[0])
    assert len(backend.checkpoints) == 1
    assert backend.checkpoints[0][1], "checkpointed before the release"
    assert backend.locks() == {}


async def test_a_failing_checkpoint_is_a_warning_not_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import atlantide.engine.runs as runs_module

    warnings: list[str] = []

    class Recorder:
        def warning(self, msg: str, *args: Any) -> None:
            warnings.append(msg % args)

    monkeypatch.setattr(runs_module, "_log", Recorder())
    backend = Checkpointed(fail=True)
    engine = engine_for(Box, provider=FakeProvider(), backend=backend)

    result = await engine.apply("Box('a', size=1)\n", extra_globals=globals_of(Box))

    assert is_successful(result), result
    assert backend.checkpoints == [(1, True)]
    assert len(warnings) == 1 and "compaction lost a race" in warnings[0]
    assert backend.locks() == {}


async def test_a_backend_without_the_hook_or_the_count_still_applies() -> None:
    """A third-party backend with no usable ``checkpoint`` or ``write_concurrency``."""

    class Legacy(SlowFencedMemory):
        checkpoint = None  # type: ignore[assignment]
        write_concurrency = "many"  # type: ignore[assignment]

    backend = Legacy(delay=0.001)
    engine = engine_for(Box, provider=FakeProvider(), backend=backend, parallelism=8)
    source = "".join(f"Box('n{i}', size={i})\n" for i in range(8))

    result = await engine.apply(source, extra_globals=globals_of(Box))

    assert is_successful(result), result
    assert backend.max_active == 1
