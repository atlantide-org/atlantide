"""The :class:`Engine`: the library entrypoint for compile -> plan -> apply/destroy.

A façade: every method delegates to a sibling module (see ``README.md``), and
every mutation runs under the state lock through
:meth:`~atlantide.engine.runs.LockedRuns.run_locked`.
"""

from __future__ import annotations

from collections.abc import Awaitable, Sequence
from typing import Any, Self, override

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError, ProviderRegistry, Resource
from atlantide.core.events import EventSink, no_sink
from atlantide.engine import compiler, selection
from atlantide.engine.locking import apply_scope, require_no_new_nodes
from atlantide.engine.model import Compiled, Plan
from atlantide.engine.planner import Planner, destroy_changeset, raise_drift
from atlantide.engine.result import catching, forward_failure, raise_on_failure
from atlantide.engine.runs import LockedRuns, Runner
from atlantide.ir import Artifact
from atlantide.lang import DEFAULT_FUEL, DEFAULT_SURFACE, LanguageSurface
from atlantide.policy import PolicyRegistry, default_policy_registry
from atlantide.reconcile import (
    AdoptOptions,
    ApplyReport,
    ChangeSet,
    Desired,
    DriftReport,
    ImportOutcome,
    ImportRequest,
    OnFailure,
    ProgressCallback,
    RefreshProgress,
    adopt,
    identity_fields,
    refresh,
    resolve_aliases,
    type_mutability,
)
from atlantide.reconcile.env import DEFAULT_NODE_TIMEOUT
from atlantide.secrets import SecretsRegistry
from atlantide.state import DEFAULT_LOCK_POLICY, LockPolicy, StateBackend, StateGraph


