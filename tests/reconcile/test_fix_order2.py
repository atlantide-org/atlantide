"""Replace ordering: create-before-destroy propagation, and dependents of a destroy-first upstream.

- A node declared ``create_before_destroy`` makes everything it depends on
  create-before-destroy too, so no upstream is deleted while it still uses it.
- A conditional REPLACE with an ``immutable()`` ref to an upstream replaced
  destroy-first is known at plan: it is destroyed before the upstream (phase 0)
  and never collapses into an UPDATE or NOOP afterwards.
"""

from __future__ import annotations

import importlib
from typing import Any, ClassVar

import pytest

from atlantide.core import Lifecycle, Resource, computed, immutable
from atlantide.graph import cbd_forcers, effective_cbd
from atlantide.ir import IRGraph
from atlantide.ir.model import IRNode
from atlantide.reconcile import Action
from atlantide.reconcile.executor import deletes as deletes_module
from tests.support import Box, FakeProvider, Harness, default_outputs

# The module, not the ``diff`` function ``atlantide.reconcile`` re-exports under its name.
diff_module = importlib.import_module("atlantide.reconcile.diff")


class Link(Resource):
    """Consumes an upstream output through an immutable field and exposes its own."""

    class Meta:
        provider: ClassVar[str] = "test"

    parent: str = immutable()
    size: int = immutable(default=1)
    out: str = computed()


A = "default:test.Box:a"
D = "default:test.Link:d"
E = "default:test.Link:e"
CBD = "lifecycle=Lifecycle(create_before_destroy=True)"


def _harness(provider: FakeProvider | None = None) -> Harness:
    return Harness.of(Box, Link, provider=provider, globals={"Lifecycle": Lifecycle})


def _same_out(op: str, res: Any) -> dict[str, Any]:
    """A recreated ``a`` keeps its ``out``: the value its consumers hold does not move."""
    return {"out": "a:same"} if isinstance(res, Box) else default_outputs(op, res)


def _changes(h: Harness, source: str) -> dict[str, Any]:
    return {c.node_id: c for c in h.diff_only(source)}


# -- the propagation rule -----------------------------------------------------------


def _node(node_id: str, *deps: str, cbd: bool = False, after: tuple[str, ...] = ()) -> IRNode:
    return IRNode(
        id=node_id,
        type="test.Box",
        provider="test",
        provider_version="1.0.0",
        properties={},
        dependencies=deps,
        create_before_destroy=cbd,
        depends_on=after,
    )


def test_effective_cbd_is_the_declared_nodes_and_their_dependency_closure() -> None:
    ir = IRGraph(
        nodes=(
            _node("a"),
            _node("b", "a"),
            _node("c", "b", cbd=True),
            _node("d", "c"),  # a dependent of a CBD node is not affected
            _node("o"),
            _node("p", after=("o",), cbd=True),  # ordering edges propagate too
            _node("q", "a", cbd=True),
        )
    )
    assert effective_cbd(ir) == {"a", "b", "c", "o", "p", "q"}
    forcers = cbd_forcers(ir)
    assert forcers["a"] == {"c", "q"}
    assert forcers["c"] == {"c"}
    assert "d" not in forcers


def test_the_upstream_of_a_cbd_node_is_replaced_create_first() -> None:
    h = _harness()
    h.apply(f"a = Box('a', size=1)\nLink('d', parent=a.out, {CBD})\n")

    changes = _changes(h, f"a = Box('a', size=2)\nLink('d', parent=a.out, {CBD})\n")

    assert (changes[A].action, changes[A].create_before_destroy) == (Action.REPLACE, True)
    # Behind a create-first upstream the dependent may still collapse at apply.
    assert (changes[D].action, changes[D].conditional) == (Action.REPLACE, True)
    assert changes[D].create_before_destroy is True


def test_a_forced_replace_of_the_upstream_is_create_first_too() -> None:
    h = _harness()
    source = f"a = Box('a', size=1)\nLink('d', parent=a.out, {CBD})\n"
    h.apply(source)
    _, ir, _, hashes = h._compile(source, h._providers())

    changes = diff_module.diff(
        ir, hashes, h.backend.load(), h._mutability(), replace=frozenset({A})
    )

    [a] = [c for c in changes if c.node_id == A]
    assert (a.action, a.create_before_destroy) == (Action.REPLACE, True)


# -- conditional dependents of a destroy-first upstream -----------------------------


def test_a_dependent_of_a_destroy_first_upstream_is_replaced_first() -> None:
    h = _harness()
    h.apply("a = Box('a', size=1)\nd = Link('d', parent=a.out)\nLink('e', parent=d.out)\n")
    source = "a = Box('a', size=2)\nd = Link('d', parent=a.out)\nLink('e', parent=d.out)\n"

    changes = _changes(h, source)
    # Transitive: e is behind d, which is now itself replaced destroy-first.
    for node_id in (D, E):
        assert (changes[node_id].action, changes[node_id].conditional) == (Action.REPLACE, False)

    h.fake().reset()
    report = h.apply(source)

    assert h.fake().calls == [
        ("delete", "e"),
        ("delete", "d"),
        ("delete", "a"),
        ("create", "a"),
        ("create", "d"),
        ("create", "e"),
    ]
    assert report.downgraded == {}


def test_a_delete_half_that_ran_is_never_downgraded() -> None:
    """The recreated ``a`` keeps its value, which a confirmation would read as
    "nothing moved"; ``d`` was already destroyed in phase 0, so it is re-created."""
    h = _harness(FakeProvider(on_create=_same_out))
    h.apply("a = Box('a', size=1)\nLink('d', parent=a.out)\n")
    h.fake().reset()

    report = h.apply("a = Box('a', size=2)\nLink('d', parent=a.out)\n")

    assert ("create", "d") in h.fake().calls
    assert report.downgraded == {}
    assert sorted(report.replaced) == [A, D]


def test_the_executor_does_not_confirm_a_predeleted_conditional_replace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The invariant held by the executor itself, for a changeset the diff did not
    settle (the planner clears a colliding create-before-destroy after the diff)."""
    monkeypatch.setattr(diff_module, "behind_destroy_first", lambda changeset, _: changeset)
    original = deletes_module.Deletes.predeletes
    monkeypatch.setattr(
        deletes_module.Deletes,
        "predeletes",
        lambda self, changeset: original(self, changeset) | {D},
    )
    h = _harness(FakeProvider(on_create=_same_out))
    h.apply("a = Box('a', size=1)\nLink('d', parent=a.out)\n")
    source = "a = Box('a', size=2)\nLink('d', parent=a.out)\n"
    assert _changes(h, source)[D].conditional is True
    h.fake().reset()

    report = h.apply(source)

    assert h.fake().calls == [("delete", "d"), ("delete", "a"), ("create", "a"), ("create", "d")]
    assert report.downgraded == {}


def test_behind_a_create_first_upstream_the_dependent_still_collapses() -> None:
    """The trade-off's other side: only a create-first upstream lets an unchanged
    value keep its consumer."""
    h = _harness(FakeProvider(on_create=_same_out))
    h.apply(f"a = Box('a', size=1, {CBD})\nLink('d', parent=a.out)\n")
    h.fake().reset()

    report = h.apply(f"a = Box('a', size=2, {CBD})\nLink('d', parent=a.out)\n")

    assert report.downgraded == {D: "noop"}
    assert h.fake().calls == [("create", "a"), ("delete", "a")]
