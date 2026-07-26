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
    # Older artifacts (no component_pins key) still load.
    legacy = artifact.dumps().replace('"component_pins": {},\n  ', "")
    assert loads(legacy).unwrap().component_pins == {}


def test_plain_construction_defaults_pins() -> None:
    artifact = Artifact(ir=_ir(), ir_hash="h", provider_pins={})
    assert artifact.component_pins == {}


def _binding(**params: object) -> PolicyBinding:
    return PolicyBinding(
        name="deny-destroy-in-protected", level=PolicyLevel.MANDATORY, params=params
    )


def test_policy_params_survive_roundtrip() -> None:
    """A deploy runs from the artifact alone, so a binding's arguments have to
    travel with it — otherwise the guard silently protects nothing."""
    artifact = build_artifact(_ir(), (_binding(stacks=["prod", "staging"]),), {})
    reloaded = loads(artifact.dumps()).unwrap()

    assert reloaded.policies[0].params == {"stacks": ["prod", "staging"]}
    assert reloaded == artifact


def test_policy_params_default_empty() -> None:
    artifact = build_artifact(_ir(), (_binding(),), {})
    assert artifact.policies[0].params == {}
    # Artifacts written before bindings carried params still load.
    legacy = artifact.dumps().replace('"params": {},\n      ', "")
    assert loads(legacy).unwrap().policies[0].params == {}
