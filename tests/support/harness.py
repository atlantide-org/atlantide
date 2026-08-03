"""Harness: one evaluate -> lower -> diff -> plan -> apply/refresh pipeline for tests.

Wraps the product call graph so a suite constructs a Harness over its types and a
provider, then drives diff/plan/apply/refresh without re-authoring the wiring.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from typing import Any

from returns.result import Result

from atlantide.core import (
    Provider,
    ProviderRegistry,
    Resource,
    field_mutability,
)
from atlantide.core.errors import AtlantideError, PreventDestroyError
from atlantide.graph import build_graph, topological_order
from atlantide.graph.select import match_targets
from atlantide.ir import lower, merkle_hashes
from atlantide.lang import evaluate_source
from atlantide.reconcile import (
    ApplyEnv,
    ApplyReport,
    ChangeSet,
    Desired,
    DriftReport,
    ImportOutcome,
    ImportRequest,
    adopt,
    apply,
    diff,
    plan,
    refresh,
)
from atlantide.reconcile.context import DEFAULT_NODE_TIMEOUT, ProgressCallback
from atlantide.secrets import SecretsRegistry
from atlantide.state import MemoryStateBackend
from atlantide.state.backend import LeaseGuard, StateBackend
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
    #: The ``--env`` selection the config is evaluated under. ``None`` (the
    #: default) means every environment a ``Config`` declares, as with the flag.
    envs: tuple[str, ...] | None = None
    parallelism: int | None = None
    #: The write guard the executor consults. Exposed so a test can put it in the
    #: state a failed lease renewal would leave it in.
    lease: LeaseGuard = field(default_factory=LeaseGuard)
    node_timeout: float = DEFAULT_NODE_TIMEOUT

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
            lease=self.lease,
            node_timeout=self.node_timeout,
            **extra,
        )

    def _compile(
        self, source: str, providers: ProviderRegistry
    ) -> tuple[Any, Any, Any, dict[str, str]]:
        registry = evaluate_source(source, extra_globals=self.globals, envs=self.envs).unwrap()
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

    def apply(
        self,
        source: str,
        on_failure: str = "halt",
        on_progress: ProgressCallback | None = None,
    ) -> ApplyReport:
        return asyncio.run(self.apply_async(source, on_failure, on_progress))

    async def apply_async(
        self,
        source: str,
        on_failure: str = "halt",
        on_progress: ProgressCallback | None = None,
    ) -> ApplyReport:
        """The same run, awaitable — so a test can cancel it from outside.

        An interrupt cancels the task *awaiting* the apply; it does not raise
        inside one node's provider call. The difference matters: ``TaskGroup``
        treats a ``CancelledError`` raised by a child as that child being
        cancelled rather than as a failure, so simulating an interrupt from
        inside a provider does not exercise the path a real Ctrl-C takes.
        """
        providers = self._providers()
        registry, ir, graph, hashes = self._compile(source, providers)
        prior = self.backend.load()
        changeset = plan(diff(ir, hashes, prior, self._mutability()), self._protected()).unwrap()
        desired = Desired(
            ir=ir,
            graph=graph,
            hashes=hashes,
            resources={r.node_id: r for r in registry.all()},
            output_decls=registry.outputs,
        )
        return await apply(
            changeset=changeset,
            desired=desired,
            prior=prior,
            env=self._env(providers),
            on_failure=on_failure,  # type: ignore[arg-type]
            progress=on_progress,
        )

    def adopt(
        self,
        source: str,
        *requests: ImportRequest | str,
        write: bool = True,
        allow_drift: bool = False,
        force: bool = False,
    ) -> list[ImportOutcome]:
        """Compile ``source`` and adopt the named nodes into state.

        A bare string is shorthand for an :class:`ImportRequest` with no external
        id. Node ids are resolved through ``match_targets``, the same matcher
        ``--target`` uses, so a test names ``"b"`` rather than
        ``"default:test.Box:b"`` — and a typo raises instead of silently adopting
        nothing.
        """
        providers = self._providers()
        _, ir, _graph, hashes = self._compile(source, providers)
        known = {node.id for node in ir.nodes}
        ordered = [_resolve(request, known) for request in requests]
        return asyncio.run(
            adopt(
                requests=ordered,
                ir=ir,
                hashes=hashes,
                prior=self.backend.load(),
                env=self._env(providers),
                write=write,
                allow_drift=allow_drift,
                force=force,
            )
        )

    def refresh(self, *, write: bool = False, prune: bool = False) -> DriftReport:
        return asyncio.run(
            refresh(
                prior=self.backend.load(),
                env=self._env(self._providers()),
                write=write,
                prune=prune,
            )
        )


def box_harness(backend: StateBackend, provider: Provider | None = None) -> Harness:
    """A :class:`Harness` over the canonical ``Box`` resource.

    Most reconcile-level behaviour — diff, replace, rollback, convergence — is
    about the engine rather than about any particular resource, so those suites
    all want the same one-resource setup. It lived in ``tests/reconcile/conftest``
    under the name ``Harness``, which shadowed the class it returns and left
    suites elsewhere importing another package's conftest to reach it.
    """
    from atlantide.core import Lifecycle
    from tests.support.resources import Box

    return Harness.of(Box, provider=provider, backend=backend, globals={"Lifecycle": Lifecycle})


def _resolve(request: ImportRequest | str, known: set[str]) -> ImportRequest:
    """Expand a short node id to the full one, leaving an unmatchable id alone.

    An id that matches nothing is passed through unchanged so ``adopt`` can report
    it as blocked — which is the behaviour the "not in this config" case tests.
    ``match_targets`` signals "no match" by raising, so the exception *is* the
    answer rather than something to probe for first.
    """
    resolved = ImportRequest(request) if isinstance(request, str) else request
    try:
        matched = sorted(match_targets([resolved.node_id], known))
    except AtlantideError:
        return resolved
    return replace(resolved, node_id=matched[0]) if len(matched) == 1 else resolved
