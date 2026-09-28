"""The S3 state journal: layout, heads, and the fold (no I/O).

State on S3 is a *snapshot* object at ``key`` plus a *journal* of small
per-write objects beside it, with a *head* per node (and per stack of outputs)
in DynamoDB saying which journal entry is committed:

``key``
    The snapshot (:class:`~atlantide.state.codec.StateDocument`): every node and
    output as of the last fold, with per-node watermarks ``wm`` (and ``owm`` for
    stacks) naming the journal seq each one already includes.
``key.d/<epoch>/log/<pct(node)>/<seq:012d>-<fence>-<nonce>.json``
    One node write (a :class:`~atlantide.state.codec.JournalEntry`).
``key.d/<epoch>/out/<pct(stack)>/<seq:012d>-<fence>-<nonce>.json``
    One stack's whole output map.
head ``\\x00h\\x00{ns}\\x00{epoch}#{node}`` / ``\\x00o\\x00{ns}\\x00{epoch}#{stack}``
    ``{fence, seq, ref, op, state_ns}``: the fence of the newest lease granted
    over the node, and the seq/key of its committed entry.

The *effective* value of node ``X`` is the entry at ``head.ref`` when
``head.seq > wm[X]``, and ``snapshot.nodes[X]`` otherwise. A write stores its
entry, then commits it with one conditional update of the head (fence and seq
checked); that update is the linearization point. This module holds the pure
part: key naming and parsing, the items a LIST shows written past their
watermark, and folding a consistent *cut* into the next snapshot.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import quote, unquote

from atlantide.core.node_id import stack_of
from atlantide.state.codec import (
    EntryKind,
    EntryOp,
    JournalEntry,
    StateDocument,
)
from atlantide.state.model import StateNode

#: ``(kind, name)``: a node id under :attr:`EntryKind.NODE`, a stack under
#: :attr:`EntryKind.OUTPUT`. Every per-item map in the journal is keyed by it.
type Item = tuple[str, str]

_HEAD_TAG: dict[str, str] = {EntryKind.NODE: "\x00h\x00", EntryKind.OUTPUT: "\x00o\x00"}


def new_epoch() -> str:
    """A fresh journal generation, minted when a snapshot is first created."""
    return uuid.uuid4().hex[:16]


@dataclass(frozen=True, slots=True)
class EntryKey:
    """One journal object as a LIST reports it, parsed from its key."""

    kind: str
    name: str
    seq: int
    fence: int
    key: str

    @property
    def item(self) -> Item:
        return (self.kind, self.name)


@dataclass(frozen=True, slots=True)
class Head:
    """A node's (or stack's) head item. ``None`` fields were never written."""

    fence: int | None = None
    seq: int | None = None
    ref: str | None = None
    op: str | None = None


@dataclass(frozen=True, slots=True)
class Layout:
    """Where one state's journal objects and head items live."""

    key: str
    namespace: str
    epoch: str

    @property
    def prefix(self) -> str:
        return f"{self.key}.d/{self.epoch}/"

    def entry_key(self, kind: str, name: str, seq: int, fence: int) -> str:
        """A fresh, never-reused key for an entry (the nonce makes it unique)."""
        nonce = uuid.uuid4().hex[:12]
        return f"{self.prefix}{kind}/{quote(name, safe='')}/{seq:012d}-{fence}-{nonce}.json"

    def parse(self, key: str) -> EntryKey | None:
        """The entry a listed key names, or ``None`` for anything else."""
        if not key.startswith(self.prefix):
            return None
        parts = key[len(self.prefix) :].split("/")
        if len(parts) != 3 or parts[0] not in _HEAD_TAG or not parts[2].endswith(".json"):
            return None
        fields = parts[2][: -len(".json")].split("-")
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            return None
        return EntryKey(parts[0], unquote(parts[1]), int(fields[0]), int(fields[1]), key)

    def head_key(self, kind: str, name: str) -> str:
        return f"{self.head_prefix(kind)}{name}"

    def head_prefix(self, kind: str) -> str:
        return f"{_HEAD_TAG[kind]}{self.namespace}\x00{self.epoch}#"

    def item_of_head_key(self, key: str) -> Item | None:
        for kind in _HEAD_TAG:
            prefix = self.head_prefix(kind)
            if key.startswith(prefix):
                return (kind, key[len(prefix) :])
        return None


def head_of_item(item: Mapping[str, Any] | None) -> Head:
    """A DynamoDB head item as a :class:`Head` (an absent item is an empty head)."""
    if not item:
        return Head()

    def number(name: str) -> int | None:
        value = item.get(name, {}).get("N")
        return int(value) if value is not None else None

    def string(name: str) -> str | None:
        value = item.get(name, {}).get("S")
        return str(value) if value is not None else None

    return Head(fence=number("fence"), seq=number("seq"), ref=string("ref"), op=string("op"))


def watermark(doc: StateDocument, item: Item) -> int:
    kind, name = item
    return (doc.wm if kind == EntryKind.NODE else doc.owm).get(name, 0)


