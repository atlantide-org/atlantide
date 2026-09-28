"""State locking: owner identity, lock scope, and the acquire/renew/run/release shape.

Holds are per node id, but :func:`apply_scope` covers the whole reachable graph,
so two applies over the same stack serialize while applies over disjoint configs
do not, provided each has its own backend instance (one instance binds one
lease; see :meth:`~atlantide.engine.runs.LockedRuns.run_locked`). The lease is
renewed for as long as the run lasts (see
:class:`~atlantide.state.leases.LockPolicy`), so its TTL bounds how long a *dead*
run blocks others rather than how long a live one may take.
"""

from __future__ import annotations

import asyncio
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Set
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Literal

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError
from atlantide.core.errors import LeaseLostError, LockError, StateError
from atlantide.core.events import (
    LEASE_ACQUIRE,
    LEASE_LOST,
    LEASE_RENEW,
    ApplyEvent,
    EventSink,
    no_sink,
)
from atlantide.core.logging import get_logger
from atlantide.engine.model import Plan
from atlantide.engine.result import forward_failure
from atlantide.graph.cbd import effective_cbd
from atlantide.reconcile.resolve import cbd_companion_id
from atlantide.state import (
    DEFAULT_LOCK_POLICY,
    Lease,
    LeaseGuard,
    LockPolicy,
    StateBackend,
    StateGraph,
)

__all__ = [
    "apply_scope",
    "held_lock",
    "lock_owner",
    "require_no_new_nodes",
    "with_lock",
]

_log = get_logger("engine.locking")


def lock_owner() -> str:
    """A fresh lock-owner identity: host, pid, and a per-acquisition token.

    The token identifies a run, not a process: concurrent engines in one process
    must not share an owner, since ``Lease.blocks`` returns False for the same
    owner and ``release_lock`` drops every row that owner holds.
    """
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class _LeaseSession:
    """One run's hold on the state lock: who holds it, over what, and on what terms.

    Every value here is fixed for the life of a run. The current lease and its loss
    are tracked in :class:`LeaseGuard` and in the backend's bound lease.
    """

    backend: StateBackend
    owner: str
    scope: frozenset[str]
    policy: LockPolicy
    guard: LeaseGuard
    events: EventSink
    run_id: str

    def emit(self, kind: str, **detail: Any) -> None:
        """Record a lease event, always naming the owner it happened to."""
        self.events(
            ApplyEvent(self.run_id, time.time(), kind, detail={"owner": self.owner, **detail})
        )

    def emit_quietly(self, kind: str, **detail: Any) -> None:
        """:meth:`emit` from the renewal task, where a sink error is logged, not raised.

        Raising there would end renewal, so the lease would lapse mid-run, and a
        lost lease would no longer cancel the run.
        """
        try:
            self.emit(kind, **detail)
        except Exception as exc:
            _log.warning("event sink failed on %s: %s: %s", kind, type(exc).__name__, exc)

    def renew(self) -> Result[Lease, LockError]:
        return _timed(
            "renew",
            lambda: self.backend.renew_lock(self.owner, self.policy.ttl, self.scope),
            self.owner,
            self.scope,
        )

    def hold(self, lease: Lease) -> None:
        """Take ``lease`` as the current one: guard against it, and fence writes on it.

        Rebinding is required: a backend that mints a fresh epoch per acquisition
        returns a newer fence on renewal, and writes fenced on the older one are
        refused as superseded.
        """
        self.guard.renewed(lease)
        self.backend.bind_lease(lease)

    def release(self) -> None:
        _unbind_and_release(self.backend, self.owner)


def _unbind_and_release(backend: StateBackend, owner: str) -> None:
    """Drop the write fence and the lock rows; the release runs even if unbinding fails."""
    try:
        backend.bind_lease(None)
    finally:
        _log.debug("release lock owner=%s", owner)
        backend.release_lock(owner)


#: The DEBUG line for each timed lock call. Pinned: tests read these messages.
_TIMED_FORMATS = {
    "acquire": "acquire lock owner=%s nodes=%d ok=%s fence=%s in %.3fs",
    "renew": "renew lock owner=%s nodes=%d ok=%s fence=%s in %.3fs",
}


