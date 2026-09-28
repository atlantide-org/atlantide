"""State writes off the event loop, and the lock scaffold's view of the backend.

:class:`StateWriter` runs the executor's state-backend calls on writer threads,
as concurrently as the backend declares safe; :class:`ReadWriteGate` keeps the
lock scaffold's calls (acquire, renew, ``bind_lease``, release) from interleaving
with those writes. :class:`SerializedBackend` is the backend as the
lock scaffold sees it: every call routed through the writer, exclusively.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterable, Iterator, Mapping, Set
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any, override

from returns.result import Result

from atlantide.core.errors import LockError
from atlantide.core.logging import get_logger
from atlantide.core.tuning import DEFAULT_PARALLELISM
from atlantide.state import Lease, StateBackend, StateGraph, StateNode

__all__ = [
    "ReadWriteGate",
    "SerializedBackend",
    "StateWriter",
    "offloads_writes",
    "state_writer",
    "writer_for",
    "writer_installed",
    "writes_in_flight",
]

_log = get_logger("reconcile.writer")


def offloads_writes(backend: StateBackend) -> bool:
    """Whether ``backend``'s calls should run on a dedicated writer thread.

    Declared by :attr:`StateBackend.offload_writes`. Any value other than a plain
    ``True``, including a missing attribute, runs inline.
    """
    return getattr(backend, "offload_writes", False) is True


def writes_in_flight(backend: StateBackend, parallelism: int | None = None) -> int:
    """How many state writes ``backend`` may have in flight at once, within ``parallelism``.

    :attr:`StateBackend.write_concurrency`, capped by ``parallelism`` since extra
    writers would stay idle. Anything other than a positive ``int`` (a missing
    attribute, ``True``) means ``1``: concurrency must be declared, never inferred.
    """
    declared = getattr(backend, "write_concurrency", 1)
    if type(declared) is not int or declared < 1:
        declared = 1
    cap = parallelism if parallelism is not None and parallelism >= 1 else DEFAULT_PARALLELISM
    return min(declared, cap)


class ReadWriteGate:
    """A readers-writer gate: many shared holders, or one exclusive, never both.

    Writer-preferring: once an exclusive caller is waiting, new shared callers
    wait behind it, so a steady stream of writes cannot starve a lease renewal.

    Thread-based because the two sides meet across threads: a shared hold is
    released on the pool thread that finished the write, while the exclusive side
    is taken, blocking, on the event-loop thread by the synchronous lease
    heartbeat. An asyncio gate could not observe a write draining while the
    heartbeat blocks the loop.
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._shared = 0
        self._exclusive = False
        self._exclusive_waiting = 0

    def acquire_shared(self) -> None:
        with self._cond:
            while self._exclusive or self._exclusive_waiting:
                self._cond.wait()
            self._shared += 1

    def release_shared(self) -> None:
        with self._cond:
            self._shared -= 1
            if self._shared == 0:
                self._cond.notify_all()

    def acquire_exclusive(self) -> None:
        with self._cond:
            self._exclusive_waiting += 1
            try:
                while self._exclusive or self._shared:
                    self._cond.wait()
            finally:
                self._exclusive_waiting -= 1
                # An interrupted waiter may have been holding shared callers back.
                self._cond.notify_all()
            self._exclusive = True

    def release_exclusive(self) -> None:
        with self._cond:
            self._exclusive = False
            self._cond.notify_all()


