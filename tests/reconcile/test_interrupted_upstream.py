"""A dependent whose consumed upstream output moved in an earlier, interrupted run.

An apply that replaces an upstream (so its outputs move) and stops before a
dependent that consumes one of those outputs through a ``$ref`` leaves the
dependent's row describing the value it was last applied with, while the
upstream's row already holds the new one. Symbolically nothing differs: the
config's marker and the stored marker are the same ``{"$ref": ...}``. The next
plan must still see that the value the dependent consumes moved:

- in an ``immutable()`` field it is a REPLACE, known now rather than conditional
  (the upstream is not changing this run, so its value is known);
- in a mutable field it is an UPDATE naming the field;
- a protected node's now-known replace is refused at plan.

The upstream is create-before-destroy: a destroy-first upstream takes its
immutable consumer down *before* its own delete (the diff makes that replace
unconditional, and the executor runs its delete half in phase 0), so an
interruption there leaves the consumer gone, not stale. That equivalent is
covered at the end of the module.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from atlantide.core import Lifecycle, PreventDestroyError
from atlantide.engine import Engine
from atlantide.reconcile import Action
from atlantide.reconcile.executor import run as run_module
from atlantide.state import MemoryStateBackend
from tests.support import Box, FakeProvider, Notifier, engine_for, globals_of

GLOBALS = globals_of(Box, Notifier, Lifecycle=Lifecycle)
A = "default:test.Box:a"
N = "default:test.Notifier:n"
B = "default:test.Box:b"
A_OLD = f"{A}~replaced"
CBD = "lifecycle=Lifecycle(create_before_destroy=True)"


def _src(size: int, *, label: str = "", lifecycle: str = "", message: str = "hello") -> str:
    """``n.target_arn`` (immutable) and ``b.ref`` (mutable) both consume ``a.out``.

    A new ``size`` replaces ``a`` create-before-destroy (``out`` becomes
    ``a:<size>``, the old ``a`` is kept as ``a~replaced`` until cleanup); a new
    ``label`` updates it, and the fake provider's update moves ``out`` to
    ``a:<size>:u``.
    """
    extra = f", lifecycle=Lifecycle({lifecycle})" if lifecycle else ""
    return (
        f"a = Box('a', size={size}, label={label!r}, {CBD})\n"
        f"Notifier('n', target_arn=a.out, message={message!r}{extra})\n"
        "Box('b', size=1, ref=a.out)\n"
    )


def _engine() -> tuple[Engine, FakeProvider]:
    provider = FakeProvider()
    return engine_for(Box, Notifier, provider=provider, backend=MemoryStateBackend()), provider


def _plan(engine: Engine, source: str, **kw: Any) -> Any:
    return engine.plan(source, extra_globals=GLOBALS, **kw)


def _changes(engine: Engine, source: str, **kw: Any) -> dict[str, Any]:
    return {c.node_id: c for c in _plan(engine, source, **kw).unwrap().changeset}


def _apply(engine: Engine, source: str, **kw: Any) -> Any:
    return asyncio.run(engine.apply(source, extra_globals=GLOBALS, **kw))


def _interrupt_after_upstream(
    engine: Engine, source: str, monkeypatch: pytest.MonkeyPatch, **kw: Any
) -> None:
    """Apply ``source`` but cancel the run once ``a`` is applied, before n and b run.

    Parks every node but ``a`` at its start, then cancels the apply from outside,
    the path a Ctrl-C takes. ``a``'s write has landed by then (a node's persist
    completes before its dependents are scheduled).
    """
    original = run_module.ChangeSetRun._apply_node

    async def run() -> None:
        reached = asyncio.Event()

        async def parked(self: Any, node_id: str) -> None:
            if node_id != A:
                reached.set()
                await asyncio.sleep(3600)  # cancelled from outside
            await original(self, node_id)

        monkeypatch.setattr(run_module.ChangeSetRun, "_apply_node", parked)
        task = asyncio.ensure_future(
            engine.apply(source, extra_globals=GLOBALS, on_failure="halt", **kw)
        )
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    monkeypatch.undo()


def test_the_interrupted_state_is_the_one_the_bug_needs(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, provider = _engine()
    _apply(engine, _src(1)).unwrap()
    before = engine.backend.load()
    provider.reset()
    _interrupt_after_upstream(engine, _src(2), monkeypatch)

    after = engine.backend.load()
    assert after.nodes[A].outputs == {"out": "a:2"}  # the upstream moved...
    assert after.nodes[N] == before.nodes[N]  # ...and the dependents never ran
    assert after.nodes[B] == before.nodes[B]
    assert after.nodes[A_OLD].outputs == {"out": "a:1"}  # the old a, still live
    assert provider.calls == [("create", "a")]


def test_the_next_plan_replaces_the_immutable_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _engine()
    _apply(engine, _src(1)).unwrap()
    _interrupt_after_upstream(engine, _src(2), monkeypatch)

    planned = _changes(engine, _src(2))

    assert planned[A].action is Action.NOOP
    assert planned[A_OLD].action is Action.DELETE  # the cleanup the run never reached
    assert planned[N].action is Action.REPLACE
    assert planned[N].changed_fields == ("target_arn",)
    assert planned[N].conditional is False  # the upstream's value is known now


def test_the_next_plan_updates_the_mutable_consumer(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, _ = _engine()
    _apply(engine, _src(1)).unwrap()
    _interrupt_after_upstream(engine, _src(2), monkeypatch)

    planned = _changes(engine, _src(2))

    assert planned[B].action is Action.UPDATE
    assert planned[B].changed_fields == ("ref",)


def test_the_resumed_apply_replaces_not_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    engine, provider = _engine()
    _apply(engine, _src(1)).unwrap()
    _interrupt_after_upstream(engine, _src(2), monkeypatch)
    provider.reset()

    report = _apply(engine, _src(2)).unwrap()

    assert ("update", "n") not in provider.calls
    assert provider.calls.index(("delete", "n")) < provider.calls.index(("create", "n"))
    assert provider.input("create", "n").target_arn == "a:2"  # type: ignore[attr-defined]
    assert provider.input("update", "b").ref == "a:2"  # type: ignore[attr-defined]
    assert report.replaced == [N]
    assert report.updated == [B]
    assert report.deleted == [A_OLD]
    assert report.downgraded == {}
    # The old a outlived every consumer of it.
    assert provider.calls[-1] == ("delete", "a")
    # Converged: nothing left to do.
    assert _plan(engine, _src(2)).unwrap().changeset.pending == []


def test_a_protected_consumer_is_refused_at_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    """The replace is no longer "known after apply", so the plan refuses it.

    Behind an upstream *update*, the first plan defers the protected node's
    verdict to apply; the interrupted apply never gets there. The next plan
    knows the value moved, and refuses before any provider call.
    """
    engine, provider = _engine()
    protected = "prevent_destroy=True"
    _apply(engine, _src(1, lifecycle=protected)).unwrap()
    relabelled = _src(1, label="y", lifecycle=protected)
    assert _plan(engine, relabelled).unwrap().warnings  # deferred to apply
    _interrupt_after_upstream(engine, relabelled, monkeypatch)
    assert engine.backend.load().nodes[A].outputs == {"out": "a:1:u"}
    provider.reset()

    planned = _plan(engine, relabelled)

    assert isinstance(planned.failure(), PreventDestroyError)
    assert N in str(planned.failure())
    refused = _apply(engine, relabelled)
    assert isinstance(refused.failure(), PreventDestroyError)
    assert provider.calls == []


def test_a_targeted_upstream_replace_leaves_the_same_gap() -> None:
    """``--target`` NOOPs every unselected node, so it leaves the same state."""
    engine, provider = _engine()
    _apply(engine, _src(1)).unwrap()
    _apply(engine, _src(2), targets=[A]).unwrap()
    assert engine.backend.load().nodes[A].outputs == {"out": "a:2"}

    planned = _changes(engine, _src(2))
    assert (planned[N].action, planned[N].changed_fields) == (Action.REPLACE, ("target_arn",))
    assert (planned[B].action, planned[B].changed_fields) == (Action.UPDATE, ("ref",))

    provider.reset()
    _apply(engine, _src(2)).unwrap()
    assert ("update", "n") not in provider.calls
    assert _plan(engine, _src(2)).unwrap().changeset.pending == []


def test_a_state_side_recreation_is_caught_behind_the_merkle_skip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--replace a`` recreates ``a`` without moving any hash; interrupted before
    the dependents, the next plan's Merkle skip would NOOP them, although the
    value they consume moved.

    A forced replace keeps ``a``'s identity, so the planner runs it destroy-first
    even though ``a`` declares create-before-destroy; ``n`` (immutable) is then
    destroyed before it, in phase 0, and comes back as a CREATE. ``b`` (mutable)
    is the consumer left behind the Merkle skip.
    """
    ids = iter(range(1, 100))
    provider = FakeProvider(on_create=lambda _, res: {"out": f"{res.logical_name}#{next(ids)}"})
    engine = engine_for(Box, Notifier, provider=provider, backend=MemoryStateBackend())
    _apply(engine, _src(1)).unwrap()
    first = engine.backend.load().nodes[A].outputs["out"]

    _interrupt_after_upstream(engine, _src(1), monkeypatch, replace=[A])
    assert engine.backend.load().nodes[A].outputs["out"] != first

    planned = _changes(engine, _src(1))
    assert planned[A].action is Action.NOOP
    assert planned[N].action is Action.CREATE  # its delete half ran first
    assert (planned[B].action, planned[B].changed_fields) == (Action.UPDATE, ("ref",))
    provider.reset()
    _apply(engine, _src(1)).unwrap()
    assert ("create", "n") in provider.calls and ("update", "n") not in provider.calls
    assert provider.input("update", "b").ref == engine.backend.load().nodes[A].outputs["out"]  # type: ignore[attr-defined]
    assert _plan(engine, _src(1)).unwrap().changeset.pending == []


