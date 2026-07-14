"""Repo-wide test helpers."""

from __future__ import annotations

from atlantide.core import Provider, ProviderRegistry, Resource
from atlantide.engine import Engine
from atlantide.policy import PolicyRegistry
from atlantide.secrets import SecretsRegistry
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import StateBackend


def make_engine(
    types: dict[str, type[Resource]],
    *providers: Provider,
    backend: StateBackend | None = None,
    policies: PolicyRegistry | None = None,
    secrets: SecretsRegistry | None = None,
    parallelism: int | None = None,
) -> Engine:
    """An Engine over the given providers/types with in-memory-state defaults."""
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return Engine(
        registry,
        backend if backend is not None else MemoryStateBackend(),
        types,
        policies=policies,
        secrets=secrets,
        parallelism=parallelism,
    )
