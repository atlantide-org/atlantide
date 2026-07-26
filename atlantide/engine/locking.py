"""State locking: owner identity, lock scope, and the acquire/renew/run/release shape.

Holds are per node id, but :func:`apply_scope` covers the whole reachable graph,
so two applies over the same stack serialize while applies over disjoint configs
do not. The lease is renewed for as long as the run lasts (see
:class:`~atlantide.state.backend.LockPolicy`), so its TTL bounds how long a *dead*
run blocks others rather than how long a live one may take.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator, Set
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, TypeVar

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError
from atlantide.core.errors import LeaseLostError, LockError
from atlantide.core.events import (
    LEASE_ACQUIRE,
    LEASE_LOST,
    LEASE_RENEW,
    ApplyEvent,
    EventSink,
    no_sink,
)
from atlantide.engine.model import Plan
from atlantide.engine.result import forward_failure
from atlantide.graph.model import DiGraph
from atlantide.graph.select import closure
from atlantide.reconcile import ChangeSet
from atlantide.state.backend import (
    DEFAULT_LOCK_POLICY,
    LOCK_TTL,
    Lease,
    LeaseGuard,
    LockPolicy,
    StateBackend,
    StateGraph,
)

__all__ = [
    "DEFAULT_LOCK_POLICY",
    "LOCK_TTL",
    "LockPolicy",
    "apply_scope",
    "held_lock",
    "lock_owner",
    "lock_scope",
    "with_lock",
]

T = TypeVar("T")


def lock_owner() -> str:
    """A fresh lock-owner identity: host, pid, and a per-acquisition token.

    The token identifies a *run*, not a process. Two engines driven concurrently
    in one process would otherwise share an owner string, and ``Lease.blocks``
    returns False for the same owner; ``release_lock`` also drops every row an
    owner holds, so the first to finish would release the other's locks.
    """
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


@dataclass(frozen=True, slots=True)
class _LeaseSession:
    """One run's hold on the state lock: who holds it, over what, and on what terms.

    Every value here is fixed for the life of a run and needed by all three
    stages of it (acquire, renew, release), which is why they travel together
    rather than as seven parameters down two call levels. The mutable half of the
    hold — the current lease and whether it was lost — lives in
    :class:`LeaseGuard` and in the backend's bound lease, not here.
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

    def renew(self) -> Result[Lease, LockError]:
        return self.backend.renew_lock(self.owner, self.policy.ttl, self.scope)

    def hold(self, lease: Lease) -> None:
        """Take ``lease`` as the current one: guard against it, and fence writes on it.

        Rebind, not just re-guard: a backend that mints a fresh epoch per
        acquisition hands a renewal a newer fence than the run is bound to, and
        every later write would be refused as superseded by this same run.
        """
        self.guard.renewed(lease)
        self.backend.bind_lease(lease)

    def release(self) -> None:
        self.backend.bind_lease(None)
        self.backend.release_lock(self.owner)


