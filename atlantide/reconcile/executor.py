"""Execute a ChangeSet against real providers, persisting state per node.

Applies (create/update/replace) run forward over the desired graph; deletes run
in reverse over the prior-state graph. Both use the parallel scheduler, so
independent work overlaps while dependencies are respected.

Guarantees:
- **incremental persist** - each node's state row is written the moment its CRUD
  succeeds, so a crash leaves a consistent, resumable state;
- **failure handling** via ``on_failure``:
  - ``"halt"`` (default): the first provider error cancels the rest; completed
    nodes stay applied, resumable on the next apply;
  - ``"rollback"``: a **compensation saga** - each completed node records an undo
    action; on failure the executor runs them in reverse completion order, then
    re-raises. Only fully-completed nodes are compensated;
- **REPLACE** is destroy-before-create by default; a ``create_before_destroy``
  REPLACE creates the new resource in the forward pass and defers destroying the
  old one to a terminal cleanup phase (no downtime).

Refs are resolved to concrete upstream outputs just before each provider call.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from atlantide.core.context import Context
from atlantide.core.errors import ProviderError
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.graph.model import DiGraph
from atlantide.graph.schedule import run_graph
from atlantide.reconcile.context import (
    PHASE_FAIL,
    PHASE_FINISH,
    PHASE_START,
    ApplyEnv,
    Desired,
    LiveOutputs,
    OnFailure,
    ProgressCallback,
    node_failure,
    provider_for,
    state_digraph,
)
from atlantide.reconcile.diff import Action, Change, ChangeSet
from atlantide.reconcile.resolve import (
    reconstruct,
    resolve_refs,
    resolve_secret_refs,
    resolve_stack_refs,
    resolve_value,
    seal_outputs,
    secret_digests,
    sensitive_output_names,
    unseal_outputs,
)
from atlantide.state.backend import (
    STATUS_CREATED,
    STATUS_CREATING,
    StateBackend,
    StateGraph,
    StateNode,
)

#: A recorded undo for one completed node: (node id, coroutine factory).
Compensation = tuple[str, Callable[[], Awaitable[None]]]


def _noop_progress(node_id: str, action: Action, phase: str) -> None:
    pass


@dataclass(slots=True)
class ApplyReport:
    """What one run did, per action — not to be confused with the other
    "outputs": ``outputs`` here is the *declared exports* (``output()`` calls),
    resolved; live per-node values are ``LiveOutputs``; committed cross-stack
    values are ``StateBackend.outputs()``."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    replaced: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    noop: list[str] = field(default_factory=list)
    rolled_back: list[str] = field(default_factory=list)  # compensated on saga rollback
    outputs: dict[str, Any] = field(default_factory=dict)  # declared exports, resolved
    #: Output names whose value derives from a sensitive field; renderers redact these.
    sensitive_outputs: frozenset[str] = frozenset()


async def apply(
    *,
    changeset: ChangeSet,
    desired: Desired,
    prior: StateGraph,
    env: ApplyEnv,
    on_failure: OnFailure = "halt",
    progress: ProgressCallback | None = None,
) -> ApplyReport:
    """Run the ChangeSet; return a per-action report. Raises on provider failure.

    ``on_failure="rollback"`` runs a compensation saga before re-raising (see the
    module docstring); ``"halt"`` (default) leaves completed nodes applied.
    ``progress(node_id, action, phase)`` is called on each node's start/finish/fail.
    """
    return await _Applier(
        changeset=changeset,
        desired=desired,
        prior=prior,
        env=env,
        on_failure=on_failure,
        on_progress=progress or _noop_progress,
    ).run()


