"""Regressions for the executor's destroy order and create-before-destroy leftovers.

- A companion row left by a failed create-before-destroy cleanup can be processed:
  the next apply destroys it, and a destroy still works.
- A second create-before-destroy replace while that companion is pending destroys
  the older leftover first instead of overwriting its row.
- A conditional replace whose resolved immutable value did not move is not a replace.
- Every delete of a resource runs after the deletes of what depended on it in
  prior state: the delete half of a destroy-before-create replace, and the
  create-before-destroy cleanup.
- A compensation's state write is lease-checked.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest

from atlantide.cli.errors import flatten_group
from atlantide.core import Lifecycle, immutable, mutable
from atlantide.core.errors import LeaseLostError, RollbackError
from atlantide.reconcile import ChangeSet
from atlantide.reconcile.executor import records as records_module
from atlantide.state import NodeStatus, StateNode
from tests.support import FakeProvider, Harness, default_outputs
from tests.support.resources import Box, _TestResource

A = "default:test.Box:a"
A_OLD = "default:test.Box:a~replaced"
CBD = "lifecycle=Lifecycle(create_before_destroy=True)"


class Dep(_TestResource):
    """A dependent whose identity is an upstream output, like a subnet's VPC id."""

    parent: str = immutable()
    note: str = mutable(default="")


class Serial(FakeProvider):
    """Gives every create a fresh ``out`` and fails deletes of chosen outputs."""

    def __init__(self) -> None:
        serial = itertools.count(1)
        super().__init__(on_create=lambda _, res: {"out": f"{res.logical_name}#{next(serial)}"})
        self.fail_outs: set[str] = set()

    async def delete(self, ctx: Any, res: Any) -> None:
        await super().delete(ctx, res)
        if getattr(res, "out", None) in self.fail_outs:
            raise RuntimeError(f"delete failed for {res.out}")


def _cbd_harness() -> tuple[Harness, Serial]:
    provider = Serial()
    return Harness.of(Box, provider=provider, globals={"Lifecycle": Lifecycle}), provider


def _box(size: int) -> str:
    return f"Box('a', size={size}, {CBD})\n"


def _outputs(h: Harness) -> dict[str, Any]:
    return {k: v.outputs.get("out") for k, v in h.backend.load().nodes.items()}


def _deleted_outs(provider: FakeProvider) -> list[Any]:
    return [getattr(res, "out", None) for op, res in provider.seen if op == "delete"]


# -- the create-before-destroy companion ------------------------------------------


def test_a_failed_cbd_cleanup_is_finished_by_the_next_apply() -> None:
    h, provider = _cbd_harness()
    h.apply(_box(1))
    provider.fail_outs.add("a#1")
    with pytest.raises(ExceptionGroup):
        h.apply(_box(2))
    assert _outputs(h) == {A: "a#2", A_OLD: "a#1"}
    provider.fail_outs.clear()
    provider.reset()

    report = h.apply(_box(2))

    assert report.deleted == [A_OLD]
    assert _deleted_outs(provider) == ["a#1"]
    assert _outputs(h) == {A: "a#2"}


def test_a_destroy_removes_a_pending_cbd_companion() -> None:
    h, provider = _cbd_harness()
    h.apply(_box(1))
    provider.fail_outs.add("a#1")
    with pytest.raises(ExceptionGroup):
        h.apply(_box(2))
    provider.fail_outs.clear()
    provider.reset()

    h.apply("")

    assert sorted(_deleted_outs(provider)) == ["a#1", "a#2"]
    assert _outputs(h) == {}


def test_a_second_cbd_replace_destroys_the_pending_companion_first() -> None:
    """Writing the new companion over the pending one would leave ``a#1`` live and
    untracked."""
    h, provider = _cbd_harness()
    h.apply(_box(1))
    provider.fail_outs.add("a#1")
    with pytest.raises(ExceptionGroup):
        h.apply(_box(2))
    provider.fail_outs = {"a#2"}  # the new cleanup fails too
    provider.reset()

    with pytest.raises(ExceptionGroup):
        h.apply(_box(3))

    assert _deleted_outs(provider) == ["a#1", "a#2"]  # the pending one, then the cleanup
    assert _outputs(h) == {A: "a#3", A_OLD: "a#2"}, "the leftover a#2 is still tracked"
    provider.fail_outs.clear()
    h.apply(_box(3))
    assert _outputs(h) == {A: "a#3"}


