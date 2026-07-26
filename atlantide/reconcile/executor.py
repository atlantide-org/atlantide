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

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from atlantide.core.context import Context
from atlantide.core.errors import ProviderError, RollbackError
from atlantide.core.events import (
    NODE_FAIL,
    NODE_FINISH,
    NODE_START,
    ROLLBACK_NODE,
    ROLLBACK_SKIPPED,
    ROLLBACK_START,
    RUN_FINISH,
    RUN_START,
    ApplyEvent,
)
from atlantide.core.node_id import stack_of
from atlantide.core.provider import Provider
from atlantide.core.resource import DataSource, Resource
from atlantide.graph.model import DiGraph
from atlantide.graph.schedule import run_graph
from atlantide.reconcile.compensation import (
    Compensator,
    _cbd_old_id,
    _with_outputs,
)
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
from atlantide.reconcile.report import ApplyReport
from atlantide.reconcile.resolve import (
    live_outputs,
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
    NO_INPUT_HASH,
    STATUS_CREATED,
    STATUS_CREATING,
    StateGraph,
    StateNode,
)

#: A recorded undo for one completed node: (node id, coroutine factory).
Compensation = tuple[str, Callable[[], Awaitable[None]]]


def _reraise_if_cancelled(exc: BaseException) -> None:
    """Let a cancellation past untouched.

    Wrapping one in a ``ProviderError`` would turn "this task was cancelled" into
    "this resource failed": the scheduler would stop unwinding, the caller would
    report a provider fault that never happened, and structured cancellation would
    be broken for every task above.

    A ``TimeoutError`` is deliberately *not* in this class. `asyncio.timeout`
    converts its own cancellation into one precisely so it reads as a failure of
    that node rather than as an interrupt — which is what it is, and which is what
    lets the saga treat it like any other provider failure.
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc


def _output_stack(key: str) -> str:
    """The stack half of a committed output key (``"{stack}:{name}"``)."""
    return key.split(":", 1)[0]


def _noop_progress(node_id: str, action: Action, phase: str) -> None:
    pass


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
        self.live_outputs: LiveOutputs = live_outputs(prior, env.secrets)
        # Frozen copy of the prior-state seed: an undo rebuilds the *old*
        # resource against the upstream values it was applied with, not the
        # values this run's forward pass writes into ``live_outputs`` before
        # the failure (the compensations run in reverse, so each upstream's
        # own rollback restores exactly these values).
        self.prior_outputs: LiveOutputs = {
            node_id: dict(outputs) for node_id, outputs in self.live_outputs.items()
        }
        self.report = ApplyReport()
        self.ctx = Context()
        self.delete_ids = {c.node_id for c in changeset.by_action(Action.DELETE)}
        # Undos in completion order; a node completes after its dependencies, so
        # reversing undoes dependents first.
        self.compensations: list[Compensation] = []
        # node id -> prior Resource, for CBD REPLACEs whose destroy is deferred.
        self.cbd_deferred: dict[str, Resource] = {}

    async def run(self) -> ApplyReport:
        self._emit(
            RUN_START,
            planned=len(self.changes),
            actionable=sum(1 for c in self.changes.values() if c.action is not Action.NOOP),
        )
        try:
            return await self._run_phases()
        finally:
            # In a `finally` so a failed or interrupted run still closes its record.
            self._emit(
                RUN_FINISH,
                created=len(self.report.created),
                updated=len(self.report.updated),
                replaced=len(self.report.replaced),
                deleted=len(self.report.deleted),
                rolled_back=len(self.report.rolled_back),
            )

    async def _run_phases(self) -> ApplyReport:
        # Phase 1: create/update/replace/noop, dependencies first.
        #
        # `BaseException`, not `Exception`: an interrupt arrives as `CancelledError`,
        # and a narrower clause would skip the saga on Ctrl-C part-way through an
        # apply that has already created resources.
        try:
            await run_graph(self.desired.graph, self._apply_node, parallelism=self.env.parallelism)
        except BaseException as exc:
            if self.on_failure == "rollback":
                skip = self._rollback_blocker()
                if skip is not None:
                    self.report.rollback_skipped = skip
                    self._emit(ROLLBACK_SKIPPED, reason=skip)
                else:
                    self._emit(ROLLBACK_START, nodes=len(self.compensations))
                    await self._shielded_rollback()
                if self.report.rollback_failed:
                    if isinstance(exc, Exception):
                        # Grouped, not replaced: `run_async` flattens the group and
                        # renders every leaf.
                        raise ExceptionGroup(
                            "apply failed and rollback did not complete",
                            [exc, *self._rollback_errors()],
                        ) from None
                    # A cancellation must stay a cancellation: grouping would hide it
                    # from every `except CancelledError` above. `render_error` still
                    # prints the rollback failures from this attribute.
                    exc._also_failed = self._rollback_errors()  # type: ignore[attr-defined]
            raise
        # Phase 1b: destroy the prior halves of CBD REPLACEs, dependents first, once
        # the replacements are in place. Terminal.
        if self.cbd_deferred:
            await self._run(self.desired.graph, self._cbd_cleanup, reverse=True)
        # Phase 2: deletes, dependents first. Terminal: recreating a destroyed
        # resource would lose its identity and outputs.
        if self.delete_ids:
            await self._run(self.prior_graph, self._delete_node, reverse=True)
        self._commit_outputs()
        return self.report

    def _commit_outputs(self) -> None:
        """Resolve the declared exports and persist the committed stack outputs.

        Exports resolve against live outputs; unchanged nodes resolve from the
        prior-state seed set up at construction. A restricted (targeted) run
        NOOPs unselected CREATEs, so an output over one has no value anywhere
        yet — skip it, leaving its committed value untouched, rather than fail
        after the infrastructure mutations already succeeded. Sensitive exports
        are sealed at rest; the report holds them in the clear and redacts at
        the render boundary.
        """
        resolved_outputs: dict[str, Any] = {}
        for name, value in self.desired.output_decls.items():
            try:
                resolved_outputs[name] = resolve_value(value, self.live_outputs)
            except KeyError:
                continue
        self.report.outputs = resolved_outputs
        self.report.sensitive_outputs = sensitive_output_names(self.desired.output_decls, self.env)
        self.env.backend.set_outputs(self._persistable_outputs(), remove=self._retired_outputs())

    def _retired_outputs(self) -> list[str]:
        """Committed output keys this run owns but no longer declares.

        ``set_outputs`` merges, so without pruning the store is append-only: a
        dropped ``output()`` — or a destroyed stack, which declares none at all —
        keeps its last value, and a dependent stack's ``StackReference`` still
        resolves to a resource that is gone. Scoped to this run's own stacks;
        another config's outputs share the store.
        """
        mine = {stack_of(node_id) for node_id in self.desired.resources}
        mine |= {stack_of(node_id) for node_id in self.prior_state.nodes}
        # Every declared name counts, not only the resolved ones: an output that
        # could not resolve under a restricted run is still declared, and pruning
        # it would drop a committed value the config continues to claim.
        declared = set(self.report.outputs) | set(self.desired.output_decls)
        return sorted(
            key
            for key in self.env.backend.outputs()
            if _output_stack(key) in mine and key not in declared
        )

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
        self._emit(NODE_START, node_id=node_id, action=change.action)
        try:
            async with asyncio.timeout(self.env.node_timeout):
                await self._apply_one(node_id, change)
        except BaseException as exc:
            # A sibling's failure cancels this node mid-CRUD; without this the
            # progress display leaves it spinning forever.
            self.on_progress(node_id, change.action, PHASE_FAIL)
            self._emit(NODE_FAIL, node_id=node_id, action=change.action, error=str(exc))
            _reraise_if_cancelled(exc)
            raise node_failure(node_id, change.action.name.lower(), exc) from exc
        self.on_progress(node_id, change.action, PHASE_FINISH)
        self._emit(NODE_FINISH, node_id=node_id, action=change.action)

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
        undo = self._compensator(provider)
        match change.action:
            case Action.CREATE:
                self._write_ahead(node_id, res)
                created = await provider.create(self.ctx, res)
                self.live_outputs[node_id] = created
                self._record(node_id, undo.undo_create(_with_outputs(res, created), node_id))
                self.report.created.append(node_id)
            case Action.UPDATE:
                prior_node = self.prior_state.get(node_id)
                self.live_outputs[node_id] = await provider.update(
                    self.ctx, self.live_outputs.get(node_id, {}), res
                )
                if prior_node is not None:
                    # Against the prior-state snapshot: resolving against
                    # ``live_outputs`` here would bake this run's post-apply
                    # upstream values into the compensating update.
                    old = reconstruct(prior_node, self.env, self.prior_outputs)
                    prior_outputs = unseal_outputs(prior_node.outputs, self.env.secrets)
                    self._record(node_id, undo.undo_update(old, prior_node, prior_outputs))
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
        old = reconstruct(prior_node, self.env, self.live_outputs) if prior_node else res
        undo = self._compensator(provider)
        if change.create_before_destroy and prior_node is not None:
            # Create the replacement now; the prior resource is destroyed in cleanup.
            #
            # The old resource's identity is persisted under a companion id first:
            # `_write_ahead` overwrites the only row describing the still-live old
            # resource, and from then until cleanup a crash would otherwise orphan
            # it with no trace. With the companion row, the next plan reports it
            # as a DELETE — planned recovery instead of silent loss.
            self.env.lease.check()
            self.env.backend.put(replace(prior_node, id=_cbd_old_id(node_id)))
            self._write_ahead(node_id, res)
            created = await provider.create(self.ctx, res)
            self.live_outputs[node_id] = created
            self.cbd_deferred[node_id] = old
            self._record(node_id, undo.undo_cbd_create(_with_outputs(res, created), prior_node))
        else:  # destroy-before-create
            # Written before the destroy, not just before the create: until the
            # create is persisted, a `created` row would describe an already-deleted
            # resource and refs to it would resolve to a dead id.
            self._write_ahead(node_id, res)
            await provider.delete(self.ctx, old)
            created = await provider.create(self.ctx, res)
            self.live_outputs[node_id] = created
            if prior_node is not None:
                self._record(
                    node_id,
                    undo.undo_replace(
                        _with_outputs(res, created), old, self._restorer(node_id, prior_node, old)
                    ),
                )
        self.report.replaced.append(node_id)

    def _compensator(self, provider: Provider) -> Compensator:
        """Undo factories bound to this run's context and state backend."""
        return Compensator(provider, self.ctx, self.env.backend)

    def _restorer(
        self, node_id: str, prior_node: StateNode, old: Resource
    ) -> Callable[[dict[str, Any]], None]:
        """Build the callback persisting the outputs of a compensating re-create.

        The row keeps the prior node's shape, notably its ``input_hash``, so the
        next plan sees the pre-replace inputs; the outputs are the new ones, since
        the recreated resource is not the one the prior row described.
        """

        def restore(outputs: dict[str, Any]) -> None:
            self.env.backend.put(
                replace(prior_node, outputs=seal_outputs(outputs, type(old), self.env.secrets))
            )

        return restore

    async def _cbd_cleanup(self, node_id: str) -> None:
        """Destroy the prior half of a create-before-destroy REPLACE.

        Its primary state row was already replaced by the new half; the companion
        row written in :meth:`_replace` is what still describes it. On success the
        companion is dropped; on failure it stays, so the next plan reports the
        leftover as a DELETE — and the report says so loudly as well.
        """
        old = self.cbd_deferred.get(node_id)
        if old is None:
            return
        try:
            await provider_for(self.env.providers, old.provider_name()).delete(self.ctx, old)
        except Exception as exc:
            self.report.orphaned[node_id] = (
                f"the replaced {old.type_name()} could not be destroyed: {exc}"
            )
            raise
        self.env.backend.delete(_cbd_old_id(node_id))

    async def _delete_node(self, node_id: str) -> None:
        if node_id not in self.delete_ids:
            return
        self.on_progress(node_id, Action.DELETE, PHASE_START)
        try:
            prior_node = self.prior_state.get(node_id)
            assert prior_node is not None
            res = reconstruct(prior_node, self.env, self.live_outputs)
            if not isinstance(res, DataSource):
                async with asyncio.timeout(self.env.node_timeout):
                    await provider_for(self.env.providers, prior_node.provider).delete(
                        self.ctx, res
                    )
            # A data source is a lookup, not something atlantide made. Dropping
            # the row is the whole of "deleting" it; calling the provider would
            # destroy infrastructure this config only ever read.
            self._forget(node_id)
        except BaseException as exc:
            self.on_progress(node_id, Action.DELETE, PHASE_FAIL)
            _reraise_if_cancelled(exc)
            raise node_failure(node_id, "delete", exc) from exc
        self.report.deleted.append(node_id)
        self.on_progress(node_id, Action.DELETE, PHASE_FINISH)

    def _forget(self, node_id: str) -> None:
        """Drop a destroyed node's row, marking it stale if the drop fails.

        The provider call has already succeeded by this point, so the resource is
        gone; a failed ``delete`` leaves a row whose hash still matches config,
        which the next plan would skip as NOOP. Poisoning it first means the
        worst case is a spurious re-plan rather than a permanent lie.
        """
        try:
            self.env.backend.delete(node_id)
        except Exception:
            self._poison(node_id)
            raise

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
        self.env.lease.check()
        self.env.backend.put(self._state_node(node_id, res, {}, STATUS_CREATING))

    def _persist(self, node_id: str, res: Resource, outputs: dict[str, Any]) -> None:
        # Guarded rather than trusted: losing the lease cancels the run, but not
        # instantly, and a node already inside a provider call would still write.
        self.env.lease.check()
        self.env.backend.put(self._state_node(node_id, res, outputs, STATUS_CREATED))

    def _record(self, node_id: str, undo: Callable[[], Awaitable[None]]) -> None:
        if self.on_failure == "rollback":
            self.compensations.append((node_id, undo))

    def _poison(self, node_id: str) -> None:
        """Clear a node's ``input_hash`` so the next plan cannot Merkle-skip it.

        A compensation is a provider call followed by a state write, and a failure
        between the two leaves state describing a resource that is no longer
        there. Because the diff is symbolic, that row's stored hash still matches
        config, so the next plan would report NOOP and the divergence would never
        surface. :data:`NO_INPUT_HASH` is the channel through which it can —
        ``refresh --write`` already uses it for the same purpose.

        Written *before* the compensation runs, not after: poisoning afterwards
        only survives an exception, while a process killed mid-rollback leaves
        the row untouched and the divergence silent. The undo's own state write
        clears the poison when it succeeds, so the cost of being early is nothing.

        Re-read from the backend rather than from ``prior_state``: an earlier
        phase of this run may already have rewritten the row, and putting the
        stale copy back would undo that write.
        """
        current = self.env.backend.load().get(node_id)
        if current is None:  # already gone; nothing to mark
            return
        try:
            self.env.backend.put(replace(current, input_hash=NO_INPUT_HASH))
        except Exception as exc:
            self.report.poison_failed[node_id] = str(exc)

    def _emit(
        self,
        phase: str,
        *,
        node_id: str | None = None,
        action: Action | None = None,
        **detail: Any,
    ) -> None:
        """Publish one event. Never raises: observation must not break the work."""
        self.env.events(
            ApplyEvent(
                run_id=self.env.run_id,
                at=time.time(),
                phase=phase,
                node_id=node_id,
                action=action.value if action is not None else None,
                detail=detail,
            )
        )

    def _rollback_blocker(self) -> str | None:
        """Why the saga must not run, or ``None`` if it may.

        There is exactly one such reason today: this run no longer holds the state
        lock. A compensation is a provider call *and* a state write, and a run that
        lost its lease may be racing whoever took it — undoing "its" creates could
        destroy theirs. Leaving the resources in place is the lesser harm, and the
        report says so rather than staying quiet about it.
        """
        lost = self.env.lease.lost
        return str(lost) if lost is not None else None

    async def _shielded_rollback(self) -> None:
        """Run the saga so that a *second* interrupt cannot abandon it half-done.

        The first Ctrl-C is what got us here; a second one during the compensation
        would otherwise cancel it between a provider call and its state write —
        the very window :meth:`_poison` exists to make visible. Shielding costs an
        impatient operator nothing but a second press against the CLI's hard exit.
        """
        rollback = asyncio.ensure_future(self._rollback())
        try:
            await asyncio.shield(rollback)
        except asyncio.CancelledError:
            await rollback  # let it finish; the cancellation is re-raised after
            raise

    async def _rollback(self) -> None:
        """Undo completed nodes in reverse completion order, sequentially.

        Every recorded undo is attempted even if an earlier one fails. All
        attempted ids are recorded in ``report.rolled_back``; those that did not
        complete are also recorded in ``report.rollback_failed``.

        Each node's row is marked stale before its compensation runs, so a
        compensation that fails part-way is visible to the next plan rather than
        reading as NOOP. See :meth:`_poison`.
        """
        for node_id, undo in reversed(self.compensations):
            self.report.rolled_back.append(node_id)
            self._poison(node_id)
            try:
                await undo()
                self._emit(ROLLBACK_NODE, node_id=node_id, undone=True)
            except Exception as exc:
                self.report.rollback_failed[node_id] = str(exc)
                self._emit(ROLLBACK_NODE, node_id=node_id, undone=False, error=str(exc))

    def _rollback_errors(self) -> list[RollbackError]:
        return [
            RollbackError(node_id, reason)
            for node_id, reason in self.report.rollback_failed.items()
        ]
