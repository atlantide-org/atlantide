"""Default state backend: embedded SQLite (WAL), single file, ACID.

Each :meth:`put`/:meth:`delete` commits one row and bumps the serial in a
transaction, so a crash mid-apply leaves a consistent state a re-run can resume
from. The ``locks`` table holds one row per locked node id (owner + lease
expiry), so disjoint applies don't contend.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator, Mapping, Set
from contextlib import contextmanager, suppress
from typing import Any

from pydantic import TypeAdapter
from returns.result import Failure, Result, Success

from atlantide.core.errors import LockError, StateError
from atlantide.state.backend import (
    Clock,
    Lease,
    StateBackend,
    StateGraph,
    StateNode,
    scope_conflict,
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

# Marshalers for the JSON-encoded columns: validate on load, compact on write.
_JSON_OBJ: TypeAdapter[dict[str, Any]] = TypeAdapter(dict[str, Any])
_DEPS: TypeAdapter[tuple[str, ...]] = TypeAdapter(tuple[str, ...])
_DIGESTS: TypeAdapter[dict[str, str]] = TypeAdapter(dict[str, str])


class SqliteStateBackend(StateBackend):
    def __init__(self, path: str, *, clock: Clock = time.time) -> None:
        self._now = clock
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
        """Add columns missing from a state db created by an older build."""
        # No versioned migrations; CREATE TABLE IF NOT EXISTS won't add columns to
        # an existing table, so add them idempotently (duplicate-column is benign).
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
        nodes = {row["id"]: _row_to_node(row) for row in rows}
        return StateGraph(nodes=nodes)

    def put(self, node: StateNode) -> None:
        with self._transaction(f"put({node.id!r})"):
            self._conn.execute(
                "INSERT OR REPLACE INTO nodes"
                "(id, type, provider, provider_version, input_hash, outputs_json,"
                " properties_json, deps_json, prevent_destroy, status, secret_digests_json)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    node.id, node.type, node.provider, node.provider_version,
                    node.input_hash, _JSON_OBJ.dump_json(node.outputs).decode(),
                    _JSON_OBJ.dump_json(node.properties).decode(),
                    _DEPS.dump_json(node.dependencies).decode(),
                    int(node.prevent_destroy), node.status,
                    _DIGESTS.dump_json(node.secret_digests).decode(),
                ),
            )
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
                (_JSON_OBJ.dump_json(merged).decode(),),
            )

    def outputs(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT value FROM meta WHERE key='outputs'").fetchone()
        return _JSON_OBJ.validate_json(row["value"]) if row else {}

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

    def close(self) -> None:
        self._conn.close()

    def __del__(self) -> None:
        # Close the connection if the caller did not; ``close()`` is the
        # primary path.
        conn = getattr(self, "_conn", None)
        if conn is not None:
            conn.close()


def _row_to_node(row: sqlite3.Row) -> StateNode:
    return StateNode(
        id=row["id"],
        type=row["type"],
        provider=row["provider"],
        provider_version=row["provider_version"],
        input_hash=row["input_hash"],
        outputs=_JSON_OBJ.validate_json(row["outputs_json"]),
        properties=_JSON_OBJ.validate_json(row["properties_json"]),
        dependencies=_DEPS.validate_json(row["deps_json"]),
        prevent_destroy=bool(row["prevent_destroy"]),
        status=row["status"],
        secret_digests=_DIGESTS.validate_json(row["secret_digests_json"]),
    )
