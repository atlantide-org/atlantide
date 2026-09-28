"""The backend contract under arbitrary run interleavings.

`tests/state/test_backend.py` states the contract as short examples of two to six
backend operations. Locking bugs can need longer sequences plus a second writer:

    acquire -> write -> renew -> write        (a backend that drops its cached
                                               ETag on renewal compare-and-swaps
                                               the next write against a re-read
                                               ETag and silently adopts a
                                               concurrent writer's document)

**The model is a run, not a call.** With `acquire`, `bind`, `unbind`, `release`
and `put` as independent equally-likely rules, a write under a stale binding is
almost never generated, and the machine passes with fencing switched off.

So the rules mirror what `with_lock` does. A run starts (acquire and bind),
writes, may renew, and finishes; a second run takes over once the first has
lapsed. The dangerous interleaving (start A, let it lapse, B takes over, A
writes) is then four likely steps.

**What is not modelled.** The machine never re-derives who should be allowed to
write: that is `fence_violation`'s job, and a second implementation here would
turn a model bug into a false failure. Where a rule needs to know who holds a
node it asks the backend's own `locks()`.
"""

from __future__ import annotations

from contextlib import suppress
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)

from atlantide.core.errors import StateError
from atlantide.state import Lease, StateBackend, StateNode
from tests.state.conftest import BackendFactory
from tests.support import FakeClock

#: A small fixed universe, so operations collide. Generated ids would rarely touch
#: the same node twice, and the interesting cases are two operations on one node.
NODES = ["n0", "n1"]
OWNERS = ["runner-a", "runner-b"]
TTL = 100.0

_scopes = st.lists(st.sampled_from(NODES), min_size=1, unique=True)


def _node(node_id: str, value: int) -> StateNode:
    return StateNode(
        id=node_id,
        type="t.T",
        provider="t",
        provider_version="1.0.0",
        input_hash=f"h{value}",
        outputs={"v": value},
    )


