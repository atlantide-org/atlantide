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

from atlantide.core.check import FAIL, OK, Check
from atlantide.core.errors import LockError, StateError
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
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
    secret_digests_json TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks (
    node_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires_at REAL NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('serial', '0');
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
            self._conn.executescript(_SCHEMA)
            self._migrate()
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            raise StateError(f"cannot open state at {path!r}: {exc}") from exc

    def _migrate(self) -> None:
        """Add any columns missing from an existing state database."""
        # There are no versioned migrations, and CREATE TABLE IF NOT EXISTS does
        # not add columns to an existing table, so each column is added
        # idempotently and a duplicate-column error is ignored.
        with suppress(sqlite3.OperationalError):
            self._conn.execute(
                "ALTER TABLE nodes ADD COLUMN secret_digests_json TEXT NOT NULL DEFAULT '{}'"
            )

    @contextmanager
    def _transaction(self, what: str) -> Iterator[None]:
        """Run a mutation atomically; roll back and re-raise as StateError."""
        try:
            self._conn.execute("BEGIN")
            yield
            self._conn.execute("COMMIT")
        except sqlite3.Error as exc:
            self._conn.execute("ROLLBACK")
            raise StateError(f"{what} failed: {exc}") from exc

    # -- state ------------------------------------------------------------

    def load(self) -> StateGraph:
        rows = self._conn.execute("SELECT * FROM nodes").fetchall()
        return StateGraph(nodes={row["id"]: node_from_row(row) for row in rows})

    def put(self, node: StateNode) -> None:
        with self._transaction(f"put({node.id!r})"):
            self._conn.execute(_INSERT_NODE, node_columns(node))
            self._bump_serial()

    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert every node in one transaction (one serial bump for the batch)."""
        rows = [node_columns(node) for node in nodes]
        if not rows:
            return
        with self._transaction(f"put_many({len(rows)} nodes)"):
            self._conn.executemany(_INSERT_NODE, rows)
            self._bump_serial()

    def delete(self, node_id: str) -> None:
        with self._transaction(f"delete({node_id!r})"):
            deleted = self._conn.execute(
                "DELETE FROM nodes WHERE id = ?", (node_id,)
            ).rowcount
            if deleted:
                self._bump_serial()

    def serial(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='serial'").fetchone()
        return int(row["value"])

    # -- committed stack outputs ------------------------------------------

    def set_outputs(self, outputs: Mapping[str, Any]) -> None:
        merged = {**self.outputs(), **outputs}
        with self._transaction("set_outputs"):
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('outputs', ?)",
                (JSON_OBJ.dump_json(merged).decode(),),
            )

    def outputs(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT value FROM meta WHERE key='outputs'").fetchone()
        return JSON_OBJ.validate_json(row["value"]) if row else {}

    def _bump_serial(self) -> None:
        self._conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) "
            "WHERE key='serial'"
        )

    # -- locking ----------------------------------------------------------

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
            for node_id in sorted(scope):
                self._conn.execute(
                    "INSERT OR REPLACE INTO locks(node_id, owner, expires_at) VALUES (?,?,?)",
                    (node_id, owner, expires),
                )
            self._conn.execute("COMMIT")
            return Success(Lease(owner=owner, expires_at=expires, scope=frozenset(scope)))
        except sqlite3.Error as exc:
            self._conn.execute("ROLLBACK")
            raise StateError(f"acquire_lock failed: {exc}") from exc

    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._conn.execute("DELETE FROM locks WHERE owner = ?", (owner,))
        return Success(None)

    def _read_holds(self, scope: Set[str]) -> dict[str, Lease]:
        """Leases currently held over any node id in ``scope``."""
        if not scope:
            return {}
        placeholders = ",".join("?" * len(scope))
        rows = self._conn.execute(
            f"SELECT node_id, owner, expires_at FROM locks WHERE node_id IN ({placeholders})",
            tuple(sorted(scope)),
        ).fetchall()
        return {
            row["node_id"]: Lease(owner=row["owner"], expires_at=row["expires_at"])
            for row in rows
        }

    # -- lock administration ----------------------------------------------

    def locks(self) -> dict[str, Lease]:
        rows = self._conn.execute("SELECT node_id, owner, expires_at FROM locks").fetchall()
        return {
            row["node_id"]: Lease(owner=row["owner"], expires_at=row["expires_at"])
            for row in rows
        }

    def force_unlock(self, node_ids: Set[str]) -> int:
        broken = 0
        with self._transaction("force_unlock"):
            for node_id in sorted(node_ids):
                broken += self._conn.execute(
                    "DELETE FROM locks WHERE node_id = ?", (node_id,)
                ).rowcount
        return broken

    # -- preflight ---------------------------------------------------------

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

    def close(self) -> None:
        self._conn.close()

    def __del__(self) -> None:
        # Fallback for callers that did not call ``close()``.
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()
