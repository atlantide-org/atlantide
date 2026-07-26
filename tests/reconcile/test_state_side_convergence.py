"""Convergence when the change is on the state side rather than in config.

The Merkle ``input_hash`` is a function of config alone, so it cannot see a
resource deleted out of band, a create that never confirmed, or a field edited in
a console. Each leaves config and state hashing identically while the
infrastructure has moved, and a diff trusting the hash reports NOOP indefinitely.

End-to-end (apply -> perturb state -> plan), since the behaviour lives in the
seam between the hash, the diff, and refresh.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from atlantide.reconcile import Action, Drift
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import STATUS_CREATING

from .conftest import Harness

A = "default:test.Box:a"
B = "default:test.Box:b"
C = "default:test.Box:c"

#: `b` reads `a`'s computed output, `c` reads `b`'s: a two-hop ref chain.
CHAIN = "a = Box('a', size=1)\nb = Box('b', size=2, ref=a.out)\nBox('c', size=3, ref=b.out)\n"


def _actions(changeset: Any) -> dict[str, Action]:
    return {c.node_id: c.action for c in changeset}


def test_dependents_re_apply_when_a_dependency_is_recreated() -> None:
    """`a` is absent from state, so it is recreated with a new output; `b` and `c`
    hold the old one with their hashes untouched."""
    h = Harness(MemoryStateBackend())
    h.apply(CHAIN)
    h.backend.delete(A)

    actions = _actions(h.diff_only(CHAIN))
    assert actions[A] is Action.CREATE
    assert actions[B] is not Action.NOOP, "b still points at the destroyed a"
    assert actions[C] is not Action.NOOP, "staleness is transitive through b"


def test_dependents_re_apply_after_an_unconfirmed_create() -> None:
    """A write-ahead row means the create may have leaked; the diff re-creates it,
    which again hands every dependent a new value."""
    h = Harness(MemoryStateBackend())
    h.apply(CHAIN)
    node = h.backend.load().nodes[A]
    h.backend.put(replace(node, status=STATUS_CREATING))

    actions = _actions(h.diff_only(CHAIN))
    assert actions[A] is Action.CREATE
    assert actions[B] is not Action.NOOP


def test_unrelated_nodes_still_noop() -> None:
    """Only actual dependents come out of NOOP; the Merkle skip is the point."""
    h = Harness(MemoryStateBackend())
    source = "Box('a', size=1)\nBox('b', size=2)\n"
    h.apply(source)
    h.backend.delete(A)

    actions = _actions(h.diff_only(source))
    assert actions[A] is Action.CREATE
    assert actions[B] is Action.NOOP


def test_re_apply_is_still_a_full_noop() -> None:
    h = Harness(MemoryStateBackend())
    h.apply(CHAIN)
    assert set(_actions(h.diff_only(CHAIN)).values()) == {Action.NOOP}


# -- refresh -> plan --------------------------------------------------------


def _drift(h: Harness, node_id: str) -> Drift:
    report = h.refresh(write=True)
    return next(n.kind for n in report.nodes if n.node_id == node_id)


def test_refreshed_input_drift_is_planned_not_noopd() -> None:
    """`refresh --write` records the drift; the next plan must act on it.

    Preserving ``input_hash`` across the sync leaves the drift report and the plan
    contradicting each other.
    """
    source = "Box('a', size=1, label='good')\n"
    h = Harness(MemoryStateBackend())
    h.apply(source)
    h.fake()._live = {"a": {"out": "a:1", "label": "HACKED"}}

    assert _drift(h, A) is Drift.DRIFTED
    change = next(iter(h.diff_only(source)))
    assert change.action is Action.UPDATE, "config must be re-asserted over the drift"


def test_synced_literal_drift_is_recorded_in_properties() -> None:
    source = "Box('a', size=1, label='good')\n"
    h = Harness(MemoryStateBackend())
    h.apply(source)
    h.fake()._live = {"a": {"out": "a:1", "label": "HACKED"}}
    h.refresh(write=True)

    node = h.backend.load().nodes[A]
    assert node.properties["label"] == "HACKED"
    assert next(iter(h.diff_only(source))).changed_fields == ("label",)


def test_a_ref_valued_field_does_not_drift_against_its_resolved_value() -> None:
    """State keeps ``{"$ref": ...}``; the provider reports the resolved string, so
    comparing them directly marks every ref-valued field DRIFTED."""
    source = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"
    h = Harness(MemoryStateBackend())
    h.apply(source)
    h.fake()._live = {"a": {"out": "a:1"}, "b": {"out": "b:2", "ref": "a:1"}}

    kinds = {n.node_id: n.kind for n in h.refresh(write=False).nodes}
    assert kinds[B] is Drift.IN_SYNC


def test_sync_does_not_overwrite_a_ref_marker_with_its_value() -> None:
    """Even on genuine drift the marker survives: it is the dependency record, and
    a literal in its place would diff against config as an unrelated change."""
    source = "a = Box('a', size=1)\nBox('b', size=2, ref=a.out)\n"
    h = Harness(MemoryStateBackend())
    h.apply(source)
    h.fake()._live = {"a": {"out": "a:1"}, "b": {"out": "b:2", "ref": "TAMPERED"}}
    h.refresh(write=True)

    node = h.backend.load().nodes[B]
    assert node.properties["ref"] == {"$ref": f"{A}#out"}
    # ...and the drift is still not lost: the hash is what carries it forward.
    assert _actions(h.diff_only(source))[B] is not Action.NOOP
