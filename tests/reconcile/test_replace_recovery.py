"""What state records after a REPLACE, a rollback, or a failing rollback.

A REPLACE destroys a live resource and provisions a new one with a new identity.
Each window in that sequence can leave state describing a resource that does not
exist, or omitting one that does; the stored hash still matches config, so the
next plan reports NOOP.
"""

from __future__ import annotations

import itertools

import pytest

from atlantide.cli.errors import flatten_group
from atlantide.core.errors import RollbackError, StateError
from atlantide.reconcile import Action
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import NO_INPUT_HASH, STATUS_CREATED, STATUS_CREATING
from tests.support import FakeProvider

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


def _leaves(group: BaseException) -> list[BaseException]:
    return flatten_group(group)


def test_failed_replace_does_not_leave_state_asserting_the_destroyed_resource() -> None:
    """Destroy-before-create: once the delete lands, the prior row is stale.

    Without a write-ahead row, state still reports `created` with the old outputs,
    so refresh classifies the node MISSING and dependents resolve refs to a value
    that no longer exists.
    """
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    h.fake().reset()
    h.fake().fail_create.add("a")

    with pytest.raises(ExceptionGroup):
        h.apply("Box('a', size=2)\n")  # size is immutable -> REPLACE

    assert h.fake().calls == [("delete", "a"), ("create", "a")]
    assert h.backend.load().nodes[A].status == STATUS_CREATING


def test_rollback_of_a_replace_records_the_recreated_identity() -> None:
    """The compensating create provisions a new resource with a new id.

    Writing the prior state row back verbatim points state at the id just
    destroyed: refresh classifies the node MISSING, destroy removes nothing, and
    the resource that exists is left untracked.
    """
    # Each create returns a distinct id, as a real provider does.
    serial = itertools.count(1)
    provider = FakeProvider(
        on_create=lambda ctx, res: {"out": f"{res.logical_name}#{next(serial)}"}
    )
    h = Harness(MemoryStateBackend(), provider)
    h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
    assert h.backend.load().nodes[A].outputs == {"out": "a#1"}
    h.fake().reset()
    h.fake().fail_update.add("b")

    with pytest.raises(ExceptionGroup):
        h.apply(
            "a = Box('a', size=7)\nBox('b', size=2, ref=a.out, label='x')\n", on_failure="rollback"
        )

    # forward: delete+create a (REPLACE, "a#3"); undo: delete it, recreate ("a#4").
    assert h.fake().calls.count(("create", "a")) == 2
    node = h.backend.load().nodes[A]
    assert node.outputs == {"out": "a#4"}, "the id of the resource that now exists"
    assert node.status == STATUS_CREATED


def test_a_compensation_that_fails_is_raised_not_swallowed() -> None:
    """A half-done undo leaves state and the provider disagreeing while the stored
    hash still matches config, so the next plan reports NOOP.

    Here `a` is created, `b` fails, and `a`'s compensating delete also fails.
    """
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.add("b")
    h.fake().fail_delete.add("a")

    with pytest.raises(ExceptionGroup) as caught:
        h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n", on_failure="rollback")

    leaves = _leaves(caught.value)
    assert any(isinstance(e, RollbackError) and A in str(e) for e in leaves)
    assert any("create failed for b" in str(e) for e in leaves), "original failure kept"


def test_refresh_keeps_a_write_ahead_row_the_provider_cannot_see() -> None:
    """A `creating` row carries no physical id, so `read` reports MISSING whether
    or not a resource was provisioned; the row is the only record of the attempt."""
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.add("a")
    with pytest.raises(ExceptionGroup):
        h.apply("Box('a', size=1)\n")
    assert h.backend.load().nodes[A].status == STATUS_CREATING

    h.fake().fail_create.clear()
    h.fake()._live = {"a": None}  # provider cannot find it
    h.refresh(write=True)

    assert A in h.backend.load().nodes, "the write-ahead row is the only trace"


def test_refresh_marks_a_confirmed_row_the_provider_cannot_find() -> None:
    """Kept, not dropped: a failed read is not proof the resource is gone.

    The hash is cleared so the next plan re-checks the node instead of skipping
    it — the divergence stays visible without the row being discarded.
    """
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    h.fake()._live = {"a": None}
    h.refresh(write=True)
    node = h.backend.load().nodes[A]
    assert node.input_hash == NO_INPUT_HASH


def test_refresh_prune_drops_a_confirmed_row_that_is_gone() -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    h.fake()._live = {"a": None}
    h.refresh(write=True, prune=True)
    assert A not in h.backend.load().nodes


def test_a_node_whose_compensation_failed_is_not_reported_as_unchanged() -> None:
    """The consequence that matters, asserted where the operator would see it.

    `a` is created, `b` fails, and `a`'s compensating delete then fails too — so
    the provider no longer agrees with state. Because the diff is symbolic, `a`'s
    stored hash still matches config, and without an explicit stale mark the next
    plan would Merkle-skip it and report NOOP: state would be permanently wrong
    about `a` with nothing ever saying so.

    This asserts the *plan*, not the state column, because the column is only a
    means to this end.
    """
    source = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.add("b")
    h.fake().fail_delete.add("a")

    with pytest.raises(ExceptionGroup):
        h.apply(source, on_failure="rollback")

    # The row is still there (its delete failed), but re-planning must not skip it.
    assert A in h.backend.load().nodes
    action = {c.node_id: c.action for c in h.diff_only(source)}[A]
    assert action is not Action.NOOP


def test_a_successful_compensation_leaves_no_stale_mark_behind() -> None:
    """Poisoning happens before the undo, so the undo's own write must clear it.

    Otherwise every rollback — including the ones that work perfectly — would
    leave rows that re-plan forever.
    """
    source = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"
    h = Harness(MemoryStateBackend())
    h.fake().fail_create.add("b")  # `a`'s delete is allowed to succeed

    with pytest.raises(ExceptionGroup):
        h.apply(source, on_failure="rollback")

    assert A not in h.backend.load().nodes, "a fully compensated create leaves no row"


def test_a_failed_delete_of_the_state_row_marks_it_stale() -> None:
    """The provider delete already succeeded, so the resource is gone; a state row
    left behind with a matching hash would read as NOOP forever."""

    class RefusesToForget(MemoryStateBackend):
        def delete(self, node_id: str) -> None:
            raise StateError("state write refused")

    h = Harness(RefusesToForget())
    h.apply("Box('a', size=1)\n")
    assert h.backend.load().nodes[A].input_hash != NO_INPUT_HASH

    with pytest.raises(ExceptionGroup):
        h.apply("")  # `a` dropped from config -> DELETE

    assert h.backend.load().nodes[A].input_hash == NO_INPUT_HASH
