"""Laws of the dependency graph, over generated graphs.

The graph layer is pure algorithm — Tarjan, Kahn, a closure walk — and it decides
the order every provider call happens in. Its failure modes are exactly the ones
hand-written examples are worst at:

* **A silent truncation.** Kahn's algorithm emits nothing for a node it never
  unblocks. An implementation that lost an edge would return a *shorter* order,
  and a shorter order is not an error — it is a plan that skips resources. The
  only reliable check is that the output is a permutation of the input, on graphs
  nobody drew by hand.
* **A cycle that is not caught.** A missed cycle is not a crash; it is a
  truncated order, i.e. the previous bullet. So cycle detection has to be tested
  against graphs that are cyclic *by construction* rather than by the author's
  belief that they drew one.
* **A selection that is not closed.** `--target` on a subnet has to pull in its
  VPC, or the apply creates a resource whose dependency does not exist yet. That
  is a property of every seed set against every graph shape, and there are more
  of both than anyone enumerates.

One thing deliberately not asserted here: that the reverse order is the forward
order reversed. It is false, and reasonably so — ties break by sorted id in each
direction independently, so two nodes with no edge between them come out in the
same relative order both ways. Asserting it would be asserting a bug.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from returns.result import Failure, Success

from atlantide.graph.build import build_graph
from atlantide.graph.model import DiGraph
from atlantide.graph.order import topological_order
from atlantide.graph.select import TargetError, closure, match_targets
from atlantide.ir.model import IRGraph
from tests.support.strategies import PlantedCycle, cyclic_ir_graphs, ir_graphs


def graphs() -> st.SearchStrategy[DiGraph]:
    """An acyclic `DiGraph`, built the way the engine builds one."""
    return ir_graphs().map(lambda ir: build_graph(ir).unwrap())


# -- ordering ----------------------------------------------------------------


@given(graphs(), st.booleans())
def test_every_dependency_is_ordered_before_its_dependent(graph: DiGraph, reverse: bool) -> None:
    """The whole point of the ordering, in both directions.

    Forward is create/update order: a node's dependencies act first. Reverse is
    destroy order and inverts the relation — a VPC cannot be destroyed while a
    subnet still references it.
    """
    order = topological_order(graph, reverse=reverse)
    position = {node_id: index for index, node_id in enumerate(order)}

    for node_id in graph.node_ids:
        for predecessor in graph.predecessors(node_id, reverse=reverse):
            assert position[predecessor] < position[node_id]


@given(graphs(), st.booleans())
def test_the_order_contains_every_node_exactly_once(graph: DiGraph, reverse: bool) -> None:
    """A Kahn implementation that drops an edge returns a short order rather than
    an error, and a short order is a plan that silently skips resources."""
    order = topological_order(graph, reverse=reverse)

    assert sorted(order) == sorted(graph.node_ids)
    assert len(order) == len(set(order))


@given(graphs(), st.booleans())
def test_the_ordering_is_deterministic(graph: DiGraph, reverse: bool) -> None:
    """The heap tie-break exists so that two runs of the same config schedule
    identically; a set-iteration tie-break would vary with `PYTHONHASHSEED`."""
    assert topological_order(graph, reverse=reverse) == topological_order(graph, reverse=reverse)


@given(ir_graphs())
def test_declaring_dependencies_in_another_order_builds_the_same_graph(ir: IRGraph) -> None:
    """Adjacency is sorted on the way in, so the order a config happens to list
    its dependencies in cannot reach the schedule — two configs that differ only
    in that order deploy in the same sequence."""
    reordered = IRGraph(
        nodes=tuple(
            replace(node, dependencies=tuple(reversed(node.dependencies))) for node in ir.nodes
        )
    )

    assert build_graph(reordered).unwrap() == build_graph(ir).unwrap()


# -- cycles ------------------------------------------------------------------


@given(cyclic_ir_graphs())
def test_a_cycle_is_reported_and_names_the_nodes_in_it(planted: PlantedCycle) -> None:
    """Detection plus attribution.

    Reporting *that* a config is cyclic is not enough to act on — the operator
    has to know which resources to break apart, and a detector that names the
    wrong component sends them to edit a file that is fine.
    """
    result = build_graph(planted.ir)

    assert isinstance(result, Failure)
    assert any(
        planted.dependency in cycle and planted.dependent in cycle
        for cycle in result.failure().cycles
    )


@given(ir_graphs())
def test_an_acyclic_graph_is_never_reported_as_cyclic(ir: IRGraph) -> None:
    """No false positives: a spurious cycle is a config that cannot be deployed
    at all, with no way for the operator to prove the tool wrong."""
    assert isinstance(build_graph(ir), Success)


@given(cyclic_ir_graphs())
def test_every_reported_cycle_is_genuinely_a_cycle(planted: PlantedCycle) -> None:
    """Each reported component must be strongly connected under the real edges —
    every member reachable from every other. A detector that reported an
    arbitrary set of ids would satisfy the test above and still be useless."""
    edges = {node.id: set(node.edges()) for node in planted.ir.nodes}

    def reachable(start: str) -> set[str]:
        seen: set[str] = set()
        stack = list(edges[start])
        while stack:
            current = stack.pop()
            if current not in seen:
                seen.add(current)
                stack.extend(edges[current])
        return seen

    for cycle in build_graph(planted.ir).failure().cycles:
        for node_id in cycle:
            assert set(cycle) <= reachable(node_id)


# -- selection ---------------------------------------------------------------


@st.composite
def graphs_with_seeds(draw: st.DrawFn) -> tuple[DiGraph, frozenset[str]]:
    """A graph and a subset of its ids.

    Seeds are drawn from the graph rather than generated freely: every caller
    feeds `closure` ids that `match_targets` already validated, so an unknown
    seed is not an input this function has to answer for.
    """
    graph = draw(graphs())
    seeds = draw(st.lists(st.sampled_from(graph.node_ids), max_size=3, unique=True))
    return graph, frozenset(seeds)


@given(graphs_with_seeds(), st.booleans())
def test_a_selection_is_closed_under_the_direction_it_walks(
    seeded: tuple[DiGraph, frozenset[str]], reverse: bool
) -> None:
    """The safety property `--target` rests on.

    Forward: nothing in the selection depends on something outside it, so a
    targeted create never references a resource that does not exist yet. Reverse:
    nothing outside the selection still points at something inside it, so a
    targeted destroy never strands a dangling reference.
    """
    graph, seeds = seeded

    selected = closure(graph, seeds, reverse=reverse)

    for node_id in selected:
        assert set(graph.predecessors(node_id, reverse=reverse)) <= selected


@given(graphs_with_seeds(), st.booleans())
def test_a_selection_contains_what_was_asked_for(
    seeded: tuple[DiGraph, frozenset[str]], reverse: bool
) -> None:
    graph, seeds = seeded

    assert seeds <= closure(graph, seeds, reverse=reverse)


@given(graphs_with_seeds(), st.booleans())
def test_closing_a_selection_again_adds_nothing(
    seeded: tuple[DiGraph, frozenset[str]], reverse: bool
) -> None:
    """Idempotence. The engine closes a selection more than once — once to pick
    the apply scope, again to pick the lock scope — and a closure that grew each
    time would widen the lock on every pass."""
    graph, seeds = seeded
    once = closure(graph, seeds, reverse=reverse)

    assert closure(graph, once, reverse=reverse) == once


@given(graphs_with_seeds(), st.booleans(), st.data())
def test_a_selection_grows_with_its_seeds(
    seeded: tuple[DiGraph, frozenset[str]], reverse: bool, data: st.DataObject
) -> None:
    """Monotone: asking for more never selects less."""
    graph, seeds = seeded
    extra = data.draw(st.lists(st.sampled_from(graph.node_ids), max_size=2, unique=True))
    wider = seeds | frozenset(extra)

    assert closure(graph, seeds, reverse=reverse) <= closure(graph, wider, reverse=reverse)


@given(graphs(), st.booleans())
def test_closing_over_everything_selects_everything(graph: DiGraph, reverse: bool) -> None:
    """A whole-graph selection is the un-targeted run, which must act on the
    whole graph — the degenerate case that keeps `--target` and no-`--target`
    on the same code path."""
    assert closure(graph, graph.node_ids, reverse=reverse) == frozenset(graph.node_ids)


@given(graphs())
def test_naming_every_node_selects_exactly_those_nodes(graph: DiGraph) -> None:
    """A full node id is the precise spelling and must resolve to itself, with no
    glob interpretation — ids contain no wildcard characters, and a pattern that
    matched more than it named would act on resources nobody asked for."""
    assert match_targets(graph.node_ids, graph.node_ids) == frozenset(graph.node_ids)


@given(graphs())
def test_a_pattern_matching_nothing_is_an_error_rather_than_an_empty_selection(
    graph: DiGraph,
) -> None:
    """A typo must not read as "did everything you asked", having done nothing."""
    with pytest.raises(TargetError):
        match_targets(["nonexistent:type:name"], graph.node_ids)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