class BackendMachine(RuleBasedStateMachine):
    """One backend, driven through arbitrary interleavings of two runs."""

    def __init__(self, backend: StateBackend, clock: FakeClock) -> None:
        super().__init__()
        self.backend = backend
        self.clock = clock
        self.accepted: dict[str, StateNode] = {}
        #: The lease this backend is fenced against, as `with_lock` keeps it: taken
        #: at the start of a run and held (going stale if another run takes over)
        #: until a renewal replaces it or the run ends.
        self.bound: Lease | None = None
        self.max_serial = 0
        self.max_fence = 0
        self._reset()

    def _reset(self) -> None:
        """Return the shared backend to empty between examples.

        One backend per test rather than one per example: the postgres fixture
        owns a fixed pool of four schemas, so a fresh backend per example would
        exhaust it. Serial keeps climbing across examples; the invariant is only
        that it never decreases.
        """
        self.backend.bind_lease(None)
        for owner in OWNERS:
            self.backend.release_lock(owner)
        self.backend.force_unlock(set(NODES))
        for node_id in list(self.backend.load().nodes):
            self.backend.delete(node_id)
        if outputs := self.backend.outputs():
            self.backend.set_outputs({}, remove=list(outputs))
        self.accepted = {}
        self.bound = None
        self.max_serial = self.backend.serial()
        self.max_fence = max((held.fence for held in self.backend.locks().values()), default=0)

    # -- run lifecycle ----------------------------------------------------

    @rule(owner=st.sampled_from(OWNERS), scope=_scopes)
    def start_run(self, owner: str, scope: list[str]) -> None:
        """What `with_lock` does: take the lease and fence writes against it."""
        lease = self.backend.acquire_lock(owner, TTL, set(scope)).value_or(None)
        if lease is None:
            return  # someone else holds part of the scope; that run does not start
        assert lease.fence >= self.max_fence, (
            f"fence went backwards: {lease.fence} after {self.max_fence}. A reused "
            f"or lowered fence lets a superseded run's writes land."
        )
        self.max_fence = lease.fence
        self._assert_recorded(lease)
        self.backend.bind_lease(lease)
        self.bound = lease

    @rule(owner=st.sampled_from(OWNERS), scope=_scopes)
    def another_run_takes_over(self, owner: str, scope: list[str]) -> None:
        """A second run acquires, succeeding only once the first has lapsed.

        Does not rebind: this models the other process, while the backend under
        test keeps the lease it already had. That is the state fencing exists for.
        """
        lease = self.backend.acquire_lock(owner, TTL, set(scope)).value_or(None)
        if lease is not None:
            self.max_fence = max(self.max_fence, lease.fence)
            self._assert_recorded(lease)

    @precondition(lambda self: self.bound is not None)
    @rule()
    def the_bound_run_is_superseded(self) -> None:
        """The scenario fencing exists for, as one step: this run's lease lapses
        and the other run takes the nodes it was holding.

        Compound on purpose. Left to chance, the machine would have to draw
        `start_run`, then a long enough `pass_time`, then `another_run_takes_over`
        with the matching owner and an overlapping scope, then a write to a node in
        it: four ordered draws out of eight rules, which in practice never reaches
        the foreign-holder state.
        """
        assert self.bound is not None
        self.clock.advance(TTL + 1.0)
        other = next(owner for owner in OWNERS if owner != self.bound.owner)
        stolen = self.backend.acquire_lock(other, TTL, self.bound.scope).value_or(None)
        if stolen is not None:
            self.max_fence = max(self.max_fence, stolen.fence)
            self._assert_recorded(stolen)

    @precondition(lambda self: self.bound is not None)
    @rule()
    def renew(self) -> None:
        """`with_lock` renews and rebinds, because a renewal mints a new fence and
        the previous binding is stale the moment it does."""
        assert self.bound is not None
        renewed = self.backend.renew_lock(self.bound.owner, TTL, self.bound.scope).value_or(None)
        if renewed is None:
            return
        assert renewed.fence >= self.bound.fence
        self.max_fence = max(self.max_fence, renewed.fence)
        self._assert_recorded(renewed)
        self.backend.bind_lease(renewed)
        self.bound = renewed

    @precondition(lambda self: self.bound is not None)
    @rule()
    def finish_run(self) -> None:
        assert self.bound is not None
        self.backend.release_lock(self.bound.owner)
        self.backend.bind_lease(None)
        self.bound = None

    @rule(seconds=st.sampled_from([1.0, TTL + 1.0]))
    def pass_time(self, seconds: float) -> None:
        """A tick, or long enough to lapse every lease."""
        self.clock.advance(seconds)

    @rule(nodes=st.lists(st.sampled_from(NODES), min_size=1, unique=True))
    def operator_breaks_the_lock(self, nodes: list[str]) -> None:
        """`atlantide state unlock`: an operator clearing a dead run's hold by hand,
        possibly while that run is still alive."""
        self.backend.force_unlock(set(nodes))

    # -- writing ----------------------------------------------------------

    @rule(node_id=st.sampled_from(NODES), value=st.integers(min_value=0, max_value=3))
    def write(self, node_id: str, value: int) -> None:
        holder = self.backend.locks().get(node_id)
        node = _node(node_id, value)
        try:
            self.backend.put(node)
        except StateError:
            assert self.backend.load().nodes.get(node_id) == self.accepted.get(node_id), (
                "a refused write still changed the store"
            )
            return
        self._assert_permitted(node_id, holder)
        self.accepted[node_id] = node

    @rule(node_id=st.sampled_from(NODES))
    def erase(self, node_id: str) -> None:
        holder = self.backend.locks().get(node_id)
        present = node_id in self.accepted
        try:
            self.backend.delete(node_id)
        except StateError:
            assert self.backend.load().nodes.get(node_id) == self.accepted.get(node_id)
            return
        if present:
            # Only judged when there was something to delete. Deleting an absent
            # node changes nothing, and the backends disagree about refusing it;
            # see `test_deleting_an_absent_node_is_not_uniformly_fenced`.
            self._assert_permitted(node_id, holder)
        self.accepted.pop(node_id, None)

    def _assert_permitted(self, node_id: str, holder: Any) -> None:
        """The absolute rule, judged against the store's own record of holds.

        Two ways a bound run must be refused, and the store — never the run's own
        clock — decides both:

        * another owner holds the node, having taken it after this lease lapsed;
        * the same owner holds it at a *newer* fence, so this process re-acquired
          and the earlier run's in-flight writes must not land.
        """
        if self.bound is None or holder is None:
            return
        assert holder.owner == self.bound.owner, (
            f"write to {node_id!r} accepted while bound to {self.bound.owner!r}, but "
            f"the store says {holder.owner!r} holds it — a superseded run just "
            f"overwrote the new holder's state"
        )
        assert holder.fence <= self.bound.fence, (
            f"write to {node_id!r} accepted at fence {self.bound.fence} while the "
            f"store holds it at {holder.fence}: a stale lease wrote"
        )

    def _assert_recorded(self, lease: Lease) -> None:
        """The store reports every node of a just-granted lease as held by it, at
        its fence. `_assert_permitted` judges writes by these reported fences, so a
        backend reporting a wrong one (e.g. `0` for all) would make that check
        vacuous rather than failing it."""
        held = self.backend.locks()
        for node_id in lease.scope:
            assert (held[node_id].owner, held[node_id].fence) == (lease.owner, lease.fence), (
                f"{node_id!r} was granted to {lease.owner!r} at fence {lease.fence}, but "
                f"the store reports {held[node_id].owner!r} at {held[node_id].fence}"
            )

    # -- invariants -------------------------------------------------------

    @invariant()
    def every_hold_carries_a_minted_fence(self) -> None:
        """A recorded hold is never unfenced (`0`) and never ahead of the newest
        fence handed out."""
        for node_id, held in self.backend.locks().items():
            assert 0 < held.fence <= self.max_fence, (
                f"{node_id!r} is held at fence {held.fence}; minted so far: {self.max_fence}"
            )

    @invariant()
    def serial_never_goes_backwards(self) -> None:
        """A content version that decreases makes the S3 backend's
        compare-and-swap meaningless and lets a stale document win."""
        current = self.backend.serial()
        assert current >= self.max_serial, f"serial fell from {self.max_serial} to {current}"
        self.max_serial = current

    @invariant()
    def the_store_agrees_with_what_it_accepted(self) -> None:
        """Read-your-writes: every accepted write is visible, and nothing else is.

        A backend that caches (S3 holds a document and an ETag between calls) can
        drift from the store after an operation it did not expect to invalidate,
        such as a lock renewal.
        """
        live = self.backend.load().nodes
        assert set(live) == set(self.accepted), (
            f"store holds {sorted(live)}, accepted writes were {sorted(self.accepted)}"
        )
        for node_id, node in self.accepted.items():
            assert live[node_id].input_hash == node.input_hash


