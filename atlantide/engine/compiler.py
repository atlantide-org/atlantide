"""Compiled configs: from Atlas-lang source, and from a stored ``.atlas`` artifact.

Both routes end in :func:`assemble_compiled`. A source compile evaluates the
config; an artifact carries no source, so its resources are rehydrated from the
IR instead.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core import (
    AtlantideError,
    PolicyBinding,
    ProviderRegistry,
    Resource,
    ResourceRegistry,
    inline_stack_outputs,
)
from atlantide.core._tree import tree_map
from atlantide.core.errors import ArtifactError, RegistryError
from atlantide.core.lifecycle import Lifecycle
from atlantide.core.markers import is_ref_marker, ref_from_marker
from atlantide.core.node_id import local_name_of
from atlantide.engine.model import Compiled
from atlantide.engine.result import catching, forward_failure
from atlantide.graph import build_graph, topological_order
from atlantide.ir import Artifact, build_artifact, lower, merkle_hashes, verify_hash
from atlantide.ir.model import IRGraph
from atlantide.lang import LanguageSurface, evaluate_source
from atlantide.secrets import is_secret_ref_marker, secret_ref_from_marker


def compile_source(  # noqa: PLR0913 - keyword-only compile options, as Engine.compile takes them
    source: str,
    filename: str,
    *,
    providers: ProviderRegistry,
    surface: LanguageSurface,
    fuel: int,
    inputs: dict[str, Any] | None = None,
    envs: Sequence[str] | None = None,
    extra_globals: dict[str, Any] | None = None,
) -> Result[Compiled, AtlantideError]:
    """Evaluate Atlas-lang source into a :class:`Compiled` (IR, graph, hashes)."""
    evaluated = evaluate_source(
        source,
        filename,
        inputs=inputs,
        envs=envs,
        extra_globals=extra_globals,
        surface=surface,
        fuel=fuel,
    )
    return evaluated.bind(lambda registry: compile_registry(registry, providers))


def compile_registry(
    registry: ResourceRegistry, providers: ProviderRegistry
) -> Result[Compiled, AtlantideError]:
    """Lower an evaluated config's registry into a :class:`Compiled`."""
    # Inline in-config cross-stack refs as graph edges before lowering, so
    # `refs()` and the resources dict both see the substituted Refs.
    return catching(lambda: inline_stack_outputs(registry)).bind(
        lambda inlined: _lowered(inlined, providers)
    )


def _lowered(
    registry: ResourceRegistry, providers: ProviderRegistry
) -> Result[Compiled, AtlantideError]:
    ir = lower(registry, providers)
    return assemble_compiled(
        ir,
        resources={r.node_id: r for r in registry.all()},
        bindings=registry.policy_bindings,
        outputs=registry.outputs,
        inputs=registry.inputs,
        envs_declared=registry.envs_declared,
        envs_selected=registry.envs_selected,
    )


def artifact_of(compiled: Compiled, component_pins: dict[str, str] | None) -> Artifact:
    """The portable, content-hashed ``.atlas`` artifact for a compiled config."""
    return build_artifact(
        compiled.ir,
        compiled.policy_bindings,
        compiled.outputs,
        component_pins,
        envs=compiled.envs_selected,
        envs_declared=compiled.envs_declared,
    )


def verify_artifact(
    artifact: Artifact, providers: ProviderRegistry
) -> Result[None, AtlantideError]:
    """Check the artifact's IR hash and that every pinned provider is compatible."""
    hashed = verify_hash(artifact)
    if isinstance(hashed, Failure):
        return forward_failure(hashed)
    return _check_pins(artifact, providers)


def _check_pins(artifact: Artifact, providers: ProviderRegistry) -> Result[None, AtlantideError]:
    for name, version in sorted(artifact.provider_pins.items()):
        result = providers.check_compatible(name, version)
        if isinstance(result, Failure):
            return forward_failure(result)
    return Success(None)


def compiled_from_artifact(
    artifact: Artifact, types: dict[str, type[Resource]]
) -> Result[Compiled, AtlantideError]:
    """Rebuild a :class:`Compiled` from an artifact's IR alone.

    The build's environment selection is carried over, so a deploy of an
    artifact built with ``--env`` keeps the unselected environments' state out
    of its diff, as the source apply would.

    An artifact that records a selection but not the declared environments was
    built by an older atlantide. Which environments it left out is unknown, so
    their state would plan as deletes; it is refused instead.
    """
    if artifact.envs and not artifact.envs_declared:
        return Failure(
            ArtifactError(
                "artifact was built with --env by an older atlantide and doesn't record "
                "the declared environments; rebuild it with `atlantide build`"
            )
        )
    ir = artifact.ir
    return catching(lambda: rehydrate_resources(ir, types)).bind(
        lambda resources: assemble_compiled(
            ir,
            resources=resources,
            bindings=artifact.policies,
            outputs=artifact.outputs,
            envs_declared=artifact.envs_declared,
            envs_selected=artifact.envs,
        )
    )


def assemble_compiled(
    ir: IRGraph,
    *,
    resources: dict[str, Resource],
    bindings: tuple[PolicyBinding, ...],
    outputs: dict[str, Any],
    inputs: dict[str, Any] | None = None,
    envs_declared: Sequence[str] = (),
    envs_selected: Sequence[str] = (),
) -> Result[Compiled, AtlantideError]:
    """Build a :class:`Compiled` from an IR graph and its (source- or artifact-sourced) parts."""
    return build_graph(ir).map(
        lambda graph: Compiled(
            ir=ir,
            graph=graph,
            hashes=merkle_hashes(ir, topological_order(graph)),
            resources=resources,
            policy_bindings=bindings,
            outputs=outputs,
            inputs=dict(inputs or {}),
            envs_declared=tuple(envs_declared),
            envs_selected=tuple(envs_selected),
        )
    )


def rehydrate_resources(ir: IRGraph, types: dict[str, type[Resource]]) -> dict[str, Resource]:
    """Rebuild live ``Resource`` objects from IR, for a deploy that has no source.

    ``{"$ref": "id#attr"}`` markers become ``Ref`` objects so validation passes
    them through and the executor resolves them at apply time. Node ids key the
    dict, so a resource's own (default-stack) node id is irrelevant.
    """
    resources: dict[str, Resource] = {}
    for node in ir.nodes:
        cls = types.get(node.type)
        if cls is None:
            raise RegistryError(
                f"cannot deploy {node.id!r}: resource type {node.type!r} is not registered"
            )
        name = local_name_of(node.id)
        properties = {key: _markers_to_refs(value) for key, value in node.properties.items()}
        lifecycle = Lifecycle(
            prevent_destroy=node.prevent_destroy,
            create_before_destroy=node.create_before_destroy,
            ignore_changes=node.ignore_changes,
            # Without aliases, a deploy lowers a rename as a destroy plus a
            # create against the target state.
            aliases=node.aliases,
        )
        # Explicit edges are kept so a deploy honours the ordering the config declared.
        resources[node.id] = cls(
            name, lifecycle=lifecycle, depends_on=list(node.depends_on), **properties
        )
    return resources


def _markers_to_refs(value: Any) -> Any:
    def leaf(v: Any) -> Any:
        if is_ref_marker(v):
            return ref_from_marker(v)
        if is_secret_ref_marker(v):
            return secret_ref_from_marker(v)
        return v

    return tree_map(value, leaf)
