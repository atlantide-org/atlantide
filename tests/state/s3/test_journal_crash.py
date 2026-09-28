"""Crash injection over a small apply against the S3 journal backend (design §8).

A scripted run — an acquire that takes over from a crashed predecessor's lapsed
lease (read its rows, revoke its lease item, retake), a write-ahead/provider/
persist cycle per node, a renewal, a delete, an output merge, an alias rekey
(bulk write), a checkpoint and a release — is first recorded to learn every AWS
call it makes. Then, for each mutating call and each read, the run is replayed
with the process dying just before (and, for writes, just after) that call:
:class:`SimulatedCrash` is a ``BaseException`` nothing in the backend can
swallow, and every later call from the dead process fails too.

After each crash a fresh backend — another process on the same (moto) store —
checks crash safety (I5):

* ``load()`` works, orphans or not;
* the state is exactly what was committed before the interrupted step, or
  exactly what it commits — never a mix, never less;
* compaction folds without changing what is read, and a second one finds
  nothing left to do;
* no head names a missing entry;
* a new holder can take over and write.

The fake provider checks write-ahead durability (I1) itself: it is only ever
invoked after its node's ``creating`` row is readable by another process.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from moto import mock_aws

from atlantide.state.s3 import S3StateBackend
from tests.support import FakeClock, create_state_store, fake_aws_credentials

from ..conftest import BUCKET, LOCK_TABLE, REGION, node
from .harness import CountingClient, Fault, SimulatedCrash, crashing, share_clients
from .support import TAKEOVER, new_backend

A, B, OLD, A2 = "dev:t:a", "dev:t:b", "dev:t:old", "dev:t:a2"
SCOPE = frozenset({A, B, OLD, A2})

#: ``({node: input hash}, outputs)``: what another process reads.
type Seen = tuple[dict[str, str], dict[str, Any]]

#: Calls that change the store: a crash *after* one differs from a crash before
#: the next call. After a read the two are the same point, so only "before" is
#: swept for reads.
_WRITES = frozenset(
    {
        "put_object",
        "put_item",
        "update_item",
        "transact_write_items",
        "delete_objects",
        "delete_item",
    }
)


def _process(clock: FakeClock) -> S3StateBackend:
    """A fresh backend: another process on the same store."""
    # No background compaction: a dead process's thread must not keep working.
    return new_backend(clock=clock, compact_every=10**9)


def _seen(backend: S3StateBackend) -> Seen:
    graph = backend.load()
    return {nid: n.input_hash for nid, n in graph.nodes.items()}, backend.outputs()


@dataclass
class Apply:
    """The scripted run, with the state each step leaves behind."""

    backend: S3StateBackend
    clock: FakeClock
    #: The state committed after each completed step (index 0: before any).
    committed: list[Seen] = field(default_factory=list)
    provided: list[str] = field(default_factory=list)

    def steps(self) -> list[tuple[Callable[[], None], Seen]]:
        base: dict[str, str] = {OLD: "v0"}
        outs: dict[str, Any] = {"dev:url": "u0"}
        be = self.backend

        def lease() -> None:
            be.bind_lease(be.acquire_lock("run", 60.0, SCOPE).unwrap())

        def provider(node_id: str) -> Callable[[], None]:
            def create() -> None:
                # I1: the write-ahead row is durable before the provider runs.
                fresh = _process(self.clock)
                assert fresh.load().nodes[node_id].input_hash == "creating"
                self.provided.append(node_id)

            return create

        def put(node_id: str, status: str) -> Callable[[], None]:
            return lambda: be.put(node(node_id, input_hash=status))

        def renew() -> None:
            be.bind_lease(be.renew_lock("run", 60.0, SCOPE).unwrap())

        def release() -> None:
            be.bind_lease(None)
            be.release_lock("run")

        s1 = {**base, A: "creating"}
        s2 = {**base, A: "created"}
        s3 = {**s2, B: "creating"}
        s4 = {**s2, B: "created"}
        s5 = {A: "created", B: "created"}
        s6 = {A2: "created", B: "created"}
        u1 = {"dev:url": "u1"}
        return [
            (lease, (base, outs)),
            (put(A, "creating"), (s1, outs)),
            (provider(A), (s1, outs)),
            (put(A, "created"), (s2, outs)),
            (renew, (s2, outs)),
            (put(B, "creating"), (s3, outs)),
            (provider(B), (s3, outs)),
            (put(B, "created"), (s4, outs)),
            (lambda: be.delete(OLD), (s5, outs)),
            (lambda: be.set_outputs(u1), (s5, u1)),
            (
                lambda: be.replace_many([A], [node(A2, input_hash="created")]),
                (s6, u1),
            ),
            (be.checkpoint, (s6, u1)),
            (release, (s6, u1)),
        ]

    def run(self) -> None:
        """Run every step, recording the committed state; stops at a crash."""
        self.committed = [({OLD: "v0"}, {"dev:url": "u0"})]
        for step, after in self.steps():
            step()
            self.committed.append(after)


def _seed() -> FakeClock:
    """The store before the run: committed state, and the lock rows of a run that
    crashed holding the whole scope, its lease since lapsed."""
    clock = FakeClock()
    setup = _process(clock)
    setup.put(node(OLD, input_hash="v0"))
    setup.set_outputs({"dev:url": "u0"})
    setup.compact()
    setup.acquire_lock("ghost", 60.0, SCOPE).unwrap()  # never released
    clock.advance(TAKEOVER)
    return clock


@pytest.fixture
def seeded(aws: None, monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    """The mocked store (``aws``), seeded by :func:`_seed`; its clock."""
    share_clients(monkeypatch)
    return _seed()


def _recorded_calls() -> list[str]:
    """Every AWS call of an uninterrupted run, in order (sizing the sweep)."""
    calls: list[str] = []
    with pytest.MonkeyPatch.context() as monkeypatch:
        fake_aws_credentials(monkeypatch, region=REGION)
        share_clients(monkeypatch)
        with mock_aws():
            create_state_store(BUCKET, LOCK_TABLE, region=REGION)
            clock = _seed()
            backend = _process(clock)
            backend._s3 = CountingClient(backend._s3, calls)
            backend._ddb = CountingClient(backend._ddb, calls)
            Apply(backend, clock).run()
    return calls


def _crash_points() -> list[Any]:
    counts: dict[str, int] = {}
    points = []
    for name in _recorded_calls():
        counts[name] = counts.get(name, 0) + 1
        for after in (False, True) if name in _WRITES else (False,):
            label = f"{'after' if after else 'before'}-{name}-{counts[name]}"
            points.append(pytest.param(name, counts[name], after, id=label))
    return points


_CRASH_POINTS = _crash_points()


@pytest.mark.parametrize(("method", "n", "after"), _CRASH_POINTS)
def test_a_crash_anywhere_in_an_apply_leaves_a_consistent_store(
    seeded: FakeClock, method: str, n: int, after: bool
) -> None:
    clock = seeded
    fault = Fault(method, n, after=after)
    run = Apply(crashing(_process(clock), fault), clock)
    with pytest.raises(SimulatedCrash):
        run.run()
    assert fault.crashed.is_set()
    step = len(run.committed) - 1  # the step the process died in
    allowed = [run.committed[step], _expected(run, step + 1)]

    survivor = _process(clock)
    seen = _seen(survivor)
    assert seen in allowed, f"crashed in step {step}: neither before nor after it"
    serial = survivor.serial()

    survivor.compact()
    assert _seen(_process(clock)) == seen, "compaction changed what is read"
    assert _process(clock).serial() == serial, "compaction moved the serial"
    again = _process(clock).compact()
    assert (again.folded, again.deleted) == (0, 0), "compaction is not idempotent"
    assert not _process(clock).fsck().missing, "a head names a missing entry"

    clock.advance(TAKEOVER)
    successor = _process(clock)
    successor.bind_lease(successor.acquire_lock("next", 60.0, SCOPE).unwrap())
    successor.put(node(B, input_hash="recovered"))
    assert _seen(_process(clock))[0][B] == "recovered"
    # Whatever the dead run left of its lease, the successor now holds it all.
    assert {hold.owner for hold in successor.locks().values()} == {"next"}
    successor.bind_lease(None)
    successor.release_lock("next")
    assert successor.locks() == {}


def _expected(run: Apply, index: int) -> Seen:
    steps = run.steps()
    return steps[index - 1][1] if index - 1 < len(steps) else run.committed[-1]


def test_the_sweep_covers_every_step_of_the_apply(seeded: FakeClock) -> None:
    """The recorded run itself: it completes, calls the provider only after each
    write-ahead, and ends in the final state."""
    clock = seeded
    backend = _process(clock)
    calls: list[str] = []
    backend._ddb = CountingClient(backend._ddb, calls)
    run = Apply(backend, clock)
    run.run()
    assert run.provided == [A, B]
    # The takeover (a refused transaction, the ghost's rows and lease read, one
    # revoke, the retried transaction), the one-call renewal and the release
    # (the lease item, then each row) are all in the sweep.
    assert calls[:7] == [
        "update_item",  # mint the fence
        "put_item",  # the lease item
        "transact_write_items",  # refused: the rows point at the ghost's lease
        "batch_get_item",  # those rows
        "batch_get_item",  # the ghost's lease item
        "update_item",  # revoke it
        "transact_write_items",  # retake the rows
    ]
    assert calls.count("put_item") == 1
    assert calls[-(1 + len(SCOPE)) :] == ["delete_item"] * (1 + len(SCOPE))
    assert _seen(_process(clock)) == run.committed[-1]
    assert len(_CRASH_POINTS) > 40
