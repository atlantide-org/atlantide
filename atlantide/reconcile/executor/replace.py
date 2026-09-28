"""The two REPLACE strategies, and the cleanup a create-before-destroy one defers.

Destroy-before-create (the default) deletes the old resource, then creates the
replacement. Create-before-destroy creates the replacement in the forward pass
and leaves the old one to :meth:`Replacements.cbd_cleanup` in phase 2.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

from atlantide.core.errors import ProviderError
from atlantide.core.provider import Provider
from atlantide.core.resource import Resource
from atlantide.reconcile.changes import Change
from atlantide.reconcile.env import provider_for
from atlantide.reconcile.executor.context import RunContext
from atlantide.reconcile.executor.saga import with_outputs
from atlantide.reconcile.resolve import cbd_companion_id, reconstruct
from atlantide.state import StateNode


class Replacements:
    """Runs one run's REPLACEs; ``delete_node`` brings a planned DELETE forward."""

    def __init__(self, run_ctx: RunContext, delete_node: Callable[[str], Awaitable[None]]) -> None:
        self.run_ctx = run_ctx
        self.delete_node = delete_node

    async def replace(
        self, node_id: str, change: Change, res: Resource, provider: Provider
    ) -> None:
        run_ctx = self.run_ctx
        prior_node = run_ctx.prior_state.get(node_id)
        old = reconstruct(prior_node, run_ctx.env, run_ctx.live_outputs) if prior_node else res
        if change.create_before_destroy and prior_node is not None:
            await self._replace_cbd(node_id, res, old, prior_node, provider)
        else:
            await self._replace_dbc(node_id, res, old, prior_node, provider)
        run_ctx.report.replaced.append(node_id)

    async def _replace_cbd(
        self, node_id: str, res: Resource, old: Resource, prior_node: StateNode, provider: Provider
    ) -> None:
        """Create-before-destroy: create the replacement now; cleanup destroys ``old``.

        The old resource's row is first copied to a companion id: ``write_ahead``
        overwrites the only row describing the still-live old resource, and a crash
        before cleanup would otherwise leave it untracked. The next plan reports the
        companion row as a DELETE.

        A companion left by an earlier replace whose cleanup failed is destroyed
        first (its planned DELETE, brought forward): overwriting its row would
        leave that older resource live and untracked.
        """
        run_ctx = self.run_ctx
        companion = replace(prior_node, id=cbd_companion_id(node_id))
        if companion.id in run_ctx.delete_ids:
            await self.delete_node(companion.id)
        elif (
            run_ctx.prior_state.get(companion.id) is not None
            and companion.id not in run_ctx.predeleted
        ):
            raise ProviderError(
                f"{companion.id!r} still tracks the resource an earlier replace of "
                f"{node_id!r} left behind, and this run does not destroy it; apply "
                f"without --target first, which destroys it"
            )
        await run_ctx.writer.run(run_ctx.records.checked_put, companion, key=companion.id)
        await run_ctx.records.write_ahead(node_id, res)
        created = await provider.create(run_ctx.ctx, res)
        run_ctx.live_outputs[node_id] = created
        run_ctx.cbd_deferred[node_id] = old
        undo = run_ctx.compensator(provider)
        run_ctx.saga.record(node_id, undo.undo_cbd_create(with_outputs(res, created), prior_node))

    async def _replace_dbc(
        self,
        node_id: str,
        res: Resource,
        old: Resource,
        prior_node: StateNode | None,
        provider: Provider,
    ) -> None:
        """Destroy-before-create: delete ``old``, then create the replacement.

        The write-ahead row goes in before the destroy, not just before the
        create: until the create is persisted, a ``created`` row would describe an
        already-deleted resource and refs to it would resolve to a dead id. A
        pre-deleted node had both done in phase 0 (see
        :meth:`~atlantide.reconcile.executor.deletes.Deletes._predestroy`).
        """
        run_ctx = self.run_ctx
        if node_id not in run_ctx.predeleted:
            await run_ctx.records.write_ahead(node_id, res)
            await provider.delete(run_ctx.ctx, old)
        created = await provider.create(run_ctx.ctx, res)
        run_ctx.live_outputs[node_id] = created
        if node_id in run_ctx.predeleted:
            # Phase 0 recorded recreating `old`; this undo runs before it.
            undo = run_ctx.compensator(provider)
            run_ctx.saga.record(node_id, undo.undo_create(with_outputs(res, created), node_id))
        elif prior_node is not None:
            # A rollback re-creates `old`, which was resolved against the outputs
            # as they are now: its row records those values, not the prior row's.
            restored = replace(
                prior_node,
                ref_digests=run_ctx.records.ref_digests(prior_node.type, prior_node.properties),
            )
            undo = run_ctx.compensator(provider)
            run_ctx.saga.record(
                node_id,
                undo.undo_replace(with_outputs(res, created), old, run_ctx.restorer(restored, old)),
            )

    async def cbd_cleanup(self, node_id: str) -> None:
        """Destroy the prior half of a create-before-destroy REPLACE.

        Its primary state row was already replaced by the new half; the companion
        row written in :meth:`_replace_cbd` is what still describes it. On success
        the companion is dropped; on failure it stays, so the next plan reports
        the leftover as a DELETE, and the report records it under ``orphaned``.
        """
        run_ctx = self.run_ctx
        old = run_ctx.cbd_deferred.get(node_id)
        if old is None:
            return
        try:
            await provider_for(run_ctx.env.providers, old.provider_name()).delete(run_ctx.ctx, old)
        except Exception as exc:
            run_ctx.report.orphaned[node_id] = (
                f"the replaced {old.type_name()} could not be destroyed: {exc}"
            )
            raise
        old_id = cbd_companion_id(node_id)
        await run_ctx.writer.run(run_ctx.env.backend.delete, old_id, key=old_id)
