"""Deployable ``.atlas`` artifacts: IR, provider pins and policy set, content-hashed.

``atlantide build`` bundles the canonical IR, the provider version each node was
compiled against, the policy set (names, levels and type filters, not code), and
declared outputs into a portable JSON artifact carrying ``hash(IR)``.
``atlantide deploy`` verifies the hash and the pins, then plans and applies from
the IR without user source or re-executing the config.

An artifact is built once and deployed unchanged across environments. The stored
``ir_hash`` is the integrity anchor: a tampered or corrupted IR does not hash to
it. Pins and policies are outside that hash.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core.errors import ArtifactError
from atlantide.core.markers import refs_to_markers
from atlantide.core.policy import PolicyBinding, PolicyLevel
from atlantide.ir.hash import hash_ir
from atlantide.ir.model import IRGraph

ARTIFACT_FORMAT = 1


@dataclass(frozen=True, slots=True)
class Artifact:
    """A self-contained deployment unit built from one config."""

    ir: IRGraph
    ir_hash: str
    provider_pins: dict[str, str]
    policies: tuple[PolicyBinding, ...] = ()
    outputs: dict[str, Any] = field(default_factory=dict)
    #: alias -> resolved git commit of each published component the config used.
    #: ``atlantide component verify`` checks the vendored code itself.
    component_pins: dict[str, str] = field(default_factory=dict)
    #: Environments ``--env`` selected at build time; empty when the config
    #: declares none. Build provenance, outside the hash like the pins.
    envs: tuple[str, ...] = ()
    #: Every environment the config declared. With ``envs`` it tells a deploy
    #: which environments the build left out, whose state is then kept out of
    #: its diff. Empty in artifacts written before it was recorded, which then
    #: narrow nothing.
    envs_declared: tuple[str, ...] = ()
    format_version: int = ARTIFACT_FORMAT

    def dumps(self) -> str:
        """Serialize to pretty, stable JSON (the ``.atlas`` file body)."""
        return json.dumps(_to_json(self), indent=2, sort_keys=True)


def build_artifact(
    ir: IRGraph,
    policies: tuple[PolicyBinding, ...],
    outputs: dict[str, Any],
    component_pins: dict[str, str] | None = None,
    envs: Sequence[str] = (),
    envs_declared: Sequence[str] = (),
) -> Artifact:
    """Bundle a compiled IR into an :class:`Artifact`.

    Provider pins are derived from the IR; ``component_pins`` come from the
    project's lock; ``envs`` records which environments the build selected, out
    of the ``envs_declared`` the config declared.
    """
    return Artifact(
        ir=ir,
        ir_hash=hash_ir(ir),
        provider_pins=_provider_pins(ir),
        policies=policies,
        outputs={key: refs_to_markers(value) for key, value in outputs.items()},
        component_pins=dict(component_pins) if component_pins else {},
        envs=tuple(envs),
        envs_declared=tuple(envs_declared),
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
            ir=IRGraph.from_stored(data["ir"]),
            ir_hash=data["ir_hash"],
            provider_pins=dict(data["provider_pins"]),
            policies=tuple(_binding_from_json(p) for p in data["policies"]),
            outputs=dict(data["outputs"]),
            component_pins=dict(data["component_pins"]),
            envs=tuple(data["envs"]),
            envs_declared=tuple(data.get("envs_declared", ())),
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
        "ir": artifact.ir.to_stored(),  # to_canonical() omits aliases and depends_on
        "provider_pins": artifact.provider_pins,
        "component_pins": artifact.component_pins,
        "policies": [_binding_json(b) for b in artifact.policies],
        "outputs": artifact.outputs,
        "envs": list(artifact.envs),
        "envs_declared": list(artifact.envs_declared),
    }


def _binding_json(binding: PolicyBinding) -> dict[str, Any]:
    return {
        "name": binding.name,
        "level": binding.level.value,
        "types": sorted(binding.types) if binding.types is not None else None,
        "params": dict(binding.params),
    }


def _binding_from_json(data: dict[str, Any]) -> PolicyBinding:
    types = data["types"]
    return PolicyBinding(
        name=data["name"],
        level=PolicyLevel(data["level"]),
        types=frozenset(types) if types is not None else None,
        params=dict(data["params"]),
    )
