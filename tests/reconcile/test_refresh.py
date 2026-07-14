"""Refresh: provider reads classified into in-sync / drifted / missing drift."""

from __future__ import annotations

from typing import Any

import pytest

from atlantide.reconcile import Drift, DriftReport
from atlantide.state import MemoryStateBackend, StateNode
from atlantide.state.backend import StateGraph
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
    # missing node removed from state
    assert state.get(f"default:{Widget.type_name()}:c") is None
    # in-sync node untouched
    assert outputs_of("a") == {"arn": "arn-a", "v": 1}


def test_refresh_no_drift() -> None:
    backend, _ = _seed()
    provider = FakeProvider(
        live={"a": {"arn": "arn-a", "v": 1}, "b": {"arn": "arn-b", "v": 1}, "c": {"arn": "arn-c"}}
    )
    report = _refresh(backend, provider, write=False)
    assert not report.has_drift
    assert len(report.in_sync) == 3


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
