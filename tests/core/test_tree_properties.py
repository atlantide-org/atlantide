"""Laws the property-value walkers must satisfy for any tree.

``core/_tree`` had no test file. It is the lowest layer in the package and every
hash, every dependency edge and every canonical form is built on it, but it was
only ever exercised through whatever the suites above happened to walk — so its
guarantees were incidental rather than stated.

The walkers were refactored recently: ``tree_any`` and ``tree_collect`` used to
carry a five-branch container ladder each, and now share one ``_children``
helper. That refactor was verified by "the suite still passes", which is a much
weaker claim than the ones below, and it is exactly the kind of change these laws
exist to make safe.

Generated rather than enumerated because the interesting inputs are *shapes* —
a ``Ref`` three levels down inside a nested model inside a tuple inside a dict —
and nobody writes those by hand.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from atlantide.core._tree import order_key, tree_any, tree_collect, tree_map
from atlantide.core.errors import IRError
from atlantide.core.markers import canonicalize
from atlantide.core.types import Ref, Transform
from tests.support.resources import NestedValue
from tests.support.strategies import property_trees


def _is_ref(value: object) -> bool:
    return isinstance(value, Ref)


# -- the two inspection walkers agree -----------------------------------------


@given(property_trees())
def test_any_and_collect_answer_the_same_question(tree: object) -> None:
    """`tree_any` is "is there one?"; `tree_collect` is "which ones?".

    They share `_children`, so this is the law that pins them together: a
    container one walker descends into and the other does not shows up here and
    nowhere else. Before the refactor they had two copies of that logic and
    nothing checked the copies agreed.
    """
    assert tree_any(tree, _is_ref) == bool(tree_collect(tree, _is_ref))


@given(property_trees())
def test_collect_returns_only_matching_values(tree: object) -> None:
    assert all(_is_ref(found) for found in tree_collect(tree, _is_ref))


@given(property_trees())
def test_a_predicate_matching_everything_finds_the_root(tree: object) -> None:
    """A matching node is collected whole and not descended into, so a
    match-everything predicate yields exactly the root."""
    assert tree_collect(tree, lambda _: True) == [tree]


@given(property_trees())
def test_a_predicate_matching_nothing_finds_nothing(tree: object) -> None:
    assert tree_collect(tree, lambda _: False) == []
    assert tree_any(tree, lambda _: False) is False


# -- what counts as a child ---------------------------------------------------


def _refs_by_hand(value: object) -> list[Ref]:
    """Every `Ref` in a generated tree, found without using the code under test.

    An oracle, not a convenience. The obvious phrasing of the property below —
    "wrapping a tree in a dict finds the same refs" — compares the walker to
    *itself*, so a walker that stops at a nested model agrees with itself
    perfectly and the property passes while the bug is live. That is not
    hypothetical: it is what the first version of this test did, and a mutation
    that removed model descent sailed through it.

    Knows only the shapes `property_trees` builds.
    """
    if isinstance(value, Ref):
        return [value]
    if isinstance(value, Transform):
        return [ref for arg in value.args for ref in _refs_by_hand(arg)]
    if isinstance(value, NestedValue):
        return _refs_by_hand(value.label) + _refs_by_hand(value.value)
    if isinstance(value, dict):
        return [ref for item in value.values() for ref in _refs_by_hand(item)]
    if isinstance(value, (list, tuple, set, frozenset)):
        return [ref for item in value for ref in _refs_by_hand(item)]
    return []


@given(property_trees())
def test_every_ref_in_the_tree_is_found(tree: object) -> None:
    """The case that motivates the whole module.

    A `Ref` inside a `Transform`'s operands or inside a nested pydantic model —
    an `SgRule`, a `Route` — is what makes a resource depend on another. A walker
    that stopped at either boundary would drop the edge silently, and the graph
    would apply in the wrong order.
    """
    assert sorted(map(repr, tree_collect(tree, _is_ref))) == sorted(map(repr, _refs_by_hand(tree)))


@given(property_trees())
def test_wrapping_a_tree_does_not_change_what_is_in_it(tree: object) -> None:
    """Depth is not supposed to matter. Weaker than the law above on its own —
    it compares the walker to itself — but it covers the containers the oracle
    and the walker would have to be wrong about *together*."""
    assert tree_collect({"outer": [tree]}, _is_ref) == tree_collect(tree, _is_ref)


@given(st.frozensets(st.integers(), min_size=1, max_size=4))
def test_sets_are_walked_only_when_asked(members: frozenset[int]) -> None:
    """`include_sets=False` is for already-canonicalized trees, where a set has
    been lowered to a list already and re-descending would double-count."""
    assert tree_any(members, lambda v: v in members, include_sets=True) is True
    assert tree_any(members, lambda v: v in members, include_sets=False) is False


# -- tree_map -----------------------------------------------------------------


@given(property_trees())
def test_mapping_with_identity_is_idempotent(tree: object) -> None:
    """Whatever `tree_map` does to shape it must do once, not once per pass —
    otherwise a value's canonical form would depend on how many times it had been
    through, and the content hash with it."""
    once = tree_map(tree, lambda v: v)
    twice = tree_map(once, lambda v: v)

    assert once == twice


@given(property_trees())
def test_a_set_never_survives_mapping(tree: object) -> None:
    """Emitting a set is never correct: it is not JSON-serializable and its
    iteration order varies with `PYTHONHASHSEED`, which would make the content
    hash — the thing the whole product promises is stable — depend on the
    interpreter's startup randomness.
    """
    assert not _contains_set(tree_map(tree, lambda v: v))


def _contains_set(value: object) -> bool:
    if isinstance(value, (set, frozenset)):
        return True
    if isinstance(value, dict):
        return any(_contains_set(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_set(item) for item in value)
    return False


@given(st.frozensets(st.integers() | st.text(max_size=4), min_size=2, max_size=6))
def test_a_mapped_set_comes_back_ordered(members: frozenset[object]) -> None:
    """Lowering a set is only half the job; the resulting list has to be *sorted*.

    Set iteration order varies with `PYTHONHASHSEED`, so an unsorted lowering
    produces a different list — and therefore a different content hash — from one
    interpreter to the next, for a config nobody edited.

    Asserted on a set passed in directly rather than found inside a generated
    tree: once mapped, a list that came from a set is indistinguishable from one
    that was always a list, so the ordering can only be checked where the input
    is known to have been a set. The first version of this test compared
    `tree_map(t) == tree_map(t)`, which is a tautology for a pure function and
    let a mutation that removed the `sorted()` pass untouched.
    """
    keys = [order_key(item) for item in tree_map(members, lambda v: v)]

    assert keys == sorted(keys)


@given(property_trees())
def test_canonicalizing_leaves_no_live_handle_anywhere(tree: object) -> None:
    """The product-level guarantee the walkers exist to provide.

    A `Ref` reaches its marker form two different ways — by descent for one
    inside a container, and via `Transform.canonical()` for one inside a
    transform's arguments, since a `Transform` is itself a handle and is replaced
    whole rather than walked into. Both paths have to end with no live object
    left, or a `Ref` gets serialized into the IR as whatever pydantic makes of it
    instead of as `{"$ref": ...}`.

    Asserted against `canonicalize` rather than `tree_map` for that reason: with
    an arbitrary leaf function the two are legitimately asymmetric, and pinning
    that asymmetry would pin an implementation detail rather than a promise.
    """
    canonical = canonicalize(tree)

    assert not tree_collect(canonical, _is_ref, include_sets=False)
    assert not tree_collect(canonical, lambda v: isinstance(v, Transform), include_sets=False)


# -- key handling -------------------------------------------------------------


@given(st.dictionaries(st.integers(min_value=0, max_value=50), st.integers(), max_size=5))
def test_stringifying_keys_preserves_every_entry(mapping: dict[int, int]) -> None:
    """`str` is not injective, and a dict comprehension keeps the last writer —
    which would drop a property from the IR, the hash, and the provider call."""
    mapped = tree_map(mapping, lambda v: v, stringify_keys=True)

    assert len(mapped) == len(mapping)
    assert set(mapped) == {str(key) for key in mapping}


def test_a_key_collision_is_refused_rather_than_silently_dropped() -> None:
    """`1` and `"1"` both encode as `"1"`. Keeping one is data loss the run would
    never mention again."""
    with pytest.raises(IRError, match="must be distinct"):
        tree_map({1: "a", "1": "b"}, lambda v: v, stringify_keys=True)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