def test_a_cbd_replace_refuses_to_overwrite_a_companion_it_does_not_destroy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """E.g. a targeted apply, whose plan leaves the pending companion out."""
    h, provider = _cbd_harness()
    h.apply(_box(1))
    provider.fail_outs.add("a#1")
    with pytest.raises(ExceptionGroup):
        h.apply(_box(2))
    provider.fail_outs.clear()
    provider.reset()
    planned = h._diff
    monkeypatch.setattr(
        h,
        "_diff",
        lambda *a: ChangeSet(changes=tuple(c for c in planned(*a).changes if c.node_id != A_OLD)),
    )

    with pytest.raises(ExceptionGroup) as caught:
        h.apply(_box(3))

    assert any("apply without --target" in str(e) for e in flatten_group(caught.value))
    assert provider.calls == []
    assert _outputs(h) == {A: "a#2", A_OLD: "a#1"}


# -- the conditional replace ---------------------------------------------------------


def test_an_unmoved_ref_on_an_immutable_field_does_not_replace() -> None:
    """A tag edit on the VPC must not rebuild its subnets."""
    stable = FakeProvider(on_update=lambda _, res: default_outputs("create", res))
    h = Harness.of(Box, Dep, provider=stable)
    h.apply("a = Box('a', size=1)\nDep('d', parent=a.out)\n")
    stable.reset()

    report = h.apply("a = Box('a', size=1, label='tag-only')\nDep('d', parent=a.out)\n")

    assert stable.calls == [("update", "a")]
    assert report.replaced == []
    assert report.downgraded == {"default:test.Dep:d": "noop"}


# -- destroy order ---------------------------------------------------------------------


def test_a_removed_dependent_is_deleted_before_its_upstream_is_replaced() -> None:
    h = Harness.of(Box, Dep)
    h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
    h.fake().reset()

    h.apply("a = Box('a', size=2)\n")

    assert h.fake().calls == [("delete", "b"), ("delete", "a"), ("create", "a")]


def test_a_replaced_dependent_is_deleted_before_its_upstream() -> None:
    h = Harness.of(Box)
    h.apply("a = Box('a', size=1)\nBox('d', size=3, ref=a.out)\n")
    h.fake().reset()

    report = h.apply("a = Box('a', size=2)\nBox('d', size=4, ref=a.out)\n")

    assert h.fake().calls == [("delete", "d"), ("delete", "a"), ("create", "a"), ("create", "d")]
    assert sorted(report.replaced) == [A, "default:test.Box:d"]
    assert h.fake().created_ref("d") == "a:2"


def test_a_removed_chain_is_deleted_dependents_first_before_the_replace() -> None:
    h = Harness.of(Box)
    h.apply("a = Box('a', size=1)\nb = Box('b', size=2, ref=a.out)\nBox('c', size=3, ref=b.out)\n")
    h.fake().reset()

    h.apply("a = Box('a', size=2)\n")

    assert h.fake().calls == [
        ("delete", "c"),
        ("delete", "b"),
        ("delete", "a"),
        ("create", "a"),
    ]


def test_a_cbd_cleanup_runs_after_a_removed_dependent_is_deleted() -> None:
    h = Harness.of(Box, globals={"Lifecycle": Lifecycle})
    h.apply(f"a = Box('a', size=1, {CBD})\nBox('b', size=2, ref=a.out)\n")
    h.fake().reset()

    h.apply(f"a = Box('a', size=2, {CBD})\n")

    assert h.fake().calls == [("create", "a"), ("delete", "b"), ("delete", "a")]


def test_an_unrelated_delete_still_waits_for_the_forward_pass() -> None:
    """Only a delete that must precede a replace is moved ahead: a failing create
    still leaves every other planned delete undone."""
    h = Harness.of(Box)
    h.apply("Box('a', size=1)\nBox('z', size=1)\n")
    h.fake().reset()
    h.fake().fail_create.add("n")

    with pytest.raises(ExceptionGroup):
        h.apply("Box('a', size=1)\nBox('n', size=1)\n")

    assert h.fake().deleted == []


