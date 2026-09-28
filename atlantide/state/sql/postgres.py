"""Remote state backend: PostgreSQL, one row per node.

Mirrors the sqlite tables (``nodes`` / ``meta`` / ``locks``) in a configurable
schema, rendered from the same spec (:mod:`atlantide.state.sql.schema`) and read
through the same row codec (:mod:`atlantide.state.codec`).
Postgres has real transactions, so each ``put``/``delete`` keeps per-node
granularity and bumps the serial in the same transaction.

Locks are taken with a conditional upsert (free, already ours, or expired) inside
one transaction, so a contended scope leaves no partial holds.

**Lease time is the server's.** Every expiry is written and judged against the
database's ``clock_timestamp()``, read inside the transaction that uses it, so
client clock skew does not affect who holds a lease. A takeover still waits
``lock_skew_margin`` seconds past expiry, as on S3, so a holder whose renewal
is late is not overtaken the moment its lease lapses. Leases returned to the
caller (and those :meth:`locks` reports) are translated to the local clock,
preserving the remaining time, because :class:`LeaseGuard` and the
``state unlock`` display compare them against ``time.time()``.

Requires the ``postgres`` extra (``psycopg``), imported in the constructor so
other backends do not need it.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from contextlib import suppress
from typing import Any, ClassVar, override

from returns.result import Failure, Result, Success

from atlantide.core.check import Check
from atlantide.core.errors import LockError, StateError
from atlantide.core.logging import get_logger
from atlantide.core.node_id import stack_of
from atlantide.state.backend import StateBackend, merge_outputs
from atlantide.state.codec import JSON_OBJ, node_columns, node_from_row, outputs_from_text
from atlantide.state.fencing import fence_violation, scope_conflict
from atlantide.state.leases import DEFAULT_SKEW_MARGIN, Clock, Lease, require_skew_margin
from atlantide.state.model import StateGraph, StateNode
from atlantide.state.sql import postgres_sql as stmt
from atlantide.state.sql.common import Contended, holds_from_rows, lease_from_row
from atlantide.state.sql.dsn import dsn_host, scrub_dsn
from atlantide.state.sql.postgres_preflight import preflight
from atlantide.state.sql.postgres_sql import Conn
from atlantide.state.sql.schema import (
    LOCK_COLUMNS,
    POSTGRES_DDL,
    POSTGRES_INSERT_NODE,
    POSTGRES_SELECT_NODES,
)

__all__ = ["PostgresStateBackend"]

_log = get_logger("state.sql.postgres")


class PostgresStateBackend(StateBackend):
    """State in PostgreSQL tables; same semantics as sqlite, shared across hosts."""

    #: Every call is a network round trip. Safe across threads because every use
    #: of the connection goes through :meth:`_run`, which holds ``_conn_lock``.
    offload_writes: ClassVar[bool] = True

    def __init__(
        self,
        dsn: str,
        *,
        schema: str = "atlantide",
        lock_skew_margin: float = DEFAULT_SKEW_MARGIN,
        clock: Clock | None = None,
    ) -> None:
        """Connect to ``dsn`` and create ``schema`` and its tables if missing.

        ``clock`` is a test hook: when given, lease time is read from it instead of
        the server. When ``None`` (outside tests), the server's clock decides every
        expiry.
        """
        require_skew_margin(lock_skew_margin)
        try:
            import psycopg
            from psycopg import sql
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise StateError(
                "the postgres state backend requires the 'postgres' extra — "
                "install atlantide[postgres]"
            ) from exc
        self._psycopg = psycopg
        self._sql = sql
        self._row_factory = dict_row
        self._schema = schema
        self._schema_id = sql.Identifier(schema)
        self._dsn = dsn
        self._skew_margin = lock_skew_margin
        #: Whether lease time comes from the database (the default) or ``clock``.
        self._server_time = clock is None
        #: The local clock: used only to translate a server-side expiry into local
        #: terms for the caller, never to decide one.
        self._now = clock if clock is not None else time.time
        # psycopg serializes single statements, not a ``transaction()`` block:
        # another thread's statement could land inside it (or open a savepoint
        # there), and a reconnect swaps ``_conn`` under a caller. The apply's
        # writer thread and the loop thread both call in, so each unit of work
        # holds this. Reentrant: work may call ``_run`` again.
        self._conn_lock = threading.RLock()
        self._conn = self._connect()
        self._create_schema()

    # -- schema -----------------------------------------------------------

    def _create_schema(self) -> None:
        """Create the schema and its tables if they are not there yet.

        The DDL runs in one transaction guarded by ``pg_advisory_xact_lock``, so
        two hosts starting against the same fresh database serialize rather than
        racing each other's ``CREATE``. The state lock cannot serve here: it lives
        in a table this may be creating.
        """

        def work(conn: Conn) -> None:
            with conn.transaction():
                # A stable key per schema name, so different schemas in one
                # database do not block each other.
                conn.execute(
                    self._sql.SQL("SELECT pg_advisory_xact_lock(hashtext({}))").format(
                        self._sql.Literal(f"atlantide.schema.{self._schema}")
                    )
                )
                conn.execute(self._query(POSTGRES_DDL))

        self._run(work)

    # -- connection -------------------------------------------------------

    def _connect(self) -> Conn:
        try:
            return self._psycopg.connect(self._dsn, autocommit=True, row_factory=self._row_factory)
        except self._psycopg.ProgrammingError:
            # A malformed connection string. psycopg's message quotes the failing
            # fragment, which may be part of the password, so it is withheld, also
            # from the exception chain.
            raise StateError(
                "cannot connect to the postgres state backend: the dsn is not a valid "
                "connection string (details withheld: they may quote the password)"
            ) from None
        except self._psycopg.Error as exc:
            raise StateError(
                f"cannot connect to the postgres state backend: {scrub_dsn(str(exc), self._dsn)}"
            ) from exc

    def _query(self, text: str) -> Any:
        """Bind the configured schema into ``text`` as a quoted identifier."""
        return self._sql.SQL(text).format(schema=self._schema_id)

    def _run[T](self, work: Callable[[Conn], T]) -> T:
        """Run ``work`` against the connection, reconnecting once if it went away.

        Long applies outlive server-side idle timeouts. Every caller is a
        self-contained statement or transaction, so a retry is safe but not
        exactly-once: a connection that drops after the server committed replays
        the work and can bump the serial twice. The serial is only compared for
        staleness, never counted, so a gap is harmless.

        A closed or broken connection is replaced. A deadlock or serialization
        failure on a live one is retried once on the same connection, after a
        rollback: the server aborted the transaction, so nothing of it landed.
        Any other ``OperationalError`` (a lock or statement timeout) is reported,
        not retried. Either attempt's driver error surfaces as :class:`StateError`.
        """
        errors = self._psycopg.errors
        with self._conn_lock:
            try:
                return work(self._conn)
            except (self._psycopg.OperationalError, self._psycopg.InterfaceError) as exc:
                if self._conn.closed or self._conn.broken:
                    _log.debug(
                        "postgres state connection lost (%s); reconnecting", type(exc).__name__
                    )
                    with suppress(Exception):
                        self._conn.close()
                    self._conn = self._connect()
                elif isinstance(exc, (errors.DeadlockDetected, errors.SerializationFailure)):
                    _log.debug(
                        "postgres state transaction aborted (%s); retrying", type(exc).__name__
                    )
                    # A no-op in autocommit once `transaction()` has exited; kept
                    # so a retry never runs inside the aborted transaction.
                    with suppress(Exception):
                        self._conn.rollback()
                else:
                    raise StateError(f"postgres state backend failed: {exc}") from exc
            except self._psycopg.Error as exc:
                raise StateError(f"postgres state backend failed: {exc}") from exc
            # The retry runs outside the handler above, so its own failure is
            # mapped here rather than escaping as a raw driver error.
            try:
                return work(self._conn)
            except self._psycopg.Error as exc:
                raise StateError(f"postgres state backend failed: {exc}") from exc

    def _execute(self, text: str, params: Sequence[Any] = ()) -> None:
        self._run(lambda conn: conn.execute(self._query(text), params))

    def _fetch_one(self, text: str, params: Sequence[Any] = ()) -> Any:
        """The first row as a ``{column: value}`` mapping, or ``None``."""
        return self._run(lambda conn: conn.execute(self._query(text), params).fetchone())

    def _fetch_all(self, text: str, params: Sequence[Any] = ()) -> list[Any]:
        """Every row as a ``{column: value}`` mapping."""
        rows: list[Any] = self._run(lambda conn: conn.execute(self._query(text), params).fetchall())
        return rows

    # -- state ------------------------------------------------------------

    @override
    def load(self) -> StateGraph:
        rows = self._fetch_all(POSTGRES_SELECT_NODES)
        return StateGraph(nodes={row["id"]: node_from_row(row) for row in rows})

    @override
    def put(self, node: StateNode) -> None:
        def work(conn: Conn) -> None:
            conn.execute(self._query(POSTGRES_INSERT_NODE), node_columns(node))
            conn.execute(self._query(stmt.BUMP_SERIAL))

        self._in_transaction(work, node.id)

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert every node in one transaction: all land or none do."""
        rows = [node_columns(node) for node in nodes]
        if not rows:
            return

        def work(conn: Conn) -> None:
            conn.cursor().executemany(self._query(POSTGRES_INSERT_NODE), rows)
            conn.execute(self._query(stmt.BUMP_SERIAL))

        self._in_transaction(work, *(row[0] for row in rows))

    @override
    def delete(self, node_id: str) -> None:
        def work(conn: Conn) -> None:
            deleted = conn.execute(self._query(stmt.DELETE_NODE), (node_id,)).rowcount
            if deleted:
                conn.execute(self._query(stmt.BUMP_SERIAL))

        self._in_transaction(work, node_id)

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Deletes and upserts in one transaction, so a rekey cannot half-land."""
        ids = [(node_id,) for node_id in delete_ids]
        rows = [node_columns(node) for node in nodes]
        if not ids and not rows:
            return

        def work(conn: Conn) -> None:
            cursor = conn.cursor()
            if ids:
                cursor.executemany(self._query(stmt.DELETE_NODE), ids)
            if rows:
                cursor.executemany(self._query(POSTGRES_INSERT_NODE), rows)
            conn.execute(self._query(stmt.BUMP_SERIAL))

        self._in_transaction(work, *(i[0] for i in ids), *(row[0] for row in rows))

    @override
    def serial(self) -> int:
        row = self._fetch_one("SELECT value FROM {schema}.meta WHERE key = 'serial'")
        return int(row["value"]) if row else 0

    def _in_transaction(self, work: Callable[[Conn], Any], *touched: str) -> None:
        """Run ``work`` atomically, refusing it if this run no longer holds ``touched``.

        The check runs in the same transaction as the write and reads the holds
        ``FOR UPDATE``, so no other run can take the lease between the check and
        the write.
        """

        def atomically(conn: Conn) -> None:
            with conn.transaction():
                self._check(conn, touched)
                work(conn)

        self._run(atomically)

    def _check(self, conn: Conn, nodes: Iterable[str]) -> None:
        touched = set(nodes)
        if self._lease is None or not touched:
            return
        held = holds_from_rows(
            conn.execute(self._query(stmt.HELD_FOR_UPDATE), (sorted(touched),)).fetchall()
        )
        # Expiry judged on the database clock, read after the rows were locked.
        violation = fence_violation(self._lease, held, self._db_now(conn), touched)
        if violation is not None:
            raise violation

    # -- time --------------------------------------------------------------

    def _db_now(self, conn: Conn) -> float:
        """Lease time now: the server's clock, or the injected test clock."""
        if not self._server_time:
            return self._now()
        return float(conn.execute(self._query(stmt.SERVER_NOW)).fetchone()["now"])

    def _local(self, expires_at: float, now: float) -> float:
        """``expires_at`` (lease time, read at ``now``) moved onto the local clock.

        The remaining duration is what is preserved: a caller comparing the
        result with ``time.time()`` sees exactly as much left as the server does,
        however far apart the two clocks are.
        """
        if not self._server_time:
            return expires_at
        return self._now() + (expires_at - now)

    # -- committed stack outputs ------------------------------------------

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        """Merge stack outputs, fenced on the bound lease's nodes in those stacks.

        Outputs are keyed ``{stack}:{name}`` and have no lock of their own: a run
        may publish a stack's outputs only while holding that stack's nodes, so
        the write is fenced on them as a ``put`` would be. A stack in which the
        lease holds no node is merged unfenced; the ``meta`` row lock still
        prevents a lost concurrent merge.
        """
        dropped = set(remove)
        stacks = {stack_of(key) for key in (*outputs, *dropped)}
        lease = self._lease
        fenced = (
            {nid for nid in lease.scope if stack_of(nid) in stacks} if lease is not None else set()
        )

        def merge(conn: Conn) -> None:
            # Read-modify-write under a row lock: otherwise two concurrent runs on
            # disjoint stacks merge onto the same base and the second commit
            # discards the first run's outputs.
            with conn.transaction():
                self._check(conn, fenced)
                conn.execute(self._query(stmt.SEED_OUTPUTS))
                row = conn.execute(self._query(stmt.OUTPUTS_FOR_UPDATE)).fetchone()
                current = outputs_from_text(row["value"]) if row else {}
                merged = merge_outputs(current, outputs, dropped)
                conn.execute(
                    self._query(stmt.UPDATE_OUTPUTS), (JSON_OBJ.dump_json(merged).decode(),)
                )

        self._run(merge)

    @override
    def outputs(self) -> dict[str, Any]:
        row = self._fetch_one("SELECT value FROM {schema}.meta WHERE key = 'outputs'")
        return outputs_from_text(row["value"]) if row else {}

    # -- locking ----------------------------------------------------------

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        if not scope:
            return Success(Lease(owner=owner, expires_at=self._now() + ttl_seconds))

        granted: list[tuple[int, float]] = []

        def take_every_node(conn: Conn) -> None:
            row = conn.execute(self._query(stmt.BUMP_FENCE)).fetchone()
            fence = int(row["value"])
            now = self._db_now(conn)
            expires = now + ttl_seconds
            stale = now - self._skew_margin
            for node_id in sorted(scope):
                taken = conn.execute(
                    self._query(stmt.LOCK), (node_id, owner, expires, fence, stale)
                ).fetchone()
                if taken is None:
                    raise Contended(self._blocker(conn, node_id, owner, stale))
            granted.append((fence, self._local(expires, now)))

        try:
            self._in_transaction(take_every_node)
        except Contended as contended:
            return Failure(contended.error)
        fence, expires = granted[-1]
        return Success(self._minted_lease(owner, expires, scope, fence))

    def _blocker(self, conn: Conn, node_id: str, owner: str, stale: float) -> LockError:
        """The error naming who holds ``node_id``, in the wording shared by all backends.

        Judged at ``stale`` (``now - margin``), as the lock condition was: a hold
        that expired within the skew margin still blocks, and should be named.
        """
        row = conn.execute(self._query(stmt.LOCK_HOLDER), (node_id,)).fetchone()
        held = {node_id: lease_from_row(row)} if row is not None else {}
        return scope_conflict(held, owner, stale, {node_id}) or LockError(
            f"node {node_id!r} is locked by another run"
        )

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._execute("DELETE FROM {schema}.locks WHERE owner = %s", (owner,))
        return Success(None)

    # -- lock administration ----------------------------------------------

    @override
    def locks(self) -> dict[str, Lease]:
        """Every hold, its expiry moved onto the local clock (see :meth:`_local`)."""

        def read(conn: Conn) -> dict[str, Lease]:
            now = self._db_now(conn)
            rows = conn.execute(self._query(f"SELECT {LOCK_COLUMNS} FROM {{schema}}.locks"))
            return holds_from_rows(rows.fetchall(), local=lambda at: self._local(at, now))

        return self._run(read)

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        if not node_ids:
            return 0
        query = self._query("DELETE FROM {schema}.locks WHERE node_id = ANY(%s)")
        return self._run(lambda conn: conn.execute(query, (sorted(node_ids),)).rowcount)

    # -- preflight ---------------------------------------------------------

    @override
    def check(self) -> list[Check]:
        """Confirm the server is reachable and this role can read and write state."""
        return preflight(self._schema, self._fetch_one, self._fetch_all)

    @override
    def close(self) -> None:
        with self._conn_lock:
            self._conn.close()

    @override
    def __repr__(self) -> str:
        # The host only: the DSN may carry a password.
        return f"PostgresStateBackend({dsn_host(self._dsn)!r}, schema={self._schema!r})"

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        """Best-effort close; a finalizer's failure is noise (see the sqlite backend)."""
        with suppress(Exception):
            self._conn.close()
