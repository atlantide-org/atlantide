"""Lease renewal: a run outliving its TTL, and one that loses its lock part-way.

The property under test is not "renewal happens" but its two consequences: a run
longer than the TTL still finishes, and a run whose hold is taken writes nothing
more. The second is the one that matters — a lost lease means a second writer
already exists, so every further write is a write into someone else's state.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from returns.result import Failure, Success

from atlantide.core.errors import LeaseLostError, LockError
from atlantide.engine.locking import with_lock
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import DEFAULT_LOCK_POLICY, Lease, LeaseGuard, LockPolicy
from tests.support import (
    Box,
    FakeClock,
    FakeProvider,
    SpyBackend,
    engine_for,
    globals_of,
    state_node,
)

SCOPE = frozenset({"default:test.Box:a"})

#: Short enough to keep the tests quick, still ordered ttl > interval > 0.
FAST = LockPolicy(ttl=0.30, renew_interval=0.02, renew_grace=0.0)


async def _sleep_through(renewals: int, policy: LockPolicy = FAST) -> None:
    """Block long enough for roughly ``renewals`` renewal intervals to elapse."""
    await asyncio.sleep(policy.renew_interval * renewals + policy.renew_interval / 2)


async def test_a_run_longer_than_the_ttl_keeps_its_lease() -> None:
    """The whole point: the TTL bounds a dead run's blast radius, not a live
    run's deadline. Without renewal this run's lease lapses mid-flight."""
    backend = SpyBackend()

    async def slow() -> str:
        await _sleep_through(4)
        return "done"

    result = await with_lock(backend, SCOPE, slow, policy=FAST)

    assert isinstance(result, Success)
    assert result.unwrap() == "done"
    assert backend.count("renew_lock") >= 3, backend.names()
    assert backend.count("release_lock") == 1


async def test_a_short_run_is_not_renewed_at_all() -> None:
    """Renewal is a background task, not a cost on every run."""
    backend = SpyBackend()

    async def quick() -> str:
        return "done"

    assert isinstance(await with_lock(backend, SCOPE, quick, policy=FAST), Success)
    assert backend.count("renew_lock") == 0
    assert backend.count("release_lock") == 1


async def test_losing_the_lease_stops_the_run_and_reports_it() -> None:
    backend = SpyBackend(fail_lock_after=1)  # the initial acquire, then nothing

    async def slow() -> str:
        await _sleep_through(6)
        return "should not get here"

    result = await with_lock(backend, SCOPE, slow, policy=FAST)

    assert isinstance(result, Failure)
    error = result.failure()
    assert isinstance(error, LeaseLostError)
    assert "refresh" in str(error), "the message has to name the recovery step"
    assert "rolled back" in str(error), "and say that nothing was undone"


async def test_no_state_is_written_after_the_lease_is_lost() -> None:
    """The assertion the whole design exists for.

    A run that keeps writing after losing its lock is writing into state another
    run now owns — the silent-divergence case. `writes_after` is deliberately
    about ordering rather than totals: writes made *before* the loss are correct
    and expected.
    """
    backend = SpyBackend(fail_lock_after=1)

    async def writes_forever() -> None:
        for index in range(100):
            backend.put(state_node(f"n{index}", type="test.Box"))
            await asyncio.sleep(FAST.renew_interval / 2)

    result = await with_lock(backend, SCOPE, writes_forever, policy=FAST)

    assert isinstance(result, Failure)
    assert backend.count("put") > 0, "the run really was writing before the loss"
    assert backend.writes_after("lock_refused") == []


async def test_the_lock_is_released_even_when_the_lease_was_lost() -> None:
    """Releasing a hold this run no longer owns is a no-op at the backend, but
    skipping the call would leak the row for every backend that still has one."""
    backend = SpyBackend(fail_lock_after=1)

    async def slow() -> None:
        await _sleep_through(6)

    await with_lock(backend, SCOPE, slow, policy=FAST)
    assert backend.count("release_lock") == 1


async def test_a_run_that_fails_on_its_own_terms_keeps_its_own_error() -> None:
    """Renewal is a supervisor, not a peer: its presence must not reshape an
    ordinary provider failure into something else."""
    backend = SpyBackend()

    async def boom() -> None:
        raise RuntimeError("provider exploded")

    with pytest.raises(RuntimeError, match="provider exploded"):
        await with_lock(backend, SCOPE, boom, policy=FAST)
    assert backend.count("release_lock") == 1


async def test_a_lock_conflict_at_acquisition_is_not_a_lease_loss() -> None:
    """Never started and stopped part-way are different situations: the first
    wrote nothing, so it must not tell the user to go and check for damage."""
    backend = SpyBackend(fail_lock_after=0)

    async def never_runs() -> None:  # pragma: no cover - must not be reached
        raise AssertionError("run must not start without the lock")

    result = await with_lock(backend, SCOPE, never_runs, policy=FAST)
    assert isinstance(result, Failure)
    assert isinstance(result.failure(), LockError)
    assert not isinstance(result.failure(), LeaseLostError)


# -- LeaseGuard ---------------------------------------------------------------


def test_guard_with_no_lease_permits_everything() -> None:
    """The unlocked paths (a read-only refresh, an embedding caller doing its own
    locking) share the executor, and must not be refused by a guard they never
    populated."""
    LeaseGuard().check()  # does not raise


def test_guard_refuses_a_write_inside_the_grace_window() -> None:
    """Local clock check, so it can catch an expiry the renewal task has not yet
    noticed — the window between renewal failing and cancellation arriving."""
    clock = FakeClock()
    guard = LeaseGuard(grace=30.0, clock=clock)
    guard.renewed(Lease(owner="me", expires_at=clock() + 100.0))

    guard.check()  # 100s left, comfortably outside the grace window
    clock.advance(75.0)  # 25s left, inside it
    with pytest.raises(LeaseLostError, match="refresh"):
        guard.check()


def test_a_guard_that_has_failed_stays_failed() -> None:
    """One transient-looking check must not let a later one pass: the lease is
    not coming back."""
    clock = FakeClock()
    guard = LeaseGuard(grace=0.0, clock=clock)
    guard.renewed(Lease(owner="me", expires_at=clock() + 10.0))
    clock.advance(20.0)

    with pytest.raises(LeaseLostError):
        guard.check()
    clock.t = 0.0  # even if time somehow ran backwards
    with pytest.raises(LeaseLostError):
        guard.check()


# -- LockPolicy ---------------------------------------------------------------


def test_the_default_policy_can_renew_several_times_per_ttl() -> None:
    """A single slow or dropped renewal must not be fatal."""
    DEFAULT_LOCK_POLICY.validate()
    assert DEFAULT_LOCK_POLICY.ttl / DEFAULT_LOCK_POLICY.renew_interval >= 3


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (LockPolicy(ttl=0), "must be positive"),
        (LockPolicy(renew_interval=0), "must be positive"),
        (LockPolicy(ttl=60, renew_interval=60), "shorter than lock_ttl"),
        (LockPolicy(ttl=60, renew_interval=90), "shorter than lock_ttl"),
        (LockPolicy(ttl=20, renew_interval=5, renew_grace=30), "renew grace"),
    ],
)
def test_a_policy_that_cannot_keep_a_lease_alive_is_rejected(
    policy: LockPolicy, expected: str
) -> None:
    """Caught at configuration time: an interval longer than the TTL guarantees
    every run loses its lease, which would look like flaky contention."""
    with pytest.raises(LockError, match=expected):
        policy.validate()


# -- end to end ---------------------------------------------------------------


async def test_an_apply_that_loses_its_lease_stops_without_rolling_back() -> None:
    """A compensation is a provider call *and* a state write.

    A run that no longer holds the lock must not make either: another run may now
    be acting on the same resources, and undoing "its" creates could destroy
    theirs. So the saga is deliberately skipped, and the error carries the
    recovery step instead.
    """

    class SlowProvider(FakeProvider):
        """Creates slowly enough for the lease to lapse mid-call."""

        async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
            await _sleep_through(6)
            return await super().create(ctx, res)

    backend = SpyBackend(fail_lock_after=1)
    engine = engine_for(Box, provider=SlowProvider(), backend=backend, lock_policy=FAST)

    result = await engine.apply(
        "Box('a', size=1)\n", extra_globals=globals_of(Box), on_failure="rollback"
    )

    assert isinstance(result, Failure)
    assert isinstance(result.failure(), LeaseLostError)
    assert backend.writes_after("lock_refused") == [], "no compensating write either"


async def test_writes_keep_working_across_a_renewal() -> None:
    """Renewal must rebind the backend, not only refresh the guard.

    A backend that mints a fresh epoch per acquisition hands the renewal a
    *newer* fence than the one the run is bound to. Without rebinding, every
    write after the first renewal is refused as "superseded" — by a lease that is
    in fact this same run's. Nothing in the lock protocol catches that; only
    writing after a renewal does.
    """
    backend = MemoryStateBackend()
    written: list[int] = []

    async def writes_across_renewals() -> None:
        for index in range(6):
            backend.put(state_node(f"n{index}", type="test.Box"))
            written.append(index)
            await _sleep_through(1)

    result = await with_lock(
        backend,
        frozenset(f"default:test.Box:n{i}" for i in range(6)),
        writes_across_renewals,
        policy=FAST,
    )

    assert isinstance(result, Success), result
    assert len(written) == 6
    assert len(backend.load().nodes) == 6


async def test_the_lease_is_unbound_once_the_run_ends() -> None:
    """Left bound, the backend would refuse every later administrative write in
    the same process — `state restore` after an apply, for instance."""
    backend = MemoryStateBackend()

    async def noop() -> None:
        return None

    await with_lock(backend, frozenset({"default:test.Box:a"}), noop, policy=FAST)
    backend.put(state_node("elsewhere", type="test.Box"))  # unfenced again


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
