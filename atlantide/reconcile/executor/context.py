"""What one ChangeSet run shares across its collaborators, wired once per run.

:class:`RunContext` holds the run's inputs and its shared mutable state
(``live_outputs``, the ``report``, the saga's recorded compensations, deferred
CBD deletes, pending deletes); :func:`wire` builds it. The orchestrator
(:class:`~atlantide.reconcile.executor.run.ChangeSetRun`) and its collaborators
(:mod:`~atlantide.reconcile.executor.confirm`,
:mod:`~atlantide.reconcile.executor.replace`,
:mod:`~atlantide.reconcile.executor.deletes`) all hold the same instance.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from atlantide.core.actions import Action
from atlantide.core.context import Context
from atlantide.core.events import ApplyEvent
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.graph.model import DiGraph
from atlantide.reconcile.changes import Change, ChangeSet
from atlantide.reconcile.env import ApplyEnv, Desired, LiveOutputs, OnFailure
from atlantide.reconcile.executor.records import NodeRecords
from atlantide.reconcile.executor.saga import Compensator, Saga
from atlantide.reconcile.progress import Phase, ProgressCallback
from atlantide.reconcile.report import ApplyReport
from atlantide.reconcile.resolve import live_outputs, seal_outputs
from atlantide.reconcile.state_ir import state_digraph
from atlantide.reconcile.writer import StateWriter, writer_for
from atlantide.state import StateGraph, StateNode


def reraise_if_cancelled(exc: BaseException) -> None:
    """Re-raise ``exc`` unchanged if it is a cancellation.

    Wrapping a cancellation in a ``ProviderError`` would report a provider fault,
    stop the scheduler from unwinding, and break structured cancellation for every
    enclosing task.

    ``asyncio.timeout`` converts its own cancellation into a ``TimeoutError``, so a
    timeout is handled as an ordinary node failure, including by the saga.
    """
    if isinstance(exc, asyncio.CancelledError):
        raise exc


class RunEvents:
    """Where a run reports: the progress callback and the event sink."""

    def __init__(self, env: ApplyEnv, on_progress: ProgressCallback) -> None:
        self.env = env
        self.on_progress = on_progress

    def phase(
        self, node_id: str, action: Action, phase: Phase, event: str | None = None, **detail: Any
    ) -> None:
        """Report one node's phase: the progress callback first, then ``event`` if given.

        The channels stay independent, so a caller listening on both does not see
        each update twice. Deletes pass no ``event`` and report progress only.
        """
        self.on_progress(node_id, action, phase)
        if event is not None:
            self.emit(event, node_id=node_id, action=action, **detail)

    def emit(
        self,
        phase: str,
        *,
        node_id: str | None = None,
        action: Action | None = None,
        **detail: Any,
    ) -> None:
        """Publish one event. Never raises, so observation cannot fail the run."""
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


@dataclass(slots=True, kw_only=True)
class RunContext:
    """One run's inputs and shared mutable state, built by :func:`wire`."""

    desired: Desired
    env: ApplyEnv
    events: RunEvents
    changes: dict[str, Change]
    prior_state: StateGraph
    prior_graph: DiGraph
    live_outputs: LiveOutputs
    # Snapshot of the prior-state seed: an undo rebuilds the old resource
    # against the upstream values it was applied with, not the values the
    # forward pass writes into ``live_outputs``. Compensations run in
    # reverse, so each upstream's rollback restores these values.
    prior_outputs: LiveOutputs
    report: ApplyReport
    writer: StateWriter
    ctx: Context
    delete_ids: set[str]
    records: NodeRecords
    saga: Saga
    # node id -> prior Resource, for CBD REPLACEs whose destroy is deferred.
    cbd_deferred: dict[str, Resource] = field(default_factory=dict)
    # Destroyed before the forward pass, dependents first (see
    # :meth:`~atlantide.reconcile.executor.deletes.Deletes.predeletes`). Set once
    # the collaborators are wired.
    predeleted: frozenset[str] = frozenset()

    def compensator(self, provider: Provider) -> Compensator:
        """Undo factories bound to this run's context."""
        return Compensator(provider, self.ctx)

    def restorer(
        self, prior_node: StateNode, old: Resource
    ) -> Callable[[dict[str, Any]], StateNode]:
        """Build the callback making the row that records a compensating re-create.

        The row keeps the prior node's shape, notably its ``input_hash``, so the
        next plan sees the pre-replace inputs; the outputs are the new ones, since
        the recreated resource is not the one the prior row described. The undo
        writes it through the rollback's records, lease-checked.
        """

        def restore(outputs: dict[str, Any]) -> StateNode:
            return replace(prior_node, outputs=seal_outputs(outputs, type(old), self.env.secrets))

        return restore


def wire(
    *,
    changeset: ChangeSet,
    desired: Desired,
    prior: StateGraph,
    env: ApplyEnv,
    on_failure: OnFailure,
    on_progress: ProgressCallback,
) -> RunContext:
    """Build one run's :class:`RunContext`; ``predeleted`` is left for the caller.

    Built in a fixed order, so which failure surfaces first does not change.
    """
    events = RunEvents(env, on_progress)
    changes = {c.node_id: c for c in changeset.changes}
    prior_graph = state_digraph(prior)
    outputs: LiveOutputs = live_outputs(prior, env.secrets)
    prior_outputs: LiveOutputs = {node_id: dict(values) for node_id, values in outputs.items()}
    report = ApplyReport()
    writer = writer_for(env.backend)
    ctx = Context()
    delete_ids = {c.node_id for c in changeset.by_action(Action.DELETE)}
    records = NodeRecords(
        desired=desired,
        env=env,
        writer=writer,
        report=report,
        live=outputs,
    )
    saga = Saga(
        enabled=on_failure == "rollback",
        lease=env.lease,
        report=report,
        records=records,
        emit=events.emit,
    )
    return RunContext(
        desired=desired,
        env=env,
        events=events,
        changes=changes,
        prior_state=prior,
        prior_graph=prior_graph,
        live_outputs=outputs,
        prior_outputs=prior_outputs,
        report=report,
        writer=writer,
        ctx=ctx,
        delete_ids=delete_ids,
        records=records,
        saga=saga,
    )