def _timed(
    verb: Literal["acquire", "renew"],
    call: Callable[[], Result[Lease, LockError]],
    owner: str,
    scope: Set[str],
) -> Result[Lease, LockError]:
    """Run one lock ``call``, logged at DEBUG with its outcome, fence and duration."""
    started = time.monotonic()
    lock = call()
    _log.debug(
        _TIMED_FORMATS[verb],
        owner,
        len(scope),
        isinstance(lock, Success),
        lock.unwrap().fence if isinstance(lock, Success) else None,
        time.monotonic() - started,
    )
    return lock


def _acquire(
    backend: StateBackend, owner: str, ttl: float, scope: Set[str]
) -> Result[Lease, LockError]:
    return _timed("acquire", lambda: backend.acquire_lock(owner, ttl, scope), owner, scope)


@contextmanager
def _released_after(release: Callable[[], None]) -> Iterator[None]:
    """Run the block, then ``release``; a release failure never masks the block's outcome.

    If the block raised, a release error is attached to that exception as a note
    and the original propagates. If the block succeeded, a release error
    propagates, since the lock stays held until its TTL.
    """
    try:
        yield
    except BaseException as exc:
        try:
            release()
        except Exception as release_exc:
            exc.add_note(
                f"additionally, releasing the state lock failed: "
                f"{type(release_exc).__name__}: {release_exc}"
            )
        raise
    release()


async def with_lock[T](  # noqa: PLR0913 - keyword-only lock options
    backend: StateBackend,
    scope: frozenset[str],
    run: Callable[[], Awaitable[T]],
    *,
    policy: LockPolicy = DEFAULT_LOCK_POLICY,
    guard: LeaseGuard | None = None,
    owner: str | None = None,
    events: EventSink = no_sink,
    run_id: str = "",
) -> Result[T, AtlantideError]:
    """Acquire the state lock over ``scope``, hold it for the whole run, release.

    The lease is renewed in the background for as long as ``run`` lasts. Losing
    the lease cancels ``run`` and surfaces as a :class:`LeaseLostError` failure
    (see :func:`_renew_until_cancelled`).

    A lock conflict at acquisition surfaces as the backend's ``Failure``
    untouched; ``run`` is only awaited while the lease is held.
    """
    policy.validate()
    # A caller may supply the owner to keep its audit records and lease under one
    # identity; otherwise a per-acquisition owner keeps concurrent runs distinct.
    owner = owner or lock_owner()
    lock = _acquire(backend, owner, policy.ttl, scope)
    if isinstance(lock, Failure):
        return forward_failure(lock)
    session = _LeaseSession(
        backend=backend,
        owner=owner,
        scope=scope,
        policy=policy,
        guard=guard if guard is not None else LeaseGuard(grace=policy.renew_grace),
        events=events,
        run_id=run_id or owner,
    )
    # Everything after a successful acquire runs under the release guard: an
    # audit sink that raises on LEASE_ACQUIRE must not leave the lock held until
    # its TTL expires.
    with _released_after(session.release):
        session.hold(lock.unwrap())
        session.emit(LEASE_ACQUIRE)
        return await _run_renewed(session, run)


async def _run_renewed[T](
    session: _LeaseSession, run: Callable[[], Awaitable[T]]
) -> Result[T, AtlantideError]:
    """Await ``run`` with a renewal task alongside it, cancelling one with the other.

    Uses plain tasks rather than a ``TaskGroup``: a group wraps provider failures
    in an ``ExceptionGroup`` the executor does not produce, and the renewal task
    is a supervisor whose end is not a failure of the run.
    """
    running = asyncio.ensure_future(run())
    renewing = asyncio.ensure_future(_renew_until_cancelled(session, running))
    try:
        return Success(await running)
    except asyncio.CancelledError:
        # Distinguish a cancellation caused by the lost lease from an interrupt
        # the caller requested; only the former is reported as a failure.
        if session.guard.lost is not None:
            return Failure(session.guard.lost)
        raise
    finally:
        renewing.cancel()
        try:
            await renewing
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # The renewal task is a supervisor: its failure must not replace the
            # run's own result or error.
            _log.warning("lease renewal task failed: %s: %s", type(exc).__name__, exc)


