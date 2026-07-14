"""Whole-state locking: owner identity, lock scope, and the acquire/run/release shape."""

from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable
from typing import TypeVar

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError
from atlantide.graph.model import DiGraph
from atlantide.reconcile import ChangeSet
from atlantide.state.backend import StateBackend

#: Lock lease time-to-live, in seconds.
LOCK_TTL = 300.0

T = TypeVar("T")


def lock_owner() -> str:
    """This process's lock-owner identity (host + pid)."""
    return f"{socket.gethostname()}-{os.getpid()}"


async def with_lock(
    backend: StateBackend, scope: frozenset[str], run: Callable[[], Awaitable[T]]
) -> Result[T, AtlantideError]:
    """Acquire the state lock over ``scope``, run, and always release.

    A lock conflict surfaces as the backend's ``Failure`` untouched; ``run`` is
    only awaited while the lease is held.
    """
    owner = lock_owner()
    lock = backend.acquire_lock(owner, LOCK_TTL, scope)
    if isinstance(lock, Failure):
        return Failure(lock.failure())
    try:
        return Success(await run())
    finally:
        backend.release_lock(owner)


def lock_scope(changeset: ChangeSet, graph: DiGraph) -> frozenset[str]:
    """Node ids to lock: the actionable (non-NOOP) changes plus every node they
    depend on, blocking a concurrent apply that mutates a shared dependency."""
    scope: set[str] = set()
    for change in changeset.actionable:
        scope.add(change.node_id)
        scope |= _ancestors(graph, change.node_id)
    return frozenset(scope)


def _ancestors(graph: DiGraph, node_id: str) -> set[str]:
    """Transitive dependencies of ``node_id`` (nodes that must act before it)."""
    seen: set[str] = set()
    stack = list(graph.deps.get(node_id, ()))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(graph.deps.get(current, ()))
    return seen