def group_listing(entries: Iterable[EntryKey]) -> dict[Item, list[EntryKey]]:
    grouped: dict[Item, list[EntryKey]] = {}
    for entry in entries:
        grouped.setdefault(entry.item, []).append(entry)
    return grouped


def items_past_watermark(doc: StateDocument, listed: Mapping[Item, list[EntryKey]]) -> set[Item]:
    """Items with a listed entry above their watermark: the only heads to read.

    An item with no such entry either has nothing past the snapshot or has a
    commit whose entry this LIST did not see; the causality re-LIST
    (:meth:`Cut.unlisted_refs`) bounds the second case.
    """
    return {
        item
        for item, entries in listed.items()
        if max(entry.seq for entry in entries) > watermark(doc, item)
    }


@dataclass(frozen=True, slots=True)
class Cut:
    """A consistent read of the journal: snapshot + the heads and entries past it.

    ``heads`` holds only the items read (:func:`items_past_watermark`);
    ``entries`` holds the committed entry of every *effective* head, i.e. one
    with ``seq`` above its watermark.
    """

    doc: StateDocument
    etag: str | None
    listed: dict[Item, list[EntryKey]] = field(default_factory=dict)
    heads: dict[Item, Head] = field(default_factory=dict)
    entries: dict[Item, JournalEntry] = field(default_factory=dict)

    def effective(self) -> dict[Item, Head]:
        """Heads whose committed entry is newer than the snapshot."""
        return {
            item: head
            for item, head in self.heads.items()
            if head.seq is not None and head.seq > watermark(self.doc, item)
        }

    def unlisted_refs(self) -> set[Item]:
        """Effective heads whose entry the LIST did not show (committed after it)."""
        keys = {entry.key for entries in self.listed.values() for entry in entries}
        return {item for item, head in self.effective().items() if head.ref not in keys}

    # -- the effective state ----------------------------------------------

    def nodes(self) -> dict[str, StateNode]:
        nodes = dict(self.doc.nodes)
        for (kind, name), entry in self.entries.items():
            if kind != EntryKind.NODE:
                continue
            if entry.op == EntryOp.DELETE or entry.record is None:
                nodes.pop(name, None)
            else:
                nodes[name] = entry.record
        return nodes

    def outputs(self) -> dict[str, Any]:
        outputs = dict(self.doc.outputs)
        for (kind, stack), entry in self.entries.items():
            if kind != EntryKind.OUTPUT:
                continue
            outputs = replace_stack(outputs, stack, entry.outputs or {})
        return outputs

    def serial(self) -> int:
        """``S.serial`` plus one per commit past the snapshot."""
        return self.doc.serial + sum(
            (head.seq or 0) - watermark(self.doc, item) for item, head in self.effective().items()
        )

    def seqs(self, kind: str) -> dict[str, int]:
        """Per item, the newest seq known: the watermark, or the head past it."""
        known = dict(self.doc.wm if kind == EntryKind.NODE else self.doc.owm)
        for (item_kind, name), head in self.heads.items():
            if item_kind == kind and head.seq is not None:
                known[name] = max(known.get(name, 0), head.seq)
        return known

    # -- folding ------------------------------------------------------------

    def fold(self, *, max_fence: int = 0) -> StateDocument:
        """The next snapshot: every effective entry folded in, serial unchanged.

        Watermarks move to the folded heads' seq; fences only ever rise.
        """
        wm, owm = dict(self.doc.wm), dict(self.doc.owm)
        for (kind, name), head in self.effective().items():
            (wm if kind == EntryKind.NODE else owm)[name] = head.seq or 0
        fences = dict(self.doc.fences)
        for (kind, name), head in self.heads.items():
            if kind == EntryKind.NODE and head.fence is not None:
                fences[name] = max(fences.get(name, 0), head.fence)
        return replace(
            self.doc,
            serial=self.serial(),
            nodes=self.nodes(),
            outputs=self.outputs(),
            fences=fences,
            max_fence=max(self.doc.max_fence, max_fence, *fences.values(), 0),
            wm=wm,
            owm=owm,
            gen=self.doc.gen + 1,
        )

    def garbage(self, folded: StateDocument) -> list[str]:
        """Listed entries ``folded`` makes unreachable: seq at or below its watermark.

        This covers superseded commits and orphans at or below the head (failed
        or refused commits). A *pending* entry (seq one past the head: a writer
        between its PUT and its commit) is above the watermark and is never
        collected.
        """
        return sorted(
            entry.key
            for item, entries in self.listed.items()
            for entry in entries
            if entry.seq <= watermark(folded, item)
        )


def replace_stack(
    outputs: Mapping[str, Any], stack: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    """``outputs`` with every key of ``stack`` replaced by ``values``."""
    kept = {key: value for key, value in outputs.items() if stack_of(key) != stack}
    return {**kept, **values}


def stack_outputs(outputs: Mapping[str, Any], stack: str) -> dict[str, Any]:
    return {key: value for key, value in outputs.items() if stack_of(key) == stack}
