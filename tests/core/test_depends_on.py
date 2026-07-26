"""``depends_on=``: ordering that is real but not expressible as a value.

Most ordering needs no declaring — reading ``other.arn`` already creates the
edge. This is for the cases where nothing is read: an IAM policy that has to
propagate before the thing using it starts, a bucket policy that must exist
before an upload.

The property that makes it safe to reach for is that it orders and nothing more:
adding one must never re-plan the resources it points at.
"""

from __future__ import annotations

import pytest

from atlantide.core.errors import IRError
from atlantide.graph import build_graph, topological_order
from atlantide.ir import lower, merkle_hashes
from atlantide.ir.hash import hash_ir
from atlantide.lang import evaluate_source
from atlantide.state import MemoryStateBackend
from tests.support import Box, globals_of

from ..reconcile.conftest import Harness

GLOBALS = globals_of(Box)

A = "default:test.Box:a"
B = "default:test.Box:b"


def _ir(source: str) -> object:
    return lower(evaluate_source(source, extra_globals=GLOBALS).unwrap())


# -- the hash invariant -------------------------------------------------------


def test_adding_an_ordering_edge_does_not_move_the_content_hash() -> None:
    """The reason `depends_on` is stored apart from `dependencies`.

    `merkle_hashes` folds each dependency's hash into the dependent's, so an
    ordering *hint* inside the hashed payload would re-hash the node and
    everything below it — an UPDATE on resources whose configuration did not
    change. Ordering is not identity.
    """
    plain = _ir("Box('a', size=1)\nBox('b', size=2)\n")
    ordered = _ir("a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n")

    assert hash_ir(plain) == hash_ir(ordered)


def test_adding_an_ordering_edge_plans_no_change() -> None:
    """The consequence, asserted where an operator would see it."""
    source = "Box('a', size=1)\nBox('b', size=2)\n"
    h = Harness(MemoryStateBackend())
    h.apply(source)

    changed = h.diff_only("a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n")

    assert changed.actionable == [], "an ordering hint is not a change"


def test_the_merkle_hashes_are_unmoved_too() -> None:
    """Belt and braces: `hash_ir` covers the document, `merkle_hashes` the
    per-node digests the diff actually compares."""

    def hashes(source: str) -> dict[str, str]:
        ir = _ir(source)
        return merkle_hashes(ir, topological_order(build_graph(ir).unwrap()))

    plain = hashes("Box('a', size=1)\nBox('b', size=2)\n")
    ordered = hashes("a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n")
    assert plain == ordered


# -- it actually orders -------------------------------------------------------


def test_an_explicit_edge_orders_the_apply() -> None:
    ir = _ir("a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n")
    order = topological_order(build_graph(ir).unwrap())
    assert order.index(A) < order.index(B)


def test_an_explicit_edge_appears_in_the_graph() -> None:
    ir = _ir("a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n")
    graph = build_graph(ir).unwrap()
    assert A in graph.deps[B]
    assert B in graph.dependents[A]


def test_a_cycle_through_an_explicit_edge_is_rejected() -> None:
    """An ordering edge is a real edge, so it can create a real cycle — and one
    made only of ordering hints is just as unschedulable as one made of values."""
    from returns.result import Failure

    ir = _ir(f"Box('a', size=1, depends_on=[{B!r}])\nBox('b', size=2, depends_on=[{A!r}])\n")
    assert isinstance(build_graph(ir), Failure)


def test_a_node_id_string_works_as_well_as_the_resource() -> None:
    by_object = _ir("a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n")
    by_id = _ir(f"Box('a', size=1)\nBox('b', size=2, depends_on=[{A!r}])\n")
    assert by_object.node(B).depends_on == by_id.node(B).depends_on


def test_an_unknown_target_is_an_error() -> None:
    """A typo would otherwise silently order nothing."""
    ir = _ir("Box('b', size=2, depends_on=['default:test.Box:ghost'])\n")
    with pytest.raises(IRError, match="unknown node"):
        build_graph(ir)


# -- authoring mistakes -------------------------------------------------------


def test_a_bare_string_is_refused() -> None:
    """`depends_on="a"` would iterate into one edge per character — the same trap
    `Lifecycle.aliases` guards against."""
    with pytest.raises(IRError, match="not a bare string"):
        Box("b", size=2, depends_on="default:test.Box:a")  # type: ignore[arg-type]


def test_something_that_is_neither_a_resource_nor_an_id_is_refused() -> None:
    with pytest.raises(IRError, match="resources or node ids"):
        Box("b", size=2, depends_on=[42])  # type: ignore[list-item]


# -- interaction with the diff ------------------------------------------------


def test_recreating_an_explicit_dependency_re_applies_its_dependent() -> None:
    """`_stale_dependents` pulls a node out of NOOP when an upstream node is
    recreated. An explicit edge has to count: the dependent was ordered after it
    for a reason, and that reason does not evaporate because no value is read.
    """
    source = "a = Box('a', size=1)\nBox('b', size=2, depends_on=[a])\n"
    h = Harness(MemoryStateBackend())
    h.apply(source)
    h.backend.delete(A)  # `a` vanishes from state -> planned as CREATE again

    actions = {c.node_id: c.action for c in h.diff_only(source)}

    assert actions[A].value == "create"
    assert actions[B].value != "noop", "the dependent is re-applied, not skipped"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
