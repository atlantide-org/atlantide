"""Harness: one evaluate -> lower -> diff -> plan -> apply/refresh pipeline for tests.

Wraps the product call graph so a suite constructs a Harness over its types and a
provider, then drives diff/plan/apply/refresh without re-authoring the wiring.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from returns.result import Result

from atlantide.core import (
    Provider,
    ProviderRegistry,
    Resource,
    field_mutability,
)
from atlantide.core.errors import PreventDestroyError
from atlantide.graph import build_graph, topological_order
from atlantide.ir import lower, merkle_hashes
from atlantide.lang import evaluate_source
from atlantide.reconcile import (
    ApplyEnv,
    ApplyReport,
    ChangeSet,
    Desired,
    DriftReport,
    apply,
    diff,
    plan,
    refresh,
)
from atlantide.secrets import SecretsRegistry
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import StateBackend
from tests.support.factories import globals_of, types_of
from tests.support.providers import FakeProvider


@dataclass
class Harness:
    """Drives a full compile+reconcile cycle over ``types`` with one ``provider``."""

    types: dict[str, type[Resource]]
    provider: Provider = field(default_factory=FakeProvider)
    backend: StateBackend = field(default_factory=MemoryStateBackend)
    secrets: SecretsRegistry = field(default_factory=SecretsRegistry)
    globals: dict[str, Any] = field(default_factory=dict)
    parallelism: int | None = None

    @classmethod
    def of(
        cls,
        *resource_classes: type[Resource],
        provider: Provider | None = None,
        globals: dict[str, Any] | None = None,
        **kw: Any,
    ) -> Harness:
        """Build a Harness from resource classes, deriving ``types`` and base ``globals``."""
        return cls(
            types=types_of(*resource_classes),
            provider=provider if provider is not None else FakeProvider(),
            globals=globals_of(*resource_classes) | (globals or {}),
            **kw,
        )

    def fake(self) -> FakeProvider:
        """This harness's provider as a :class:`FakeProvider` (calls/failure knobs)."""
        assert isinstance(self.provider, FakeProvider), "provider is not a FakeProvider"
        return self.provider

    # -- wiring -----------------------------------------------------------

    def _providers(self) -> ProviderRegistry:
        registry = ProviderRegistry()
        registry.register(self.provider)
        return registry

    def _env(self, providers: ProviderRegistry) -> ApplyEnv:
        extra: dict[str, Any] = {"parallelism": self.parallelism} if self.parallelism else {}
        return ApplyEnv(
            types=self.types,
            providers=providers,
            backend=self.backend,
            secrets=self.secrets,
            **extra,
        )

    def _compile(
        self, source: str, providers: ProviderRegistry
    ) -> tuple[Any, Any, Any, dict[str, str]]:
        registry = evaluate_source(source, extra_globals=self.globals).unwrap()
        ir = lower(registry, providers)
        graph = build_graph(ir).unwrap()
        hashes = merkle_hashes(ir, topological_order(graph))
        return registry, ir, graph, hashes

    def _mutability(self) -> dict[str, dict[str, Any]]:
        return {name: field_mutability(cls) for name, cls in self.types.items()}

    def _protected(self) -> frozenset[str]:
        return frozenset(n.id for n in self.backend.load().nodes.values() if n.prevent_destroy)

    # -- stages -----------------------------------------------------------

    def diff_only(self, source: str) -> ChangeSet:
        _, ir, _, hashes = self._compile(source, self._providers())
        return diff(ir, hashes, self.backend.load(), self._mutability())

    def plan_only(self, source: str) -> Result[ChangeSet, PreventDestroyError]:
        return plan(self.diff_only(source), self._protected())

    def apply(self, source: str, on_failure: str = "halt") -> ApplyReport:
        providers = self._providers()
        registry, ir, graph, hashes = self._compile(source, providers)
        prior = self.backend.load()
        changeset = plan(diff(ir, hashes, prior, self._mutability()), self._protected()).unwrap()
        desired = Desired(
            ir=ir,
            graph=graph,
            hashes=hashes,
            resources={r.node_id: r for r in registry.all()},
        )
        return asyncio.run(
            apply(
                changeset=changeset,
                desired=desired,
                prior=prior,
                env=self._env(providers),
                on_failure=on_failure,  # type: ignore[arg-type]
            )
        )

    def refresh(self, *, write: bool = False) -> DriftReport:
        return asyncio.run(
            refresh(prior=self.backend.load(), env=self._env(self._providers()), write=write)
        )
