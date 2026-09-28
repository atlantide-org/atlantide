"""Node writes: one journal entry and one head commit each, or a bulk snapshot swap."""

from __future__ import annotations

from collections.abc import Iterable, Set
from dataclasses import replace
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core.errors import FencedWriteError, StateError
from atlantide.core.logging import get_logger
from atlantide.state import fencing
from atlantide.state.codec import (
    EntryKind,
    EntryOp,
    JournalEntry,
    StateDocument,
    encode_entry,
)
from atlantide.state.leases import Lease
from atlantide.state.model import StateNode
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import CAS_CODES, head_update
from atlantide.state.s3.journal import Head, Layout, head_of_item
from atlantide.state.s3.lease_items import LeaseItems
from atlantide.state.s3.maintenance import Compactor, Maintenance
from atlantide.state.s3.reads import Reads
from atlantide.state.s3.snapshots import SnapshotMoved, Snapshots, compression, encryption
from atlantide.state.s3.view import ViewCache
from atlantide.util.aws import error_code

__all__ = ["Writes", "stamp"]

_log = get_logger("state.s3.writes")


def stamp(lease: Lease | None) -> tuple[int, str]:
    """The ``(fence, owner)`` a journal entry records: the lease's, or ``(0, "")`` if unfenced."""
    if lease is None:
        return 0, ""
    return lease.fence, lease.owner


