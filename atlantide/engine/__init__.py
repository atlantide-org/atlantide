"""atlantide.engine: orchestrates compile -> plan -> apply/destroy.

Wires the pure stages (Atlas-lang -> IR -> graph -> Merkle -> diff) to the
effectful ones (executor, state backend), taking the whole-state lock around any
mutation. Plan shaping lives in :mod:`atlantide.engine.planner`, artifact
rehydration in :mod:`atlantide.engine.hydrate`, and locking in
:mod:`atlantide.engine.locking`.

Two-tier error model: the pure/planning stages surface failure as
``Result[..., AtlantideError]`` and compose via ``.bind``/``.map``; the async
execution path raises, and those exceptions are collected into an
``ExceptionGroup`` at the boundary. Do not convert one to the other.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core import (
    AtlantideError,
    PolicyViolationError,
    ProviderRegistry,
    Resource,
    ResourceRegistry,
    field_mutability,
    inline_stack_outputs,
)
from atlantide.engine.hydrate import assemble_compiled, rehydrate_resources
from atlantide.engine.locking import lock_scope, with_lock
from atlantide.engine.model import Compiled, Plan
from atlantide.engine.planner import Planner, protected_ids
from atlantide.graph import build_graph
from atlantide.ir import Artifact, build_artifact, lower, verify_hash
from atlantide.ir.model import IRGraph
from atlantide.lang import evaluate_source
from atlantide.policy import PolicyRegistry, default_policy_registry
from atlantide.reconcile import (
    ApplyEnv,
    ApplyReport,
    ChangeSet,
    Desired,
    DriftReport,
    OnFailure,
    ProgressCallback,
    RefreshProgress,
    alias_remap,
    diff,
    persist_migration,
    plan,
    refresh,
    resolve_aliases,
)
from atlantide.reconcile import apply as _run_changeset
from atlantide.secrets import SecretsRegistry
from atlantide.state import StateGraph
from atlantide.state.backend import StateBackend

__all__ = ["Compiled", "Engine", "Plan"]


class Engine:
    def __init__(
        self,
        providers: ProviderRegistry,
        backend: StateBackend,
        types: dict[str, type[Resource]],
        *,
        parallelism: int | None = None,
        policies: PolicyRegistry | None = None,
        secrets: SecretsRegistry | None = None,
    ) -> None:
        self.providers = providers
        self.backend = backend
        self.types = types
        self.parallelism = parallelism
        self.policies = policies if policies is not None else default_policy_registry()
        # An empty registry suffices until a config declares a secret; sealing a
        # concrete sensitive value then requires a configured provider.
        self.secrets = secrets if secrets is not None else SecretsRegistry()
        self.mutability = {name: field_mutability(cls) for name, cls in types.items()}
        self._planner = Planner(
            mutability=self.mutability,
            types=self.types,
            secrets=self.secrets,
            policies=self.policies,
        )

    def __enter__(self) -> Engine:
        return self

    def __exit__(self, *exc: object) -> None:
        """Release the state backend (e.g. close the SQLite connection)."""
        self.backend.close()

    # -- pure stages ------------------------------------------------------

    def compile(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        extra_globals: dict[str, Any] | None = None,
    ) -> Result[Compiled, AtlantideError]:
        """Evaluate Atlas-lang source into a :class:`Compiled` (IR, graph, hashes)."""
        evaluated = evaluate_source(source, filename, inputs=inputs, extra_globals=extra_globals)
        return evaluated.bind(self._compile_registry)

    def _compile_registry(self, registry: ResourceRegistry) -> Result[Compiled, AtlantideError]:
        try:
            # Fold in-config cross-stack refs into real graph edges before lowering,
            # so `refs()` and the resources dict below both see the substituted Refs.
            registry = inline_stack_outputs(registry)
        except AtlantideError as exc:
            return Failure(exc)
        ir = lower(registry, self.providers)
        return assemble_compiled(
            ir,
            resources={r.node_id: r for r in registry.all()},
            bindings=registry.policy_bindings,
            outputs=registry.outputs,
        )

    def plan(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        extra_globals: dict[str, Any] | None = None,
    ) -> Result[Plan, AtlantideError]:
        """Compile and diff against current state; the Plan carries any violations."""
        compiled = self.compile(source, filename, inputs=inputs, extra_globals=extra_globals)
        return compiled.bind(self._plan_from_compiled)

    def _plan_from_compiled(self, built: Compiled) -> Result[Plan, AtlantideError]:
        # Map renamed resources (Lifecycle.aliases) onto their existing state
        # nodes before diffing, so a rename is a NOOP rather than destroy+create.
        migrated, _ = resolve_aliases(self.backend.load(), built.ir)
        return self._planner.build(built, migrated, self._stack_outputs())

    def _stack_outputs(self) -> dict[str, Any]:
        """Committed cross-stack outputs, with any sealed sensitive value unsealed."""
        return {k: self.secrets.unseal(v) for k, v in self.backend.outputs().items()}

    # -- effectful stages -------------------------------------------------

    async def apply(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        extra_globals: dict[str, Any] | None = None,
        on_failure: OnFailure = "rollback",
        progress: ProgressCallback | None = None,
    ) -> Result[ApplyReport, AtlantideError]:
        """Compile, plan, and execute the changeset under the state lock."""
        compiled = self.compile(source, filename, inputs=inputs, extra_globals=extra_globals)
        if isinstance(compiled, Failure):
            return _forward_failure(compiled)
        return await self._apply_compiled(compiled.unwrap(), on_failure, progress)

    # -- build / deploy (portable artifacts) ------------------------------

    def build(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        extra_globals: dict[str, Any] | None = None,
        component_pins: dict[str, str] | None = None,
    ) -> Result[Artifact, AtlantideError]:
        """Compile a config into a portable, content-hashed ``.atlas`` artifact.

        ``component_pins`` (alias -> resolved commit, from the project's lock) is
        recorded in the artifact as provenance for any published components used.
        """
        compiled = self.compile(source, filename, inputs=inputs, extra_globals=extra_globals)
        return compiled.map(
            lambda c: build_artifact(c.ir, c.policy_bindings, c.outputs, component_pins)
        )

    def verify_artifact(self, artifact: Artifact) -> Result[None, AtlantideError]:
        """Check the artifact's IR hash and that every pinned provider is compatible."""
        hashed = verify_hash(artifact)
        if isinstance(hashed, Failure):
            return _forward_failure(hashed)
        return self._check_pins(artifact)

    async def deploy(
        self,
        artifact: Artifact,
        *,
        on_failure: OnFailure = "rollback",
        progress: ProgressCallback | None = None,
    ) -> Result[ApplyReport, AtlantideError]:
        """Apply an artifact directly from its IR — no source, no re-execution.

        Secrets are references, not values, so a source-less deploy resolves each
        handle from the *target* environment's secrets backend at apply time.
        """
        verified = self.verify_artifact(artifact)
        if isinstance(verified, Failure):
            return _forward_failure(verified)
        built = self._compiled_from_artifact(artifact)
        if isinstance(built, Failure):
            return _forward_failure(built)
        return await self._apply_compiled(built.unwrap(), on_failure, progress)

    def _check_pins(self, artifact: Artifact) -> Result[None, AtlantideError]:
        for name, version in sorted(artifact.provider_pins.items()):
            result = self.providers.check_compatible(name, version)
            if isinstance(result, Failure):
                return Failure(result.failure())
        return Success(None)

    def _compiled_from_artifact(self, artifact: Artifact) -> Result[Compiled, AtlantideError]:
        ir = artifact.ir
        try:
            resources = rehydrate_resources(ir, self.types)
        except AtlantideError as exc:
            return Failure(exc)
        return assemble_compiled(
            ir, resources=resources, bindings=artifact.policies, outputs=artifact.outputs
        )

    async def _apply_compiled(
        self, compiled: Compiled, on_failure: OnFailure, progress: ProgressCallback | None = None
    ) -> Result[ApplyReport, AtlantideError]:
        planned = self._plan_from_compiled(compiled)
        if isinstance(planned, Failure):
            return _forward_failure(planned)
        plan_obj = planned.unwrap()
        prepared = self._runner_for_plan(plan_obj, on_failure, progress)
        if isinstance(prepared, Failure):  # async boundary: unwrap before awaiting
            return _forward_failure(prepared)
        ir = plan_obj.compiled.ir
        # Lock the changeset's scope plus any old ids an alias will retire.
        old_ids = frozenset(alias_remap(self.backend.load(), ir))
        scope = lock_scope(plan_obj.changeset, plan_obj.compiled.graph) | old_ids
        return await self._locked(prepared.unwrap(), scope, prepare=self._alias_migration(ir))

    def _runner_for_plan(
        self,
        plan_obj: Plan,
        on_failure: OnFailure = "halt",
        progress: ProgressCallback | None = None,
    ) -> Result[Callable[[StateGraph], Awaitable[ApplyReport]], AtlantideError]:
        if plan_obj.blocked:
            joined = "; ".join(f"{v.policy}: {v.message}" for v in plan_obj.blocked)
            return Failure(
                PolicyViolationError(f"policy denied apply: {joined}", list(plan_obj.blocked))
            )
        c = plan_obj.compiled
        desired = Desired(
            ir=c.ir, graph=c.graph, hashes=c.hashes,
            resources=c.resources, output_decls=c.outputs,
        )
        return Success(self._runner(plan_obj.changeset, desired, on_failure, progress))

    async def destroy(
        self, *, progress: ProgressCallback | None = None
    ) -> Result[ApplyReport, AtlantideError]:
        prior = self.backend.load()
        empty = IRGraph(nodes=())
        empty_graph = build_graph(empty).unwrap()  # empty IR is acyclic
        desired = Desired(ir=empty, graph=empty_graph, hashes={}, resources={})
        prepared = plan(diff(empty, {}, prior, self.mutability), protected_ids(prior)).map(
            lambda cs: self._runner(cs, desired, "halt", progress)
        )
        if isinstance(prepared, Failure):  # async boundary: unwrap before awaiting
            return _forward_failure(prepared)
        # destroy touches every recorded node, so lock the whole prior graph.
        return await self._locked(prepared.unwrap(), frozenset(prior.nodes))

    async def refresh(
        self, *, write: bool = False, progress: RefreshProgress | None = None
    ) -> Result[DriftReport, AtlantideError]:
        """Read live provider state for every recorded node and report drift.

        Read-only by default; ``write=True`` syncs detected drift back into state
        (and takes the whole-state lock, since it mutates).
        """
        prior = self.backend.load()

        async def run() -> DriftReport:
            return await refresh(prior=prior, env=self._env(), write=write, progress=progress)

        if not write:
            return Success(await run())
        return await with_lock(self.backend, frozenset(prior.nodes), run)

    def _runner(
        self,
        changeset: ChangeSet,
        desired: Desired,
        on_failure: OnFailure = "halt",
        progress: ProgressCallback | None = None,
    ) -> Callable[[StateGraph], Awaitable[ApplyReport]]:
        """Bind a changeset to the shared executor; ``_locked`` supplies prior state."""

        def run(prior: StateGraph) -> Awaitable[ApplyReport]:
            return _run_changeset(
                changeset=changeset,
                desired=desired,
                prior=prior,
                env=self._env(),
                on_failure=on_failure,
                progress=progress,
            )

        return run

    def _env(self) -> ApplyEnv:
        """The run environment; ``stack_outputs`` snapshots committed outputs now."""
        extra = {"parallelism": self.parallelism} if self.parallelism else {}
        return ApplyEnv(
            types=self.types,
            providers=self.providers,
            backend=self.backend,
            secrets=self.secrets,
            stack_outputs=self._stack_outputs(),
            **extra,
        )

    async def _locked(
        self,
        run: Callable[[StateGraph], Awaitable[ApplyReport]],
        scope: frozenset[str],
        *,
        prepare: Callable[[StateGraph], StateGraph] | None = None,
    ) -> Result[ApplyReport, AtlantideError]:
        """Run under the state lock, feeding ``run`` the state loaded post-acquire.

        ``prepare`` (apply only) may rewrite persisted state first — the alias
        rekey — and returns the state the run should see.
        """

        def under_lock() -> Awaitable[ApplyReport]:
            prior = self.backend.load()
            return run(prepare(prior) if prepare is not None else prior)

        return await with_lock(self.backend, scope, under_lock)

    def _alias_migration(self, ir: IRGraph) -> Callable[[StateGraph], StateGraph]:
        """A ``_locked`` prepare-hook that persists any alias rekey, so the executor
        and future runs see the renamed nodes' new ids."""

        def prepare(prior: StateGraph) -> StateGraph:
            migrated, remap = resolve_aliases(prior, ir)
            if not remap:
                return prior
            persist_migration(self.backend, prior, migrated, remap)
            return self.backend.load()

        return prepare


def _forward_failure(result: Result[Any, AtlantideError]) -> Failure[AtlantideError]:
    """Re-tag a planning ``Failure`` to satisfy the async path's return type.

    The pure ``Result`` cannot be ``.bind``-ed across an ``await``, so each async
    stage unwraps by hand; this centralises that bridge.
    """
    return Failure(result.failure())
