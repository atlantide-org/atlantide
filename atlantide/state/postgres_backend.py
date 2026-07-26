"""Remote state backend: PostgreSQL, one row per node.

A direct port of the sqlite schema — ``nodes`` / ``meta`` / ``locks`` — in a
configurable schema, sharing its column names and its row codec
(:mod:`atlantide.state.codec`). Postgres has real transactions, so each
``put``/``delete`` keeps its per-node granularity and bumps the serial in the
same transaction; a whole-graph blob would lose that and buy nothing.

Locks are taken with a conditional upsert (free, already ours, or expired) inside
one transaction, so a contended scope leaves no partial holds.

Requires the ``postgres`` extra (``psycopg``); the import is deferred to the
constructor so local runs never pay for it.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence, Set
from typing import Any, TypeVar

from returns.result import Failure, Result, Success
from typing_extensions import override

from atlantide.core.check import FAIL, OK, WARN, Check
from atlantide.core.errors import LockError, StateError
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
    close_quietly,
    merge_outputs,
    scope_conflict,
)
from atlantide.state.codec import JSON_OBJ, NODE_COLUMNS, node_columns, node_from_row

T = TypeVar("T")

# The JSON columns are jsonb, so the server validates them on write, but they
# are read back as text and the shared row codec decodes them unchanged.
_JSON_COLUMNS = frozenset(name for name in NODE_COLUMNS if name.endswith("_json"))
_projection = ", ".join(
    f"{name}::text AS {name}" if name in _JSON_COLUMNS else name for name in NODE_COLUMNS
)
_placeholders = ", ".join("%s::jsonb" if name in _JSON_COLUMNS else "%s" for name in NODE_COLUMNS)
_assignments = ", ".join(f"{name} = EXCLUDED.{name}" for name in NODE_COLUMNS[1:])

_SELECT_NODES = f"SELECT {_projection} FROM {{schema}}.nodes"
_INSERT_NODE = (
    f"INSERT INTO {{schema}}.nodes ({', '.join(NODE_COLUMNS)})"
    f" VALUES ({_placeholders})"
    f" ON CONFLICT (id) DO UPDATE SET {_assignments}"
)

_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.nodes (
    id                  TEXT PRIMARY KEY,
    type                TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    provider_version    TEXT    NOT NULL,
    input_hash          TEXT    NOT NULL,
    outputs_json        JSONB   NOT NULL,
    properties_json     JSONB   NOT NULL,
    deps_json           JSONB   NOT NULL,
    prevent_destroy     BOOLEAN NOT NULL,
    status              TEXT    NOT NULL,
    secret_digests_json JSONB   NOT NULL
);
CREATE TABLE IF NOT EXISTS {schema}.meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {schema}.locks (
    node_id    TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    fence      BIGINT NOT NULL
);
INSERT INTO {schema}.meta(key, value) VALUES ('serial', '0') ON CONFLICT DO NOTHING;
INSERT INTO {schema}.meta(key, value) VALUES ('fence', '0') ON CONFLICT DO NOTHING;
"""

#: Take one node: unheld, held by the same owner, or expired.
#: An empty RETURNING means the row is held by a live, different owner.
_LOCK = """
INSERT INTO {schema}.locks (node_id, owner, expires_at, fence) VALUES (%s, %s, %s, %s)
ON CONFLICT (node_id) DO UPDATE SET owner = EXCLUDED.owner,
    expires_at = EXCLUDED.expires_at, fence = EXCLUDED.fence
WHERE {schema}.locks.owner = EXCLUDED.owner OR {schema}.locks.expires_at < %s
RETURNING node_id
"""

#: Mint the next epoch. Runs inside the acquire transaction, so two contending
#: acquirers cannot be handed the same one.
_BUMP_FENCE = (
    "UPDATE {schema}.meta SET value = (CAST(value AS BIGINT) + 1)::TEXT "
    "WHERE key = 'fence' RETURNING value"
)

#: The holds over the nodes about to be written, locked FOR UPDATE so they cannot
#: change between this read and the write in the same transaction.
_HELD_FOR_UPDATE = (
    "SELECT node_id, owner, expires_at, fence FROM {schema}.locks "
    "WHERE node_id = ANY(%s) FOR UPDATE"
)

_BUMP_SERIAL = (
    "UPDATE {schema}.meta SET value = (CAST(value AS BIGINT) + 1)::TEXT WHERE key = 'serial'"
)


class _Contended(Exception):
    """Internal: abort the lock transaction so no partial holds are committed."""

    def __init__(self, error: LockError) -> None:
        self.error = error


