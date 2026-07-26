"""Default state backend: embedded SQLite (WAL), single file, ACID.

Each :meth:`put`/:meth:`delete` commits one row and bumps the serial in a
transaction, so a crash mid-apply leaves a consistent state a re-run can resume
from. The ``locks`` table holds one row per locked node id (owner + lease
expiry), so disjoint applies don't contend.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterable, Iterator, Mapping, Set
from contextlib import contextmanager, suppress
from typing import Any

from returns.result import Failure, Result, Success
from typing_extensions import override

from atlantide.core.check import FAIL, OK, Check
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
from atlantide.state.codec import (
    JSON_OBJ,
    NODE_COLUMNS,
    node_columns,
    node_from_row,
)

_INSERT_NODE = (
    f"INSERT OR REPLACE INTO nodes ({', '.join(NODE_COLUMNS)})"
    f" VALUES ({', '.join('?' * len(NODE_COLUMNS))})"
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL,
    provider         TEXT NOT NULL,
    provider_version TEXT NOT NULL,
    input_hash       TEXT NOT NULL,
    outputs_json     TEXT NOT NULL,
    properties_json  TEXT NOT NULL,
    deps_json        TEXT NOT NULL,
    prevent_destroy  INTEGER NOT NULL,
    status           TEXT NOT NULL,
    secret_digests_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks (
    node_id TEXT PRIMARY KEY,
    owner   TEXT NOT NULL,
    expires_at REAL NOT NULL,
    fence   INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('serial', '0');
INSERT OR IGNORE INTO meta(key, value) VALUES ('fence', '0');
"""


