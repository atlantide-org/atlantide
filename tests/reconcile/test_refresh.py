"""Refresh: provider reads classified into in-sync / drifted / missing drift."""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.reconcile import Drift, DriftReport
from atlantide.state import MemoryStateBackend, StateNode
from atlantide.state.backend import NO_INPUT_HASH, StateGraph
from tests.support import FakeProvider, Harness, Widget, state_node


def _node(name: str, outputs: dict[str, Any]) -> StateNode:
    return state_node(name, type=Widget.type_name(), outputs=outputs, properties={"label": name})


def _seed() -> tuple[MemoryStateBackend, dict[str, StateNode]]:
    nodes = {
        "a": _node("a", {"arn": "arn-a", "v": 1}),  # will stay in sync
        "b": _node("b", {"arn": "arn-b", "v": 1}),  # will drift
        "c": _node("c", {"arn": "arn-c"}),  # will be missing
    }
    backend = MemoryStateBackend()
    for node in nodes.values():
        backend.put(node)
    return backend, nodes


def _refresh(backend: MemoryStateBackend, provider: FakeProvider, *, write: bool) -> DriftReport:
    return Harness.of(Widget, provider=provider, backend=backend).refresh(write=write)


def test_refresh_detects_input_field_drift() -> None:
    # node 'a' has stored input property label='a'; the provider observes label='edited'
    # -> the mutable *input* drifted, not just outputs.
    backend, _ = _seed()
    provider = FakeProvider(
        live={
            "a": {"arn": "arn-a", "v": 1, "label": "edited"},  # input drift
            "b": {"arn": "arn-b", "v": 1, "label": "b"},  # in sync
            "c": {"arn": "arn-c", "label": "c"},
        }
    )
    report = _refresh(backend, provider, write=False)
    kinds = {n.node_id.rsplit(":", 1)[-1]: n.kind for n in report.nodes}
    assert kinds["a"] is Drift.DRIFTED
    assert kinds["b"] is Drift.IN_SYNC
    drifted = next(n for n in report.nodes if n.node_id.endswith(":a"))
    assert drifted.changed == {"label": ("a", "edited")}  # stored input -> observed


def test_refresh_classifies_drift() -> None:
    backend, _ = _seed()
    provider = FakeProvider(
        live={
            "a": {"arn": "arn-a", "v": 1},  # unchanged
            "b": {"arn": "arn-b", "v": 2},  # v drifted 1 -> 2
            "c": None,  # gone at the provider
        }
    )
    report = _refresh(backend, provider, write=False)

    kinds = {n.node_id.rsplit(":", 1)[-1]: n.kind for n in report.nodes}
    assert kinds == {"a": Drift.IN_SYNC, "b": Drift.DRIFTED, "c": Drift.MISSING}
    assert report.has_drift
    assert [n.node_id.rsplit(":", 1)[-1] for n in report.drifted] == ["b"]
    assert [n.node_id.rsplit(":", 1)[-1] for n in report.missing] == ["c"]

    drifted = report.drifted[0]
    assert drifted.changed == {"v": (1, 2)}

    # report order is deterministic (sorted by node id)
    assert [n.node_id for n in report.nodes] == sorted(n.node_id for n in report.nodes)


def test_refresh_read_only_leaves_state_untouched() -> None:
    backend, _ = _seed()
    provider = FakeProvider(live={"a": {"arn": "arn-a", "v": 1}, "b": {"v": 99}, "c": None})
    before = backend.load()
    _refresh(backend, provider, write=False)
    assert backend.load() == before


def test_refresh_write_syncs_state() -> None:
    backend, _ = _seed()
    provider = FakeProvider(
        live={"a": {"arn": "arn-a", "v": 1}, "b": {"arn": "arn-b", "v": 2}, "c": None}
    )
    _refresh(backend, provider, write=True)
    state: StateGraph = backend.load()

    def outputs_of(name: str) -> dict[str, Any]:
        node = state.get(f"default:{Widget.type_name()}:{name}")
        assert node is not None
        return node.outputs

    # drifted node's outputs overwritten with live values
    assert outputs_of("b") == {"arn": "arn-b", "v": 2}
    # a node the provider could not find is *kept* — see `test_refresh_write_
    # keeps_a_row_it_could_not_find`. Its hash is cleared so the next plan
    # re-checks it rather than skipping it.
    gone = state.get(f"default:{Widget.type_name()}:c")
    assert gone is not None
    assert gone.input_hash == NO_INPUT_HASH
    # in-sync node untouched
    assert outputs_of("a") == {"arn": "arn-a", "v": 1}