class StateWriter:
    """Runs state-backend calls off the event loop, as concurrently as the backend allows.

    A synchronous ``put`` against S3 or Postgres is a network round trip that,
    made on the loop, stalls every in-flight node. With ``offload``, calls run on
    a pool of ``workers`` threads (from :func:`writes_in_flight`; ``1`` makes it a
    single-writer FIFO) while the loop keeps scheduling provider calls.

    A readers-writer :class:`ReadWriteGate` arbitrates two kinds of call:

    - :meth:`run` (the executor's state writes) holds the gate shared, so up to
      ``workers`` of them overlap;
    - :meth:`call` (the lock scaffold's acquire, renew, ``bind_lease`` and
      release) holds it exclusively: it waits for every admitted write to land
      and admits none until done. A write between a renewal and the following
      lease rebind would carry the superseded fence.

    A write takes its shared hold on the submitting thread, not on the pool
    thread. This makes the renewal and the rebind (back-to-back :meth:`call`
    invocations on the loop thread) a single window: writes submitted earlier
    drain before the renewal, and none can be submitted until the loop runs again.

    :meth:`run` returns only once the call completes, as on the inline path, so a
    write-ahead row is durable before the provider call that follows it and a
    node's persist lands before its dependents start. Writes with the same ``key``
    (a node id) are serialized in submission order.
    """

    def __init__(self, *, offload: bool, workers: int = 1) -> None:
        self.workers = max(1, workers) if offload else 1
        self._pool = (
            ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="atlantide-state")
            if offload
            else None
        )
        self._gate = ReadWriteGate()
        #: key -> (lock, holders + waiters); touched only from the event loop.
        self._keys: dict[str, tuple[asyncio.Lock, list[int]]] = {}

    @override
    def __repr__(self) -> str:
        return f"StateWriter(offloaded={self.offloaded}, workers={self.workers})"

    @property
    def offloaded(self) -> bool:
        return self._pool is not None

    async def run[T](self, fn: Callable[..., T], /, *args: Any, key: str | None = None) -> T:
        """Await ``fn(*args)`` on the writer pool; inline when not offloading.

        ``key`` (a node id) orders this write after any earlier in-flight write with
        the same key, in addition to the ordering the executor's own awaits give.

        A submitted write is never abandoned: a cancellation waits for it to land
        and is re-raised after, as with an inline call, so a later state call (e.g.
        a rollback's) cannot run alongside it.
        """
        if self._pool is None:
            return fn(*args)
        if key is None:
            return await self._submit(self._pool, fn, args)
        async with self._ordered(key):
            return await self._submit(self._pool, fn, args)

    async def _submit[T](self, pool: ThreadPoolExecutor, fn: Callable[..., T], args: Any) -> T:
        # Admission blocks the loop only while another thread holds or awaits the
        # gate exclusively (the engine's lock calls run on this loop's thread). The
        # wait is bounded by writes already on pool threads, which drain without
        # the loop.
        self._gate.acquire_shared()
        try:
            future = asyncio.get_running_loop().run_in_executor(
                pool, partial(self._shared_call, fn, args)
            )
        except BaseException:
            self._gate.release_shared()
            raise
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            await _settle(future)
            raise

    def _shared_call[T](self, fn: Callable[..., T], args: Any) -> T:
        try:
            return fn(*args)
        finally:
            self._gate.release_shared()

    @contextlib.asynccontextmanager
    async def _ordered(self, key: str) -> AsyncIterator[None]:
        entry = self._keys.get(key)
        if entry is None:
            entry = self._keys[key] = (asyncio.Lock(), [0])
        lock, users = entry
        users[0] += 1
        try:
            async with lock:
                yield
        finally:
            users[0] -= 1
            if not users[0]:
                del self._keys[key]

    def call[T](self, fn: Callable[..., T], /, *args: Any) -> T:
        """Blocking ``fn(*args)``, alone: after every admitted write, before any new one.

        Must not be called from inside a :meth:`run` callable: it would wait for its
        own shared hold. The callable runs on a writer thread, which is idle once
        every write has drained.
        """
        if self._pool is None:
            return fn(*args)
        requested = time.monotonic()
        self._gate.acquire_exclusive()
        admitted = time.monotonic()
        try:
            return self._pool.submit(fn, *args).result()
        finally:
            self._gate.release_exclusive()
            _log.debug(
                "exclusive state call %s: waited %.3fs for the gate, ran %.3fs",
                getattr(fn, "__qualname__", type(fn).__name__),
                admitted - requested,
                time.monotonic() - admitted,
            )

    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)


