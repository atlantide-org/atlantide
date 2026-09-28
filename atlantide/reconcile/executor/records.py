"""A run's state rows: what each node's row holds, and the writes that land it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from atlantide.core.resource import Resource
from atlantide.reconcile.applied import matches, ref_digests
from atlantide.reconcile.env import ApplyEnv, Desired, LiveOutputs
from atlantide.reconcile.report import ApplyReport
from atlantide.reconcile.resolve import seal_outputs, secret_digests
from atlantide.reconcile.writer import StateWriter
from atlantide.state import (
    NO_INPUT_HASH,
    NodeStatus,
    StateGraph,
    StateNode,
)


class NodeRecords:
    """Builds and writes one run's state rows, fenced by the run's lease.

    ``live`` is the run's plaintext outputs, which the executor updates as nodes
    apply. A row's ``ref_digests`` are taken against it when the row is built, so
    they record the upstream values the node was just given.
    """

    def __init__(
        self,
        *,
        desired: Desired,
        env: ApplyEnv,
        writer: StateWriter,
        report: ApplyReport,
        live: LiveOutputs,
    ) -> None:
        self.desired = desired
        self.env = env
        self.writer = writer
        self.report = report
        self.live = live
        self.ir_by_id = {node.id: node for node in desired.ir.nodes}

    def ref_digests(
        self, type_name: str, properties: Mapping[str, Any], outputs: LiveOutputs | None = None
    ) -> dict[str, str]:
        """The ``ref_digests`` of a row with these properties, resolved against
        ``outputs`` (default: the run's live outputs)."""
        return ref_digests(
            type_name,
            properties,
            self.live if outputs is None else outputs,
            types=self.env.types,
            secrets=self.env.secrets,
        )

    def matches(self, field: str, value: Any, recorded: str) -> bool | None:
        """Whether ``value`` is the one a row's ``recorded`` digest names."""
        return matches(field, value, recorded, self.env.secrets)

    def state_node(
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
            depends_on=ir_node.depends_on,
            prevent_destroy=res.lifecycle.prevent_destroy,
            secret_digests=secret_digests(res, node_id, self.env.secrets),
            ref_digests=self.ref_digests(ir_node.type, ir_node.properties),
            status=status,
        )

    def collapsed_row(self, prior_node: StateNode) -> StateNode:
        """``prior_node`` as the config now describes it, for a replace that did nothing.

        Takes the desired input hash, properties, dependencies (``depends_on``
        too) and ``prevent_destroy``; keeps what only a provider call could change. The
        ``ref_digests`` are the config's refs resolved now: the re-diff found
        every one equal to what the resource holds, possibly under a new marker.
        """
        ir_node = self.ir_by_id[prior_node.id]
        return replace(
            prior_node,
            input_hash=self.desired.hashes[prior_node.id],
            properties=ir_node.properties,
            dependencies=ir_node.dependencies,
            depends_on=ir_node.depends_on,
            prevent_destroy=ir_node.prevent_destroy,
            ref_digests=self.ref_digests(ir_node.type, ir_node.properties),
        )

    async def write_ahead(self, node_id: str, res: Resource) -> None:
        """Record a 'creating' row before the provider create.

        A create that succeeds at the provider but is cancelled or crashes before
        persist stays tracked, so destroy/refresh can still reclaim it. Must complete
        before the provider call.
        """
        await self.writer.run(
            self.checked_put, self.state_node(node_id, res, {}, NodeStatus.CREATING), key=node_id
        )

    async def persist(self, node_id: str, res: Resource, outputs: dict[str, Any]) -> None:
        # Lease-checked: lease loss cancels the run asynchronously, so a node
        # already inside a provider call can still reach this write.
        await self.writer.run(
            self.checked_put,
            self.state_node(node_id, res, outputs, NodeStatus.CREATED),
            key=node_id,
        )

    async def rewrite(self, row: StateNode) -> None:
        """Write a row no provider call produced: a state-only change.

        Goes through the same fenced, lease-checked writer as :meth:`persist`, so
        every backend (the S3 journal, sqlite, postgres) records it the same way.
        """
        await self.writer.run(self.checked_put, row, key=row.id)

    def checked_put(self, node: StateNode) -> None:
        """Check the lease, then write the row, on whichever thread writes.

        The check runs here rather than at enqueue time, so a queued write is still
        refused if the lease lapses while it waits.
        """
        self.env.lease.check()
        self.env.backend.put(node)

    def checked_delete(self, node_id: str) -> None:
        """Check the lease, then drop the row: :meth:`checked_put`'s delete."""
        self.env.lease.check()
        self.env.backend.delete(node_id)

    async def drop(self, node_id: str) -> None:
        """Drop a row through the fenced, lease-checked writer, as :meth:`rewrite` writes one."""
        await self.writer.run(self.checked_delete, node_id, key=node_id)

    async def load(self) -> StateGraph:
        """Read the whole state through the writer, off the event loop."""
        return await self.writer.run(self.env.backend.load)

    async def forget(self, node_id: str) -> None:
        """Drop a destroyed node's row, marking it stale if the drop fails.

        The provider delete has already succeeded, so the resource is gone; a failed
        row ``delete`` leaves a row whose hash still matches config, which the next
        plan would skip as NOOP. The stale mark forces a re-plan instead.
        """
        try:
            await self.writer.run(self.env.backend.delete, node_id, key=node_id)
        except Exception:
            await self.writer.run(self.mark_stale, node_id, key=node_id)
            raise

    def mark_stale(self, node_id: str, rows: Mapping[str, StateNode] | None = None) -> None:
        """Clear a node's ``input_hash`` so the next plan cannot Merkle-skip it.

        A compensation is a provider call followed by a state write; a failure
        between the two leaves a row describing a resource that no longer exists.
        The diff is symbolic, so that row's stored hash still matches config and the
        next plan would report NOOP. :data:`NO_INPUT_HASH` forces a re-plan, as it
        does for ``refresh --write``.

        Must run before the compensation, so a process killed mid-rollback still
        leaves the mark. The undo's own state write clears it on success.

        Reads the row from ``rows`` (the backend's rows, read once per rollback),
        else from the backend; never from the prior state, since an earlier phase
        of this run may have rewritten it.

        Never raises: a failed read or write is recorded in ``poison_failed``. A
        backend outage is a common cause of the failure being compensated, and
        must not stop the compensations that follow.
        """
        try:
            current = (self.env.backend.load().nodes if rows is None else rows).get(node_id)
            if current is None:
                return
            self.checked_put(replace(current, input_hash=NO_INPUT_HASH))
        except Exception as exc:
            self.report.poison_failed[node_id] = str(exc)