def test_refresh_write_keeps_a_row_it_could_not_find() -> None:
    """One failed read is not evidence enough to discard the only record that a
    resource exists.

    A read can be wrong for reasons that have nothing to do with the resource —
    an unpaginated listing, a missing permission, an eventually-consistent view.
    Deleting on that evidence turns a transient misread into a permanent loss,
    and the next apply builds a second resource alongside the first.
    """
    backend, _ = _seed()
    provider = FakeProvider(live={"a": None, "b": None, "c": None})
    report = _refresh(backend, provider, write=True)

    assert len(report.missing) == 3, "still reported"
    assert len(backend.load()) == 3, "but not discarded"


def test_refresh_write_prune_is_how_you_actually_forget_them() -> None:
    """The opt-in. Once an operator has confirmed the resources are gone, this is
    what drops the rows."""
    backend, _ = _seed()
    provider = FakeProvider(live={"a": None, "b": None, "c": None})
    Harness.of(Widget, provider=provider, backend=backend).refresh(write=True, prune=True)

    assert len(backend.load()) == 0


def test_refresh_no_drift() -> None:
    backend, _ = _seed()
    provider = FakeProvider(
        live={"a": {"arn": "arn-a", "v": 1}, "b": {"arn": "arn-b", "v": 1}, "c": {"arn": "arn-c"}}
    )
    report = _refresh(backend, provider, write=False)
    assert not report.has_drift
    assert len(report.in_sync) == 3


def test_in_sync_records_the_inputs_the_read_never_checked() -> None:
    """IN_SYNC is a claim scoped to what the provider reported.

    Every node here stores a `label` input, and this provider's read returns only
    outputs — so nothing about `label` was verified. The verdict is still IN_SYNC
    (no observed value disagrees) but it must carry the fact that the one input
    this resource has went unchecked, or it reads as "atlantide verified your
    infrastructure" when no API call established that.
    """
    backend, _ = _seed()
    provider = FakeProvider(
        live={"a": {"arn": "arn-a", "v": 1}, "b": {"arn": "arn-b", "v": 1}, "c": {"arn": "arn-c"}}
    )
    report = _refresh(backend, provider, write=False)
    assert not report.has_drift
    for node in report.nodes:
        assert node.kind is Drift.IN_SYNC
        assert node.unobserved == ("label",)
        assert node.observed == ()


def test_a_read_that_reports_its_inputs_has_nothing_unobserved() -> None:
    backend, _ = _seed()
    provider = FakeProvider(
        live={
            "a": {"arn": "arn-a", "v": 1, "label": "a"},
            "b": {"arn": "arn-b", "v": 1, "label": "b"},
            "c": {"arn": "arn-c", "label": "c"},
        }
    )
    report = _refresh(backend, provider, write=False)
    assert not report.has_drift
    for node in report.nodes:
        assert node.unobserved == ()
        assert node.observed == ("label",)


def test_missing_nodes_carry_no_coverage_claim() -> None:
    """A resource that is gone has no fields to have checked."""
    backend, _ = _seed()
    provider = FakeProvider(live={"a": None, "b": None, "c": None})
    report = _refresh(backend, provider, write=False)
    assert len(report.missing) == 3
    for node in report.nodes:
        assert node.observed == () and node.unobserved == ()


def test_drifted_node_still_reports_what_went_unchecked() -> None:
    """Coverage is orthogonal to the verdict: a drifted node can also have
    unchecked fields, and finding one problem does not imply there is only one."""
    backend, _ = _seed()
    provider = FakeProvider(
        live={
            "a": {"arn": "arn-a", "v": 2},  # output drift, `label` unchecked
            "b": {"arn": "arn-b", "v": 1, "label": "b"},
            "c": {"arn": "arn-c", "label": "c"},
        }
    )
    report = _refresh(backend, provider, write=False)
    drifted = next(n for n in report.nodes if n.node_id.endswith(":a"))
    assert drifted.kind is Drift.DRIFTED
    assert drifted.changed == {"v": (1, 2)}
    assert drifted.unobserved == ("label",)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
