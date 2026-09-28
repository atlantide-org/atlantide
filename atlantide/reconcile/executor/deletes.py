"""A run's destroys: which run before the forward pass, and the deletes themselves.

Phase 0 destroys what a destroy-before-create REPLACE must wait for
(:meth:`Deletes.predeletes`); a planned DELETE runs at most once, in phase 0,
brought forward by a create-before-destroy replace, or in phase 2.
"""

from __future__ import annotations

import asyncio

from atlantide.core.actions import Action
from atlantide.core.events import NODE_FAIL, NODE_START
from atlantide.core.resource import DataSource
from atlantide.reconcile.changes import Change, ChangeSet
from atlantide.reconcile.env import node_failure, provider_for
from atlantide.reconcile.executor.context import RunContext, reraise_if_cancelled
from atlantide.reconcile.progress import Phase
from atlantide.reconcile.resolve import reconstruct


class Deletes:
    """Phase 0's destroys and every planned DELETE, over one run's context."""

    def __init__(self, run_ctx: RunContext) -> None:
        self.run_ctx = run_ctx

    def predeletes(self, changeset: ChangeSet) -> frozenset[str]:
        """Nodes to destroy before the forward pass, so no delete precedes a dependent's.

        A destroy-before-create REPLACE deletes its old resource in the forward
        pass. Whatever depended on that resource in prior state and is itself
        destroyed this run (a DELETE, or the delete half of a destroy-before-create
        REPLACE) must be gone first, and so, transitively, must its own dependents.
        Every other delete keeps its place after the forward pass, so a failed
        forward pass leaves it undone.

        A conditional REPLACE is left in place: it is confirmed only once its
        upstreams have applied. The diff never leaves one behind a
        destroy-before-create upstream it refers to through an ``immutable()``
        field (see :func:`~atlantide.reconcile.ordering.behind_destroy_first`), so
        what remains here is a replace that may still collapse.
        """
        run_ctx = self.run_ctx

        def dbc(change: Change) -> bool:
            return (
                change.action is Action.REPLACE
                and not change.create_before_destroy
                and run_ctx.prior_state.get(change.node_id) is not None
            )

        def destroyed(change: Change) -> bool:
            if change.action is Action.DELETE:
                return True
            return dbc(change) and not change.conditional

        found: set[str] = set()
        pending = [c.node_id for c in changeset.changes if dbc(c)]
        while pending:
            node_id = pending.pop()
            if node_id not in run_ctx.prior_graph:
                continue
            for dependent in run_ctx.prior_graph.successors(node_id, reverse=False):
                change = run_ctx.changes.get(dependent)
                if dependent not in found and change is not None and destroyed(change):
                    found.add(dependent)
                    pending.append(dependent)
        return frozenset(found)

    async def predelete_node(self, node_id: str) -> None:
        """Phase 0: destroy a node ahead of the forward pass (see :meth:`predeletes`)."""
        if node_id not in self.run_ctx.predeleted:
            return
        if node_id in self.run_ctx.delete_ids:
            await self.delete_node(node_id)
        else:
            await self._predestroy(node_id)

    async def _predestroy(self, node_id: str) -> None:
        """Delete the old half of a destroy-before-create REPLACE; phase 1 creates the new.

        The write-ahead row goes in first, as in
        :meth:`~atlantide.reconcile.executor.replace.Replacements._replace_dbc`. Its
        resource is the unresolved desired one, since the upstreams it refers to
        may not exist yet; the persist after the create writes the full row.
        The saga records an undo recreating the original once the delete is
        done, so a later failure in this or the forward pass rolls it back; the
        create, when it runs, records the undo removing the replacement. Progress
        reports the node's start and finish from phase 1, or its failure here.
        """
        run_ctx = self.run_ctx
        prior_node = run_ctx.prior_state.get(node_id)
        assert prior_node is not None  # a destroy-before-create REPLACE has a prior row
        try:
            async with asyncio.timeout(run_ctx.env.node_timeout):
                old = reconstruct(prior_node, run_ctx.env, run_ctx.live_outputs)
                provider = provider_for(run_ctx.env.providers, prior_node.provider)
                await run_ctx.records.write_ahead(node_id, run_ctx.desired.resources[node_id])
                await provider.delete(run_ctx.ctx, old)
                undo = run_ctx.compensator(provider)
                run_ctx.saga.record(
                    node_id, undo.undo_delete(old, run_ctx.restorer(prior_node, old))
                )
        except BaseException as exc:
            run_ctx.events.phase(node_id, Action.REPLACE, Phase.START, NODE_START)
            run_ctx.events.phase(node_id, Action.REPLACE, Phase.FAIL, NODE_FAIL, error=str(exc))
            reraise_if_cancelled(exc)
            raise node_failure(node_id, "replace", exc) from exc

    async def delete_node(self, node_id: str) -> None:
        """Destroy a DELETE node and drop its row. Runs at most once per node:
        phase 0 or a CBD replace may bring it forward, and phase 2 then skips it."""
        run_ctx = self.run_ctx
        if node_id not in run_ctx.delete_ids:
            return
        run_ctx.delete_ids.discard(node_id)
        run_ctx.events.phase(node_id, Action.DELETE, Phase.START)
        try:
            prior_node = run_ctx.prior_state.get(node_id)
            assert prior_node is not None
            res = reconstruct(prior_node, run_ctx.env, run_ctx.live_outputs)
            if not isinstance(res, DataSource):
                async with asyncio.timeout(run_ctx.env.node_timeout):
                    await provider_for(run_ctx.env.providers, prior_node.provider).delete(
                        run_ctx.ctx, res
                    )
            # A data source is a lookup: deleting it only drops the row, since a
            # provider delete would destroy infrastructure this config only reads.
            await run_ctx.records.forget(node_id)
        except BaseException as exc:
            run_ctx.events.phase(node_id, Action.DELETE, Phase.FAIL)
            reraise_if_cancelled(exc)
            raise node_failure(node_id, "delete", exc) from exc
        run_ctx.report.deleted.append(node_id)
        run_ctx.events.phase(node_id, Action.DELETE, Phase.FINISH)
