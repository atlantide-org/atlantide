"""Artifact serialization, including published-component provenance pins."""

from __future__ import annotations

from atlantide.core import PolicyBinding, PolicyLevel
from atlantide.ir import Artifact, build_artifact, loads
from atlantide.ir.model import IRGraph, IRNode


def _ir() -> IRGraph:
    return IRGraph(
        nodes=(
            IRNode(
                id="s:local.Null:a",
                type="local.Null",
                provider="local",
                provider_version="1.0.0",
                properties={},
                dependencies=(),
            ),
        )
    )


def test_build_artifact_records_component_pins() -> None:
    artifact = build_artifact(_ir(), (), {}, {"acme": "c" * 40})
    assert artifact.component_pins == {"acme": "c" * 40}


def test_component_pins_survive_roundtrip() -> None:
    artifact = build_artifact(_ir(), (), {}, {"acme": "c" * 40})
    reloaded = loads(artifact.dumps()).unwrap()
    assert reloaded.component_pins == {"acme": "c" * 40}
    assert reloaded == artifact


def test_component_pins_default_empty() -> None:
    artifact = build_artifact(_ir(), (), {})
    assert artifact.component_pins == {}
    # An artifact missing the key is malformed, not defaulted.
    stripped = artifact.dumps().replace('"component_pins": {},\n  ', "")
    assert "malformed artifact" in str(loads(stripped).failure())


def test_plain_construction_defaults_pins() -> None:
    artifact = Artifact(ir=_ir(), ir_hash="h", provider_pins={})
    assert artifact.component_pins == {}


def _binding(**params: object) -> PolicyBinding:
    return PolicyBinding(
        name="deny-destroy-in-protected", level=PolicyLevel.MANDATORY, params=params
    )


def test_policy_params_survive_roundtrip() -> None:
    """A deploy runs from the artifact alone, so a binding's arguments have to
    travel with it, or the guard protects nothing."""
    artifact = build_artifact(_ir(), (_binding(stacks=["prod", "staging"]),), {})
    reloaded = loads(artifact.dumps()).unwrap()

    assert reloaded.policies[0].params == {"stacks": ["prod", "staging"]}
    assert reloaded == artifact


def test_policy_params_default_empty() -> None:
    artifact = build_artifact(_ir(), (_binding(),), {})
    assert artifact.policies[0].params == {}
    # A binding without params is malformed, not defaulted.
    stripped = artifact.dumps().replace('"params": {},\n      ', "")
    assert "malformed artifact" in str(loads(stripped).failure())


def test_a_data_node_keeps_its_kind_through_a_roundtrip() -> None:
    node = IRNode(
        id="s:local.Null:d",
        type="local.Null",
        provider="local",
        provider_version="1.0.0",
        properties={},
        dependencies=(),
        kind="data",
    )
    artifact = build_artifact(IRGraph(nodes=(node,)), (), {})
    [reloaded] = loads(artifact.dumps()).unwrap().ir.nodes
    assert reloaded.is_data
    assert not _ir().nodes[0].is_data


def test_an_unknown_node_kind_is_a_malformed_artifact() -> None:
    """Only ``resource`` and ``data`` exist; anything else would be read as a
    managed resource and could be deleted."""
    text = (
        build_artifact(_ir(), (), {})
        .dumps()
        .replace('"id": "s:local.Null:a"', '"id": "s:local.Null:a", "kind": "table"')
    )
    assert '"kind": "table"' in text
    assert str(loads(text).failure()) == "malformed artifact: unknown node kind 'table'"