async def _settle(future: asyncio.Future[Any]) -> None:
    """Wait for ``future`` to finish, whatever further cancellations arrive."""
    while not future.done():
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait((future,))
    if not future.cancelled():
        future.exception()  # mark retrieved; the caller re-raises the cancellation


_INLINE = StateWriter(offload=False)

#: The writer installed for a backend, by ``id`` (backends need not be hashable).
#: Holds the backend too, so the id cannot be recycled while installed.
_WRITERS: dict[int, tuple[StateBackend, StateWriter]] = {}


@contextlib.contextmanager
def state_writer(
    backend: StateBackend, *, offload: bool | None = None, parallelism: int | None = None
) -> Iterator[StateWriter]:
    """Install a :class:`StateWriter` for ``backend`` for the duration of the block.

    The executor picks it up through :func:`writer_for`. Re-entrant: a nested
    install reuses the outer writer, so there is never more than one per backend.
    ``parallelism`` caps the writer pool (see :func:`writes_in_flight`).
    """
    installed = _WRITERS.get(id(backend))
    if installed is not None:
        yield installed[1]
        return
    writer = StateWriter(
        offload=offloads_writes(backend) if offload is None else offload,
        workers=writes_in_flight(backend, parallelism),
    )
    _WRITERS[id(backend)] = (backend, writer)
    try:
        yield writer
    finally:
        del _WRITERS[id(backend)]
        writer.close()


def writer_for(backend: StateBackend) -> StateWriter:
    """The writer installed for ``backend``, or one that calls inline."""
    installed = _WRITERS.get(id(backend))
    return installed[1] if installed is not None else _INLINE


def writer_installed(backend: StateBackend) -> bool:
    """Whether a :func:`state_writer` block for ``backend`` is open, i.e. a run is using it.

    A locked run checks this before installing its own: :func:`state_writer`
    would hand it the other run's writer, whose pool and gate belong to that run.
    """
    return id(backend) in _WRITERS


class SerializedBackend(StateBackend):
    """``backend`` as the lock scaffold sees it: every call queued on the state writer.

    The lease heartbeat runs on the event loop while writes run on writer threads.
    A direct backend call would race an in-flight write on the same client, and a
    write landing between a renewal and the lease rebind is refused as superseded.
    Through :meth:`StateWriter.call`, a renewal waits for admitted writes and no
    write is admitted before the rebind.

    Anything not overridden is delegated as-is.
    """

    def __init__(self, inner: StateBackend, writer: StateWriter) -> None:
        self._inner = inner
        self._writer = writer

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @override
    def load(self) -> StateGraph:
        return self._writer.call(self._inner.load)

    @override
    def put(self, node: StateNode) -> None:
        self._writer.call(self._inner.put, node)

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        self._writer.call(self._inner.put_many, nodes)

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        self._writer.call(self._inner.replace_many, delete_ids, nodes)

    @override
    def delete(self, node_id: str) -> None:
        self._writer.call(self._inner.delete, node_id)

    @override
    def serial(self) -> int:
        return self._writer.call(self._inner.serial)

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        return self._writer.call(self._inner.acquire_lock, owner, ttl_seconds, scope)

    @override
    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        return self._writer.call(self._inner.renew_lock, owner, ttl_seconds, scope)

    @override
    def bind_lease(self, lease: Lease | None) -> None:
        self._writer.call(self._inner.bind_lease, lease)

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        return self._writer.call(self._inner.release_lock, owner)

    @override
    def locks(self) -> dict[str, Lease]:
        return self._writer.call(self._inner.locks)

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        return self._writer.call(self._inner.force_unlock, node_ids)

    @override
    def checkpoint(self) -> None:
        # Forwarded explicitly: the inherited no-op would otherwise shadow
        # `__getattr__` and skip the inner backend's compaction.
        self._writer.call(self._inner.checkpoint)

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        self._writer.call(lambda: self._inner.set_outputs(outputs, remove=remove))

    @override
    def outputs(self) -> dict[str, Any]:
        return self._writer.call(self._inner.outputs)

    @override
    def close(self) -> None:
        self._inner.close()