async def _renew_until_cancelled[T](session: _LeaseSession, running: asyncio.Future[T]) -> None:
    """Push the lease's expiry out on an interval until the run ends.

    A renewal ``Failure`` means the hold lapsed and another owner took it:
    acquiring is reentrant for the same owner, and an unreachable store raises
    instead. A second writer already exists, so the run is stopped before it
    writes anything else.

    The run is cancelled rather than flagged so that a node blocked in a long
    provider poll stops too; :meth:`LeaseGuard.check` covers the window before
    the cancellation is delivered.

    Runs on the event loop thread and blocks it for each renewal. For an
    offloaded backend the engine routes every lock call through
    :meth:`~atlantide.reconcile.writer.StateWriter.call`, which holds the writer
    exclusively, so a renewal and the rebind that follows it form one window no
    state write can enter. A separate heartbeat thread would need the same
    exclusion.
    """
    while True:
        await asyncio.sleep(session.policy.renew_interval)
        try:
            renewed = session.renew()
            if isinstance(renewed, Success):
                session.hold(renewed.unwrap())
        except Exception as exc:
            # A store error (reconnect, throttle) does not show the hold lapsed,
            # and raising here would end renewal. Renewal continues on the normal
            # cadence; if the lease decays meanwhile, the guard's grace check
            # refuses the next write.
            session.emit_quietly(LEASE_RENEW, error=str(exc))
            continue
        if isinstance(renewed, Failure):
            session.guard.fail(
                LeaseLostError(
                    f"lost the state lock part-way through this run: "
                    f"{renewed.failure()}. Nothing has been rolled back — a "
                    f"compensation is itself a write, and another run may now own "
                    f"these resources. Run `atlantide refresh` to see what exists "
                    f"before applying again"
                )
            )
            running.cancel()
            session.emit_quietly(LEASE_LOST, reason=str(renewed.failure()))
            return
        session.emit_quietly(LEASE_RENEW)


@contextmanager
def held_lock(
    backend: StateBackend, scope: Set[str], *, policy: LockPolicy = DEFAULT_LOCK_POLICY
) -> Iterator[Lease]:
    """Synchronous :func:`with_lock`: hold ``scope`` for the block, always release.

    The administrative commands (``state backup``/``restore``/``migrate``) are
    synchronous and touch state directly rather than through the engine, but need
    the same exclusion as an apply: a snapshot read row-by-row during an apply is
    torn and indistinguishable from a complete one.

    A lock conflict raises rather than returning a ``Result``, since every caller
    is a CLI command that aborts on it.

    Not renewed: these commands do one bounded read or write within the TTL, and
    have no event loop for a background task.
    """
    owner = lock_owner()
    lock = _acquire(backend, owner, policy.ttl, scope)
    if isinstance(lock, Failure):
        raise lock.failure()
    with _released_after(lambda: _unbind_and_release(backend, owner)):
        backend.bind_lease(lock.unwrap())
        yield lock.unwrap()


def require_no_new_nodes(fresh: StateGraph, scope: Set[str], command: str, suffix: str) -> None:
    """Refuse to go on when state gained rows outside ``scope`` while the lock was awaited.

    Every run that re-reads state under the lease (see ``README.md``) sized its
    lock from a pre-lock snapshot. A row created meanwhile is outside the lease,
    and omitting it would report success for work that skipped it. Raises
    ``StateError`` naming the new ids, ``command`` and what to do (``suffix``).
    """
    created = set(fresh.nodes) - scope
    if created:
        raise StateError(
            f"state gained node(s) while {command} waited for the lock: "
            + ", ".join(sorted(created))
            + f" — {suffix}"
        )


def apply_scope(plan_obj: Plan, prior: StateGraph) -> frozenset[str]:
    """The lock scope for an apply, sized for a changeset recomputed under the lease.

    An apply re-diffs once the lease is held, and actions can change (a NOOP may
    become a CREATE, a state-only node a DELETE). The scope therefore covers every
    reachable node: the whole desired graph plus everything in state. Applies over
    disjoint configs still run concurrently.

    ``graph.deps`` is keyed by every IR node, and ``build_graph`` rejects an edge
    to an unknown id, so it already holds each node's dependency closure. An
    alias's old id is a state row, and its new id a desired node.

    A create-before-destroy replace first copies the old row to its companion id
    (``<id>~replaced``), so every node the diff replaces create-before-destroy
    (one the config marks, or anything such a node depends on: see
    :func:`~atlantide.graph.cbd.effective_cbd`) brings its companion into scope.
    The set is taken from the IR because the re-diff under the lease may replace
    a node the pre-lock plan did not. Only those nodes: a companion per node
    would double the lock rows. A companion left by an earlier run is a state
    row, so it is covered already.
    """
    companions = frozenset(cbd_companion_id(n) for n in effective_cbd(plan_obj.compiled.ir))
    return frozenset(plan_obj.compiled.graph.deps) | frozenset(prior.nodes) | companions
