"""Diff classification: CREATE/UPDATE/REPLACE/DELETE/NOOP + known-after-apply."""

from __future__ import annotations

from atlantide.reconcile import Action
from atlantide.state import MemoryStateBackend

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"


def _actions(changeset: object) -> dict[str, Action]:
    return {c.node_id: c.action for c in changeset}  # type: ignore[attr-defined]


def test_all_create_on_empty_state() -> None:
    h = Harness(MemoryStateBackend())
    cs = h.diff_only("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
    assert _actions(cs) == {A: Action.CREATE, B: Action.CREATE}


def test_create_then_noop() -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    cs = h.diff_only("Box('a', size=1)\n")
    assert _actions(cs) == {A: Action.NOOP}


def test_mutable_change_is_update() -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1, label='x')\n")
    cs = h.diff_only("Box('a', size=1, label='y')\n")
    change = next(iter(cs))
    assert change.action is Action.UPDATE
    assert change.changed_fields == ("label",)


def test_immutable_change_is_replace() -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    cs = h.diff_only("Box('a', size=2)\n")
    change = next(iter(cs))
    assert change.action is Action.REPLACE
    assert change.changed_fields == ("size",)
    assert change.conditional is False


def test_removed_resource_is_delete() -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\nBox('b', size=2)\n")
    cs = h.diff_only("Box('a', size=1)\n")
    assert _actions(cs) == {A: Action.NOOP, B: Action.DELETE}


def test_new_resource_is_create() -> None:
    h = Harness(MemoryStateBackend())
    h.apply("Box('a', size=1)\n")
    cs = h.diff_only("Box('a', size=1)\nBox('b', size=2)\n")
    assert _actions(cs) == {A: Action.NOOP, B: Action.CREATE}


def test_dependency_change_propagates_as_update() -> None:
    # b depends on a.out; change a's input -> a UPDATEs, b sees a hash change too.
    h = Harness(MemoryStateBackend())
    h.apply("a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n")
    cs = h.diff_only("a = Box('a', size=1, label='moved')\nBox('b', size=2, ref=a.out)\n")
    actions = _actions(cs)
    assert actions[A] is Action.UPDATE
    # b's own inputs are symbolically identical but its Merkle hash moved with a.
    assert actions[B] is Action.UPDATE
    b_change = next(c for c in cs if c.node_id == B)
    assert "ref" in b_change.changed_fields  # attributed to the ref-bearing field
