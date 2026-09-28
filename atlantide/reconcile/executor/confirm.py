"""The apply-time re-check of a conditional REPLACE, and its row-only outcomes.

A conditional REPLACE is re-diffed once its upstreams have applied
(:meth:`Confirmation.confirmed`). When nothing immutable moved it runs as an
UPDATE, or as a NOOP whose row is rewritten without a provider call
(:meth:`Confirmation.record_collapsed`); a ``prevent_destroy`` change alone is
also a row-only write (:meth:`Confirmation.record_state_only`).
"""

from __future__ import annotations

from dataclasses import replace

from atlantide.core.actions import Action
from atlantide.core.errors import PreventDestroyError
from atlantide.core.fields import field_mutability
from atlantide.reconcile.changes import Change
from atlantide.reconcile.classify import reclassify
from atlantide.reconcile.executor.context import RunContext
from atlantide.reconcile.executor.saga import restore_row
from atlantide.reconcile.resolve import resolve_properties
from atlantide.state import StateNode


class Confirmation:
    """Confirms conditional REPLACEs, and writes the rows no provider call produced."""

    def __init__(self, run_ctx: RunContext) -> None:
        self.run_ctx = run_ctx

    def confirmed(self, node_id: str, change: Change) -> Change:
        """Re-diff a conditional REPLACE now that its upstreams have applied.

        Its ``$ref`` fields resolve against the upstreams' new outputs and are
        compared with the row's ``ref_digests``, the values the node was last
        applied with. The run-start outputs are not a substitute: a run that
        moved an upstream and stopped before this node left them moved. Only a
        field the row records nothing for (a literal, or a row written before the
        record) falls back to the stored row resolved against the run-start
        outputs. :func:`reclassify` then classifies the concrete values with the
        diff's own rules. A pure computation: nothing is written and no provider
        is called, so a failure here leaves the row as it was for the next plan
        to re-examine.

        A confirmed replace of a node the config protects is refused here, before
        any provider call on it: the planner deferred that verdict to apply (see
        :func:`~atlantide.reconcile.guards.deferred_to_apply`).
        """
        run_ctx = self.run_ctx
        prior_node = run_ctx.prior_state.get(node_id)
        assert prior_node is not None and change.desired is not None  # a REPLACE has both
        fresh = reclassify(
            change,
            desired_properties=resolve_properties(change.desired.properties, run_ctx.live_outputs),
            prior_properties=resolve_properties(
                prior_node.properties, run_ctx.prior_outputs, strict=False
            ),
            mutability=field_mutability(type(run_ctx.desired.resources[node_id])),
            recorded=prior_node.ref_digests,
            matches=run_ctx.records.matches,
        )
        if fresh.action is Action.REPLACE and change.desired.prevent_destroy:
            raise PreventDestroyError(
                f"prevent_destroy blocks destroying: {node_id} (its replacement was "
                f"known only after apply, and an immutable value changed)"
            )
        return fresh

    async def record_state_only(self, node_id: str, change: Change) -> None:
        """Persist a new ``prevent_destroy`` on an otherwise unchanged row.

        The Merkle hash does not cover the flag, so the stored hash, properties
        and outputs are already current; only the flag is rewritten.
        """
        run_ctx = self.run_ctx
        prior_node = run_ctx.prior_state.get(node_id)
        assert prior_node is not None and change.desired is not None  # a NOOP has both
        row = replace(prior_node, prevent_destroy=change.desired.prevent_destroy)
        await self._rewrite(prior_node, row)
        run_ctx.report.noop.append(node_id)
        run_ctx.report.state_only.append(node_id)

    async def record_collapsed(self, node_id: str) -> None:
        """Persist a conditional REPLACE that turned out to change nothing.

        The resource is untouched, but the config did change (an upstream did,
        and the Merkle hash folds it in), so the row takes this config's hash,
        properties and flags: the next plan then Merkle-skips the node instead of
        re-offering the replace. Outputs, provider version and secret digests stay
        the prior row's, since no provider call produced new ones; a rotated secret
        still shows up in the next plan's audit.
        """
        run_ctx = self.run_ctx
        prior_node = run_ctx.prior_state.get(node_id)
        assert prior_node is not None  # a REPLACE has a prior row
        await self._rewrite(prior_node, run_ctx.records.collapsed_row(prior_node))
        run_ctx.report.noop.append(node_id)

    async def _rewrite(self, prior_node: StateNode, row: StateNode) -> None:
        """Write a row no provider call produced, undoable by restoring ``prior_node``.

        The undo is recorded first: restoring the prior row is harmless if the
        write never lands, and a cancelled write may still land.
        """
        self.run_ctx.saga.record(prior_node.id, restore_row(prior_node))
        await self.run_ctx.records.rewrite(row)
