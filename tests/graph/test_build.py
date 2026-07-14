"""Graph construction, cycle detection, and topological ordering."""

from __future__ import annotations

from atlantide.core import CycleError, is_successful
from atlantide.graph import build_graph, topological_order

from .conftest import ir_from


def test_build_deps_and_dependents() -> None:
    # c depends on a and b; b depends on a.
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["a", "b"]})).unwrap()
    assert graph.node_ids == ("a", "b", "c")
    assert graph.deps == {"a": (), "b": ("a",), "c": ("a", "b")}
    assert graph.dependents == {"a": ("b", "c"), "b": ("c",), "c": ()}


def test_topological_order_linear() -> None:
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["b"]})).unwrap()
    assert topological_order(graph) == ["a", "b", "c"]
    assert topological_order(graph, reverse=True) == ["c", "b", "a"]


def test_topological_order_diamond_is_deterministic() -> None:
    # a -> {b, c} -> d
    graph = build_graph(ir_from({"a": [], "b": ["a"], "c": ["a"], "d": ["b", "c"]})).unwrap()
    order = topological_order(graph)
    assert order[0] == "a"
    assert order[-1] == "d"
    assert order == ["a", "b", "c", "d"]  # ties broken by sorted id
    assert order.index("b") < order.index("d")
    assert order.index("c") < order.index("d")


def test_disconnected_nodes() -> None:
    graph = build_graph(ir_from({"a": [], "b": [], "c": ["b"]})).unwrap()
    assert topological_order(graph) == ["a", "b", "c"]


def test_self_loop_is_cycle() -> None:
    result = build_graph(ir_from({"a": ["a"]}))
    assert not is_successful(result)
    err = result.failure()
    assert isinstance(err, CycleError)
    assert err.cycles == [["a"]]


def test_two_node_cycle() -> None:
    result = build_graph(ir_from({"a": ["b"], "b": ["a"]}))
    assert not is_successful(result)
    assert result.failure().cycles == [["a", "b"]]


def test_reports_all_cycles() -> None:
    # Two independent 2-cycles plus an acyclic tail.
    spec = {"a": ["b"], "b": ["a"], "x": ["y"], "y": ["x"], "t": ["a"]}
    result = build_graph(ir_from(spec))
    assert not is_successful(result)
    cycles = sorted(result.failure().cycles)
    assert cycles == [["a", "b"], ["x", "y"]]


def test_longer_cycle() -> None:
    result = build_graph(ir_from({"a": ["c"], "b": ["a"], "c": ["b"]}))
    assert not is_successful(result)
    assert sorted(result.failure().cycles[0]) == ["a", "b", "c"]