def test_an_empty_scope_acquire_is_a_no_op(make_backend: BackendFactory) -> None:
    """Pins a divergence the state machine surfaced.

    `acquire_lock(owner, ttl, set())` is documented as "a no-op success". The
    backends disagree on the fence it carries: memory and sqlite mint the next
    one, s3 and postgres return `0`, which `Lease` defines as unfenced.

    Harmless while an empty scope makes `fence_violation` refuse every write as
    out-of-scope regardless of fence, so this records the behaviour rather than
    changing it. It stops being harmless if an empty-scope lease may ever write.
    """
    backend = make_backend()
    backend.acquire_lock("a", TTL, {"n0"}).unwrap()

    lease = backend.acquire_lock("a", TTL, set()).unwrap()

    assert lease.scope == frozenset()
    assert backend.locks().keys() == {"n0"}, "a no-op acquire must not record a hold"


def test_deleting_an_absent_node_is_not_uniformly_fenced(make_backend: BackendFactory) -> None:
    """A second divergence the machine surfaced.

    Deleting an existing node under a lease someone else has taken is refused by
    all four backends; that is the guarantee. Deleting one that was never there is
    refused by memory, sqlite and postgres, and accepted by S3, whose `delete`
    short-circuits on a node absent from its cached document before the fence
    check.

    The accepted call writes nothing (`serial` does not move), so no stale-lease
    write lands. The divergence is in the error contract, not in data integrity:
    `delete` of an absent node does not reliably raise.
    """
    backend = make_backend()
    ours = backend.acquire_lock("a", TTL, {"n1"}).unwrap()
    backend.acquire_lock("b", TTL, {"n0"}).unwrap()  # someone else holds n0
    backend.bind_lease(ours)
    before = backend.serial()

    with suppress(StateError):  # memory, sqlite and postgres refuse; s3 does not
        backend.delete("n0")

    assert backend.serial() == before, "the delete must not have written anything"
    assert "n0" not in backend.load().nodes


def test_the_contract_holds_under_arbitrary_sequences(make_backend: BackendFactory) -> None:
    """Runs on memory, sqlite, s3 and postgres, the same four backends the
    example-based contract covers."""
    clock = FakeClock()
    backend = make_backend(clock=clock)

    run_state_machine_as_test(
        lambda: BackendMachine(backend, clock),
        settings=settings(
            max_examples=40,
            stateful_step_count=20,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
        ),
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
