"""A conditional ("known after apply") REPLACE is confirmed before it runs.

The diff marks a REPLACE conditional when its only immutable changes are fields
holding a ``$ref`` to an upstream whose value is unknown at plan time. The
executor reaches such a node after its upstreams have applied, so it re-diffs it
against the resolved values: if no immutable value actually moved, the node runs
as an UPDATE (mutable fields moved) or a NOOP that records the new input hash,
and the provider never destroys it. A retagged bucket must not rebuild the
CloudFront distribution in front of it.
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest

from atlantide.core import (
    Lifecycle,
    PreventDestroyError,
    Resource,
    computed,
    immutable,
    is_successful,
)
from atlantide.core.events import ApplyEvent
from atlantide.core.fields import field_mutability
from atlantide.reconcile import Action, reclassify
from atlantide.reconcile.executor import records as records_module
from atlantide.state import MemoryStateBackend, NodeStatus, StateNode
from tests.support import (
    Box,
    FakeProvider,
    Harness,
    Notifier,
    actions_of,
    default_outputs,
    leaves,
)

A = "default:test.Box:a"
N = "default:test.Notifier:n"
C = "default:test.Box:c"


def _stable_updates(_: Any, res: Any) -> dict[str, Any]:
    """An update that keeps every computed output: ``a.out`` stays ``a:1``."""
    return default_outputs("create", res)


class Pinned(Resource):
    """Two immutable fields: a literal one and one a config wires to a Ref."""

    class Meta:
        provider: ClassVar[str] = "test"

    size: int = immutable()
    anchor: str = immutable(default="")
    out: str = computed()


class StatusSpy(MemoryStateBackend):
    """Memory state that remembers the status of every row written."""

    def __init__(self) -> None:
        super().__init__()
        self.written: list[tuple[str, str]] = []

    def put(self, node: StateNode) -> None:
        self.written.append((node.id, node.status))
        super().put(node)


def _harness(*, stable: bool = True, backend: Any = None) -> Harness:
    return Harness.of(
        Box,
        Notifier,
        Pinned,
        provider=FakeProvider(on_update=_stable_updates if stable else None),
        globals={"Lifecycle": Lifecycle},
        backend=backend if backend is not None else MemoryStateBackend(),
    )


def _config(label: str = "x", *, message: str = "hello", lifecycle: str = "") -> str:
    """``n.target_arn`` (immutable) holds a Ref to ``a.out`` (computed)."""
    extra = f", lifecycle=Lifecycle({lifecycle})" if lifecycle else ""
    return (
        f"a = Box('a', size=1, label={label!r})\n"
        f"Notifier('n', target_arn=a.out, message={message!r}{extra})\n"
    )


def _applied(h: Harness, **kw: Any) -> None:
    h.apply(_config(**kw))
    h.fake().reset()


# -- the plan ------------------------------------------------------------------


def test_an_upstream_update_plans_a_conditional_replace() -> None:
    h = _harness()
    _applied(h)
    change = {c.node_id: c for c in h.diff_only(_config("y"))}[N]
    assert change.action is Action.REPLACE
    assert change.conditional is True


def test_a_literal_immutable_change_makes_the_replace_unconditional() -> None:
    """``conditional`` means "might not be a replace". A literal immutable change
    is a replace whatever the refs resolve to."""
    h = _harness()
    h.apply("a = Box('a', size=1)\nPinned('p', size=1, anchor=a.out)\n")
    moved = "a = Box('a', size=1, label='y')\nPinned('p', size=2, anchor=a.out)\n"
    change = {c.node_id: c for c in h.diff_only(moved)}["default:test.Pinned:p"]
    assert change.action is Action.REPLACE
    assert change.changed_fields == ("anchor", "size")
    assert change.conditional is False


# -- the re-diff ---------------------------------------------------------------


def test_reclassify_keeps_a_confirmed_replace_verbatim() -> None:
    h = _harness()
    _applied(h)
    change = {c.node_id: c for c in h.diff_only(_config("y"))}[N]
    confirmed = reclassify(
        change,
        desired_properties={"target_arn": "a:1:u", "message": "hello"},
        prior_properties={"target_arn": "a:1", "message": "hello"},
        mutability=field_mutability(Notifier),
    )
    assert confirmed is change


def test_reclassify_collapses_to_update_or_noop() -> None:
    h = _harness()
    _applied(h)
    change = {c.node_id: c for c in h.diff_only(_config("y"))}[N]
    same = {"target_arn": "a:1", "message": "hello"}
    muts = field_mutability(Notifier)
    noop = reclassify(change, desired_properties=same, prior_properties=same, mutability=muts)
    assert (noop.action, noop.changed_fields, noop.conditional) == (Action.NOOP, (), False)
    update = reclassify(
        change,
        desired_properties={**same, "message": "bye"},
        prior_properties=same,
        mutability=muts,
    )
    assert (update.action, update.changed_fields) == (Action.UPDATE, ("message",))
    assert update.desired is change.desired and update.prior is change.prior


# -- the apply -----------------------------------------------------------------


def test_an_unmoved_ref_does_not_replace_and_the_next_plan_is_clean() -> None:
    h = _harness()
    _applied(h)
    prior_row = h.backend.load().nodes[N]

    report = h.apply(_config("y"))

    assert h.fake().calls == [("update", "a")]  # n: no create, no delete, no update
    assert report.replaced == []
    assert report.updated == [A]
    assert report.noop == [N]
    assert report.downgraded == {N: "noop"}
    row = h.backend.load().nodes[N]
    # Only what the config moved is rewritten: the new hash, same outputs.
    assert row.input_hash != prior_row.input_hash
    assert row.outputs == prior_row.outputs
    assert row.status == NodeStatus.CREATED
    assert set(actions_of(h.diff_only(_config("y"))).values()) == {Action.NOOP}


def test_a_moved_mutable_field_downgrades_to_update() -> None:
    h = _harness()
    _applied(h)

    report = h.apply(_config("y", message="bye"))

    assert h.fake().calls == [("update", "a"), ("update", "n")]
    assert sorted(report.updated) == [A, N]
    assert report.downgraded == {N: "update"}
    assert set(actions_of(h.diff_only(_config("y", message="bye"))).values()) == {Action.NOOP}


def test_a_moved_immutable_value_still_replaces() -> None:
    h = _harness(stable=False)  # the update moves a.out to "a:1:u"
    _applied(h)

    report = h.apply(_config("y"))

    assert h.fake().calls == [("update", "a"), ("delete", "n"), ("create", "n")]
    assert report.replaced == [N]
    assert report.downgraded == {}
    assert h.fake().input("create", "n").target_arn == "a:1:u"  # type: ignore[attr-defined]


def test_a_confirmed_create_before_destroy_replace_keeps_its_order() -> None:
    h = _harness(stable=False)
    _applied(h, lifecycle="create_before_destroy=True")

    h.apply(_config("y", lifecycle="create_before_destroy=True"))

    assert h.fake().calls == [("update", "a"), ("create", "n"), ("delete", "n")]


def test_the_progress_reports_the_action_taken() -> None:
    h = _harness()
    _applied(h)
    progress: list[tuple[str, Action, str]] = []

    h.apply(_config("y"), on_progress=lambda *args: progress.append(args))

    assert [entry for entry in progress if entry[0] == N] == [
        (N, Action.REPLACE, "start"),
        (N, Action.NOOP, "finish"),
    ]


def test_the_finish_event_names_the_planned_action() -> None:
    events: list[ApplyEvent] = []
    h = _harness()
    _applied(h)
    h.events = events.append

    h.apply(_config("y"))

    mine = [(e.phase, e.action, e.detail) for e in events if e.node_id == N]
    assert mine == [
        ("node_start", "replace", {}),
        ("node_finish", "noop", {"planned": "replace"}),
    ]


def test_a_collapsed_replace_writes_no_creating_row() -> None:
    """Write-ahead exists for a create; a replace that turns out not to be one
    must not leave a ``creating`` row behind, even for a moment."""
    spy = StatusSpy()
    h = _harness(backend=spy)
    _applied(h)
    spy.written.clear()

    h.apply(_config("y"))

    assert spy.written == [(A, NodeStatus.CREATED), (N, NodeStatus.CREATED)]


# -- failure while confirming ----------------------------------------------------


def test_a_failure_in_the_re_diff_leaves_the_row_untouched(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _harness()
    _applied(h)
    prior_row = h.backend.load().nodes[N]

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("re-diff crashed")

    monkeypatch.setattr("atlantide.reconcile.executor.confirm.reclassify", boom)
    with pytest.raises(ExceptionGroup) as failed:
        h.apply(_config("y"), on_failure="rollback")
    assert any("re-diff crashed" in str(leaf) for leaf in leaves(failed.value))

    # a's update ran and was compensated; nothing was destroyed or created for n.
    assert h.fake().calls == [("update", "a"), ("update", "a")]
    assert h.backend.load().nodes[N] == prior_row
    monkeypatch.undo()
    # The upstream was rolled back, so the next run re-plans the same
    # conditional replace, and resolves it.
    change = {c.node_id: c for c in h.diff_only(_config("y"))}[N]
    assert (change.action, change.conditional) == (Action.REPLACE, True)
    assert h.apply(_config("y")).downgraded == {N: "noop"}
    assert set(actions_of(h.diff_only(_config("y"))).values()) == {Action.NOOP}


def test_a_cancellation_during_the_collapsed_write_leaves_state_consistent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    h = _harness()
    _applied(h)
    prior_row = h.backend.load().nodes[N]
    original = records_module.NodeRecords.rewrite

    async def interrupted() -> None:
        reached = asyncio.Event()

        async def parked(self: Any, row: Any) -> None:
            if row.id == N:
                reached.set()
                await asyncio.sleep(3600)  # cancelled from outside
            await original(self, row)

        monkeypatch.setattr(records_module.NodeRecords, "rewrite", parked)
        task = asyncio.ensure_future(h.apply_async(_config("y"), "halt"))
        await asyncio.wait_for(reached.wait(), timeout=5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(interrupted())

    assert h.fake().calls == [("update", "a")]
    assert h.backend.load().nodes[N] == prior_row  # never a creating row, never deleted
    monkeypatch.undo()
    # A halted run keeps a's update, so the resumed run meets n unconfirmed and
    # converges without ever destroying it.
    h.apply(_config("y"))
    assert h.fake().created == [] and h.fake().deleted == []
    assert set(actions_of(h.diff_only(_config("y"))).values()) == {Action.NOOP}


def test_a_rollback_restores_a_collapsed_row() -> None:
    """The collapsed NOOP's write is part of the run: the saga undoes it."""
    h = _harness()
    base = "a = Box('a', size=1, label={l!r})\nn = Notifier('n', target_arn=a.out)\n"
    tail = "Box('c', size=1, label={c!r}, depends_on=[n])\n"
    h.apply(base.format(l="x") + tail.format(c="x"))
    prior_row = h.backend.load().nodes[N]
    h.fake().fail_update.add("c")

    with pytest.raises(ExceptionGroup) as failed:
        h.apply(base.format(l="y") + tail.format(c="y"), on_failure="rollback")
    assert any("update failed for c" in str(leaf) for leaf in leaves(failed.value))

    assert h.backend.load().nodes[N] == prior_row