class Engine:
    def __init__(  # noqa: PLR0913 - public API: keyword-only engine options
        self,
        providers: ProviderRegistry,
        backend: StateBackend,
        types: dict[str, type[Resource]],
        *,
        parallelism: int | None = None,
        policies: PolicyRegistry | None = None,
        secrets: SecretsRegistry | None = None,
        lock_policy: LockPolicy = DEFAULT_LOCK_POLICY,
        node_timeout: float = DEFAULT_NODE_TIMEOUT,
        surface: LanguageSurface = DEFAULT_SURFACE,
        fuel: int = DEFAULT_FUEL,
    ) -> None:
        self.providers = providers
        self.backend = backend
        self.types = types
        self.parallelism = parallelism
        self.lock_policy = lock_policy
        self.node_timeout = node_timeout
        # Modules config may import; installed provider plugins widen it.
        self.surface = surface
        # Evaluation step budget for every compile. It never changes what a
        # successful compile produces, only whether a large or runaway config finishes.
        self.fuel = fuel
        # Where run events go, and what identifies this run in them. Both are set
        # per locked run; an unlocked or embedded caller gets a discarding sink.
        self.events: EventSink = no_sink
        self.run_id = ""
        self.policies = policies if policies is not None else default_policy_registry()
        # An empty registry suffices until a config declares a secret; sealing a
        # concrete sensitive value then requires a configured provider.
        self.secrets = secrets if secrets is not None else SecretsRegistry()
        self.mutability = type_mutability(types)
        self._planner = Planner(
            mutability=self.mutability,
            types=self.types,
            secrets=self.secrets,
            policies=self.policies,
        )
        self._runs = LockedRuns(self)

    @override
    def __repr__(self) -> str:
        return f"Engine(backend={self.backend!r}, parallelism={self.parallelism!r})"

    def __enter__(self) -> Self:
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
        envs: Sequence[str] | None = None,
        extra_globals: dict[str, Any] | None = None,
    ) -> Result[Compiled, AtlantideError]:
        """Evaluate Atlas-lang source into a :class:`Compiled` (IR, graph, hashes)."""
        return compiler.compile_source(
            source,
            filename,
            providers=self.providers,
            surface=self.surface,
            fuel=self.fuel,
            inputs=inputs,
            envs=envs,
            extra_globals=extra_globals,
        )

    def plan(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        envs: Sequence[str] | None = None,
        extra_globals: dict[str, Any] | None = None,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[Plan, AtlantideError]:
        """Compile and diff against current state; the Plan carries any violations.

        ``targets`` narrows the plan to the named resources and everything they
        depend on; ``replace`` forces the named ones to be recreated; ``envs``
        narrows it to the named environments of the config's ``Config``.
        """
        return self.compile(
            source, filename, inputs=inputs, envs=envs, extra_globals=extra_globals
        ).bind(
            lambda compiled: self._plan_from_compiled(compiled, targets=targets, replace=replace)
        )

    def _plan_from_compiled(
        self,
        compiled: Compiled,
        *,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
        prior: StateGraph | None = None,
    ) -> Result[Plan, AtlantideError]:
        # `prior` is state the caller has already read; otherwise it is loaded here.
        # Map renamed resources (Lifecycle.aliases) onto their existing state
        # nodes before diffing, so a rename is a NOOP rather than destroy+create.
        loaded = prior if prior is not None else self.backend.load()
        migrated, _ = resolve_aliases(loaded, compiled.ir)
        chosen = catching(lambda: selection.narrowing(compiled, migrated, targets, replace))
        if isinstance(chosen, Failure):
            return forward_failure(chosen)
        selected, forced = chosen.unwrap()
        return self._planner.build(
            compiled,
            migrated,
            self._runs.stack_outputs(),
            selected=selected,
            replace=forced,
        )

    # -- effectful stages -------------------------------------------------

    async def apply(  # noqa: PLR0913 - public API: keyword-only run options
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        envs: Sequence[str] | None = None,
        extra_globals: dict[str, Any] | None = None,
        on_failure: OnFailure = "rollback",
        progress: ProgressCallback | None = None,
        expect: ChangeSet | None = None,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[ApplyReport, AtlantideError]:
        """Compile, plan, and execute the changeset under the state lock.

        ``expect`` is the changeset the caller showed a human and had approved.
        The apply re-diffs once it holds the lease, so that a node another run
        created meanwhile is not marked CREATE; what executes may therefore differ
        from what was approved. Passing ``expect`` turns that difference into a
        :class:`~atlantide.core.errors.PlanDriftError`.
        """
        compiled = self.compile(
            source, filename, inputs=inputs, envs=envs, extra_globals=extra_globals
        )
        if isinstance(compiled, Failure):
            return forward_failure(compiled)
        return await self.apply_compiled(
            compiled.unwrap(),
            on_failure=on_failure,
            progress=progress,
            expect=expect,
            targets=targets,
            replace=replace,
        )

    # -- build / deploy (portable artifacts) ------------------------------

    def build(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        envs: Sequence[str] | None = None,
        extra_globals: dict[str, Any] | None = None,
        component_pins: dict[str, str] | None = None,
    ) -> Result[Artifact, AtlantideError]:
        """Compile a config into a portable, content-hashed ``.atlas`` artifact.

        ``component_pins`` (alias -> resolved commit, from the project's lock) is
        recorded in the artifact as provenance for any published components used.
        """
        return self.compile(
            source, filename, inputs=inputs, envs=envs, extra_globals=extra_globals
        ).map(lambda compiled: compiler.artifact_of(compiled, component_pins))

    def verify_artifact(self, artifact: Artifact) -> Result[None, AtlantideError]:
        """Check the artifact's IR hash and that every pinned provider is compatible."""
        return compiler.verify_artifact(artifact, self.providers)

    async def deploy(
        self,
        artifact: Artifact,
        *,
        on_failure: OnFailure = "rollback",
        progress: ProgressCallback | None = None,
    ) -> Result[ApplyReport, AtlantideError]:
        """Apply an artifact directly from its IR, without source or re-execution.

        Secrets are references, not values, so a source-less deploy resolves each
        handle from the *target* environment's secrets backend at apply time.
        """
        verified = self.verify_artifact(artifact)
        if isinstance(verified, Failure):
            return forward_failure(verified)
        compiled = compiler.compiled_from_artifact(artifact, self.types)
        if isinstance(compiled, Failure):
            return forward_failure(compiled)
        return await self.apply_compiled(
            compiled.unwrap(), on_failure=on_failure, progress=progress
        )

    async def apply_compiled(
        self,
        compiled: Compiled,
        *,
        on_failure: OnFailure = "rollback",
        progress: ProgressCallback | None = None,
        expect: ChangeSet | None = None,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[ApplyReport, AtlantideError]:
        """:meth:`apply` for a config already compiled, typically the plan's.

        Saves re-evaluating the source when the caller has just planned it (the
        CLI shows the plan, then applies it). Everything past compilation is
        identical: the gating plan, the lock, and the re-diff under the lease.
        Planning only reads a :class:`Compiled`, so reusing one is safe.
        """
        # The gating plan carries the same narrowing the run will use: judging
        # `prevent_destroy` or a mandatory policy against the *full* changeset
        # would block a targeted apply for nodes it does not touch. The same read
        # of state sizes the lock scope.
        snapshot = self.backend.load()
        planned = self._plan_from_compiled(
            compiled, targets=targets, replace=replace, prior=snapshot
        )
        if isinstance(planned, Failure):
            return forward_failure(planned)
        plan_obj = planned.unwrap()
        # Report a policy denial before taking the lock. This plan's changeset
        # sizes the scope; `run_replanned` computes the one that is executed.
        blocked = self._runs.runner_for_plan(plan_obj, on_failure, progress)
        if isinstance(blocked, Failure):  # async boundary: unwrap before awaiting
            return forward_failure(blocked)
        ir = plan_obj.compiled.ir
        scope = apply_scope(plan_obj, snapshot)

        def run_replanned(prior: StateGraph) -> Awaitable[ApplyReport]:
            # Re-diffed under the lease, against the state `run_locked` read
            # there: a node another run created meanwhile is still CREATE in the
            # pre-lock changeset. The re-diff keeps the same narrowing, so a
            # targeted apply cannot widen once it holds the lock. A row created
            # meanwhile is outside `scope`, and would otherwise diff as a DELETE
            # the lease does not cover.
            require_no_new_nodes(prior, scope, "apply", "re-run apply")
            fresh = raise_on_failure(
                self._plan_from_compiled(compiled, targets=targets, replace=replace, prior=prior)
            )
            if expect is not None:
                raise_drift(expect, fresh.changeset)
            return raise_on_failure(self._runs.runner_for_plan(fresh, on_failure, progress))(prior)

        return await self._runs.run_locked(
            run_replanned, scope, prepare=self._runs.alias_migration(ir)
        )

    async def destroy(
        self,
        *,
        progress: ProgressCallback | None = None,
        targets: Sequence[str] = (),
    ) -> Result[ApplyReport, AtlantideError]:
        """Destroy everything in state, or only ``targets`` and their dependents.

        A targeted destroy closes over dependents, not dependencies: removing a
        VPC also removes what still points at it.
        """
        prior = self.backend.load()
        # Validate patterns and `prevent_destroy` before locking, so the common
        # refusals surface as a clean Failure without contending for the lease.
        gate = destroy_changeset(prior, self.mutability, targets)
        if isinstance(gate, Failure):
            return forward_failure(gate)
        # destroy touches every recorded node, so lock the whole prior graph.
        scope = frozenset(prior.nodes)
        return await self._runs.run_locked(self._destroy_runner(scope, targets, progress), scope)

    def _destroy_runner(
        self, scope: frozenset[str], targets: Sequence[str], progress: ProgressCallback | None
    ) -> Runner:
        """The destroy run, re-diffed against the state read under the lease.

        A row created while waiting for the lock is outside ``scope``, so the run
        refuses rather than report success while that resource remains.
        """
        desired = Desired.empty()

        def run_replanned(fresh: StateGraph) -> Awaitable[ApplyReport]:
            require_no_new_nodes(fresh, scope, "destroy", "re-run destroy to include them")
            changeset = raise_on_failure(destroy_changeset(fresh, self.mutability, targets))
            return self._runs.runner(changeset, desired, "halt", progress)(fresh)

        return run_replanned

    def destroy_targets(self, patterns: Sequence[str]) -> Result[list[str], AtlantideError]:
        """What a targeted destroy would remove, for the confirmation preview.

        The preview is the selection, not the whole store, so the operator
        approves exactly what a `--target` destroy removes.
        """
        prior = self.backend.load()
        if not patterns:
            return Success(sorted(prior.nodes))
        return catching(lambda: sorted(selection.destroy_selection(prior, patterns)))

    async def import_nodes(
        self,
        compiled: Compiled,
        requests: Sequence[ImportRequest],
        *,
        write: bool = True,
        allow_drift: bool = False,
        force: bool = False,
    ) -> Result[list[ImportOutcome], AtlantideError]:
        """Adopt existing resources into state, so the next plan reports them unchanged.

        Requests are ordered topologically before they run: a node's ``$ref``
        inputs resolve against its dependencies' recorded outputs, so a VPC has to
        be adopted before the subnet referencing it can be read at all.

        The lock covers only the adopted nodes rather than the whole state, so an
        import can run alongside an apply to a disjoint subgraph. ``write=False``
        is a pure read and takes no lock, as with a read-only ``refresh``.
        """
        ordered_or_error = catching(lambda: selection.import_order(compiled, requests))
        if isinstance(ordered_or_error, Failure):
            return forward_failure(ordered_or_error)
        ordered = ordered_or_error.unwrap()
        options = AdoptOptions(write=write, allow_drift=allow_drift, force=force)

        async def run(prior: StateGraph) -> list[ImportOutcome]:
            # `prior` is loaded inside the lock for a write run: the
            # ALREADY_TRACKED and dependency checks must see rows a run that held
            # the lock meanwhile added or removed, not a pre-lock snapshot.
            return await adopt(
                requests=ordered,
                ir=compiled.ir,
                hashes=compiled.hashes,
                prior=prior,
                env=self._runs.env(),
                options=options,
            )

        if not write:
            return Success(await run(self.backend.load()))
        return await self._runs.run_locked(run, frozenset(request.node_id for request in ordered))

    def identity_fields(self, compiled: Compiled, node_ids: Sequence[str]) -> dict[str, str | None]:
        """Per node, the id field its type is located by, or ``None`` if found by name.

        Reads neither state nor providers, so the listing that precedes an import
        does no I/O. Not routed through the run environment, which would query
        committed stack outputs this answer does not need.
        """
        return identity_fields(
            ir=compiled.ir, types=self.types, providers=self.providers, node_ids=node_ids
        )

    def importable(self, compiled: Compiled) -> list[str]:
        """Node ids this config declares that state does not yet track.

        Exactly the nodes a plan would report as CREATE: each is either a resource
        that does not exist yet, or one that does and could be imported.
        """
        prior = self.backend.load()
        return sorted(node.id for node in compiled.ir.nodes if node.id not in prior.nodes)

    async def refresh(
        self,
        *,
        write: bool = False,
        prune: bool = False,
        progress: RefreshProgress | None = None,
    ) -> Result[DriftReport, AtlantideError]:
        """Read live provider state for every recorded node and report drift.

        Read-only by default; ``write=True`` syncs detected drift back into state
        (and takes the whole-state lock, since it mutates). ``prune=True``
        additionally drops rows whose resource the provider could not find (see
        :func:`~atlantide.reconcile.refresh.refresh` on why that is opt-in).
        """
        prior = self.backend.load()
        if not write:
            return Success(
                await refresh(prior=prior, env=self._runs.env(), write=False, progress=progress)
            )
        # The pre-lock snapshot only sizes the lock scope; the rows are re-read
        # under the lease (see `run_locked`), since refreshing the snapshot would
        # resurrect rows a run holding the lock meanwhile deleted. Rows created
        # meanwhile are outside the lease and are left for the next refresh.
        scope = frozenset(prior.nodes)

        async def run(fresh: StateGraph) -> DriftReport:
            return await refresh(
                prior=_covered(fresh, scope),
                env=self._runs.env(),
                write=True,
                prune=prune,
                progress=progress,
            )

        return await self._runs.run_locked(run, scope)


def _covered(graph: StateGraph, scope: frozenset[str]) -> StateGraph:
    """The part of ``graph`` whose rows ``scope`` covers."""
    return StateGraph(nodes={i: node for i, node in graph.nodes.items() if i in scope})
