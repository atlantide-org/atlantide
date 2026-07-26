"""Round-tripping the ``.atlas`` artifact, for any artifact.

An artifact is the promotion unit: build once in CI, deploy the same bytes to
staging and then to production. Everything that makes that safe is a property of
this one serialize/parse pair, and each one fails in a way examples are bad at
catching.

* **The round trip.** ``deploy`` plans from a parsed artifact, never from source.
  A field that survives ``dumps`` but not ``loads`` does not raise anything — it
  reappears as a default, and the plan built from it is quietly a plan for a
  different config. The dangerous instance is ``aliases``: lose it and a rename
  lowers to a destroy plus a create, against live infrastructure.
* **The hash anchor.** ``ir_hash`` is what makes a corrupted or edited artifact
  detectable at all. It has to survive the round trip *and* it has to actually
  move when the IR does — a hash that verifies whatever you hand it is worse than
  no hash, because the deploy path trusts it.
* **Failing closed on garbage.** ``loads`` takes a file off disk or out of an
  artifact store. Returning a *wrong* artifact from a damaged one would plan
  against a config nobody wrote. Refusing is the only acceptable answer, and it
  has to be the answer for arbitrary input rather than for the malformed strings
  someone thought of.

``loads`` returns a ``Result`` rather than raising, so failure here is a
``Failure`` value — not something ``pytest.raises`` would see.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from hypothesis import given
from hypothesis import strategies as st
from returns.result import Failure, Success

from atlantide.core.policy import PolicyBinding, PolicyLevel
from atlantide.ir.artifact import Artifact, build_artifact, loads, verify_hash
from atlantide.ir.hash import hash_ir
from atlantide.ir.model import IRGraph
from tests.support.strategies import ir_graphs, json_values, names


def policy_bindings() -> st.SearchStrategy[PolicyBinding]:
    """A policy binding as an artifact stores it: names, levels and type filters.

    ``types=None`` (applies to everything) is drawn alongside a real filter
    because it is the one value that is not a ``frozenset`` and so the one the
    serializer has to special-case.
    """
    return st.builds(
        PolicyBinding,
        name=names,
        level=st.sampled_from(list(PolicyLevel)),
        types=st.none() | st.frozensets(names, max_size=3),
        params=st.dictionaries(names, json_values(max_leaves=3), max_size=3),
    )


def artifacts() -> st.SearchStrategy[Artifact]:
    """A built artifact. Constructed through ``build_artifact`` rather than
    assembled field by field, so ``ir_hash`` and ``provider_pins`` are whatever
    the real builder derives — an artifact with a hand-set hash would make
    ``verify_hash`` vacuous."""
    return st.builds(
        build_artifact,
        ir=ir_graphs(),
        policies=st.lists(policy_bindings(), max_size=3).map(tuple),
        outputs=st.dictionaries(names, json_values(max_leaves=4), max_size=3),
        component_pins=st.dictionaries(names, names, max_size=2),
    )


@given(artifacts())
def test_an_artifact_survives_a_round_trip(artifact: Artifact) -> None:
    """The whole promise: the bytes written by ``build`` parse back to the
    artifact ``deploy`` acts on."""
    assert loads(artifact.dumps()) == Success(artifact)


@given(artifacts())
def test_a_built_artifacts_hash_verifies(artifact: Artifact) -> None:
    assert verify_hash(artifact) == Success(None)


@given(artifacts())
def test_the_hash_still_verifies_after_a_round_trip(artifact: Artifact) -> None:
    """Separate from the round trip itself: equality could hold while the IR was
    re-encoded into something that hashes differently, and this is the check
    ``deploy`` actually runs before it plans."""
    parsed = loads(artifact.dumps()).unwrap()

    assert verify_hash(parsed) == Success(None)


@given(artifacts(), st.data())
def test_a_tampered_ir_no_longer_verifies(artifact: Artifact, data: st.DataObject) -> None:
    """The anchor has to move when the IR does, or it is not an anchor.

    Perturbing one node's properties is the edit that matters — it is what an
    attacker changing an instance size or a bucket policy would do, and it leaves
    the artifact structurally valid.
    """
    index = data.draw(st.integers(min_value=0, max_value=len(artifact.ir.nodes) - 1))
    target = artifact.ir.nodes[index]
    tampered = replace(
        artifact,
        ir=IRGraph(
            nodes=tuple(
                replace(node, properties={**node.properties, "tampered": True})
                if node.id == target.id
                else node
                for node in artifact.ir.nodes
            )
        ),
    )

    assert isinstance(verify_hash(tampered), Failure)


@given(st.text(max_size=200))
def test_arbitrary_text_is_refused_rather_than_misread(text: str) -> None:
    """Never a wrong artifact. Either a real one or a `Failure`."""
    match loads(text):
        case Failure(_):
            return
        case Success(artifact):
            # Anything that did parse must re-serialize to itself — i.e. it really
            # was an artifact, not garbage that happened to survive the parse.
            assert loads(artifact.dumps()) == Success(artifact)


@given(artifacts(), st.data())
def test_a_truncated_artifact_is_refused(artifact: Artifact, data: st.DataObject) -> None:
    """What a killed writer or a half-finished download leaves behind, and the
    corruption most likely to still look like JSON."""
    whole = artifact.dumps()
    cut = data.draw(st.integers(min_value=0, max_value=max(0, len(whole) - 1)))

    assert isinstance(loads(whole[:cut]), Failure)


@given(artifacts())
def test_the_serialized_form_is_stable(artifact: Artifact) -> None:
    """Two builds of the same config produce the same file. Without this, a
    promotion pipeline that compares artifacts sees every rebuild as a change."""
    assert artifact.dumps() == artifact.dumps()


@given(ir_graphs(), st.lists(names, min_size=1, max_size=3), st.lists(names, max_size=3))
def test_a_rename_directive_survives_the_round_trip_without_moving_the_hash(
    ir: IRGraph, aliases: list[str], ordering: list[str]
) -> None:
    """`aliases` and `depends_on` are migration and ordering directives, not
    identity — the contract stated on `IRNode`.

    Both halves matter and they pull opposite ways. They must survive `dumps`
    (that is what `to_stored` is for; without them a deploy lowers a rename to a
    destroy plus a create) while staying out of the hash (adding an ordering hint
    would otherwise re-hash the node and everything below it, producing an UPDATE
    on resources whose configuration did not change).
    """
    directed = IRGraph(
        nodes=tuple(
            replace(node, aliases=tuple(aliases), depends_on=tuple(ordering)) for node in ir.nodes
        )
    )

    parsed = loads(build_artifact(directed, (), {}).dumps()).unwrap()

    assert parsed.ir == directed  # the directives came back
    assert hash_ir(directed) == hash_ir(ir)  # and never entered the hash


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
