"""Wrong-shaped values that must be rejected at config time.

Each would otherwise produce a working plan doing the wrong thing: a property
missing from the IR, a dependency edge that never forms, a rename lowered to a
destroy plus a create.
"""

from __future__ import annotations

import pytest

from atlantide.core import Lifecycle, Stack
from atlantide.core.errors import IRError, RegistryError
from atlantide.core.markers import canonicalize, refs_to_markers
from atlantide.core.stack import current_stack, current_stack_region
from atlantide.core.types import Ref
from atlantide.ir import Artifact, loads
from atlantide.ir.model import IRGraph, IRNode
from tests.support import Tagged

# -- Lifecycle --------------------------------------------------------------


@pytest.mark.parametrize("field", ["ignore_changes", "aliases"])
def test_lifecycle_rejects_a_bare_string(field: str) -> None:
    """`tuple("old")` is `('o','l','d')`, indistinguishable downstream from three
    deliberate entries: the alias matches no state node and the rename lowers to a
    destroy plus a create."""
    with pytest.raises(RegistryError, match=field):
        Lifecycle(**{field: "old"})


@pytest.mark.parametrize("value", [["a", "b"], ("a", "b")])
def test_lifecycle_still_accepts_a_sequence(value: object) -> None:
    assert Lifecycle(aliases=value).aliases == ("a", "b")  # type: ignore[arg-type]


# -- stack tag merge --------------------------------------------------------


def test_non_dict_tags_under_a_stack_are_rejected() -> None:
    """The merge runs after `__init__`, so replacing a non-dict would discard the
    declared value before `input_values()` sees it, dropping the property from the
    IR and its edge from the graph."""
    with (
        Stack("prod", region="eu-north-1", tags={"env": "prod"}),
        pytest.raises(IRError, match="tags"),
    ):
        Tagged("a", size=1, tags=Ref("default:test.Tagged:other", "tags"))


def test_dict_tags_still_merge_with_the_stack() -> None:
    with Stack("prod", region="eu-north-1", tags={"env": "prod", "team": "core"}):
        resource = Tagged("a", size=1, tags={"team": "data"})
    assert resource.tags == {"env": "prod", "team": "data"}  # own wins


# -- Stack re-entry ---------------------------------------------------------


def test_reusing_one_stack_object_does_not_leak_context() -> None:
    """Tokens belong to a `with` entry, not to the object.

    Held on the instance, a re-entry overwrites the enclosing entry's tokens and
    the outer `__exit__` replays consumed ones, leaving the contextvars set for
    the rest of the process. Hoisting a Stack to a module constant reaches this.
    """
    shared = Stack("prod", region="eu-north-1")
    with shared:
        with shared:
            assert current_stack() == "prod"
        assert current_stack() == "prod"
    assert current_stack() == "default"
    assert current_stack_region() is None


def test_two_stacks_still_nest() -> None:
    with Stack("outer", region="eu-north-1"):
        with Stack("inner", region="us-east-1"):
            assert (current_stack(), current_stack_region()) == ("inner", "us-east-1")
        assert (current_stack(), current_stack_region()) == ("outer", "eu-north-1")


# -- canonicalization -------------------------------------------------------


@pytest.mark.parametrize(
    "value", [{1: "a", "1": "b"}, {True: "x", "True": "y"}], ids=["int_str", "bool_str"]
)
def test_colliding_property_keys_are_rejected(value: dict[object, object]) -> None:
    """`str` is not injective and a dict comprehension keeps the last writer,
    dropping a property from the IR, the hash, and the provider call."""
    with pytest.raises(IRError, match="encode as"):
        canonicalize(value)


def test_a_set_of_handles_canonicalizes() -> None:
    """Sorting runs after the leaf transform, so a set of handles is a list of
    dicts by then and plain `sorted` cannot compare them."""
    assert canonicalize(frozenset({Ref("n", "a"), Ref("m", "b")})) == [
        {"$ref": "m#b"},
        {"$ref": "n#a"},
    ]


def test_a_set_lowers_to_a_deterministic_order() -> None:
    """A set has no order, so emitting one in iteration order makes the IR hash
    depend on PYTHONHASHSEED."""
    assert refs_to_markers({"o": {"y", "x", "z"}}) == {"o": ["x", "y", "z"]}
    assert canonicalize({"o": {"y", "x", "z"}}) == {"o": ["x", "y", "z"]}


# -- artifact round-trip ----------------------------------------------------


def _node(**kw: object) -> IRNode:
    return IRNode(
        id="s:t:new",
        type="t",
        provider="p",
        provider_version="1.0.0",
        properties={},
        dependencies=(),
        **kw,  # type: ignore[arg-type]
    )


def test_artifact_round_trip_keeps_aliases() -> None:
    """`to_canonical` omits `aliases` by design, so reusing it as the file format
    drops the rename directive and a deploy destroys and recreates the resource."""
    artifact = Artifact(
        ir=IRGraph(nodes=(_node(aliases=("s:t:old",)),)),
        ir_hash="",
        provider_pins={"p": "1.0.0"},
    )
    restored = loads(artifact.dumps()).unwrap()
    assert restored.ir.nodes[0].aliases == ("s:t:old",)


def test_aliases_stay_out_of_the_hashed_form() -> None:
    with_alias = _node(aliases=("s:t:old",))
    assert with_alias.to_canonical() == _node().to_canonical()
    assert "aliases" in with_alias.to_stored()


def test_join_refuses_a_bare_string() -> None:
    """`join("-", "abc")` would silently tuple the string into characters."""
    import pytest

    from atlantide.core.errors import IRError
    from atlantide.core.types import join

    with pytest.raises(IRError, match="string"):
        join("-", "abc")


def test_interpolate_rejects_mixed_placeholder_numbering() -> None:
    """Mixed `{}`/`{0}` passes per-field checks but explodes in vformat at
    apply; it must fail the plan instead."""
    import pytest

    from atlantide.core.errors import LanguageError
    from atlantide.core.types import check_template

    with pytest.raises(LanguageError, match="numbering"):
        check_template("{0} and {}")
    check_template("{} and {}")  # single style: fine
    check_template("{0} and {1}")