def test_an_independent_removed_node_is_still_deleted_after_the_forward_pass() -> None:
    h = Harness.of(Box)
    h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\nBox('z', size=1)\n")
    h.fake().reset()

    h.apply("a = Box('a', size=2)\n")

    assert h.fake().calls == [("delete", "b"), ("delete", "a"), ("create", "a"), ("delete", "z")]


PRIOR_PAIR = "a = Box('a', size=1)\nBox('d', size=3, ref=a.out)\n"


@pytest.mark.parametrize(
    ("fail", "tail"),
    [
        ("a", ""),  # before the dependent's create
        ("c", "Box('c', size=1, depends_on=[d])\n"),  # after it
    ],
)
def test_a_rollback_recreates_a_dependent_deleted_ahead_of_the_forward_pass(
    fail: str, tail: str
) -> None:
    h = Harness.of(Box)
    h.apply(PRIOR_PAIR)
    h.fake().reset()
    h.fake().fail_create.add(fail)

    with pytest.raises(ExceptionGroup):
        h.apply(f"a = Box('a', size=2)\nd = Box('d', size=4, ref=a.out)\n{tail}", "rollback")

    assert h.fake().calls[:2] == [("delete", "d"), ("delete", "a")]
    recreated = h.fake().seen_values("size", "create")
    assert 3 in recreated, "d's original was recreated"
    # The failed create's own write-ahead row is left as before; d's is restored.
    row = h.backend.load().nodes["default:test.Box:d"]
    assert row.status == NodeStatus.CREATED, "no creating row"
    assert row.properties["size"] == 3


# -- compensation writes -----------------------------------------------------------------


def test_compensation_state_writes_are_lease_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """The re-created resource's row and a restored row go through the checked put."""
    checked: list[StateNode] = []
    original = records_module.NodeRecords.checked_put

    def spy(self: Any, node: StateNode) -> None:
        checked.append(node)
        original(self, node)

    stable = FakeProvider(on_update=lambda _, res: default_outputs("create", res))
    h = Harness.of(Box, Dep, provider=stable)
    h.apply("a = Box('a', size=1)\nr = Box('r', size=1)\nDep('d', parent=a.out)\n")
    prior = h.backend.load().nodes
    stable.fail_create.add("c")
    monkeypatch.setattr(records_module.NodeRecords, "checked_put", spy)

    with pytest.raises(ExceptionGroup):
        h.apply(
            "a = Box('a', size=1, label='x')\nr = Box('r', size=2)\n"
            "d = Dep('d', parent=a.out)\nBox('c', size=1, depends_on=[r, d])\n",
            on_failure="rollback",
        )

    kept = {i: n.input_hash for i, n in prior.items()}
    restored = {n.id: n for n in checked if kept.get(n.id) == n.input_hash}
    # d's collapsed row is put back verbatim; r's re-created row keeps its prior shape.
    assert restored["default:test.Dep:d"] == prior["default:test.Dep:d"]
    assert restored["default:test.Box:r"].outputs == {"out": "r:1"}


class LosesLeaseRecreating(FakeProvider):
    """Loses the lease while a rollback re-creates the replaced ``r``."""

    def __init__(self, harness: Harness) -> None:
        super().__init__(fail_create={"c"})
        self.harness = harness

    async def create(self, ctx: Any, res: Any) -> dict[str, Any]:
        outputs = await super().create(ctx, res)
        if res.logical_name == "r" and res.size == 1:  # the compensating re-create
            self.harness.lease.fail(LeaseLostError("another run took the lock"))
        return outputs


def test_a_replace_undo_refuses_its_row_once_the_lease_is_lost() -> None:
    h = Harness.of(Box)
    h.apply("r = Box('r', size=1)\n")
    prior = h.backend.load().nodes["default:test.Box:r"]
    h.provider = LosesLeaseRecreating(h)

    with pytest.raises(ExceptionGroup) as caught:
        h.apply("r = Box('r', size=2)\nBox('c', size=1, depends_on=[r])\n", "rollback")

    failed = {e.node_id for e in flatten_group(caught.value) if isinstance(e, RollbackError)}
    assert "default:test.Box:r" in failed
    row = h.backend.load().nodes["default:test.Box:r"]
    assert row.input_hash != prior.input_hash, "the re-created row was not written"
