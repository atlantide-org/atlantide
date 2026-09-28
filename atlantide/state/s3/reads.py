"""Consistent reads of the whole state: snapshot, LIST, heads, entries."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from itertools import batched
from typing import Any

from botocore.exceptions import ClientError

from atlantide.core.errors import StateError
from atlantide.core.logging import get_logger
from atlantide.state.codec import JournalEntry, decode_entry
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.dynamo import MISSING_CODES, backoff, ddb_str, parallel
from atlantide.state.s3.journal import (
    Cut,
    EntryKey,
    Head,
    Item,
    Layout,
    group_listing,
    head_of_item,
    items_past_watermark,
)
from atlantide.state.s3.snapshots import EntryVanished, Snapshot, Snapshots
from atlantide.util.aws import error_code

__all__ = ["Reads"]

_log = get_logger("state.s3.reads")


class Reads:
    """Reads of the journal and of DynamoDB items; holds no state of its own."""

    def __init__(self, ctx: S3Context, snapshots: Snapshots) -> None:
        self._ctx = ctx
        self._snapshots = snapshots

    def cut(self) -> Cut | None:
        """A consistent read of the whole state, or ``None`` if there is none yet.

        Restarts when the snapshot is replaced under it (a fold deleted entries
        this read had counted on, or a bulk write moved values it had not
        re-read), bounded by :data:`limits.READ_ATTEMPTS`.
        """
        for attempt in range(limits.READ_ATTEMPTS):
            snap = self._snapshots.get()
            if snap is None:
                return None
            layout = self._ctx.layout(snap.doc.epoch)
            try:
                cut = self.read_journal(snap, layout)
            except EntryVanished:
                _log.debug(
                    "read of %s restarted (attempt %d): an entry was compacted",
                    self._ctx.uri,
                    attempt + 1,
                )
                continue
            if self._snapshots.etag() != snap.etag:
                _log.debug(
                    "read of %s restarted (attempt %d): the snapshot was replaced",
                    self._ctx.uri,
                    attempt + 1,
                )
                continue
            return cut
        raise StateError(
            f"state {self._ctx.uri} kept being rewritten while it was read "
            f"({limits.READ_ATTEMPTS} attempts) — retry; if it persists, run "
            f"`atlantide state fsck`"
        )

    def read_journal(self, snap: Snapshot, layout: Layout) -> Cut:
        listed = group_listing(self.list_entries(layout))
        heads = self.read_heads(layout, items_past_watermark(snap.doc, listed))
        cut = Cut(snap.doc, snap.etag, listed, heads)
        # Causality: a head naming an entry the LIST did not show was committed
        # after the LIST, and so may depend on commits the LIST also missed.
        # Re-LIST until every effective head's entry is accounted for.
        for _ in range(limits.RELIST_ATTEMPTS):
            unlisted = cut.unlisted_refs()
            if not unlisted:
                break
            _log.debug(
                "re-listing the journal of %s: %d head(s) name unlisted entries",
                self._ctx.uri,
                len(unlisted),
            )
            listed = group_listing(self.list_entries(layout))
            unread = items_past_watermark(snap.doc, listed) - set(heads)
            heads = {**heads, **self.read_heads(layout, unread)}
            cut = Cut(snap.doc, snap.etag, listed, heads)
        effective = cut.effective()
        fetched = parallel(lambda item: self.get_entry(effective[item].ref or ""), list(effective))
        return Cut(snap.doc, snap.etag, listed, heads, dict(zip(effective, fetched, strict=True)))

    def list_entries(self, layout: Layout) -> list[EntryKey]:
        ctx = self._ctx
        entries: list[EntryKey] = []
        request: dict[str, Any] = {
            "Bucket": ctx.bucket,
            "Prefix": layout.prefix,
            "MaxKeys": limits.LIST_PAGE,
        }
        while True:
            try:
                page = ctx.clients.s3.list_objects_v2(**request)
            except ClientError as exc:
                raise ctx.read_error(exc) from exc
            parsed = (layout.parse(obj["Key"]) for obj in page.get("Contents", []))
            entries.extend(entry for entry in parsed if entry is not None)
            token = page.get("NextContinuationToken")
            if not page.get("IsTruncated") or not token:
                return entries
            request["ContinuationToken"] = token

    def get_entry(self, key: str) -> JournalEntry:
        ctx = self._ctx
        try:
            response = ctx.clients.s3.get_object(Bucket=ctx.bucket, Key=key)
        except ClientError as exc:
            if error_code(exc) in MISSING_CODES:
                raise EntryVanished(key) from exc
            raise ctx.read_error(exc) from exc
        return decode_entry(response["Body"].read())

    def read_heads(self, layout: Layout, items: Iterable[Item]) -> dict[Item, Head]:
        """The heads of ``items`` (consistent reads, batched and in parallel)."""
        by_key = {layout.head_key(kind, name): (kind, name) for kind, name in items}
        batches = list(batched(sorted(by_key), limits.BATCH_GET_MAX))
        found: dict[Item, Head] = {}
        for items_found in parallel(
            lambda batch: self.batch_get(self._ctx.heads_table, batch), batches
        ):
            for item in items_found:
                found[by_key[item["node_id"]["S"]]] = head_of_item(item)
        return found

    def batch_get(self, table: str, keys: Sequence[str]) -> list[dict[str, Any]]:
        """Items for ``keys``, re-requesting ``UnprocessedKeys`` and failing closed."""
        request: dict[str, Any] = {
            table: {"Keys": [{"node_id": ddb_str(key)} for key in keys], "ConsistentRead": True}
        }
        found: list[dict[str, Any]] = []
        for attempt in range(limits.DDB_ATTEMPTS):
            if attempt:
                backoff(attempt - 1)  # DynamoDB returns unprocessed keys when throttled
            try:
                response = self._ctx.clients.ddb.batch_get_item(RequestItems=request)
            except ClientError as exc:
                raise StateError(f"cannot read {table!r}: {exc}") from exc
            found.extend(response.get("Responses", {}).get(table, []))
            request = response.get("UnprocessedKeys") or {}
            if not request:
                return found
        raise StateError(
            f"could not read {table!r} completely (DynamoDB kept returning unprocessed keys)"
        )

    def get_head(self, key: str) -> Head:
        ctx = self._ctx
        try:
            response = ctx.clients.ddb.get_item(
                TableName=ctx.heads_table, Key={"node_id": ddb_str(key)}, ConsistentRead=True
            )
        except ClientError as exc:
            raise StateError(f"cannot read {ctx.heads_table!r}: {exc}") from exc
        return head_of_item(response.get("Item"))
