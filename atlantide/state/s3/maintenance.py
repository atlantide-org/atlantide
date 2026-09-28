"""Compaction (folding the journal into the snapshot) and ``fsck``."""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping, Sequence, Set
from dataclasses import dataclass, field
from itertools import batched

from botocore.exceptions import ClientError

from atlantide.core.errors import StateError
from atlantide.state.codec import (
    EntryKind,
    EntryOp,
    StateDocument,
)
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context, backend_log
from atlantide.state.s3.dynamo import ddb_str, head_update
from atlantide.state.s3.fences import Fences
from atlantide.state.s3.journal import (
    EntryKey,
    Head,
    Item,
    Layout,
    group_listing,
    head_of_item,
    watermark,
)
from atlantide.state.s3.reads import Reads
from atlantide.state.s3.snapshots import EntryVanished, SnapshotMoved, Snapshots
from atlantide.util.aws import error_code

__all__ = ["CompactionReport", "Compactor", "FsckReport", "Maintenance", "label"]


@dataclass(frozen=True, slots=True)
class CompactionReport:
    """What one :meth:`S3StateBackend.compact` did."""

    folded: int = 0
    deleted: int = 0
    #: The snapshot changed during the fold (another compactor or a bulk write).
    skipped: bool = False
    serial: int = 0
    gen: int = 0


@dataclass(slots=True)
class FsckReport:
    """What :meth:`S3StateBackend.fsck` found (and, with ``rebuild``, repaired)."""

    heads: int = 0
    entries: int = 0
    #: ``(node or stack, ref)`` of committed heads whose entry is missing.
    missing: list[tuple[str, str]] = field(default_factory=list)
    #: Items with entries past the snapshot but no committed head (lost heads).
    headless: list[str] = field(default_factory=list)
    #: Entries one past their head: a writer between its PUT and its commit, or
    #: one that crashed there. Never collected.
    pending: list[str] = field(default_factory=list)
    #: Entries a compaction will delete (superseded, or refused commits).
    collectable: int = 0
    #: ``(item, ref)`` of heads rebuilt from their highest-seq entry.
    rebuilt: list[tuple[str, str]] = field(default_factory=list)

    @property
    def unrepaired(self) -> list[str]:
        """Lost heads (:attr:`headless`) that ``rebuild_heads`` did not re-point."""
        rebuilt = {name for name, _ in self.rebuilt}
        return [name for name in self.headless if name not in rebuilt]

    @property
    def healthy(self) -> bool:
        """Nothing is left broken: no missing entry, and every lost head rebuilt."""
        return not self.missing and not self.unrepaired


def label(item: Item) -> str:
    kind, name = item
    return name if kind == EntryKind.NODE else f"outputs of stack {name!r}"


class Compactor:
    """Starts a background fold every ``every`` commits, one at a time.

    The commit count and the thread handle are guarded by ``ctx.mutex``.
    """

    def __init__(self, ctx: S3Context, *, every: int, run: Callable[[], None]) -> None:
        self._ctx = ctx
        self._every = max(1, every)
        self._run = run
        self._commits = 0
        self._thread: threading.Thread | None = None

    def maybe_start(self, count: int) -> None:
        """Count ``count`` commits; start a fold if that crossed a multiple of ``every``."""
        with self._ctx.mutex:
            before = self._commits
            self._commits += count
            due = before // self._every != self._commits // self._every
            running = self._thread is not None and self._thread.is_alive()
            if not due or running:
                return
            self._thread = threading.Thread(
                target=self._run, name="atlantide-state-compactor", daemon=True
            )
            self._thread.start()

    def join(self, timeout: float) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)