class _Applier:
    """Executes one ChangeSet: forward apply, CBD cleanup, deletes, saga rollback.

    The run's shared mutable state — ``live_outputs``, the ``report``, recorded
    ``compensations``, and deferred CBD deletes — lives on ``self``, so each phase
    is a separate method.
    """

    def __init__(
        self,
        *,
        changeset: ChangeSet,
        desired: Desired,
        prior: StateGraph,
        env: ApplyEnv,
        on_failure: OnFailure,
        on_progress: ProgressCallback,
    ) -> None:
        self.desired = desired
        self.env = env
        self.on_failure = on_failure
        self.on_progress = on_progress

        self.ir_by_id = {node.id: node for node in desired.ir.nodes}
        self.changes = {c.node_id: c for c in changeset.changes}
        self.prior_state = prior
        self.prior_graph = state_digraph(prior)
        # Live values for ref resolution: prior outputs, with sealed sensitive
        # values decrypted back to plaintext. Persisted nodes stay sealed.
        self.live_outputs: LiveOutputs = {
            nid: unseal_outputs(node.outputs, env.secrets) for nid, node in prior.nodes.items()
        }
        self.report = ApplyReport()
        self.ctx = Context()
        self.delete_ids = {c.node_id for c in changeset.by_action(Action.DELETE)}
        # Completed-node undos, in completion order (a node completes only after its
        # dependencies, so reversing this list undoes dependents-first).
        self.compensations: list[Compensation] = []
        # CBD REPLACEs create the new resource forward and defer destroying the old
        # (node id -> the old Resource) to a terminal cleanup phase.
        self.cbd_deferred: dict[str, Resource] = {}

    async def run(self) -> ApplyReport:
        # Phase 1: create/update/replace/noop, dependencies first.
        try:
            await run_graph(
                self.desired.graph, self._apply_node, parallelism=self.env.parallelism
            )
        except Exception:
            if self.on_failure == "rollback":
                await self._rollback()
            raise
        # Phase 1b: destroy the old halves of CBD REPLACEs, dependents first (new
        # resources and rewired dependents already in place). Terminal.
        if self.cbd_deferred:
            await self._run(self.desired.graph, self._cbd_cleanup, reverse=True)
        # Phase 2: deletes, dependents first. Terminal — not rolled back (recreating
        # a just-destroyed resource would lose its identity/outputs).
        if self.delete_ids:
            await self._run(self.prior_graph, self._delete_node, reverse=True)
        # Declared exports resolve against live outputs (refs to unchanged nodes
        # resolve — their outputs were seeded from prior state at construction).
        self.report.outputs = {
            name: resolve_value(value, self.live_outputs)
            for name, value in self.desired.output_decls.items()
        }
        self.report.sensitive_outputs = sensitive_output_names(
            self.desired.output_decls, self.env
        )
        # Persist stack outputs so a StackReference in another config can read them.
        # Sensitive exports (e.g. a generated password) are sealed at rest; the
        # in-memory report keeps them in the clear for the caller (redacted only
        # at the render boundary).
        if self.report.outputs:
            self.env.backend.set_outputs(self._persistable_outputs())
        return self.report

    def _persistable_outputs(self) -> dict[str, Any]:
        return {
            name: (
                self.env.secrets.seal(value)
                if name in self.report.sensitive_outputs and isinstance(value, str)
                else value
            )
            for name, value in self.report.outputs.items()
        }

    async def _run(
        self, graph: DiGraph, step: Callable[[str], Awaitable[None]], *, reverse: bool = False
    ) -> None:
        await run_graph(graph, step, parallelism=self.env.parallelism, reverse=reverse)

    # Per-node phases

    async def _apply_node(self, node_id: str) -> None:
        change = self.changes[node_id]
        if change.action is Action.NOOP:
            self.report.noop.append(node_id)
            return
        self.on_progress(node_id, change.action, PHASE_START)
        try:
            await self._apply_one(node_id, change)
        except Exception as exc:
            self.on_progress(node_id, change.action, PHASE_FAIL)
            raise node_failure(node_id, change.action.name.lower(), exc) from exc
        self.on_progress(node_id, change.action, PHASE_FINISH)

    async def _apply_one(self, node_id: str, change: Change) -> None:
        # Resolve upstream-output refs, then secret + stack-output handles (in-memory).
        res = resolve_stack_refs(
            resolve_secret_refs(
                resolve_refs(self.desired.resources[node_id], self.live_outputs),
                self.env.secrets,
            ),
            self.env.stack_outputs,
        )
        provider = provider_for(self.env.providers, res.provider_name())
        match change.action:
            case Action.CREATE:
                self._write_ahead(node_id, res)  # record the attempt before the create
                created = await provider.create(self.ctx, res)
                self.live_outputs[node_id] = created
                self._record(
                    node_id,
                    _undo_create(
                        provider, self.ctx, _with_outputs(res, created), node_id, self.env.backend
                    ),
                )
                self.report.created.append(node_id)
            case Action.UPDATE:
                prior_node = self.prior_state.get(node_id)
                self.live_outputs[node_id] = await provider.update(
                    self.ctx, self.live_outputs.get(node_id, {}), res
                )
                if prior_node is not None:
                    old = reconstruct(prior_node, self.env, self.live_outputs)
                    prior_outputs = unseal_outputs(prior_node.outputs, self.env.secrets)
                    self._record(
                        node_id,
                        _undo_update(
                            provider, self.ctx, old, prior_node, prior_outputs, self.env.backend
                        ),
                    )
                self.report.updated.append(node_id)
            case Action.REPLACE:
                await self._replace(node_id, change, res, provider)
            case _:
                raise ProviderError(f"unexpected {change.action} for {node_id!r} in apply phase")
        self._persist(node_id, res, self.live_outputs[node_id])

    async def _replace(
        self, node_id: str, change: Change, res: Resource, provider: Provider
    ) -> None:
        prior_node = self.prior_state.get(node_id)
        old = (
            reconstruct(prior_node, self.env, self.live_outputs) if prior_node else res
        )
        if change.create_before_destroy and prior_node is not None:
            # create the new resource first; destroy the old in the cleanup phase.
            created = await provider.create(self.ctx, res)
            self.live_outputs[node_id] = created
            self.cbd_deferred[node_id] = old
            self._record(
                node_id,
                _undo_cbd_create(
                    provider, self.ctx, _with_outputs(res, created), prior_node, self.env.backend
                ),
            )
        else:  # destroy-before-create: remove the old, then create the new
            await provider.delete(self.ctx, old)
            created = await provider.create(self.ctx, res)
            self.live_outputs[node_id] = created
            if prior_node is not None:
                self._record(
                    node_id,
                    _undo_replace(
                        provider, self.ctx, _with_outputs(res, created), old, prior_node,
                        self.env.backend,
                    ),
                )
        self.report.replaced.append(node_id)

    async def _cbd_cleanup(self, node_id: str) -> None:
        old = self.cbd_deferred.get(node_id)
        if old is not None:
            await provider_for(self.env.providers, old.provider_name()).delete(self.ctx, old)

    async def _delete_node(self, node_id: str) -> None:
        if node_id not in self.delete_ids:
            return
        self.on_progress(node_id, Action.DELETE, PHASE_START)
        try:
            prior_node = self.prior_state.get(node_id)
            assert prior_node is not None
            res = reconstruct(prior_node, self.env, self.live_outputs)
            await provider_for(self.env.providers, prior_node.provider).delete(self.ctx, res)
            self.env.backend.delete(node_id)
        except Exception as exc:
            self.on_progress(node_id, Action.DELETE, PHASE_FAIL)
            raise node_failure(node_id, "delete", exc) from exc
        self.report.deleted.append(node_id)
        self.on_progress(node_id, Action.DELETE, PHASE_FINISH)

    # State persistence / compensation

    def _state_node(
        self, node_id: str, res: Resource, outputs: dict[str, Any], status: str
    ) -> StateNode:
        ir_node = self.ir_by_id[node_id]
        return StateNode(
            id=node_id,
            type=res.type_name(),
            provider=res.provider_name(),
            provider_version=ir_node.provider_version,
            input_hash=self.desired.hashes[node_id],
            outputs=seal_outputs(outputs, type(res), self.env.secrets),
            properties=ir_node.properties,
            dependencies=ir_node.dependencies,
            prevent_destroy=res.lifecycle.prevent_destroy,
            secret_digests=secret_digests(res, node_id, self.env.secrets),
            status=status,
        )

    def _write_ahead(self, node_id: str, res: Resource) -> None:
        """Record a 'creating' row before the provider create.

        A create that succeeds at the provider but is cancelled or crashes before
        persist stays tracked, so destroy/refresh can still reclaim it.
        """
        self.env.backend.put(self._state_node(node_id, res, {}, STATUS_CREATING))

    def _persist(self, node_id: str, res: Resource, outputs: dict[str, Any]) -> None:
        self.env.backend.put(self._state_node(node_id, res, outputs, STATUS_CREATED))

    def _record(self, node_id: str, undo: Callable[[], Awaitable[None]]) -> None:
        if self.on_failure == "rollback":
            self.compensations.append((node_id, undo))

    async def _rollback(self) -> None:
        """Undo completed nodes in reverse completion order (best-effort, sequential).

        Every recorded node is attempted even if one undo fails, so a flaky
        compensation can't strand the rest; ids undone (whether or not their
        provider call errored) land in ``report.rolled_back``.
        """
        for node_id, undo in reversed(self.compensations):
            self.report.rolled_back.append(node_id)
            with contextlib.suppress(Exception):
                await undo()