class Writes:
    """Journals node writes and commits them at their heads."""

    def __init__(
        self,
        ctx: S3Context,
        *,
        snapshots: Snapshots,
        reads: Reads,
        views: ViewCache,
        lease_items: LeaseItems,
        maintenance: Maintenance,
        compactor: Compactor,
    ) -> None:
        self._ctx = ctx
        self._snapshots = snapshots
        self._reads = reads
        self._views = views
        self._lease_items = lease_items
        self._maintenance = maintenance
        self._compactor = compactor

    def check_scope(self, touched: Iterable[str]) -> None:
        lease = self._ctx.bound_lease()
        if lease is None:
            return
        for node_id in sorted(touched):
            if node_id not in lease.scope:
                raise FencedWriteError(fencing.outside_scope(node_id))

    def write_node(self, node_id: str, record: StateNode | None) -> None:
        """Journal one node write and commit it at the node's head.

        A write that changes nothing (the node is stored verbatim, or a delete of
        an absent node) issues no request and does not move the serial.
        """
        view, layout = self._views.writable()
        with self._ctx.mutex:
            if record is None and node_id not in view.nodes:
                return
            if record is not None and view.nodes.get(node_id) == record:
                return
            known = view.node_seq.get(node_id, 0)
        self.check_scope({node_id})
        lease = self._ctx.bound_lease()
        fence, owner = stamp(lease)
        op = EntryOp.PUT if record is not None else EntryOp.DELETE
        expect, seq = known, known + 1
        for _ in range(limits.CAS_ATTEMPTS):
            entry = JournalEntry(
                kind=EntryKind.NODE,
                name=node_id,
                seq=seq,
                fence=fence,
                op=op,
                owner=owner,
                record=record,
            )
            ref = self.put_entry(layout, entry)
            moved = self.commit(layout, entry, ref, expect, lease)
            if moved is None:
                break
            # The head is ahead of this view (another unfenced writer, or this
            # view is stale): rebase onto it. The orphaned entry sits at or below
            # the head and is collected by the next compaction.
            _log.debug("head of %r moved to seq %d under a write; rebasing", node_id, moved)
            expect, seq = moved, max(moved, known) + 1
        else:
            raise StateError(
                f"the head of {node_id!r} in {self._ctx.uri} kept moving under this "
                f"write ({limits.CAS_ATTEMPTS} attempts) — another run is writing it"
            )
        self._views.record((EntryKind.NODE, node_id), seq, record)
        self._compactor.maybe_start(1)

    def put_entry(self, layout: Layout, entry: JournalEntry) -> str:
        """Store an entry under a fresh key; returns the key (its future ``ref``).

        A refused ``If-None-Match`` is, in practice, this call's own PUT that
        landed before botocore retried it after a lost response. The entry is
        stored again under a new key rather than trusting what the refused key
        holds: the head must only ever name an entry this write is sure of, and
        the unreferenced copy is an orphan like any rebased write's, collected
        by the next compaction.
        """
        ctx = self._ctx
        body = encode_entry(entry)
        for _ in range(limits.CAS_ATTEMPTS):
            key = layout.entry_key(entry.kind, entry.name, entry.seq, entry.fence)
            try:
                ctx.clients.s3.put_object(
                    Bucket=ctx.bucket,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    IfNoneMatch="*",  # guards against a nonce collision only
                    **compression(body),
                    **encryption(ctx.kms_key_id),
                )
            except ClientError as exc:
                if error_code(exc) in CAS_CODES:
                    _log.debug("journal entry %s was refused as existing; storing anew", key)
                    continue
                raise StateError(f"cannot write state journal {ctx.uri}: {exc}") from exc
            return key
        raise StateError(
            f"cannot write state journal {ctx.uri}: every fresh key was refused as "
            f"existing ({limits.CAS_ATTEMPTS} attempts)"
        )

    def commit(
        self, layout: Layout, entry: JournalEntry, ref: str, expect: int, lease: Lease | None
    ) -> int | None:
        """Point the head at ``ref``: ``None`` on success, else the seq it moved to.

        The durability point of a write. Conditional on the head's seq being the
        one this write is based on and, under a lease, on its fence equalling
        the lease's, so a superseded or never-recorded lease is refused
        atomically with the commit.
        """
        ctx = self._ctx
        head_key = layout.head_key(entry.kind, entry.name)
        try:
            ctx.clients.ddb.update_item(
                **head_update(
                    ctx.heads_table,
                    head_key,
                    seq=entry.seq,
                    ref=ref,
                    op=entry.op,
                    namespace=ctx.namespace,
                    expect=expect,
                    fence=lease.fence if lease is not None else None,
                ),
                ReturnValuesOnConditionCheckFailure="ALL_OLD",
            )
        except ClientError as exc:
            if error_code(exc) != "ConditionalCheckFailedException":
                raise StateError(f"cannot commit to {ctx.heads_table!r}: {exc}") from exc
            old: Any = exc.response.get("Item")
            head = head_of_item(old) if old is not None else self._reads.get_head(head_key)
            if head.ref == ref:
                return None  # commit applied; a retried call lost its response
            if lease is not None and head.fence != lease.fence:
                raise self.fenced(lease, entry.name, head.fence or 0) from exc
            return head.seq or 0
        return None

    def fenced(self, lease: Lease, node_id: str, recorded: int) -> FencedWriteError:
        """The refusal for a node whose head is not at this lease's fence.

        The head decides; the lock table is read only to name the node's current
        holder in the error.
        """
        if recorded < lease.fence:
            return FencedWriteError(
                fencing.never_recorded(node_id, lease.fence, self._ctx.uri, recorded)
            )
        holder = self._lease_items.read_holds({node_id}).get(node_id)
        if holder is not None and holder.owner != lease.owner:
            detail = f" (fence {recorded} superseded {lease.fence})"
            return FencedWriteError(fencing.held_by_another(node_id, holder.owner, detail=detail))
        taker = "the same owner" if holder is not None else "another run"
        return FencedWriteError(
            fencing.superseded(node_id, lease.fence, recorded, taker=taker, refresh=True)
        )

    # -- bulk writes (the snapshot path) ------------------------------------

    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Deletes and upserts as one snapshot write, so a rekey cannot half-land.

        A single effective change uses the ordinary journal write instead, which
        is atomic on its own and does not rewrite the snapshot.
        """
        dropped = set(delete_ids)
        fresh = {node.id: node for node in nodes}
        view, _ = self._views.writable()
        with self._ctx.mutex:
            changes = {nid for nid in dropped - set(fresh) if nid in view.nodes} | {
                nid for nid, node in fresh.items() if view.nodes.get(nid) != node
            }
        if not changes:
            return
        if len(changes) == 1:
            (only,) = changes
            self.check_scope(dropped | set(fresh))
            self.write_node(only, fresh.get(only))
            return
        self.bulk(dropped, fresh)

    def bulk(self, dropped: set[str], fresh: dict[str, StateNode]) -> None:
        """Fold the journal and apply the change in one snapshot compare-and-swap.

        Atomic to every reader (one object). The fence check is a pre-check, not
        a transaction: the touched nodes' heads are re-read just before the
        ``If-Match`` PUT and the write is refused (or rebased) if any moved,
        leaving a window of one round trip.
        """
        ctx = self._ctx
        touched = dropped | set(fresh)
        self.check_scope(touched)
        lease = ctx.bound_lease()
        for _ in range(limits.CAS_ATTEMPTS):
            self._snapshots.ensure()
            cut = self._reads.cut()
            if cut is None:  # pragma: no cover - the snapshot was just ensured
                continue
            folded = cut.fold()
            nodes = {nid: n for nid, n in folded.nodes.items() if nid not in dropped}
            nodes.update(fresh)
            if nodes == folded.nodes:
                with ctx.mutex:
                    # Re-read on next use rather than install this cut: a
                    # concurrent commit may have updated the view since.
                    self._views.drop()
                return
            layout = ctx.layout(cut.doc.epoch)
            if not self.heads_unmoved(layout, touched, folded, lease):
                continue
            updated = replace(folded, serial=folded.serial + 1, nodes=nodes)
            try:
                self._snapshots.put(updated, cut.etag)
            except SnapshotMoved:
                continue
            with ctx.mutex:
                self._views.drop()
            self._maintenance.delete_quietly(cut.garbage(updated))
            return
        raise StateError(
            f"remote state {ctx.uri} kept changing under this bulk write "
            f"({limits.CAS_ATTEMPTS} attempts) — re-run once it settles"
        )

    def heads_unmoved(
        self, layout: Layout, touched: Set[str], folded: StateDocument, lease: Lease | None
    ) -> bool:
        """The bulk pre-check: whether every touched head is still where the fold left it.

        Under a lease, a head not at the lease's fence raises
        :class:`FencedWriteError`. ``False`` means rebase.
        """
        heads = self._reads.read_heads(layout, [(EntryKind.NODE, nid) for nid in touched])
        for node_id in sorted(touched):
            head = heads.get((EntryKind.NODE, node_id), Head())
            if lease is not None and head.fence != lease.fence:
                raise self.fenced(lease, node_id, head.fence or 0)
            if (head.seq or 0) > folded.wm.get(node_id, 0):
                return False
        return True