# -- prevent_destroy -------------------------------------------------------------


def test_a_protected_conditional_replace_is_allowed_at_plan_and_collapses() -> None:
    h = _harness()
    _applied(h, lifecycle="prevent_destroy=True")
    planned = h.plan_only(_config("y", lifecycle="prevent_destroy=True"))
    assert is_successful(planned)

    report = h.apply(_config("y", lifecycle="prevent_destroy=True"))

    assert report.downgraded == {N: "noop"}
    assert h.fake().calls == [("update", "a")]


def test_a_protected_conditional_replace_is_refused_at_apply_once_confirmed() -> None:
    h = _harness(stable=False)
    _applied(h, lifecycle="prevent_destroy=True")
    prior_row = h.backend.load().nodes[N]

    with pytest.raises(ExceptionGroup) as refused:
        h.apply(_config("y", lifecycle="prevent_destroy=True"))

    errors = leaves(refused.value)
    assert any(isinstance(e, PreventDestroyError) for e in errors), errors
    assert "n" not in h.fake().deleted and "n" not in h.fake().created
    assert h.backend.load().nodes[N] == prior_row


def test_a_protected_node_behind_a_recreated_upstream_is_refused_at_plan() -> None:
    """A recreated upstream gets a new identity, so the replace is as good as
    certain: the plan refuses it rather than recreating the upstream first."""
    h = _harness()
    _applied(h, lifecycle="prevent_destroy=True")
    moved = (
        "a = Box('a', size=2, label='x')\n"  # size is immutable: a is replaced
        "Notifier('n', target_arn=a.out, lifecycle=Lifecycle(prevent_destroy=True))\n"
    )
    change = {c.node_id: c for c in h.diff_only(moved)}[N]
    # Known at plan: a destroy-first upstream needs n gone before its delete.
    assert (change.action, change.conditional) == (Action.REPLACE, False)
    assert isinstance(h.plan_only(moved).failure(), PreventDestroyError)


