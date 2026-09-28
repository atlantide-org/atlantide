"""One ChangeSet's execution: forward apply, CBD cleanup, deletes, saga rollback."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from atlantide.core.actions import Action
from atlantide.core.errors import ProviderError
from atlantide.core.events import (
    NODE_FAIL,
    NODE_FINISH,
    NODE_START,
    ROLLBACK_SKIPPED,
    ROLLBACK_START,
    RUN_FINISH,
    RUN_START,
)
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.graph.model import DiGraph
from atlantide.graph.schedule import run_graph
from atlantide.reconcile.changes import Change, ChangeSet
from atlantide.reconcile.env import (
    ApplyEnv,
    Desired,
    OnFailure,
    node_failure,
    provider_for,
)
from atlantide.reconcile.executor.confirm import Confirmation
from atlantide.reconcile.executor.context import reraise_if_cancelled, wire
from atlantide.reconcile.executor.deletes import Deletes
from atlantide.reconcile.executor.outputs import commit_outputs
from atlantide.reconcile.executor.replace import Replacements
from atlantide.reconcile.executor.saga import with_outputs
from atlantide.reconcile.progress import (
    Phase,
    ProgressCallback,
)
from atlantide.reconcile.report import ApplyReport
from atlantide.reconcile.resolve import (
    reconstruct,
    resolve_refs,
    resolve_secret_refs,
    resolve_stack_refs,
    unseal_outputs,
)
from atlantide.state import StateGraph
from atlantide.util.errors import attach_also_failed


class ChangeSetRun:
    """Executes one ChangeSet: forward apply, CBD cleanup, deletes, saga rollback.

    Orchestrates the phases. The run's shared mutable state (``live_outputs``,
    the ``report``, the saga's recorded compensations and deferred CBD deletes)
    lives on one :class:`~atlantide.reconcile.executor.context.RunContext`, wired
    once and shared with the collaborators: :class:`Confirmation` (the apply-time
    re-check of a conditional REPLACE), :class:`Replacements` (both REPLACE
    strategies) and :class:`Deletes` (phase 0 and every planned DELETE).
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
        self.run_ctx = wire(
            changeset=changeset,
            desired=desired,
            prior=prior,
            env=env,
            on_failure=on_failure,
            on_progress=on_progress,
        )
        self.confirmation = Confirmation(self.run_ctx)
        self.deletes = Deletes(self.run_ctx)
        self.replacements = Replacements(self.run_ctx, delete_node=self.deletes.delete_node)
        # Destroyed before the forward pass, dependents first (see `Deletes.predeletes`).
        self.run_ctx.predeleted = self.deletes.predeletes(changeset)

    async def run(self) -> ApplyReport:
        self.run_ctx.events.emit(
            RUN_START,
            planned=len(self.run_ctx.changes),
            actionable=sum(1 for c in self.run_ctx.changes.values() if c.action is not Action.NOOP),
        )
        try:
            return await self._run_phases()
        finally:
            # Also emitted for failed and interrupted runs.
            self.run_ctx.events.emit(
                RUN_FINISH,
                created=len(self.run_ctx.report.created),
                updated=len(self.run_ctx.report.updated),
                replaced=len(self.run_ctx.report.replaced),
                deleted=len(self.run_ctx.report.deleted),
                rolled_back=len(self.run_ctx.report.rolled_back),
            )

    async def _run_phases(self) -> ApplyReport:
        # Phase 0: the destroys a destroy-before-create REPLACE must wait for,
        # dependents first over the prior-state graph. A delete half records its
        # undo like phase 1 does; a DELETE, as in phase 2, is terminal.
        # Phase 1: create/update/replace/noop, dependencies first. Catches
        # `BaseException` so an interrupt (`CancelledError`) also runs the saga.
        try:
            if self.run_ctx.predeleted:
                await self._run(self.run_ctx.prior_graph, self.deletes.predelete_node, reverse=True)
            await run_graph(
                self.run_ctx.desired.graph,
                self._apply_node,
                parallelism=self.run_ctx.env.parallelism,
            )
        except BaseException as exc:
            await self._handle_forward_failure(exc)
            raise
        # Phase 2: deletes and the prior halves of CBD REPLACEs, in one pass,
        # dependents first over the prior-state graph, once the replacements are
        # in place. Terminal: recreating a destroyed resource would lose its
        # identity and outputs.
        if self.run_ctx.cbd_deferred or self.run_ctx.delete_ids:
            await self._run(self.run_ctx.prior_graph, self._destroy_node, reverse=True)
        commit_outputs(
            desired=self.run_ctx.desired,
            env=self.run_ctx.env,
            live=self.run_ctx.live_outputs,
            prior=self.run_ctx.prior_state,
            report=self.run_ctx.report,
        )
        return self.run_ctx.report

    async def _handle_forward_failure(self, exc: BaseException) -> None:
        """Run the saga for a failed forward pass; the caller re-raises ``exc``.

        Raises an ``ExceptionGroup`` instead when the rollback itself did not
        complete and ``exc`` is an ordinary exception.
        """
        if not self.run_ctx.saga.enabled:
            return
        skip = self.run_ctx.saga.blocker()
        if skip is not None:
            self.run_ctx.report.rollback_skipped = skip
            self.run_ctx.events.emit(ROLLBACK_SKIPPED, reason=skip)
        else:
            self.run_ctx.events.emit(ROLLBACK_START, nodes=len(self.run_ctx.saga.compensations))
            await self.run_ctx.saga.rollback_shielded()
        if not self.run_ctx.report.rollback_failed:
            return
        if isinstance(exc, Exception):
            # `run_async` flattens the group and renders every leaf.
            raise ExceptionGroup(
                "apply failed and rollback did not complete", [exc, *self.run_ctx.saga.errors()]
            ) from None
        # A cancellation stays a cancellation: grouping would hide it from
        # `except CancelledError` handlers. `render_error` prints the attached
        # rollback failures.
        attach_also_failed(exc, self.run_ctx.saga.errors())

    async def _run(
        self, graph: DiGraph, step: Callable[[str], Awaitable[None]], *, reverse: bool = False
    ) -> None:
        await run_graph(graph, step, parallelism=self.run_ctx.env.parallelism, reverse=reverse)

    # Per-node phases

    async def _apply_node(self, node_id: str) -> None:
        change = self.run_ctx.changes[node_id]
        if change.action is Action.NOOP and not change.state_only:
            self.run_ctx.report.noop.append(node_id)
            return
        # A state-only NOOP is reported like any written node, so the audit log
        # records who changed a resource's protection.
        detail: dict[str, Any] = {"state_only": True} if change.state_only else {}
        self.run_ctx.events.phase(node_id, change.action, Phase.START, NODE_START, **detail)
        # What actually runs: a conditional REPLACE may turn out to be an UPDATE
        # or a NOOP once its refs resolve.
        effective = change
        try:
            async with asyncio.timeout(self.run_ctx.env.node_timeout):
                # A node whose delete half ran in phase 0 is past confirming:
                # downgrading it now would leave it destroyed and uncreated.
                if change.conditional and node_id not in self.run_ctx.predeleted:
                    effective = self.confirmation.confirmed(node_id, change)
                await self._apply_one(node_id, effective)
        except BaseException as exc:
            # Also covers cancellation by a sibling's failure, so progress does
            # not show the node as still running.
            self.run_ctx.events.phase(
                node_id, effective.action, Phase.FAIL, NODE_FAIL, error=str(exc)
            )
            reraise_if_cancelled(exc)
            raise node_failure(node_id, effective.action.name.lower(), exc) from exc
        if effective.action is not change.action:
            # Report what ran, and what had been planned.
            self.run_ctx.report.downgraded[node_id] = effective.action.value
            detail = {"planned": change.action.value}
        self.run_ctx.events.phase(node_id, effective.action, Phase.FINISH, NODE_FINISH, **detail)

    async def _apply_one(self, node_id: str, change: Change) -> None:
        if change.action is Action.NOOP:  # no provider call, only the state row
            if change.state_only:
                await self.confirmation.record_state_only(node_id, change)
            else:  # a conditional REPLACE in which no value moved
                await self.confirmation.record_collapsed(node_id)
            return
        # Stack outputs first: one inside a Transform must be in place before
        # `resolve_refs` evaluates it.
        res = resolve_secret_refs(
            resolve_refs(
                resolve_stack_refs(
                    self.run_ctx.desired.resources[node_id], self.run_ctx.env.stack_outputs
                ),
                self.run_ctx.live_outputs,
            ),
            self.run_ctx.env.secrets,
        )
        provider = provider_for(self.run_ctx.env.providers, res.provider_name())
        match change.action:
            case Action.CREATE:
                await self._create(node_id, res, provider)
            case Action.UPDATE:
                await self._update(node_id, res, provider)
            case Action.REPLACE:
                await self.replacements.replace(node_id, change, res, provider)
            case _:
                raise ProviderError(f"unexpected {change.action} for {node_id!r} in apply phase")
        await self.run_ctx.records.persist(node_id, res, self.run_ctx.live_outputs[node_id])

    async def _create(self, node_id: str, res: Resource, provider: Provider) -> None:
        await self.run_ctx.records.write_ahead(node_id, res)
        created = await provider.create(self.run_ctx.ctx, res)
        self.run_ctx.live_outputs[node_id] = created
        undo = self.run_ctx.compensator(provider)
        self.run_ctx.saga.record(node_id, undo.undo_create(with_outputs(res, created), node_id))
        self.run_ctx.report.created.append(node_id)

    async def _update(self, node_id: str, res: Resource, provider: Provider) -> None:
        prior_node = self.run_ctx.prior_state.get(node_id)
        self.run_ctx.live_outputs[node_id] = await provider.update(
            self.run_ctx.ctx, self.run_ctx.live_outputs.get(node_id, {}), res
        )
        if prior_node is not None:
            # Resolved against the prior-state snapshot: ``live_outputs`` would
            # put this run's post-apply upstream values into the compensating
            # update.
            old = reconstruct(prior_node, self.run_ctx.env, self.run_ctx.prior_outputs)
            prior_outputs = unseal_outputs(prior_node.outputs, self.run_ctx.env.secrets)
            undo = self.run_ctx.compensator(provider)
            self.run_ctx.saga.record(node_id, undo.undo_update(old, prior_node, prior_outputs))
        self.run_ctx.report.updated.append(node_id)

    async def _destroy_node(self, node_id: str) -> None:
        """Phase 2: a CBD REPLACE's cleanup, or a planned delete."""
        if node_id in self.run_ctx.cbd_deferred:
            await self.replacements.cbd_cleanup(node_id)
        else:
            await self.deletes.delete_node(node_id)
