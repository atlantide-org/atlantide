"""Committed stack outputs: one journal entry and head per changed stack."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence, Set
from itertools import batched
from typing import Any, NamedTuple

from botocore.exceptions import ClientError

from atlantide.core.errors import FencedWriteError, StateError
from atlantide.core.node_id import stack_of
from atlantide.state.backend import merge_outputs
from atlantide.state.codec import (
    EntryKind,
    EntryOp,
    JournalEntry,
)
from atlantide.state.leases import Lease
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import (
    ddb_num,
    ddb_str,
    failed_items,
    head_update,
    parallel,
    transact,
)
from atlantide.state.s3.journal import Head, Layout, stack_outputs
from atlantide.state.s3.maintenance import Compactor
from atlantide.state.s3.reads import Reads
from atlantide.state.s3.snapshots import EntryVanished
from atlantide.state.s3.view import View, ViewCache
from atlantide.state.s3.writes import Writes, stamp

__all__ = ["OutputWrites"]


class _StackChange(NamedTuple):
    """One stack's pending output commit."""

    #: The seq its head is expected at.
    expect_seq: int
    #: The seq to commit.
    seq: int
    #: The stack's whole merged output map (empty: the stack's outputs are deleted).
    merged: dict[str, Any]


type _Changes = Mapping[str, _StackChange]


def _stack_changes(
    view: View, outputs: Mapping[str, Any], dropped: Set[str]
) -> dict[str, _StackChange]:
    """The stacks whose merged outputs differ from ``view``'s, in stack order."""
    changes: dict[str, _StackChange] = {}
    for stack in sorted({stack_of(key) for key in (*outputs, *dropped)}):
        current = stack_outputs(view.outputs, stack)
        merged = merge_outputs(
            current,
            stack_outputs(outputs, stack),
            {key for key in dropped if stack_of(key) == stack},
        )
        if merged != current:
            known = view.out_seq.get(stack, 0)
            changes[stack] = _StackChange(view.out_behind.get(stack, known), known + 1, merged)
    return changes


