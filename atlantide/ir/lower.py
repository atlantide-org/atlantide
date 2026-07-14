"""Lower an evaluated :class:`ResourceRegistry` to Atlas IR.

This is the single place ``Ref``s become dependency edges: each resource's
canonical inputs already carry ``{"$ref": "node#attr"}`` markers, and its
``refs()`` give the upstream node ids the diff/scheduler need. Secret fields
already carry a ``{"$secret_ref": ...}`` handle (a name, never a value), so no
special handling is needed here — the plaintext is resolved only at apply.
"""

from __future__ import annotations

from returns.pipeline import is_successful

from atlantide.core.registry import ProviderRegistry
from atlantide.core.resource import ResourceRegistry
from atlantide.ir.model import IRGraph, IRNode


def lower(registry: ResourceRegistry, providers: ProviderRegistry | None = None) -> IRGraph:
    """Build the Atlas IR for a set of declared resources.

    ``providers`` (when given) stamps each node with its provider's semver;
    absent or unregistered providers lower to an empty version string.
    """
    nodes: list[IRNode] = []
    for resource in registry.all():  # already sorted by node_id
        provider_name = resource.provider_name()
        version = _resolve_version(provider_name, providers)
        dependencies = tuple(sorted({ref.node_id for ref in resource.refs()}))
        lifecycle = resource.lifecycle
        nodes.append(
            IRNode(
                id=resource.node_id,
                type=resource.type_name(),
                provider=provider_name,
                provider_version=version,
                properties=resource.canonical_inputs(),
                dependencies=dependencies,
                prevent_destroy=lifecycle.prevent_destroy,
                create_before_destroy=lifecycle.create_before_destroy,
                ignore_changes=lifecycle.ignore_changes,
                aliases=lifecycle.aliases,
            )
        )
    return IRGraph(nodes=tuple(nodes))


def _resolve_version(provider_name: str, providers: ProviderRegistry | None) -> str:
    if not provider_name or providers is None:
        return ""
    resolved = providers.get(provider_name)
    return resolved.unwrap().version if is_successful(resolved) else ""
