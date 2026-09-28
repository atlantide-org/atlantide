"""The compensation saga: undo factories, and running them when a run fails.

Each completed node records an undo; on failure (with ``on_failure="rollback"``)
they run in reverse completion order. The undo factories close over provider
and context alone and never touch the run's shared mutable state; their state
writes go through the :class:`NodeRecords` the rollback hands them, which checks
the lease before each one.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from atlantide.core.context import Context
from atlantide.core.errors import RollbackError
from atlantide.core.events import ROLLBACK_NODE
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.reconcile.executor.records import NodeRecords
from atlantide.reconcile.report import ApplyReport
from atlantide.reconcile.resolve import cbd_companion_id
from atlantide.state import LeaseGuard, StateNode

#: A built undo: an async callable writing state through the records it is given.
type Undo = Callable[[NodeRecords], Awaitable[None]]

#: A recorded undo for one completed node: (node id, coroutine factory).
type Compensation = tuple[str, Undo]

#: Publishes one run event: ``(phase, *, node_id=..., **detail)``.
type Emit = Callable[..., None]


def with_outputs(res: Resource, outputs: dict[str, Any]) -> Resource:
    """``res`` with the computed outputs of the create (notably its id) restored.

    A compensation deletes the resource just created; the id lets the provider act
    on it directly rather than locating it by attributes, which can match an
    unrelated resource sharing those attributes (e.g. a VPC CIDR).
    """
    fields = type(res).model_fields
    updates = {key: value for key, value in outputs.items() if key in fields}
    return res.model_copy(update=updates) if updates else res


@dataclass(frozen=True, slots=True)
class Compensator:
    """Provider and context shared by every undo, bound once per applied node.

    Each undo is a provider call followed by the state write that realigns the
    recorded state with the provider. That write goes through the rollback's
    :class:`NodeRecords`, lease-checked, not straight to the backend: the lease can
    be lost while the provider call is in flight.
    """

    provider: Provider
    ctx: Context

    def undo_create(self, res: Resource, node_id: str) -> Undo:
        async def undo(records: NodeRecords) -> None:
            await self.provider.delete(self.ctx, res)
            await records.drop(node_id)

        return undo

    def undo_update(
        self, old: Resource, prior_node: StateNode, prior_outputs: dict[str, Any]
    ) -> Undo:
        async def undo(records: NodeRecords) -> None:
            await self.provider.update(self.ctx, prior_outputs, old)  # plaintext for the provider
            await records.rewrite(prior_node)  # prior row verbatim, still sealed

        return undo

    def undo_replace(
        self, new: Resource, old: Resource, restore: Callable[[dict[str, Any]], StateNode]
    ) -> Undo:
        """Undo a destroy-before-create REPLACE by recreating the original.

        ``restore`` builds the row recording the re-create's own outputs, written
        like every other undo's row. The prior state row cannot
        be written back verbatim: it names a physical id that no longer exists, so
        refresh would report the node MISSING and the recreated resource would be
        untracked.
        """

        async def undo(records: NodeRecords) -> None:
            await self.provider.delete(self.ctx, new)
            recreated = await self.provider.create(self.ctx, old)  # a fresh id, not the prior one
            await records.rewrite(restore(recreated))

        return undo

    def undo_delete(self, old: Resource, restore: Callable[[dict[str, Any]], StateNode]) -> Undo:
        """Undo the delete half of a destroy-before-create REPLACE run ahead of its create.

        Recreates the original as :meth:`undo_replace` does; the replacement, if it
        was created, has its own ``undo_create``, which runs first.
        """

        async def undo(records: NodeRecords) -> None:
            recreated = await self.provider.create(self.ctx, old)  # a fresh id, not the prior one
            await records.rewrite(restore(recreated))

        return undo

    def undo_cbd_create(self, new: Resource, prior_node: StateNode) -> Undo:
        """Undo a create-before-destroy REPLACE's forward half.

        The old resource is still live (its deletion is deferred to cleanup), so
        undo removes the freshly-created replacement and restores the prior state
        row. The companion row is dropped with it: once the primary row describes
        the old resource again, a leftover companion would plan a DELETE of that
        same live resource.
        """

        async def undo(records: NodeRecords) -> None:
            await self.provider.delete(self.ctx, new)
            await records.rewrite(prior_node)
            await records.drop(cbd_companion_id(prior_node.id))

        return undo


def restore_row(prior_node: StateNode) -> Undo:
    """Undo a state-only write: put the prior row back verbatim.

    Nothing was done at the provider, so there is nothing else to undo. The row
    is written through the rollback's records, lease-checked.
    """

    async def undo(records: NodeRecords) -> None:
        await records.rewrite(prior_node)

    return undo


class Saga:
    """One run's recorded undos, and the rollback that runs them.

    Records nothing unless the run asked for ``on_failure="rollback"``.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        lease: LeaseGuard,
        report: ApplyReport,
        records: NodeRecords,
        emit: Emit,
    ) -> None:
        self.enabled = enabled
        self.lease = lease
        self.report = report
        self.records = records
        self.emit = emit
        # Undos in completion order; a node completes after its dependencies, so
        # reversing undoes dependents first.
        self.compensations: list[Compensation] = []

    def record(self, node_id: str, undo: Undo) -> None:
        if self.enabled:
            self.compensations.append((node_id, undo))

    def blocker(self) -> str | None:
        """Why the saga must not run, or ``None`` if it may.

        The saga is blocked when this run no longer holds the state lease. A
        compensation is a provider call and a state write, and the new lease holder
        may be acting on the same resources. The resources stay in place and the
        caller records the reason in the report.
        """
        lost = self.lease.lost
        return str(lost) if lost is not None else None

    async def rollback_shielded(self) -> None:
        """Run the saga shielded, so a further cancellation cannot abandon it half-done.

        The rollback starts after the first interrupt. Another cancellation, however
        many arrive (a second Ctrl-C, then a lease-loss cancel), would otherwise stop
        it between a provider call and its state write (see
        :meth:`NodeRecords.mark_stale`). A second Ctrl-C still hard-exits via the CLI.
        """
        rollback = asyncio.ensure_future(self.rollback())
        try:
            await asyncio.shield(rollback)
        except asyncio.CancelledError:
            # Finish, then re-raise the cancellation. Awaiting the task unshielded
            # would let the next cancellation cancel the rollback itself.
            while not rollback.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.wait((rollback,))
            rollback.result()
            raise

    async def rollback(self) -> None:
        """Undo completed nodes in reverse completion order, sequentially.

        Every recorded undo is attempted even if an earlier one fails, unless the
        lease is lost part-way: the undos not yet run are then not attempted (see
        :meth:`blocker`) and are recorded in ``report.rollback_failed`` only. All
        attempted ids are recorded in ``report.rolled_back``; those that did not
        complete are also recorded in ``report.rollback_failed``.

        Each node's row is marked stale before its compensation runs, so a
        compensation that fails part-way is visible to the next plan rather than
        reading as NOOP. See :meth:`NodeRecords.mark_stale`. State is read once
        for all of them: each node's row is written only by its own compensation.
        If that read fails, no row can be marked, and each node records the failure
        in ``report.poison_failed``; the compensations still run.
        """
        rows = await self._rows()
        for node_id, undo in reversed(self.compensations):
            if (lost := self.blocker()) is not None:
                reason = f"not attempted: {lost}"
                self.report.rollback_failed[node_id] = reason
                self.emit(ROLLBACK_NODE, node_id=node_id, undone=False, error=reason)
                continue
            self.report.rolled_back.append(node_id)
            if isinstance(rows, Exception):
                self.report.poison_failed[node_id] = f"state could not be read: {rows}"
            else:
                await self.records.writer.run(self.records.mark_stale, node_id, rows, key=node_id)
            try:
                await undo(self.records)
                self.emit(ROLLBACK_NODE, node_id=node_id, undone=True)
            except Exception as exc:
                self.report.rollback_failed[node_id] = str(exc)
                self.emit(ROLLBACK_NODE, node_id=node_id, undone=False, error=str(exc))

    async def _rows(self) -> Mapping[str, StateNode] | Exception:
        """The backend's rows for :meth:`NodeRecords.mark_stale`, or why they could
        not be read.

        Read once rather than per node: a backend outage is a common cause of the
        forward failure, and retrying the read for every node of a large rollback
        would only multiply its timeouts.
        """
        if not self.compensations:
            return {}
        try:
            return (await self.records.load()).nodes
        except Exception as exc:
            return exc

    def errors(self) -> list[RollbackError]:
        return [
            RollbackError(node_id, reason)
            for node_id, reason in self.report.rollback_failed.items()
        ]