def test_the_plan_says_why_an_unchanged_field_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    from atlantide.cli.views.plan import field_diffs, plan_json

    engine, _ = _engine()
    _apply(engine, _src(1)).unwrap()
    _interrupt_after_upstream(engine, _src(2), monkeypatch)

    planned = _plan(engine, _src(2)).unwrap()
    change = {c.node_id: c for c in planned.changeset}[N]

    assert change.upstream_moved == ("target_arn",)
    [line] = field_diffs(change)
    assert line.startswith("target_arn: ")
    assert line.endswith("(its value moved since this resource was last applied)")
    rows = {row["node_id"]: row for row in plan_json(planned)["changes"]}
    assert rows[N]["upstream_moved"] == ["target_arn"]
    assert rows[N]["conditional"] is False
    assert rows[A]["upstream_moved"] == []


# -- behind a destroy-first upstream ----------------------------------------------

DBC_SRC = "a = Box('a', size={size})\nNotifier('n', target_arn=a.out)\n"


def test_a_destroy_first_upstream_takes_its_consumer_down_first() -> None:
    engine, provider = _engine()
    _apply(engine, DBC_SRC.format(size=1)).unwrap()

    planned = _changes(engine, DBC_SRC.format(size=2))
    assert (planned[A].action, planned[A].create_before_destroy) == (Action.REPLACE, False)
    # Known at plan: the delete half cannot wait for a's new value.
    assert (planned[N].action, planned[N].conditional) == (Action.REPLACE, False)

    provider.reset()
    _apply(engine, DBC_SRC.format(size=2)).unwrap()
    assert provider.calls == [("delete", "n"), ("delete", "a"), ("create", "a"), ("create", "n")]