class OutputWrites:
    """Merges and commits stack outputs, fenced on the leased nodes of each stack."""

    def __init__(
        self,
        ctx: S3Context,
        *,
        reads: Reads,
        views: ViewCache,
        writes: Writes,
        compactor: Compactor,
    ) -> None:
        self._ctx = ctx
        self._reads = reads
        self._views = views
        self._writes = writes
        self._compactor = compactor

    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        """Merge stack outputs, one journal entry and head per changed stack.

        Outputs have no lock of their own: a run may publish a stack's outputs
        while it holds that stack's nodes, so the commit is fenced on exactly
        those heads, as in the other backends. A stack in which the lease holds
        no node is merged unfenced; the head's seq still keeps a concurrent merge
        from being lost.

        Up to 100 items (changed stacks plus fence checks) commit in one
        transaction, atomically. Beyond that the fences are pre-checked and the
        stacks committed in chunks of 100, each chunk atomic.
        """
        dropped = set(remove)
        lease = self._ctx.bound_lease()
        for _ in range(limits.CAS_ATTEMPTS):
            view, layout = self._views.writable()
            with self._ctx.mutex:
                changes = _stack_changes(view, outputs, dropped)
            if not changes:
                return
            fenced = (
                sorted(nid for nid in lease.scope if stack_of(nid) in changes)
                if lease is not None
                else []
            )
            moved = self.commit(layout, changes, fenced, lease)
            if not moved:
                return
            self._refresh_stacks(layout, moved)
        raise StateError(
            f"the outputs in {self._ctx.uri} kept moving under this write "
            f"({limits.CAS_ATTEMPTS} attempts) — another run is writing them"
        )

    def commit(
        self, layout: Layout, changes: _Changes, fenced: Sequence[str], lease: Lease | None
    ) -> set[str]:
        """Journal and commit ``changes``; returns the stacks whose head moved.

        What commits is recorded in the view here, chunk by chunk.
        """
        ctx = self._ctx
        fence, owner = stamp(lease)
        entries = [
            JournalEntry(
                kind=EntryKind.OUTPUT,
                name=stack,
                seq=change.seq,
                fence=fence,
                op=EntryOp.PUT if change.merged else EntryOp.DELETE,
                owner=owner,
                outputs=change.merged or None,
            )
            for stack, change in changes.items()
        ]
        refs = parallel(lambda entry: self._writes.put_entry(layout, entry), entries)
        updates = [
            {
                "Update": head_update(
                    ctx.heads_table,
                    layout.head_key(entry.kind, entry.name),
                    seq=entry.seq,
                    ref=ref,
                    op=entry.op,
                    namespace=ctx.namespace,
                    expect=changes[entry.name].expect_seq,
                )
            }
            for entry, ref in zip(entries, refs, strict=True)
        ]
        checks = [self._fence_check(layout, nid, lease) for nid in fenced if lease is not None]
        if len(updates) + len(checks) <= limits.TRANSACT_MAX:
            moved = self.transact(layout, updates + checks, entries, fenced, lease)
            if not moved:
                self._committed(changes, entries)
            return moved
        if lease is not None:
            self._precheck_fences(layout, fenced, lease)
        moved = set()
        for chunk, chunk_updates in zip(
            batched(entries, limits.TRANSACT_MAX),
            batched(updates, limits.TRANSACT_MAX),
            strict=True,
        ):
            chunk_moved = self.transact(layout, list(chunk_updates), chunk, (), lease)
            if not chunk_moved:
                # Each chunk is atomic: record it now, so a rebase for a later
                # chunk does not re-commit (and conflict with) this one.
                self._committed(changes, chunk)
            moved |= chunk_moved
        return moved

    def _committed(self, changes: _Changes, entries: Sequence[JournalEntry]) -> None:
        """Reflect the committed ``entries`` in the view and count them toward compaction."""
        for entry in entries:
            change = changes[entry.name]
            self._views.record((EntryKind.OUTPUT, entry.name), change.seq, change.merged)
        self._compactor.maybe_start(len(entries))

    def _fence_check(self, layout: Layout, node_id: str, lease: Lease) -> dict[str, Any]:
        return {
            "ConditionCheck": {
                "TableName": self._ctx.heads_table,
                "Key": {"node_id": ddb_str(layout.head_key(EntryKind.NODE, node_id))},
                "ConditionExpression": "fence = :f",
                "ExpressionAttributeValues": {":f": ddb_num(lease.fence)},
            }
        }

    def transact(
        self,
        layout: Layout,
        items: list[dict[str, Any]],
        entries: Sequence[JournalEntry],
        fenced: Sequence[str],
        lease: Lease | None,
    ) -> set[str]:
        """One output transaction; the stacks whose seq moved, or a fence refusal."""
        try:
            exc = transact(self._ctx.clients.ddb, items)
        except ClientError as error:
            raise StateError(f"cannot commit outputs to {self._ctx.uri}: {error}") from error
        if exc is None:
            return set()
        refusal = self._refusal_for(exc, layout, entries, fenced, lease)
        if isinstance(refusal, FencedWriteError):
            raise refusal from exc
        return refusal

    def _refusal_for(
        self,
        exc: ClientError,
        layout: Layout,
        entries: Sequence[JournalEntry],
        fenced: Sequence[str],
        lease: Lease | None,
    ) -> FencedWriteError | set[str]:
        """Judge a contended output transaction: the refusal for the first fence
        check that failed, else the stacks whose head moved (all of them when
        the reasons name none)."""
        failed = failed_items(exc)
        for index in failed:
            if index >= len(entries) and lease is not None:
                node_id = fenced[index - len(entries)]
                head = self._reads.get_head(layout.head_key(EntryKind.NODE, node_id))
                return self._writes.fenced(lease, node_id, head.fence or 0)
        return {entries[i].name for i in failed if i < len(entries)} or {
            entry.name for entry in entries
        }

    def _precheck_fences(self, layout: Layout, fenced: Sequence[str], lease: Lease) -> None:
        heads = self._reads.read_heads(layout, [(EntryKind.NODE, nid) for nid in fenced])
        for node_id in fenced:
            head = heads.get((EntryKind.NODE, node_id), Head())
            if head.fence != lease.fence:
                raise self._writes.fenced(lease, node_id, head.fence or 0)

    def _refresh_stacks(self, layout: Layout, stacks: Set[str]) -> None:
        """Re-read the heads and entries of ``stacks`` into the view (a rebase)."""
        mutex = self._ctx.mutex
        heads = self._reads.read_heads(layout, [(EntryKind.OUTPUT, stack) for stack in stacks])
        for stack in sorted(stacks):
            head = heads.get((EntryKind.OUTPUT, stack), Head())
            with mutex:
                view = self._views.view
                known = view.out_seq.get(stack, 0) if view is not None else 0
            if head.seq is None or head.seq <= known or head.ref is None:
                # The head is behind the view (a recreated heads table): the next
                # commit expects its seq, and still writes above the watermark.
                with mutex:
                    if self._views.view is not None:
                        self._views.view.out_behind[stack] = head.seq or 0
                continue
            try:
                entry = self._reads.get_entry(head.ref)
            except EntryVanished:
                with mutex:
                    self._views.drop()  # compacted meanwhile: re-read everything
                return
            with mutex:
                if self._views.view is not None:
                    self._views.view.absorb(
                        (EntryKind.OUTPUT, stack), head.seq, entry.outputs or {}
                    )
