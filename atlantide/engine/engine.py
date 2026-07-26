"""The Engine: orchestrates compile -> plan -> apply/destroy.

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

from collections.abc import Awaitable, Callable, Sequence
from typing import Any, TypeVar

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
from atlantide.core.errors import StateError
from atlantide.core.events import EventSink, no_sink
from atlantide.engine.drift import raise_drift
from atlantide.engine.hydrate import assemble_compiled, rehydrate_resources
from atlantide.engine.locking import (
    DEFAULT_LOCK_POLICY,
    LockPolicy,
    apply_scope,
    lock_owner,
    with_lock,
)
from atlantide.engine.model import Compiled, Plan
from atlantide.engine.planner import Planner, protected_ids
from atlantide.engine.result import forward_failure, raise_on_failure
from atlantide.graph import build_graph
from atlantide.graph.order import topological_order
from atlantide.graph.select import closure, match_targets
from atlantide.ir import Artifact, build_artifact, lower, verify_hash
from atlantide.ir.model import IRGraph
from atlantide.lang import DEFAULT_SURFACE, LanguageSurface, evaluate_source
from atlantide.policy import PolicyRegistry, default_policy_registry
from atlantide.reconcile import (
    ApplyEnv,
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
    alias_remap,
    diff,
    identity_fields,
    persist_migration,
    plan,
    refresh,
    resolve_aliases,
    restrict,
)
from atlantide.reconcile import apply as _run_changeset
from atlantide.reconcile.context import DEFAULT_NODE_TIMEOUT, state_digraph
from atlantide.secrets import SecretsRegistry
from atlantide.state import StateGraph
from atlantide.state.backend import LeaseGuard, StateBackend

_T = TypeVar("_T")


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
        lock_policy: LockPolicy = DEFAULT_LOCK_POLICY,
        node_timeout: float = DEFAULT_NODE_TIMEOUT,
        surface: LanguageSurface = DEFAULT_SURFACE,
    ) -> None:
        self.providers = providers
        self.backend = backend
        self.types = types
        self.parallelism = parallelism
        self.lock_policy = lock_policy
        self.node_timeout = node_timeout
        # Which modules config may import — widened by installed provider plugins.
        self.surface = surface
        # Where run events go, and what identifies this run in them. Both are set
        # per locked run; an unlocked or embedded caller gets a discarding sink.
        self.events: EventSink = no_sink
        self.run_id = ""
        # The lease held by the run currently under `_locked`, handed to the
        # executor by `_env` as a write guard. Replaced per locked run; the default
        # holds no lease and never refuses, which is what the unlocked paths (a
        # read-only refresh, an embedding caller) want.
        self._lease = LeaseGuard(grace=lock_policy.renew_grace)
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
        evaluated = evaluate_source(
            source, filename, inputs=inputs, extra_globals=extra_globals, surface=self.surface
        )
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
            inputs=registry.inputs,
        )

    def plan(
        self,
        source: str,
        filename: str = "<config>",
        *,
        inputs: dict[str, Any] | None = None,
        extra_globals: dict[str, Any] | None = None,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[Plan, AtlantideError]:
        """Compile and diff against current state; the Plan carries any violations.

        ``targets`` narrows the plan to the named resources and everything they
        depend on; ``replace`` forces the named ones to be recreated.
        """
        compiled = self.compile(source, filename, inputs=inputs, extra_globals=extra_globals)
        return compiled.bind(
            lambda built: self._plan_from_compiled(built, targets=targets, replace=replace)
        )

    def _plan_from_compiled(
        self,
        built: Compiled,
        *,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[Plan, AtlantideError]:
        # Map renamed resources (Lifecycle.aliases) onto their existing state
        # nodes before diffing, so a rename is a NOOP rather than destroy+create.
        migrated, _ = resolve_aliases(self.backend.load(), built.ir)
        try:
            selected = self._selection(built, migrated, targets)
            forced = self._match_only(built, migrated, replace)
        except AtlantideError as exc:
            return Failure(exc)
        return self._planner.build(
            built,
            migrated,
            self._stack_outputs(),
            selected=selected,
            replace=forced,
        )

    def _selection(
        self, built: Compiled, prior: StateGraph, patterns: Sequence[str]
    ) -> frozenset[str] | None:
        """Node ids the patterns name, closed over their dependencies.

        ``None`` when nothing was asked for — distinct from an empty set, which
        would mean "act on nothing". Ids in state but absent from the desired
        graph are matchable too: a resource being deleted is a legitimate target,
        and it has no node in the config any more.
        """
        if not patterns:
            return None
        known = set(built.graph.node_ids) | set(prior.nodes)
        seeds = match_targets(patterns, known)
        return closure(built.graph, seeds & set(built.graph.node_ids), reverse=False) | seeds

    def _match_only(
        self, built: Compiled, prior: StateGraph, patterns: Sequence[str]
    ) -> frozenset[str]:
        """Exactly the node ids the patterns name — no dependency closure.

        ``--replace`` recreates what the operator named and nothing else. Closing
        over dependencies (what ``--target`` wants) would force-replace the whole
        upstream tree — a subnet's VPC and everything beside it.
        """
        if not patterns:
            return frozenset()
        known = set(built.graph.node_ids) | set(prior.nodes)
        return match_targets(patterns, known)

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
        expect: ChangeSet | None = None,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[ApplyReport, AtlantideError]:
        """Compile, plan, and execute the changeset under the state lock.

        ``expect`` is the changeset the caller showed a human and had approved.
        The apply re-diffs once it holds the lease — it has to, or a node another
        run created in the meantime would still be marked CREATE — so what
        executes is not necessarily what was approved. Passing ``expect`` turns
        that difference into a :class:`PlanDriftError` instead of a silent
        substitution.
        """
        compiled = self.compile(source, filename, inputs=inputs, extra_globals=extra_globals)
        if isinstance(compiled, Failure):
            return forward_failure(compiled)
        return await self._apply_compiled(
            compiled.unwrap(), on_failure, progress, expect, targets=targets, replace=replace
        )

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
            return forward_failure(hashed)
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
            return forward_failure(verified)
        built = self._compiled_from_artifact(artifact)
        if isinstance(built, Failure):
            return forward_failure(built)
        return await self._apply_compiled(built.unwrap(), on_failure, progress)

    def _check_pins(self, artifact: Artifact) -> Result[None, AtlantideError]:
        for name, version in sorted(artifact.provider_pins.items()):
            result = self.providers.check_compatible(name, version)
            if isinstance(result, Failure):
                return forward_failure(result)
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
        self,
        compiled: Compiled,
        on_failure: OnFailure,
        progress: ProgressCallback | None = None,
        expect: ChangeSet | None = None,
        targets: Sequence[str] = (),
        replace: Sequence[str] = (),
    ) -> Result[ApplyReport, AtlantideError]:
        # The gating plan carries the same narrowing the run will use: judging
        # `prevent_destroy` or a mandatory policy against the *full* changeset
        # would block a targeted apply for nodes it was never going to touch.
        planned = self._plan_from_compiled(compiled, targets=targets, replace=replace)
        if isinstance(planned, Failure):
            return forward_failure(planned)
        plan_obj = planned.unwrap()
        # Report a policy denial before taking the lock. This plan's changeset
        # sizes the scope; `run_replanned` computes the one that is executed.
        blocked = self._runner_for_plan(plan_obj, on_failure, progress)
        if isinstance(blocked, Failure):  # async boundary: unwrap before awaiting
            return forward_failure(blocked)
        ir = plan_obj.compiled.ir
        prior = self.backend.load()
        scope = apply_scope(plan_obj, prior) | frozenset(alias_remap(prior, ir))

        def run_replanned(prior: StateGraph) -> Awaitable[ApplyReport]:
            # Re-diff against the state read after the lease was taken: a node
            # another run created meanwhile is still CREATE in the pre-lock
            # changeset, and applying that creates a second resource. The re-diff
            # keeps the same narrowing, so a targeted apply cannot widen once it
            # holds the lock.
            fresh = raise_on_failure(
                self._plan_from_compiled(compiled, targets=targets, replace=replace)
            )
            if expect is not None:
                raise_drift(expect, fresh.changeset)
            runner = raise_on_failure(self._runner_for_plan(fresh, on_failure, progress))
            return runner(prior)

        return await self._locked(run_replanned, scope, prepare=self._alias_migration(ir))

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
            ir=c.ir,
            graph=c.graph,
            hashes=c.hashes,
            resources=c.resources,
            output_decls=c.outputs,
        )
        return Success(self._runner(plan_obj.changeset, desired, on_failure, progress))

    async def destroy(
        self,
        *,
        progress: ProgressCallback | None = None,
        targets: Sequence[str] = (),
    ) -> Result[ApplyReport, AtlantideError]:
        """Destroy everything in state, or only ``targets`` and their dependents.

        A targeted destroy closes over *dependents*, not dependencies: removing a
        VPC means removing what still points at it. The opposite closure would
        destroy the things the target is built from and leave the target itself
        dangling.
        """
        prior = self.backend.load()
        empty = IRGraph(nodes=())
        empty_graph = build_graph(empty).unwrap()  # empty IR is acyclic
        desired = Desired(ir=empty, graph=empty_graph, hashes={}, resources={})

        def changeset_for(state: StateGraph) -> Result[ChangeSet, AtlantideError]:
            changes = diff(empty, {}, state, self.mutability)
            if targets:
                try:
                    changes = restrict(changes, self._destroy_selection(state, targets))
                except AtlantideError as exc:
                    return Failure(exc)
            return plan(changes, protected_ids(state))

        # Validate patterns and `prevent_destroy` before locking, so the common
        # refusals surface as a clean Failure without contending for the lease.
        gate = changeset_for(prior)
        if isinstance(gate, Failure):
            return forward_failure(gate)
        # destroy touches every recorded node, so lock the whole prior graph.
        scope = frozenset(prior.nodes)

        def run_replanned(fresh: StateGraph) -> Awaitable[ApplyReport]:
            # Re-diff against the state read after the lease was taken, exactly
            # as apply does: a row created while waiting for the lock is not in
            # the pre-lock changeset (nor in the lease's scope), and silently
            # completing without it reports success while the resource lives on.
            created = set(fresh.nodes) - scope
            if created:
                raise StateError(
                    "state gained node(s) while destroy waited for the lock: "
                    + ", ".join(sorted(created))
                    + " — re-run destroy to include them"
                )
            changeset = raise_on_failure(changeset_for(fresh))
            return self._runner(changeset, desired, "halt", progress)(fresh)

        return await self._locked(run_replanned, scope)

    def destroy_targets(self, patterns: Sequence[str]) -> Result[list[str], AtlantideError]:
        """What a targeted destroy would remove, for the confirmation preview.

        The preview has to be the selection, not the whole store: showing every
        recorded node before a `--target` destroy would ask the operator to
        approve a list that is not what happens.
        """
        prior = self.backend.load()
        if not patterns:
            return Success(sorted(prior.nodes))
        try:
            return Success(sorted(self._destroy_selection(prior, patterns)))
        except AtlantideError as exc:
            return Failure(exc)

    def _destroy_selection(self, prior: StateGraph, patterns: Sequence[str]) -> frozenset[str]:
        """Targets plus everything that still depends on them, from state alone.

        The desired graph is empty during a destroy, so the edges come from what
        each stored node recorded as its dependencies.
        """
        seeds = match_targets(patterns, set(prior.nodes))
        return closure(state_digraph(prior), seeds, reverse=True)

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

        The lock covers only the nodes being adopted — the rows nothing else is
        touching — rather than the whole state, so an import can run alongside an
        apply to a disjoint subgraph. ``write=False`` is a pure read and takes no
        lock, exactly as a read-only ``refresh`` does.
        """
        try:
            ordered = self._import_order(compiled, requests)
        except AtlantideError as exc:
            return Failure(exc)

        async def run(prior: StateGraph) -> list[ImportOutcome]:
            # `prior` is loaded inside the lock for a write run: the
            # ALREADY_TRACKED and dependency checks must see rows a run that held
            # the lock meanwhile added or removed, not a pre-lock snapshot.
            return await adopt(
                requests=ordered,
                ir=compiled.ir,
                hashes=compiled.hashes,
                prior=prior,
                env=self._env(),
                write=write,
                allow_drift=allow_drift,
                force=force,
            )

        if not write:
            return Success(await run(self.backend.load()))
        return await self._locked(run, frozenset(request.node_id for request in ordered))

    @staticmethod
    def _import_order(compiled: Compiled, requests: Sequence[ImportRequest]) -> list[ImportRequest]:
        """Requests in dependency order; ones naming no known node keep their place.

        An unknown node id is not rejected here — ``adopt`` reports it per request,
        so a typo in a twenty-node batch does not abort the nineteen that are fine.
        """
        rank = {node_id: i for i, node_id in enumerate(topological_order(compiled.graph))}
        return sorted(requests, key=lambda r: rank.get(r.node_id, len(rank)))

    def identity_fields(self, compiled: Compiled, node_ids: Sequence[str]) -> dict[str, str | None]:
        """Per node, the id field its type is located by — ``None`` if found by name.

        Reads nothing — not state, not the providers — so the listing that
        precedes an import is free. Deliberately not routed through ``_env()``,
        which would query committed stack outputs this answer does not need.
        """
        return identity_fields(
            ir=compiled.ir, types=self.types, providers=self.providers, node_ids=node_ids
        )

    def importable(self, compiled: Compiled) -> list[str]:
        """Node ids this config declares that state does not yet track.

        Exactly the nodes a plan would report as CREATE, which is the same
        question asked from the other side: each is either a resource that does
        not exist yet, or one that does and could be imported.
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
        additionally drops rows whose resource the provider could not find — see
        :func:`~atlantide.reconcile.refresh.refresh` on why that is opt-in.
        """
        prior = self.backend.load()
        if not write:
            return Success(
                await refresh(prior=prior, env=self._env(), write=False, progress=progress)
            )
        # The pre-lock snapshot only sizes the lock scope. The rows to refresh are
        # re-read once the lease is held: refreshing the snapshot would re-write
        # rows a run holding the lock meanwhile deleted — resurrecting state for
        # cleanly-destroyed resources. Rows created meanwhile are outside the
        # lease and are left for the next refresh rather than written unheld.
        scope = frozenset(prior.nodes)

        async def run(fresh: StateGraph) -> DriftReport:
            covered = StateGraph(nodes={i: node for i, node in fresh.nodes.items() if i in scope})
            return await refresh(
                prior=covered, env=self._env(), write=True, prune=prune, progress=progress
            )

        return await self._locked(run, scope)

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
        extra: dict[str, Any] = {"parallelism": self.parallelism} if self.parallelism else {}
        return ApplyEnv(
            types=self.types,
            providers=self.providers,
            backend=self.backend,
            secrets=self.secrets,
            stack_outputs=self._stack_outputs(),
            lease=self._lease,
            node_timeout=self.node_timeout,
            events=self.events,
            run_id=self.run_id,
            **extra,
        )

    async def _locked(
        self,
        run: Callable[[StateGraph], Awaitable[_T]],
        scope: frozenset[str],
        *,
        prepare: Callable[[StateGraph], StateGraph] | None = None,
    ) -> Result[_T, AtlantideError]:
        """Run under the state lock, feeding ``run`` the state loaded post-acquire.

        The one locked-run scaffold: apply, destroy, ``refresh --write`` and
        import all route through it, so they share the fresh guard, owner/run-id
        wiring, and events plumbing. ``prepare`` (apply only) may rewrite
        persisted state first — the alias rekey — and returns the state the run
        should see.
        """

        def under_lock() -> Awaitable[_T]:
            prior = self.backend.load()
            return run(prepare(prior) if prepare is not None else prior)

        # A fresh guard per locked run: `_env` reads it when the executor is
        # built, which happens inside `under_lock`, after the lease is taken.
        self._lease = LeaseGuard(grace=self.lock_policy.renew_grace)
        # The lock owner already encodes host + pid + a per-run token, so it is the
        # "who" an audit record needs; a second identifier could only disagree.
        self.run_id = lock_owner()
        return await with_lock(
            self.backend,
            scope,
            under_lock,
            policy=self.lock_policy,
            guard=self._lease,
            owner=self.run_id,
            events=self.events,
            run_id=self.run_id,
        )

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