def test_the_plan_warns_that_a_protected_replace_is_judged_at_apply() -> None:
    from tests.support import engine_for, globals_of

    engine = engine_for(Box, Notifier, provider=FakeProvider(on_update=_stable_updates))
    source = _config(lifecycle="prevent_destroy=True")
    asyncio.run(engine.apply(source, extra_globals=globals_of(Box, Notifier, Lifecycle=Lifecycle)))
    planned = engine.plan(
        _config("y", lifecycle="prevent_destroy=True"),
        extra_globals=globals_of(Box, Notifier, Lifecycle=Lifecycle),
    ).unwrap()
    assert planned.warnings == (
        f"{N}: prevent_destroy is checked at apply — its replacement is known only "
        "after apply, and the apply refuses it if an immutable value actually changes",
    )


def test_a_failed_downgraded_update_is_reported_as_an_update() -> None:
    events: list[ApplyEvent] = []
    h = _harness()
    _applied(h)
    h.events = events.append
    h.fake().fail_update.add("n")

    with pytest.raises(ExceptionGroup) as failed:
        h.apply(_config("y", message="bye"))

    assert any("update of 'default:test.Notifier:n' failed" in str(e) for e in leaves(failed.value))
    mine = [(e.phase, e.action) for e in events if e.node_id == N]
    assert mine == [("node_start", "replace"), ("node_fail", "update")]
    assert h.fake().created == [] and h.fake().deleted == []