class Maintenance:
    """Folds and checks the journal; needs no lock (the fold is a compare-and-swap)."""

    def __init__(self, ctx: S3Context, snapshots: Snapshots, reads: Reads, fences: Fences) -> None:
        self._ctx = ctx
        self._snapshots = snapshots
        self._reads = reads
        self._fences = fences

    def compact(self) -> CompactionReport:
        """Fold every committed entry into the snapshot, then delete what it supersedes.

        Lock-free: the fold is a compare-and-swap on the snapshot, and commits
        made after its read stay above the new watermarks. A failed swap means
        another compactor or a bulk write updated the snapshot first; nothing is
        deleted then.
        """
        cut = self._reads.cut()
        if cut is None:
            return CompactionReport()
        folded = cut.fold(max_fence=self._fences.read_fence_counter())
        garbage = cut.garbage(folded)
        effective = len(cut.effective())
        changed = (
            effective or folded.max_fence != cut.doc.max_fence or folded.fences != cut.doc.fences
        )
        if not changed and not garbage:
            return CompactionReport(serial=cut.doc.serial, gen=cut.doc.gen)
        if changed:
            try:
                self._snapshots.put(folded, cut.etag)
            except SnapshotMoved:
                return CompactionReport(skipped=True, serial=folded.serial)
        else:
            folded = cut.doc
        deleted = self.delete(garbage)
        return CompactionReport(
            folded=effective, deleted=deleted, serial=folded.serial, gen=folded.gen
        )

    def delete(self, keys: Sequence[str]) -> int:
        ctx = self._ctx
        deleted = 0
        for batch in batched(keys, limits.DELETE_MAX):
            try:
                response = ctx.clients.s3.delete_objects(
                    Bucket=ctx.bucket,
                    Delete={"Objects": [{"Key": key} for key in batch], "Quiet": True},
                )
            except ClientError as exc:
                raise StateError(f"cannot delete compacted journal entries: {exc}") from exc
            deleted += len(batch) - len(response.get("Errors", []))
        return deleted

    def delete_quietly(self, keys: Sequence[str]) -> None:
        try:
            self.delete(keys)
        except StateError as exc:
            backend_log.warning("%s", exc)

    # -- fsck -------------------------------------------------------------

    def fsck(self, *, rebuild_heads: bool = False) -> FsckReport:
        """Cross-check heads against journal entries; optionally rebuild lost heads.

        Heads are found by scanning the heads table for this state; the read and
        write paths never scan. A head whose committed entry is missing is
        corruption. An item with entries past the snapshot but no committed head
        has lost its head (e.g. a recreated table). With ``rebuild_heads`` its
        head is pointed at the highest-seq entry; that entry may be an
        uncommitted write, so every rebuilt item is reported for review.
        """
        for attempt in range(limits.READ_ATTEMPTS):
            snap = self._snapshots.get()
            if snap is None:
                return FsckReport()
            layout = self._ctx.layout(snap.doc.epoch)
            entries = self._reads.list_entries(layout)
            keys = {entry.key for entry in entries}
            heads = self.scan_heads(layout)
            if self.unlisted(snap.doc, heads, keys):
                # Committed after the LIST (the scan came later): re-LIST, as a
                # read does, before calling any of them missing.
                keys |= {entry.key for entry in self._reads.list_entries(layout)}
            if self._snapshots.etag() == snap.etag:
                return self.check(
                    snap.doc, layout, entries, keys, heads, rebuild_heads=rebuild_heads
                )
            # A fold or bulk write replaced the snapshot mid-check: its
            # watermarks, and the entries it let a compaction delete, differ.
            backend_log.debug(
                "fsck of %s restarted (attempt %d): the snapshot was replaced",
                self._ctx.uri,
                attempt + 1,
            )
        raise StateError(
            f"state {self._ctx.uri} kept being rewritten while it was checked "
            f"({limits.READ_ATTEMPTS} attempts) — retry once it settles"
        )

    @staticmethod
    def unlisted(doc: StateDocument, heads: Mapping[Item, Head], keys: Set[str]) -> list[Item]:
        """Heads past the snapshot whose committed entry is not among ``keys``."""
        return [
            item
            for item, head in sorted(heads.items())
            if head.seq is not None and head.seq > watermark(doc, item) and head.ref not in keys
        ]

    def check(
        self,
        doc: StateDocument,
        layout: Layout,
        entries: Sequence[EntryKey],
        keys: Set[str],
        heads: Mapping[Item, Head],
        *,
        rebuild_heads: bool,
    ) -> FsckReport:
        """The report for one consistent read: ``heads`` against the listed ``entries``."""
        listed = group_listing(entries)
        report = FsckReport(heads=len(heads), entries=len(entries))
        for item in self.unlisted(doc, heads, keys):
            report.missing.append((label(item), heads[item].ref or ""))
        for item, found in sorted(listed.items()):
            head = heads.get(item, Head())
            wm = watermark(doc, item)
            # On a seq tie, prefer the higher fence: a newer lease's committed
            # entry beside a superseded writer's refused one.
            top = max(found, key=lambda entry: (entry.seq, entry.fence))
            if head.seq is None and top.seq > wm:
                if head.fence is not None and top.fence == head.fence:
                    # Written under the head's own fence but never committed: a
                    # run that died between its entry and its commit. A head
                    # lost with the table is re-fenced far above any entry by
                    # the fence counter re-seed, so this is not a lost head.
                    report.pending.append(top.key)
                    continue
                report.headless.append(label(item))
                if rebuild_heads and self.rebuild_head(layout, top):
                    report.rebuilt.append((label(item), top.key))
                continue
            for entry in found:
                if entry.seq <= max(wm, head.seq or 0) and entry.key != head.ref:
                    report.collectable += 1
                elif head.seq is not None and entry.seq == head.seq + 1:
                    report.pending.append(entry.key)
        return report

    def scan_heads(self, layout: Layout) -> dict[Item, Head]:
        ctx = self._ctx
        heads: dict[Item, Head] = {}
        pages = ctx.clients.ddb.get_paginator("scan").paginate(
            TableName=ctx.heads_table,
            ConsistentRead=True,
            FilterExpression="state_ns = :ns",
            ExpressionAttributeValues={":ns": ddb_str(ctx.namespace)},
        )
        try:
            for page in pages:
                for item in page.get("Items", []):
                    if (named := layout.item_of_head_key(item["node_id"]["S"])) is not None:
                        heads[named] = head_of_item(item)
        except ClientError as exc:
            if error_code(exc) == "ResourceNotFoundException":
                raise ctx.lock_error(exc) from exc
            raise StateError(f"cannot read {ctx.heads_table!r}: {exc}") from exc
        return heads

    def rebuild_head(self, layout: Layout, entry: EntryKey) -> bool:
        """Point a seq-less head at ``entry`` (keeping any fence it has)."""
        ctx = self._ctx
        op: str = EntryOp.PUT
        try:
            op = self._reads.get_entry(entry.key).op
            ctx.clients.ddb.update_item(
                **head_update(
                    ctx.heads_table,
                    layout.head_key(entry.kind, entry.name),
                    seq=entry.seq,
                    ref=entry.key,
                    op=op,
                    namespace=ctx.namespace,
                    seed_fence=entry.fence,
                )
            )
        except EntryVanished:
            return False
        except ClientError as exc:
            if error_code(exc) == "ConditionalCheckFailedException":
                return False  # committed concurrently: not lost
            raise StateError(f"cannot rebuild the head of {entry.name!r}: {exc}") from exc
        return True
