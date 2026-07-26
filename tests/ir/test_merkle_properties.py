"""What the Merkle hash promises, stated as laws over arbitrary graphs.

``apply`` skips a node whose stored ``input_hash`` equals the freshly computed
one, with **no provider read at all**. That makes the hash function load-bearing
in both directions and in a way examples cover badly:

* a hash that fails to move when something changed is a resource that silently
  never updates again;
* a hash that moves when nothing changed is churn on every run — and, for an
  immutable field, a destroy-and-recreate.

The second failure has already happened here once (a poisoned row manufacturing a
change on ref-bearing fields), which is why "and no other node's hash moved" is
asserted explicitly below rather than left implied.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.graph import build_graph, topological_order
from atlantide.ir.merkle import merkle_hashes
from atlantide.ir.model import IRGraph, IRNode
from tests.support.strategies import ir_graphs


def _hashes(ir: IRGraph) -> dict[str, str]:
    return merkle_hashes(ir, topological_order(build_graph(ir).unwrap()))


def _dependents_of(ir: IRGraph, node_id: str) -> set[str]:
    """Every node reachable *from* ``node_id`` by following edges upward."""
    reachable = {node_id}
    changed = True
    while changed:
        changed = False
        for node in ir.nodes:
            if node.id not in reachable and reachable & set(node.dependencies):
                reachable.add(node.id)
                changed = True
    return reachable - {node_id}


@given(ir_graphs())
def test_hashing_is_deterministic(ir: IRGraph) -> None:
    """The headline claim, at the level it is actually computed."""
    assert _hashes(ir) == _hashes(ir)


@given(ir_graphs())
def test_every_node_gets_a_hash(ir: IRGraph) -> None:
    assert set(_hashes(ir)) == {node.id for node in ir.nodes}


@given(ir_graphs(min_nodes=2))
def test_changing_a_node_moves_its_hash_and_every_dependents_and_nothing_else(
    ir: IRGraph,
) -> None:
    """The NOOP-skip promise in one sentence.

    A change must ripple to everything downstream — a dependent's resolved inputs
    move when its dependency's outputs do, and the dependency hashes folded into
    the payload are what carry that. And it must ripple no further: a node that
    neither changed nor depends on the change has nothing to re-apply, and moving
    its hash would mean a plan full of updates nobody asked for.
    """
    before = _hashes(ir)
    target = ir.nodes[0]
    edited = IRGraph(
        nodes=tuple(
            replace(node, properties={**node.properties, "__probe__": "moved"})
            if node.id == target.id
            else node
            for node in ir.nodes
        )
    )

    after = _hashes(edited)
    moved = {node_id for node_id in before if before[node_id] != after[node_id]}

    assert moved == {target.id} | _dependents_of(ir, target.id)


@given(ir_graphs())
def test_declaring_dependencies_in_another_order_hashes_the_same(ir: IRGraph) -> None:
    """`merkle_hashes` sorts the dependency hashes it folds in. Without that, the
    order two edges happened to be written in would change the content hash, and
    a config that had not been touched would re-plan."""
    reversed_deps = IRGraph(
        nodes=tuple(
            replace(node, dependencies=tuple(reversed(node.dependencies))) for node in ir.nodes
        )
    )

    assert _hashes(ir) == _hashes(reversed_deps)


@given(ir_graphs(), st.text(min_size=1, max_size=6))
def test_ignored_fields_never_move_the_hash(ir: IRGraph, noise: str) -> None:
    """`ignore_changes` means "drift here is not my business". If it moved the
    hash it would trigger the UPDATE it exists to prevent."""
    ignoring = IRGraph(
        nodes=tuple(
            replace(node, properties={**node.properties, "vol": "a"}, ignore_changes=("vol",))
            for node in ir.nodes
        )
    )
    noisy = IRGraph(
        nodes=tuple(
            replace(node, properties={**node.properties, "vol": noise}, ignore_changes=("vol",))
            for node in ir.nodes
        )
    )

    assert _hashes(ignoring) == _hashes(noisy)


@given(ir_graphs())
def test_a_hash_is_a_sha256_digest(ir: IRGraph) -> None:
    """Cheap shape check — a hash silently becoming a repr or an id would still
    compare equal to itself and pass every law above."""
    assert all(
        len(digest) == 64 and set(digest) <= set("0123456789abcdef")
        for digest in _hashes(ir).values()
    )


def test_two_nodes_differing_only_in_type_hash_differently() -> None:
    """Type is part of identity: the same properties under a different resource
    type are a different thing to build."""

    def node(type_name: str) -> IRGraph:
        return IRGraph(
            nodes=(
                IRNode(
                    id="s:t:a",
                    type=type_name,
                    provider="p",
                    provider_version="1.0.0",
                    properties={"size": 1},
                    dependencies=(),
                ),
            )
        )

    assert _hashes(node("t.A")) != _hashes(node("t.B"))


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
