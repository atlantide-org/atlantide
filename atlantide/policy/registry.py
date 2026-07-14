"""PolicyRegistry: resolve a policy name to the provider that defines it."""

from __future__ import annotations

from atlantide.core.errors import RegistryError
from atlantide.policy.base import PolicyContext, PolicyProvider, PolicyResult


class PolicyRegistry:
    """Holds one or more `PolicyProvider`s; evaluates by finding the owner of a name."""

    def __init__(self) -> None:
        self._providers: list[PolicyProvider] = []

    def register(self, provider: PolicyProvider) -> None:
        self._providers.append(provider)

    def has(self, name: str) -> bool:
        return any(p.has(name) for p in self._providers)

    def evaluate(self, name: str, ctx: PolicyContext) -> PolicyResult:
        for provider in self._providers:
            if provider.has(name):
                return provider.evaluate(name, ctx)
        raise RegistryError(f"unknown policy {name!r}")
