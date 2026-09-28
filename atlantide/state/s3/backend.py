"""Remote state backend: an S3 snapshot + per-node journal, DynamoDB heads and leases.

A write stores the node's new record as a small S3 journal entry, then commits
it with one conditional ``UpdateItem`` on the node's head in DynamoDB, fenced at
that head against the bound lease. Reads fold the snapshot and the committed
entries past its watermarks; compaction folds them in under an ETag
compare-and-swap. The key layout is :mod:`atlantide.state.s3.journal`.

:class:`S3StateBackend` delegates to collaborators that share one
:class:`~atlantide.state.s3.context.S3Context` and its mutex. The protocol,
including what it does not make atomic, is in ``state/README.md`` §Layout,
§Writes and fencing, and §Locking.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Set
from dataclasses import dataclass
from typing import Any, ClassVar, override

from botocore.exceptions import BotoCoreError, ClientError
from returns.result import Result

from atlantide.core.check import Check
from atlantide.core.errors import LockError, StateError
from atlantide.state.backend import StateBackend
from atlantide.state.leases import DEFAULT_SKEW_MARGIN, Clock, Lease, require_skew_margin
from atlantide.state.model import StateGraph, StateNode
from atlantide.state.s3 import limits
from atlantide.state.s3.context import S3Context, backend_log
from atlantide.state.s3.dynamo import Clients
from atlantide.state.s3.fences import Fences
from atlantide.state.s3.lease_items import LeaseItems
from atlantide.state.s3.lock_table import LockTable
from atlantide.state.s3.maintenance import CompactionReport, Compactor, FsckReport, Maintenance
from atlantide.state.s3.outputs import OutputWrites
from atlantide.state.s3.preflight import run_checks, run_probe
from atlantide.state.s3.reads import Reads
from atlantide.state.s3.snapshots import Snapshots
from atlantide.state.s3.view import ViewCache
from atlantide.state.s3.writes import Writes

__all__ = ["S3StateBackend"]


@dataclass(slots=True)
class _Parts:
    """The collaborators behind one :class:`S3StateBackend`, all on one context."""

    snapshots: Snapshots
    reads: Reads
    views: ViewCache
    fences: Fences
    lease_items: LeaseItems
    locks: LockTable
    maintenance: Maintenance
    compactor: Compactor
    writes: Writes
    output_writes: OutputWrites


def _wire(ctx: S3Context, compact_every: int, checkpoint: Callable[[], None]) -> _Parts:
    """Build the collaborator graph over ``ctx``; ``checkpoint`` is what the
    background compactor runs."""
    snapshots = Snapshots(ctx)
    reads = Reads(ctx, snapshots)
    views = ViewCache(ctx, reads, snapshots)
    fences = Fences(ctx)
    lease_items = LeaseItems(ctx, reads)
    locks = LockTable(
        ctx,
        snapshots=snapshots,
        views=views,
        reads=reads,
        lease_items=lease_items,
        fences=fences,
    )
    maintenance = Maintenance(ctx, snapshots, reads, fences)
    compactor = Compactor(ctx, every=compact_every, run=checkpoint)
    writes = Writes(
        ctx,
        snapshots=snapshots,
        reads=reads,
        views=views,
        lease_items=lease_items,
        maintenance=maintenance,
        compactor=compactor,
    )
    output_writes = OutputWrites(ctx, reads=reads, views=views, writes=writes, compactor=compactor)
    return _Parts(
        snapshots=snapshots,
        reads=reads,
        views=views,
        fences=fences,
        lease_items=lease_items,
        locks=locks,
        maintenance=maintenance,
        compactor=compactor,
        writes=writes,
        output_writes=output_writes,
    )


class S3StateBackend(StateBackend):
    """State as an S3 snapshot + journal, heads and leases in DynamoDB."""

    #: Every call is a network round trip; boto3 clients are thread-safe and the
    #: cached view is guarded by a lock.
    offload_writes: ClassVar[bool] = True
    #: Writes to different nodes are independent commits on independent heads.
    write_concurrency: int = limits.DEFAULT_WRITE_CONCURRENCY

    def __init__(  # noqa: PLR0913 - one keyword per [state] setting
        self,
        bucket: str,
        key: str,
        *,
        lock_table: str,
        journal_table: str | None = None,
        region: str | None = None,
        profile: str | None = None,
        endpoint_url: str | None = None,
        kms_key_id: str | None = None,
        lock_skew_margin: float = DEFAULT_SKEW_MARGIN,
        write_concurrency: int | None = None,
        compact_every: int = limits.COMPACT_EVERY,
        clock: Clock = time.time,
    ) -> None:
        require_skew_margin(lock_skew_margin)
        if write_concurrency is not None:
            if write_concurrency < 1:
                raise StateError(
                    f"[state].write_concurrency must be at least 1, got {write_concurrency}"
                )
            self.write_concurrency = write_concurrency
        self._now = clock
        self._ctx = ctx = S3Context(
            bucket=bucket,
            key=key,
            lock_table=lock_table,
            heads_table=journal_table or lock_table,
            kms_key_id=kms_key_id,
            skew_margin=lock_skew_margin,
            clock=clock,
            clients=Clients.connect(profile=profile, region=region, endpoint_url=endpoint_url),
            bound_lease=lambda: self._lease,
        )
        # Resolved when the compactor thread starts, so a patched `checkpoint` is the one run.
        parts = _wire(ctx, compact_every, checkpoint=lambda: self.checkpoint())
        self._snapshots = parts.snapshots
        self._reads = parts.reads
        self._views = parts.views
        self._fences = parts.fences
        self._lease_items = parts.lease_items
        self._locks = parts.locks
        self._maintenance = parts.maintenance
        self._compactor = parts.compactor
        self._writes = parts.writes
        self._output_writes = parts.output_writes

    @override
    def __repr__(self) -> str:
        ctx = self._ctx
        return (
            f"S3StateBackend({ctx.uri!r}, lock_table={ctx.lock_table!r}, "
            f"journal_table={ctx.heads_table!r})"
        )

    # The clients live on the shared context; tests swap them for wrapping ones.

    @property
    def _s3(self) -> Any:
        return self._ctx.clients.s3

    @_s3.setter
    def _s3(self, client: Any) -> None:
        self._ctx.clients.s3 = client

    @property
    def _ddb(self) -> Any:
        return self._ctx.clients.ddb

    @_ddb.setter
    def _ddb(self, client: Any) -> None:
        self._ctx.clients.ddb = client

    # -- reading ----------------------------------------------------------
    # Copies are taken under the lock: writer threads mutate the view in place.

    @override
    def load(self) -> StateGraph:
        view = self._views.ensure()
        with self._ctx.mutex:
            return StateGraph(nodes=dict(view.nodes))

    @override
    def serial(self) -> int:
        view = self._views.ensure()
        with self._ctx.mutex:
            return view.serial

    @override
    def outputs(self) -> dict[str, Any]:
        view = self._views.ensure()
        with self._ctx.mutex:
            return dict(view.outputs)

    # -- writing ----------------------------------------------------------

    @override
    def put(self, node: StateNode) -> None:
        self._writes.write_node(node.id, node)

    @override
    def delete(self, node_id: str) -> None:
        self._writes.write_node(node_id, None)

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        self._writes.replace_many((), nodes)

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        self._writes.replace_many(delete_ids, nodes)

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        self._output_writes.set_outputs(outputs, remove=remove)

    # -- locking ----------------------------------------------------------

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        return self._locks.acquire(owner, ttl_seconds, scope)

    @override
    def renew_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        """Push the lease's expiry out, keeping the bound lease's fence
        (see :meth:`LockTable.renew <atlantide.state.s3.lock_table.LockTable.renew>`)."""
        return self._locks.renew(owner, ttl_seconds, scope)

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        return self._locks.release(owner)

    @override
    def locks(self) -> dict[str, Lease]:
        return self._locks.locks()

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        return self._locks.force_unlock(node_ids)

    # -- maintenance ------------------------------------------------------

    @override
    def checkpoint(self) -> None:
        """Fold the journal into the snapshot; best-effort, logging AWS and state errors."""
        try:
            self.compact()
        except (StateError, ClientError, BotoCoreError) as exc:
            backend_log.warning("state compaction of %s deferred: %s", self._ctx.uri, exc)

    def compact(self) -> CompactionReport:
        """Fold every committed entry into the snapshot, then delete what it supersedes."""
        return self._maintenance.compact()

    def fsck(self, *, rebuild_heads: bool = False) -> FsckReport:
        """Cross-check heads against journal entries; optionally rebuild lost heads."""
        return self._maintenance.fsck(rebuild_heads=rebuild_heads)

    # -- preflight ---------------------------------------------------------

    @override
    def check(self) -> list[Check]:
        ctx = self._ctx
        return run_checks(
            ctx.clients.s3,
            ctx.clients.ddb,
            bucket=ctx.bucket,
            key=ctx.key,
            lock_table=ctx.lock_table,
            journal_table=ctx.heads_table,
        )

    @override
    def probe(self) -> Check:
        ctx = self._ctx
        return run_probe(
            ctx.clients.s3,
            ctx.clients.ddb,
            bucket=ctx.bucket,
            key=ctx.key,
            journal_table=ctx.heads_table,
            namespace=ctx.namespace,
        )

    @override
    def close(self) -> None:
        self._compactor.join(timeout=60.0)
        with self._ctx.mutex:
            self._views.drop()
