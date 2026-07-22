"""Deployable ``.atlas`` artifacts: IR + provider pins + policy set, content-hashed.

``atlantide build`` bundles the canonical IR, the provider version each node was
compiled against, the policy set (names + levels + type filters — **not** code),
and declared outputs into a portable JSON artifact carrying ``hash(IR)``.
``atlantide deploy`` verifies the hash and the pins, then plans/applies straight
from the IR — no user source, no re-execution of config.

The artifact is a promotion unit: build once, deploy the same bytes across
environments. The stored ``ir_hash`` is the integrity anchor — a tampered or
corrupted IR no longer hashes to it. Pins/policies live outside that hash.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core.errors import ArtifactError
from atlantide.core.markers import refs_to_markers
from atlantide.core.policy import PolicyBinding, PolicyLevel
from atlantide.ir.hash import hash_ir
from atlantide.ir.model import IR_VERSION, IRGraph, IRNode

ARTIFACT_FORMAT = 1


@dataclass(frozen=True, slots=True)
class Artifact:
    """A self-contained deployment unit built from one config."""

    ir: IRGraph
    ir_hash: str
    provider_pins: dict[str, str]
    policies: tuple[PolicyBinding, ...] = ()
    outputs: dict[str, Any] = field(default_factory=dict)
    #: alias -> resolved git commit of each published component the config used,
    #: recording which component code produced this IR. Integrity of the vendored
    #: code itself is checked by ``atlantide component verify``.
    component_pins: dict[str, str] = field(default_factory=dict)
    format_version: int = ARTIFACT_FORMAT

    def dumps(self) -> str:
        """Serialize to pretty, stable JSON (the ``.atlas`` file body)."""
        return json.dumps(_to_json(self), indent=2, sort_keys=True)


def build_artifact(
    ir: IRGraph,
    policies: tuple[PolicyBinding, ...],
    outputs: dict[str, Any],
    component_pins: dict[str, str] | None = None,
) -> Artifact:
    """Bundle a compiled IR into an :class:`Artifact`.

    Provider pins are derived from the IR; ``component_pins`` come from the
    project's lock.
    """
    return Artifact(
        ir=ir,
        ir_hash=hash_ir(ir),
        provider_pins=_provider_pins(ir),
        policies=policies,
        outputs={key: refs_to_markers(value) for key, value in outputs.items()},
        component_pins=dict(component_pins) if component_pins else {},
    )


def verify_hash(artifact: Artifact) -> Result[None, ArtifactError]:
    """Recompute ``hash(IR)`` and check it matches the stored anchor."""
    actual = hash_ir(artifact.ir)
    if actual != artifact.ir_hash:
        return Failure(
            ArtifactError(
                f"artifact hash mismatch: stored {artifact.ir_hash}, "
                f"computed {actual} — corrupted or altered IR"
            )
        )
    return Success(None)


def loads(text: str) -> Result[Artifact, ArtifactError]:
    """Parse a ``.atlas`` file body back into an :class:`Artifact`."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return Failure(ArtifactError(f"invalid artifact JSON: {exc}"))
    if not isinstance(data, dict):
        return Failure(ArtifactError("artifact must be a JSON object"))
    if data.get("format_version") != ARTIFACT_FORMAT:
        return Failure(
            ArtifactError(
                f"unsupported artifact format {data.get('format_version')!r} "
                f"(this build reads {ARTIFACT_FORMAT})"
            )
        )
    try:
        artifact = Artifact(
            ir=_ir_from_json(data["ir"]),
            ir_hash=data["ir_hash"],
            provider_pins=dict(data["provider_pins"]),
            policies=tuple(_binding_from_json(p) for p in data.get("policies", [])),
            outputs=dict(data.get("outputs", {})),
            component_pins=dict(data.get("component_pins", {})),
        )
    except (KeyError, TypeError, ValueError) as exc:
        return Failure(ArtifactError(f"malformed artifact: {exc}"))
    return Success(artifact)


# -- serialization helpers ---------------------------------------------------


def _provider_pins(ir: IRGraph) -> dict[str, str]:
    pins: dict[str, str] = {}
    for node in ir.nodes:
        if not node.provider:
            continue
        existing = pins.get(node.provider)
        if existing is not None and existing != node.provider_version:
            raise ArtifactError(
                f"provider {node.provider!r} pinned at two versions "
                f"({existing} and {node.provider_version})"
            )
        pins[node.provider] = node.provider_version
    return pins


def _to_json(artifact: Artifact) -> dict[str, Any]:
    return {
        "format_version": artifact.format_version,
        "ir_hash": artifact.ir_hash,
        "ir": artifact.ir.to_canonical(),
        "provider_pins": artifact.provider_pins,
        "component_pins": artifact.component_pins,
        "policies": [_binding_json(b) for b in artifact.policies],
        "outputs": artifact.outputs,
    }


def _binding_json(binding: PolicyBinding) -> dict[str, Any]:
    return {
        "name": binding.name,
        "level": binding.level.value,
        "types": sorted(binding.types) if binding.types is not None else None,
    }


def _binding_from_json(data: dict[str, Any]) -> PolicyBinding:
    types = data.get("types")
    return PolicyBinding(
        name=data["name"],
        level=PolicyLevel(data["level"]),
        types=frozenset(types) if types is not None else None,
    )


def _ir_from_json(data: dict[str, Any]) -> IRGraph:
    nodes = tuple(
        IRNode(
            id=node["id"],
            type=node["type"],
            provider=node["provider"],
            provider_version=node["provider_version"],
            properties=node["properties"],
            dependencies=tuple(node["dependencies"]),
            prevent_destroy=node.get("prevent_destroy", False),
            create_before_destroy=node.get("create_before_destroy", False),
            ignore_changes=tuple(node.get("ignore_changes", ())),
        )
        for node in data["nodes"]
    )
    return IRGraph(nodes=nodes, version=data.get("version", IR_VERSION))