class PostgresStateBackend(StateBackend):
    """State in PostgreSQL tables; same semantics as sqlite, shared across hosts."""

    def __init__(self, dsn: str, *, schema: str = "atlantide", clock: Clock = time.time) -> None:
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
        self._now = clock
        self._conn = self._connect()
        self._create_schema()

    # -- schema -----------------------------------------------------------

    def _create_schema(self) -> None:
        """Create the schema and its tables if they are not there yet.

        The DDL runs in one transaction guarded by ``pg_advisory_xact_lock``, so
        two hosts starting against the same fresh database serialize rather than
        racing each other's ``CREATE``. The ordinary state lock cannot serve here:
        it lives in a table this may be creating.
        """

        def work(conn: Any) -> None:
            with conn.transaction():
                # A stable key per schema name, so different schemas in one
                # database do not block each other.
                conn.execute(
                    self._sql.SQL("SELECT pg_advisory_xact_lock(hashtext({}))").format(
                        self._sql.Literal(f"atlantide.schema.{self._schema}")
                    )
                )
                conn.execute(self._query(_DDL))

        self._run(work)

    # -- connection -------------------------------------------------------

    def _connect(self) -> Any:
        try:
            return self._psycopg.connect(self._dsn, autocommit=True, row_factory=self._row_factory)
        except self._psycopg.Error as exc:
            raise StateError(f"cannot connect to the postgres state backend: {exc}") from exc

    def _query(self, text: str) -> Any:
        """Bind the configured schema into ``text`` as a quoted identifier."""
        return self._sql.SQL(text).format(schema=self._schema_id)

    def _run(self, work: Callable[[Any], T]) -> T:
        """Run ``work`` against the connection, reconnecting once if it went away.

        Long applies outlive server-side idle timeouts, and every caller here is
        a self-contained statement or transaction, so a retry is safe.

        "Safe" here means idempotent, not exactly-once: a connection that drops
        after the server committed but before the response arrived replays the
        work, so a serial can be bumped twice. The serial is only ever compared
        for staleness, never counted, so a gap is immaterial — but any future
        caller that treats it as a count must not rely on this path.
        """
        try:
            return work(self._conn)
        except (self._psycopg.OperationalError, self._psycopg.InterfaceError):
            self._conn = self._connect()
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
        rows = self._fetch_all(_SELECT_NODES)
        return StateGraph(nodes={row["id"]: node_from_row(row) for row in rows})

    @override
    def put(self, node: StateNode) -> None:
        def work(conn: Any) -> None:
            conn.execute(self._query(_INSERT_NODE), node_columns(node))
            conn.execute(self._query(_BUMP_SERIAL))

        self._in_transaction(work, node.id)

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert every node in one transaction — all of them land, or none do."""
        rows = [node_columns(node) for node in nodes]
        if not rows:
            return

        def work(conn: Any) -> None:
            conn.cursor().executemany(self._query(_INSERT_NODE), rows)
            conn.execute(self._query(_BUMP_SERIAL))

        self._in_transaction(work, *(row[0] for row in rows))

    @override
    def delete(self, node_id: str) -> None:
        def work(conn: Any) -> None:
            deleted = conn.execute(
                self._query("DELETE FROM {schema}.nodes WHERE id = %s"), (node_id,)
            ).rowcount
            if deleted:
                conn.execute(self._query(_BUMP_SERIAL))

        self._in_transaction(work, node_id)

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Deletes and upserts in one transaction, so a rekey cannot half-land."""
        ids = [(node_id,) for node_id in delete_ids]
        rows = [node_columns(node) for node in nodes]
        if not ids and not rows:
            return

        def work(conn: Any) -> None:
            cursor = conn.cursor()
            if ids:
                cursor.executemany(self._query("DELETE FROM {schema}.nodes WHERE id = %s"), ids)
            if rows:
                cursor.executemany(self._query(_INSERT_NODE), rows)
            conn.execute(self._query(_BUMP_SERIAL))

        self._in_transaction(work, *(i[0] for i in ids), *(row[0] for row in rows))

    @override
    def serial(self) -> int:
        row = self._fetch_one("SELECT value FROM {schema}.meta WHERE key = 'serial'")
        return int(row["value"]) if row else 0

    def _in_transaction(self, work: Callable[[Any], Any], *touched: str) -> None:
        """Run ``work`` atomically, refusing it if this run no longer holds ``touched``.

        The check is inside the same transaction as the write, and reads the holds
        ``FOR UPDATE`` — so a lease cannot be taken from under this run between
        the check and the write. That ordering is the whole point: an unfenced
        upsert is how two runs whose leases overlapped silently merged their state.
        """

        def atomically(conn: Any) -> None:
            with conn.transaction():
                self._check(conn, touched)
                work(conn)

        self._run(atomically)

    def _check(self, conn: Any, touched: Sequence[str]) -> None:
        if self._lease is None or not touched:
            return
        rows = conn.execute(self._query(_HELD_FOR_UPDATE), (list(touched),)).fetchall()
        held = {
            row["node_id"]: Lease(
                owner=row["owner"],
                expires_at=float(row["expires_at"]),
                fence=int(row["fence"]),
            )
            for row in rows
        }
        self._refuse_unfenced(set(touched), held)

    # -- committed stack outputs ------------------------------------------

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        dropped = set(remove)

        def merge(conn: Any) -> None:
            # Read-modify-write under a row lock in one transaction: two
            # concurrent runs over disjoint stacks (which per-node locking
            # permits) would otherwise both merge onto the same base and the
            # second commit silently discard the first run's outputs. The row is
            # created first so `FOR UPDATE` has something to lock on a fresh store.
            with conn.transaction():
                conn.execute(
                    self._query(
                        "INSERT INTO {schema}.meta(key, value) VALUES ('outputs', '{{}}') "
                        "ON CONFLICT (key) DO NOTHING"
                    )
                )
                row = conn.execute(
                    self._query("SELECT value FROM {schema}.meta WHERE key = 'outputs' FOR UPDATE")
                ).fetchone()
                current = JSON_OBJ.validate_json(row["value"]) if row else {}
                merged = merge_outputs(current, outputs, dropped)
                conn.execute(
                    self._query("UPDATE {schema}.meta SET value = %s WHERE key = 'outputs'"),
                    (JSON_OBJ.dump_json(merged).decode(),),
                )

        self._run(merge)

    @override
    def outputs(self) -> dict[str, Any]:
        row = self._fetch_one("SELECT value FROM {schema}.meta WHERE key = 'outputs'")
        return JSON_OBJ.validate_json(row["value"]) if row else {}

    # -- locking ----------------------------------------------------------

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        now = self._now()
        expires = now + ttl_seconds
        if not scope:
            return Success(Lease(owner=owner, expires_at=expires))

        minted: list[int] = []

        def take_every_node(conn: Any) -> None:
            row = conn.execute(self._query(_BUMP_FENCE)).fetchone()
            fence = int(row["value"])
            minted.append(fence)
            for node_id in sorted(scope):
                taken = conn.execute(
                    self._query(_LOCK), (node_id, owner, expires, fence, now)
                ).fetchone()
                if taken is None:
                    raise _Contended(self._blocker(conn, node_id, owner, now))

        try:
            self._in_transaction(take_every_node)
        except _Contended as contended:
            return Failure(contended.error)
        return Success(self._minted_lease(owner, expires, scope, minted[-1]))

    def _blocker(self, conn: Any, node_id: str, owner: str, now: float) -> LockError:
        """The error naming who holds ``node_id``, in the wording shared by all backends."""
        row = conn.execute(
            self._query("SELECT owner, expires_at FROM {schema}.locks WHERE node_id = %s"),
            (node_id,),
        ).fetchone()
        held = (
            {node_id: Lease(owner=row["owner"], expires_at=float(row["expires_at"]))}
            if row is not None
            else {}
        )
        return scope_conflict(held, owner, now, {node_id}) or LockError(
            f"node {node_id!r} is locked by another run"
        )

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._execute("DELETE FROM {schema}.locks WHERE owner = %s", (owner,))
        return Success(None)

    # -- lock administration ----------------------------------------------

    @override
    def locks(self) -> dict[str, Lease]:
        rows = self._fetch_all("SELECT node_id, owner, expires_at FROM {schema}.locks")
        return {
            row["node_id"]: Lease(owner=row["owner"], expires_at=float(row["expires_at"]))
            for row in rows
        }

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        if not node_ids:
            return 0
        deleted: int = self._run(
            lambda conn: (
                conn.execute(
                    self._query("DELETE FROM {schema}.locks WHERE node_id = ANY(%s)"),
                    (sorted(node_ids),),
                ).rowcount
            )
        )
        return deleted

    # -- preflight ---------------------------------------------------------

    @override
    def check(self) -> list[Check]:
        """Confirm the server is reachable and this role can read and write state."""
        try:
            self._fetch_one("SELECT 1 AS ok")
        except StateError as exc:
            return [Check("connection", FAIL, str(exc))]
        return [
            Check("connection", OK, f"schema {self._schema}"),
            self._check_tables(),
            self._check_writable(),
        ]

    def _check_tables(self) -> Check:
        rows = self._fetch_all(
            "SELECT tablename FROM pg_tables WHERE schemaname = %s", (self._schema,)
        )
        present = {row["tablename"] for row in rows}
        missing = sorted({"nodes", "meta", "locks"} - present)
        if missing:
            return Check(
                "tables",
                FAIL,
                f"missing {', '.join(missing)} in schema {self._schema!r} — the "
                f"backend creates them on connect, so this role likely lacks CREATE",
            )
        return Check("tables", OK, "nodes, meta, locks")

    def _check_writable(self) -> Check:
        """A read-only role plans fine and then fails mid-apply; find that out now."""
        row = self._fetch_one(
            "SELECT has_table_privilege(%s, 'INSERT') AS ok",
            (f"{self._schema}.nodes",),
        )
        if row is not None and row["ok"]:
            return Check("write access", OK, "INSERT granted on nodes")
        return Check(
            "write access",
            WARN,
            f"this role cannot INSERT into {self._schema}.nodes — plan works, apply will fail",
        )

    @override
    def close(self) -> None:
        self._conn.close()

    def __del__(self) -> None:  # pragma: no cover - best-effort cleanup
        close_quietly(self)
