"""The backend's cached view of state, kept current by its own writes."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Self, override

from atlantide.core.errors import StateError
from atlantide.state.codec import (
    EntryKind,
)
from atlantide.state.model import StateNode
from atlantide.state.s3.context import S3Context
from atlantide.state.s3.journal import Cut, Item, Layout, replace_stack
from atlantide.state.s3.reads import Reads
from atlantide.state.s3.snapshots import Snapshots

__all__ = ["View", "ViewCache"]


@dataclass(slots=True, repr=False)
class View:
    """The state as this backend last read it, kept current by its own writes.

    Mutated only under ``ctx.mutex``. ``node_seq``/``out_seq`` hold the newest
    journal seq known per node/stack: the value the next commit expects to find
    in the head.
    """

    epoch: str | None
    nodes: dict[str, StateNode]
    outputs: dict[str, Any]
    serial: int
    node_seq: dict[str, int]
    out_seq: dict[str, int]
    #: Stack -> the seq its head holds, where that is *behind*
    #: ``out_seq`` (a recreated heads table); the next commit expects it.
    out_behind: dict[str, int] = field(default_factory=dict)

    @override
    def __repr__(self) -> str:
        # Counts only: nodes and outputs hold values that must not reach a log.
        return (
            f"View(epoch={self.epoch!r}, serial={self.serial}, nodes={len(self.nodes)}, "
            f"outputs={len(self.outputs)})"
        )

    def absorb(self, item: Item, seq: int, value: Any) -> None:
        """Apply a commit (this backend's, or one it rebased onto) unless the view has it.

        ``value`` is a node's record (``None``: deleted) or a stack's whole
        output map.
        """
        kind, name = item
        seqs = self.node_seq if kind == EntryKind.NODE else self.out_seq
        known = seqs.get(name, 0)
        if known >= seq:
            return
        if kind == EntryKind.NODE:
            if value is None:
                self.nodes.pop(name, None)
            else:
                self.nodes[name] = value
        else:
            self.outputs = replace_stack(self.outputs, name, value)
            self.out_behind.pop(name, None)
        seqs[name] = seq
        # Every commit adds one to the serial, including any this view had not
        # seen that a rebased write skipped over.
        self.serial += seq - known

    @classmethod
    def of(cls, cut: Cut | None) -> Self:
        if cut is None:
            return cls(None, {}, {}, 0, {}, {})
        return cls(
            epoch=cut.doc.epoch,
            nodes=cut.nodes(),
            outputs=cut.outputs(),
            serial=cut.serial(),
            node_seq=cut.seqs(EntryKind.NODE),
            out_seq=cut.seqs(EntryKind.OUTPUT),
        )


class ViewCache:
    """Holds the :class:`View`, plus this backend's commits it has not absorbed yet.

    ``view`` and ``unseen`` are guarded by ``ctx.mutex``.
    """

    def __init__(self, ctx: S3Context, reads: Reads, snapshots: Snapshots) -> None:
        self._ctx = ctx
        self._reads = reads
        self._snapshots = snapshots
        self.view: View | None = None
        #: This backend's commits made while the view was dropped, replayed onto
        #: the next view installed: that view may have been read before them.
        self.unseen: list[tuple[Item, int, Any]] = []

    def ensure(self) -> View:
        """The cached view, read once then maintained by this backend's writes.

        An acquire drops it, so a run reads what was committed when its lease
        was taken; a write that finds the view stale rebases on the head it meets.
        """
        view = self.view
        if view is not None:
            return view
        fresh = View.of(self._reads.cut())
        with self._ctx.mutex:
            if self.view is None:
                # A commit that landed while this read was in flight found no
                # view to update; the read may have missed it.
                for item, seq, value in self.unseen:
                    fresh.absorb(item, seq, value)
                self.unseen.clear()
                self.view = fresh
            return self.view

    def drop(self) -> None:
        """Forget the view; the next use re-reads. Caller holds ``ctx.mutex``."""
        self.view = None

    def record(self, item: Item, seq: int, value: Any) -> None:
        """Reflect one of this backend's commits in the view (or keep it for the next)."""
        with self._ctx.mutex:
            if self.view is not None:
                self.view.absorb(item, seq, value)
            else:
                self.unseen.append((item, seq, value))

    def writable(self) -> tuple[View, Layout]:
        """The view plus the journal layout, creating the snapshot on a first write."""
        view = self.ensure()
        if view.epoch is None:
            self._snapshots.ensure()
            with self._ctx.mutex:
                if self.view is view:  # keep a view a concurrent first write installed
                    self.drop()
            view = self.ensure()
        if view.epoch is None:  # pragma: no cover - the snapshot was just created
            raise StateError(f"state {self._ctx.uri} has no journal epoch")
        return view, self._ctx.layout(view.epoch)