def test_an_interruption_after_the_consumer_s_delete_half_resumes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The destroy-first equivalent of the gap above: the run stops after phase 0
    deleted ``n`` and before ``a`` is touched. ``n``'s write-ahead row says it is
    not there, so the next plan re-creates it, after ``a``'s replace."""
    engine, provider = _engine()
    _apply(engine, DBC_SRC.format(size=1)).unwrap()
    provider.reset()
    original = run_module.ChangeSetRun._apply_node

    async def run() -> None:
        reached = asyncio.Event()

        async def parked(self: Any, node_id: str) -> None:
            reached.set()
            await asyncio.sleep(3600)  # cancelled from outside
            await original(self, node_id)

        monkeypatch.setattr(run_module.ChangeSetRun, "_apply_node", parked)
        task = asyncio.ensure_future(
            engine.apply(DBC_SRC.format(size=2), extra_globals=GLOBALS, on_failure="halt")
        )
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    monkeypatch.undo()
    assert provider.calls == [("delete", "n")]

    planned = _changes(engine, DBC_SRC.format(size=2))
    assert planned[N].action is Action.CREATE
    assert planned[A].action is Action.REPLACE

    provider.reset()
    _apply(engine, DBC_SRC.format(size=2)).unwrap()
    assert provider.calls == [("delete", "a"), ("create", "a"), ("create", "n")]
    assert provider.input("create", "n").target_arn == "a:2"  # type: ignore[attr-defined]
    assert _plan(engine, DBC_SRC.format(size=2)).unwrap().changeset.pending == []