async def with_lock(
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

    The lease is renewed in the background for as long as ``run`` lasts, so the
    TTL bounds how long a *dead* run blocks others rather than how long a live
    one may take. Losing the lease cancels ``run`` and surfaces as a
    :class:`LeaseLostError` failure — see :func:`_renew_until_cancelled`.

    A lock conflict at acquisition surfaces as the backend's ``Failure``
    untouched; ``run`` is only awaited while the lease is held.
    """
    policy.validate()
    # Per-acquisition, so concurrent runs cannot alias. A caller may supply it to
    # keep its audit records and the lease it holds under one identity.
    owner = owner or lock_owner()
    lock = backend.acquire_lock(owner, policy.ttl, scope)
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
    session.hold(lock.unwrap())  # fence every write this run makes
    session.emit(LEASE_ACQUIRE)
    try:
        return await _run_renewed(session, run)
    finally:
        session.release()


async def _run_renewed(
    session: _LeaseSession, run: Callable[[], Awaitable[T]]
) -> Result[T, AtlantideError]:
    """Await ``run`` with a renewal task alongside it, cancelling one with the other.

    Deliberately not a ``TaskGroup``: a group turns an ordinary provider failure
    into an ``ExceptionGroup`` wrapping of a different shape than the executor
    already produces, and the renewal task is a supervisor rather than a peer —
    its ending should not be reported as a second failure of the run.
    """
    running = asyncio.ensure_future(run())
    renewing = asyncio.ensure_future(_renew_until_cancelled(session, running))
    try:
        return Success(await running)
    except asyncio.CancelledError:
        # Distinguish "the lease went away, so we cancelled the run" from an
        # interrupt the caller asked for; only the former is ours to report.
        if session.guard.lost is not None:
            return Failure(session.guard.lost)
        raise
    finally:
        renewing.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renewing


async def _renew_until_cancelled(session: _LeaseSession, running: asyncio.Future[T]) -> None:
    """Push the lease's expiry out on an interval until the run ends.

    A renewal ``Failure`` can only mean one thing: the hold lapsed and another
    owner took it, because acquiring is reentrant for the same owner and an
    unreachable store raises instead. So there is no retrying to be done — a
    second writer already exists — and the only safe move is to stop this run
    before it writes anything else.

    The run is cancelled rather than merely flagged so that a node blocked in a
    long provider poll does not keep going for another half hour; :meth:`
    LeaseGuard.check` covers the window before the cancellation is delivered.

    Runs on the event loop thread. Backend lock calls are synchronous, and the
    executor already makes synchronous backend writes from inside its tasks, so
    this adds no new blocking pattern — and the sqlite backend's connection is
    bound to its creating thread, which rules out a worker-thread heartbeat.
    """
    while True:
        await asyncio.sleep(session.policy.renew_interval)
        try:
            renewed = session.renew()
        except Exception as exc:
            # A store hiccup (reconnect, throttle) is not evidence the hold
            # lapsed — and raising here would propagate out of `_run_renewed`'s
            # finally-await and replace a successful run's result. Keep renewing
            # on the normal cadence; if the lease truly decays meanwhile, the
            # guard's grace check refuses the next write.
            session.emit(LEASE_RENEW, error=str(exc))
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
            session.emit(LEASE_LOST, reason=str(renewed.failure()))
            running.cancel()
            return
        session.hold(renewed.unwrap())
        session.emit(LEASE_RENEW)


@contextmanager
def held_lock(
    backend: StateBackend, scope: Set[str], *, policy: LockPolicy = DEFAULT_LOCK_POLICY
) -> Iterator[Lease]:
    """Synchronous :func:`with_lock`: hold ``scope`` for the block, always release.

    The administrative commands (``state backup``/``restore``/``migrate``) are
    synchronous and touch state directly rather than through the engine, but they
    need the same exclusion an apply gets: a snapshot read row-by-row while an
    apply is writing is torn, and a torn snapshot reads as a complete one.

    A lock conflict raises rather than returning a ``Result``, because every
    caller here is a CLI command whose only handling is to abort.

    Not renewed: these commands do one bounded read or write, so the TTL covers
    them outright and a background task would need an event loop they do not have.
    """
    owner = lock_owner()
    lock = backend.acquire_lock(owner, policy.ttl, scope)
    if isinstance(lock, Failure):
        raise lock.failure()
    backend.bind_lease(lock.unwrap())
    try:
        yield lock.unwrap()
    finally:
        backend.bind_lease(None)
        backend.release_lock(owner)


def lock_scope(changeset: ChangeSet, graph: DiGraph) -> frozenset[str]:
    """Node ids to lock: the actionable (non-NOOP) changes plus every node they
    depend on, so a concurrent apply cannot mutate a shared dependency.

    A DELETE's node is absent from the desired graph, so its dependencies come
    from the change's prior state; a concurrent run could otherwise be updating
    the dependency this one is about to delete.
    """
    scope: set[str] = set()
    for change in changeset.actionable:
        scope.add(change.node_id)
        scope |= closure(graph, graph.deps.get(change.node_id, ()), reverse=False)
        if change.prior is not None:
            scope |= set(change.prior.dependencies)
    return frozenset(scope)


def apply_scope(plan_obj: Plan, prior: StateGraph) -> frozenset[str]:
    """The lock scope for an apply, sized for a changeset recomputed under the lease.

    :func:`lock_scope` covers the changes one diff produced, but an apply re-diffs
    once the lease is held and the actions can move: a NOOP may become a CREATE, a
    node seen only in state may become a DELETE. The scope therefore covers every
    reachable node — the whole desired graph plus everything already in state —
    which still lets applies over disjoint configs run concurrently.
    """
    ids = set(plan_obj.compiled.graph.deps) | set(prior.nodes)
    for node in plan_obj.compiled.ir.nodes:
        ids.add(node.id)
        ids |= closure(
            plan_obj.compiled.graph,
            plan_obj.compiled.graph.deps.get(node.id, ()),
            reverse=False,
        )
    return frozenset(ids)
