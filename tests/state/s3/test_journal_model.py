"""The S3 journal backend against a model of its committed history (design §8).

Two backends — two processes — share one store and one clock. Hypothesis drives
them through runs (acquire + bind, writes, bulk writes, output merges, renewals,
releases), compactions, clock jumps and crashes mid-write. The model is the
linearized committed history: what a write *must* do follows from who holds the
newest grant, not from reading the implementation back.

* A write lands iff its run holds the newest grant over the nodes (I2) — or it
  changes nothing by that run's own view, in which case it is skipped.
* A crash mid-write leaves the node at its old value or its new one, and a
  superseded run's crashed write never becomes visible (I5).
* After every step another process reads exactly the model (I4, I6), and the
  serial moves whenever what it reads moved (I7).
* A run's post-acquire read is the whole committed history (I6).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import (
    RuleBasedStateMachine,
    invariant,
    precondition,
    rule,
    run_state_machine_as_test,
)
from moto import mock_aws

from atlantide.core import is_successful
from atlantide.core.errors import FencedWriteError
from atlantide.state.s3 import S3StateBackend
from tests.support import FakeClock, create_state_store, fake_aws_credentials

from ..conftest import BUCKET, LOCK_TABLE, REGION, node
from .harness import Fault, SimulatedCrash, crashing, share_clients

NODES = ("dev:t:n0", "dev:t:n1")
SCOPE = frozenset(NODES)
OUT = "dev:out"
TTL = 100.0

_procs = st.sampled_from([0, 1])
_nodes = st.sampled_from(NODES)
#: ``None`` is a delete (a removal, for an output).
_values = st.sampled_from([None, "v1", "v2", "v3"])

_keys = itertools.count()


@dataclass
class _Proc:
    """One process: its backend, and the run it is in (if any)."""

    backend: S3StateBackend
    owner: str | None = None
    grant: int = 0
    #: What this run believes is stored: the history at its acquire plus its own
    #: writes. A write equal to it is skipped without a request.
    nodes: dict[str, str] = field(default_factory=dict)
    out: Any = None


class JournalModel(RuleBasedStateMachine):
    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self.clock = clock
        # A state of its own per example; the store (and fence counter) is shared.
        self.key = f"model/{next(_keys)}.json"
        self.procs = [_Proc(self._backend()), _Proc(self._backend())]
        self.nodes: dict[str, str] = {}
        self.out: Any = None
        #: node -> (owner, expires_at) of its lock row.
        self.rows: dict[str, tuple[str, float]] = {}
        self.grants = 0
        self.owners = itertools.count()
        self.seen: tuple[Any, int] = (({}, {}), 0)

    def _backend(self) -> S3StateBackend:
        return S3StateBackend(
            BUCKET,
            self.key,
            lock_table=LOCK_TABLE,
            region=REGION,
            lock_skew_margin=0.0,
            clock=self.clock,
            compact_every=10**9,
        )

    def _outputs(self) -> dict[str, Any]:
        return {} if self.out is None else {OUT: self.out}

    def _running(self) -> list[int]:
        return [i for i, proc in enumerate(self.procs) if proc.owner is not None]

    def _run(self, pick: int) -> tuple[int, _Proc]:
        """One of the processes in a run (the rules using it require one)."""
        running = self._running()
        index = running[pick % len(running)]
        return index, self.procs[index]

    def _current(self, proc: _Proc) -> bool:
        """Whether ``proc``'s run holds the newest grant (so its writes land)."""
        return proc.owner is not None and proc.grant == self.grants

    def _forget_run(self, proc: _Proc) -> None:
        proc.backend.bind_lease(None)
        proc.owner = None

    # -- runs ---------------------------------------------------------------

    @precondition(lambda self: len(self._running()) < len(self.procs))
    @rule(pick=_procs)
    def start(self, pick: int) -> None:
        idle = [i for i, proc in enumerate(self.procs) if proc.owner is None]
        p = idle[pick % len(idle)]
        proc = self.procs[p]
        owner = f"p{p}-{next(self.owners)}"
        now = self.clock()
        free = all(
            (row := self.rows.get(nid)) is None or row[0] == owner or row[1] < now for nid in NODES
        )
        taken = proc.backend.acquire_lock(owner, TTL, SCOPE)
        assert is_successful(taken) == free
        if not free:
            return
        if any(other.owner is not None for other in self.procs if other is not proc):
            event("a run takes over from a lapsed one")
        proc.backend.bind_lease(taken.unwrap())
        self.grants += 1
        proc.owner, proc.grant = owner, self.grants
        self.rows = {nid: (owner, now + TTL) for nid in NODES}
        # I6: the run starts from the whole committed history.
        loaded = proc.backend.load()
        assert {nid: n.input_hash for nid, n in loaded.nodes.items()} == self.nodes
        assert proc.backend.outputs() == self._outputs()
        proc.nodes, proc.out = dict(self.nodes), self.out

    @precondition(lambda self: len(self._running()) == 1)
    @rule()
    def lapse_and_take_over(self) -> None:
        """The takeover interleaving as one step, so it is explored often: the
        running process pauses past its lease, and the other starts a run over
        the same nodes."""
        self.clock.advance(TTL + 1.0)
        self.start(0)

    @precondition(lambda self: self._running())
    @rule(pick=_procs)
    def renew(self, pick: int) -> None:
        _, proc = self._run(pick)
        assert proc.owner is not None
        renewed = proc.backend.renew_lock(proc.owner, TTL, SCOPE)
        assert is_successful(renewed) == self._current(proc)
        if is_successful(renewed):
            proc.backend.bind_lease(renewed.unwrap())
            self.rows = {nid: (proc.owner, self.clock() + TTL) for nid in NODES}
        else:
            # Only a takeover revokes a lease, so this is the old holder of one.
            event("a superseded run's renewal fails")
            self._release(proc)  # a lost lease ends the run

    @precondition(lambda self: self._running())
    @rule(pick=_procs)
    def release(self, pick: int) -> None:
        self._release(self._run(pick)[1])

    def _release(self, proc: _Proc) -> None:
        owner = proc.owner
        assert owner is not None
        self._forget_run(proc)
        proc.backend.release_lock(owner)
        self.rows = {nid: row for nid, row in self.rows.items() if row[0] != owner}

    @rule(seconds=st.sampled_from([1.0, TTL + 1.0]))
    def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)

    # -- writes -------------------------------------------------------------

    def _expect_write(self, proc: _Proc, changes: dict[str, str | None], attempt: Any) -> None:
        """Run ``attempt``; it lands iff it changes something and the run is current."""
        effective = {nid: v for nid, v in changes.items() if proc.nodes.get(nid) != v}
        if not effective:
            attempt()
            return
        if not self._current(proc):
            event("a superseded run's write is refused")
            with pytest.raises(FencedWriteError):
                attempt()
            return
        event("a current run's write lands")
        attempt()
        for nid, value in effective.items():
            self._set(nid, value)
            _set(proc.nodes, nid, value)

    def _set(self, node_id: str, value: str | None) -> None:
        _set(self.nodes, node_id, value)

    @precondition(lambda self: self._running())
    @rule(pick=_procs, node_id=_nodes, value=_values)
    def write(self, pick: int, node_id: str, value: str | None) -> None:
        _, proc = self._run(pick)
        be = proc.backend

        def attempt() -> None:
            if value is None:
                be.delete(node_id)
            else:
                be.put(node(node_id, input_hash=value))

        self._expect_write(proc, {node_id: value}, attempt)

    @precondition(lambda self: self._running())
    @rule(pick=_procs, first=_values, second=_values)
    def bulk(self, pick: int, first: str | None, second: str | None) -> None:
        _, proc = self._run(pick)
        wanted = dict(zip(NODES, (first, second), strict=True))
        drop = [nid for nid, v in wanted.items() if v is None]
        fresh = [node(nid, input_hash=v) for nid, v in wanted.items() if v is not None]
        self._expect_write(proc, wanted, lambda: proc.backend.replace_many(drop, fresh))

    @precondition(lambda self: self._running())
    @rule(pick=_procs, value=_values)
    def outputs(self, pick: int, value: str | None) -> None:
        _, proc = self._run(pick)
        if proc.out == value:
            self._set_outputs(proc, value)  # no change by its view: skipped
            return
        if not self._current(proc):
            with pytest.raises(FencedWriteError):
                self._set_outputs(proc, value)
            return
        self._set_outputs(proc, value)
        self.out = proc.out = value

    def _set_outputs(self, proc: _Proc, value: str | None) -> None:
        if value is None:
            proc.backend.set_outputs({}, remove=[OUT])
        else:
            proc.backend.set_outputs({OUT: value})

    @rule(p=_procs)
    def compact(self, p: int) -> None:
        self.procs[p].backend.compact()

    @precondition(lambda self: self._running())
    @rule(
        pick=_procs,
        node_id=_nodes,
        value=_values,
        method=st.sampled_from(["put_object", "update_item"]),
        after=st.booleans(),
    )
    def crash_mid_write(
        self, pick: int, node_id: str, value: str | None, method: str, after: bool
    ) -> None:
        """The process dies around its entry PUT or its head commit."""
        p, proc = self._run(pick)
        if proc.nodes.get(node_id) == value:
            return
        before = self.nodes.get(node_id)
        s3, ddb = proc.backend._s3, proc.backend._ddb
        crashing(proc.backend, Fault(method, 1, after=after))
        be = proc.backend
        try:
            if value is None:
                be.delete(node_id)
            else:
                be.put(node(node_id, input_hash=value))
        except SimulatedCrash:
            event(f"a crash {'after' if after else 'before'} {method}")
            seen = self._backend().load().nodes.get(node_id)
            now = seen.input_hash if seen is not None else None
            assert now in (before, value), "a crash left neither the old nor the new value"
            if not self._current(proc):
                assert now == before, "a superseded run's crashed write became visible"
            self._set(node_id, now)
            # The process is gone; its lock rows stay until they expire.
            self.procs[p] = _Proc(self._backend())
            return
        except FencedWriteError:
            assert not self._current(proc)
        else:
            assert self._current(proc)
            self._set(node_id, value)
            _set(proc.nodes, node_id, value)
        be._s3, be._ddb = s3, ddb

    # -- invariants ----------------------------------------------------------

    @invariant()
    def another_process_reads_the_model(self) -> None:
        reader = self._backend()
        graph = reader.load()
        content = ({nid: n.input_hash for nid, n in graph.nodes.items()}, reader.outputs())
        assert content == (self.nodes, self._outputs())
        serial = reader.serial()
        last_content, last_serial = self.seen
        assert serial >= last_serial, "the serial went backwards"
        if content != last_content:
            assert serial != last_serial, "content changed but the serial did not"
        self.seen = (content, serial)


def _set(nodes: dict[str, str], node_id: str, value: str | None) -> None:
    if value is None:
        nodes.pop(node_id, None)
    else:
        nodes[node_id] = value


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    fake_aws_credentials(monkeypatch, region=REGION)
    share_clients(monkeypatch)
    with mock_aws():
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
        yield


def test_the_journal_matches_its_committed_history(store: None) -> None:
    clock = FakeClock()
    run_state_machine_as_test(
        lambda: JournalModel(clock),
        settings=settings(
            max_examples=30,
            stateful_step_count=25,
            deadline=None,
            suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
        ),
    )