def _with_outputs(res: Resource, outputs: dict[str, Any]) -> Resource:
    """``res`` with its freshly-created computed outputs (e.g. the real id) restored.

    A compensation deletes the resource just created; the id lets the provider act
    on it directly rather than locating it by attributes, which is unsafe when those
    (e.g. a VPC's CIDR) are shared with an unrelated resource.
    """
    fields = type(res).model_fields
    updates = {key: value for key, value in outputs.items() if key in fields}
    return res.model_copy(update=updates) if updates else res


def _undo_create(
    provider: Provider, ctx: Context, res: Resource, node_id: str, backend: StateBackend
) -> Callable[[], Awaitable[None]]:
    async def undo() -> None:
        await provider.delete(ctx, res)
        backend.delete(node_id)

    return undo


def _undo_update(
    provider: Provider,
    ctx: Context,
    old: Resource,
    prior_node: StateNode,
    prior_outputs: dict[str, Any],
    backend: StateBackend,
) -> Callable[[], Awaitable[None]]:
    async def undo() -> None:
        await provider.update(ctx, prior_outputs, old)  # plaintext outputs for the provider
        backend.put(prior_node)  # restore the prior (sealed) state row verbatim

    return undo


def _undo_replace(
    provider: Provider,
    ctx: Context,
    new: Resource,
    old: Resource,
    prior_node: StateNode,
    backend: StateBackend,
) -> Callable[[], Awaitable[None]]:
    async def undo() -> None:
        await provider.delete(ctx, new)  # remove the replacement
        await provider.create(ctx, old)  # recreate the original
        backend.put(prior_node)

    return undo


def _undo_cbd_create(
    provider: Provider, ctx: Context, new: Resource, prior_node: StateNode, backend: StateBackend
) -> Callable[[], Awaitable[None]]:
    """Undo a create-before-destroy REPLACE's forward half.

    The old resource is still live (its deletion is deferred to cleanup), so undo
    removes the freshly-created replacement and restores the prior state row.
    """

    async def undo() -> None:
        await provider.delete(ctx, new)
        backend.put(prior_node)

    return undo