class SqliteStateBackend(StateBackend):
    def __init__(self, path: str, *, clock: Clock = time.time) -> None:
        self._now = clock
        self._path = path
        try:
            self._conn = sqlite3.connect(path, isolation_level=None)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise StateError(f"cannot open state at {path!r}: {exc}") from exc

    def _create_schema(self) -> None:
        """Create the tables if this file does not have them yet.

        One script, one explicit transaction: ``BEGIN IMMEDIATE`` takes sqlite's
        write lock for the whole thing, so two processes opening the same new file
        concurrently serialize instead of racing each other's DDL. The ordinary
        state lock is no use here — it lives in a table this may be creating.
        """
        self._conn.executescript(f"BEGIN IMMEDIATE;{_SCHEMA}COMMIT;")

    def _check(self, *touched: str) -> None:
        """Refuse a write this run's lease no longer covers.

        Read inside the caller's transaction, so the holds cannot change between
        the check and the write.
        """
        if self._lease is None:
            return
        self._refuse_unfenced(set(touched), self._read_holds(set(touched)))

    @contextmanager
    def _transaction(self, what: str, *, immediate: bool = False) -> Iterator[None]:
        """Run a mutation atomically; roll back and re-raise as StateError.

        Any exception rolls back, not only ``sqlite3.Error``: the connection is in
        autocommit mode outside ``BEGIN``, so an escaping exception would leave it
        inside an open transaction and fail every later write.

        ``immediate`` takes the write lock up front — required when the mutation
        *reads* its merge base inside the transaction (``set_outputs``), where a
        deferred BEGIN would let two writers read the same base and the loser
        fail with a busy-snapshot error instead of serializing.
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            # Guarded: when BEGIN itself failed there is no transaction, and a
            # bare ROLLBACK would raise and mask the original error.
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise StateError(f"{what} failed: {exc}") from exc
        except BaseException:
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise

    # -- state ------------------------------------------------------------

    @override
    def load(self) -> StateGraph:
        rows = self._conn.execute("SELECT * FROM nodes").fetchall()
        return StateGraph(nodes={row["id"]: node_from_row(row) for row in rows})

    @override
    def put(self, node: StateNode) -> None:
        columns = node_columns(node)  # serialize before BEGIN, as put_many does
        with self._transaction(f"put({node.id!r})"):
            self._check(node.id)
            self._conn.execute(_INSERT_NODE, columns)
            self._bump_serial()

    @override
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert every node in one transaction (one serial bump for the batch)."""
        rows = [node_columns(node) for node in nodes]
        if not rows:
            return
        with self._transaction(f"put_many({len(rows)} nodes)"):
            self._check(*(row[0] for row in rows))
            self._conn.executemany(_INSERT_NODE, rows)
            self._bump_serial()

    @override
    def delete(self, node_id: str) -> None:
        with self._transaction(f"delete({node_id!r})"):
            self._check(node_id)
            deleted = self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,)).rowcount
            if deleted:
                self._bump_serial()

    @override
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Deletes and upserts in one transaction, so a rekey cannot half-land."""
        ids = [(node_id,) for node_id in delete_ids]
        rows = [node_columns(node) for node in nodes]  # serialize before BEGIN
        if not ids and not rows:
            return
        with self._transaction(f"replace_many({len(ids)} deleted, {len(rows)} upserted)"):
            self._check(*(i[0] for i in ids), *(row[0] for row in rows))
            self._conn.executemany("DELETE FROM nodes WHERE id = ?", ids)
            self._conn.executemany(_INSERT_NODE, rows)
            self._bump_serial()

    @override
    def serial(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='serial'").fetchone()
        return int(row["value"])

    # -- committed stack outputs ------------------------------------------

    @override
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        dropped = set(remove)
        # The merge base is read inside the (immediate) transaction: reading it
        # outside would let two concurrent runs — which per-node locking
        # deliberately permits for disjoint stacks — both merge onto the same
        # base, and the second commit silently discard the first run's outputs.
        with self._transaction("set_outputs", immediate=True):
            merged = merge_outputs(self.outputs(), outputs, dropped)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('outputs', ?)",
                (JSON_OBJ.dump_json(merged).decode(),),
            )

    @override
    def outputs(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT value FROM meta WHERE key='outputs'").fetchone()
        return JSON_OBJ.validate_json(row["value"]) if row else {}

    def _next_fence(self) -> int:
        """Mint the next epoch. Called inside the acquire transaction, so two
        contending acquirers cannot be handed the same one."""
        self._conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='fence'"
        )
        row = self._conn.execute("SELECT value FROM meta WHERE key='fence'").fetchone()
        return int(row["value"])

    def _bump_serial(self) -> None:
        self._conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key='serial'"
        )

    # -- locking ----------------------------------------------------------

    @override
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        now = self._now()
        expires = now + ttl_seconds
        try:
            self._conn.execute("BEGIN IMMEDIATE")  # serialize contending acquirers
            if err := scope_conflict(self._read_holds(scope), owner, now, scope):
                self._conn.execute("ROLLBACK")
                return Failure(err)
            fence = self._next_fence()
            for node_id in sorted(scope):
                self._conn.execute(
                    "INSERT OR REPLACE INTO locks(node_id, owner, expires_at, fence) "
                    "VALUES (?,?,?,?)",
                    (node_id, owner, expires, fence),
                )
            self._conn.execute("COMMIT")
            return Success(self._minted_lease(owner, expires, scope, fence))
        except sqlite3.Error as exc:
            # Guarded: when BEGIN itself failed there is no transaction, and a
            # bare ROLLBACK would raise and mask the original error.
            with suppress(sqlite3.Error):
                self._conn.execute("ROLLBACK")
            raise StateError(f"acquire_lock failed: {exc}") from exc

    @override
    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._conn.execute("DELETE FROM locks WHERE owner = ?", (owner,))
        return Success(None)

    def _read_holds(self, scope: Set[str]) -> dict[str, Lease]:
        """Leases currently held over any node id in ``scope``."""
        if not scope:
            return {}
        placeholders = ",".join("?" * len(scope))
        rows = self._conn.execute(
            f"SELECT node_id, owner, expires_at, fence FROM locks "
            f"WHERE node_id IN ({placeholders})",
            tuple(sorted(scope)),
        ).fetchall()
        return {row["node_id"]: _lease_of(row) for row in rows}

    # -- lock administration ----------------------------------------------

    @override
    def locks(self) -> dict[str, Lease]:
        rows = self._conn.execute("SELECT node_id, owner, expires_at, fence FROM locks").fetchall()
        return {row["node_id"]: _lease_of(row) for row in rows}

    @override
    def force_unlock(self, node_ids: Set[str]) -> int:
        broken = 0
        with self._transaction("force_unlock"):
            for node_id in sorted(node_ids):
                broken += self._conn.execute(
                    "DELETE FROM locks WHERE node_id = ?", (node_id,)
                ).rowcount
        return broken

    # -- preflight ---------------------------------------------------------

    @override
    def check(self) -> list[Check]:
        """A local file is usable when it opens and its directory is writable."""
        try:
            self._conn.execute("SELECT 1 FROM nodes LIMIT 1").fetchall()
        except sqlite3.Error as exc:
            return [Check("state file", FAIL, f"{self._path} unreadable: {exc}")]
        nodes = len(self.load())
        return [
            Check("state file", OK, f"{self._path} ({nodes} node(s))"),
            Check(
                "sharing",
                OK,
                "local sqlite — single machine; set [state].backend for a shared one",
            ),
        ]

    @override
    def close(self) -> None:
        self._conn.close()

    def __del__(self) -> None:
        """Best-effort close for callers that did not call :meth:`close`."""
        close_quietly(self)


def _lease_of(row: Any) -> Lease:
    """One `locks` row as a Lease."""
    return Lease(owner=row["owner"], expires_at=row["expires_at"], fence=int(row["fence"]))
