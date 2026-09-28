"""Releasing the state lock: never skipped, never masking the run's own outcome.

An event sink that raises right after acquisition must not strand the lock
until its TTL, and a release that fails must not replace the exception the run
itself raised.
"""

from __future__ import annotations

from typing import Any

import pytest
from returns.result import Result, Success

from atlantide.core.errors import LockError
from atlantide.core.events import LEASE_ACQUIRE, ApplyEvent
from atlantide.engine.locking import held_lock, with_lock
from atlantide.state import MemoryStateBackend
from tests.support import debug_records

SCOPE = frozenset({"default:test.Box:a"})


class _ReleaseFails(MemoryStateBackend):
    """Releases (so the lock is freed) and then raises."""

    def release_lock(self, owner: str) -> Result[None, LockError]:
        super().release_lock(owner)
        raise RuntimeError("store unreachable during release")


def _is_free(backend: MemoryStateBackend) -> bool:
    return isinstance(backend.acquire_lock("someone-else", 30.0, SCOPE), Success)


async def test_a_sink_failing_on_acquire_still_releases() -> None:
    backend = MemoryStateBackend()

    def sink(event: ApplyEvent) -> None:
        if event.phase == LEASE_ACQUIRE:
            raise OSError("audit log full")

    async def run() -> str:  # pragma: no cover - never reached
        return "ran"

    with pytest.raises(OSError, match="audit log full"):
        await with_lock(backend, SCOPE, run, events=sink)
    assert _is_free(backend)


async def test_a_failing_release_does_not_mask_the_runs_error() -> None:
    backend = _ReleaseFails()

    async def run() -> Any:
        raise ValueError("the real problem")

    with pytest.raises(ValueError, match="the real problem") as info:
        await with_lock(backend, SCOPE, run)
    assert any("store unreachable" in note for note in info.value.__notes__)


async def test_a_failing_release_after_success_is_raised() -> None:
    backend = _ReleaseFails()

    async def run() -> str:
        return "ok"

    with pytest.raises(RuntimeError, match="store unreachable"):
        await with_lock(backend, SCOPE, run)


def test_held_lock_keeps_the_blocks_error_over_a_release_error() -> None:
    backend = _ReleaseFails()
    with pytest.raises(KeyError, match="inner") as info, held_lock(backend, SCOPE):
        raise KeyError("inner")
    assert any("store unreachable" in note for note in info.value.__notes__)


def test_held_lock_raises_a_release_error_after_a_clean_block() -> None:
    backend = _ReleaseFails()
    with pytest.raises(RuntimeError, match="store unreachable"), held_lock(backend, SCOPE):
        pass


def test_held_lock_releases_when_unbinding_fails() -> None:
    class _UnbindFails(MemoryStateBackend):
        def bind_lease(self, lease: Any) -> None:
            if lease is None:
                raise RuntimeError("unbind failed")
            super().bind_lease(lease)

    backend = _UnbindFails()
    with pytest.raises(RuntimeError, match="unbind failed"), held_lock(backend, SCOPE):
        pass
    assert _is_free(backend)


def test_acquire_and_release_are_logged_at_debug_with_ids_and_counts() -> None:
    backend = MemoryStateBackend()
    with debug_records("atlantide.engine.locking") as records, held_lock(backend, SCOPE) as lease:
        pass
    acquired, released = (record.getMessage() for record in records)
    assert acquired.startswith(f"acquire lock owner={lease.owner} nodes=1 ok=True fence=")
    assert released == f"release lock owner={lease.owner}"


def test_a_refused_acquire_is_logged_without_a_fence() -> None:
    backend = MemoryStateBackend()
    assert _is_free(backend) is True  # "someone-else" now holds SCOPE
    with (
        debug_records("atlantide.engine.locking") as records,
        pytest.raises(LockError),
        held_lock(backend, SCOPE),
    ):
        pass  # pragma: no cover - never reached
    [refused] = (record.getMessage() for record in records)
    assert "ok=False fence=None" in refused
