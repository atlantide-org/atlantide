"""Default state backend: embedded SQLite (WAL), single file, ACID.

Each :meth:`put`/:meth:`delete` commits one row and bumps the serial in a
transaction, so a crash mid-apply leaves a consistent state a re-run can resume
from. The ``locks`` table holds one row per locked node id (owner + lease
expiry), so disjoint applies don't contend.

Every write transaction is ``BEGIN IMMEDIATE``. Most writes read the ``locks``
table before writing (the fence check); under a deferred ``BEGIN`` that read
opens a WAL snapshot, and if another process commits before the write, SQLite
cannot upgrade the stale snapshot and fails with "database is locked" without
waiting on the busy timeout. Taking the write lock up front makes contending
writers queue on the busy timeout instead.

The connection is opened with ``check_same_thread=False`` so both an apply's
state-writer thread and the loop thread (committed outputs, compensations, the
pre-run load) can use it. A sqlite3 connection is not safe for simultaneous use
from two threads, so every public method holds one reentrant lock for its whole
body and no transaction interleaves with another thread's statements.

Writes are not offloaded by default (``offload_writes = False``): a commit is a
local WAL append of ~0.1 ms, shorter than the thread hop. Offloading measured
~10% slower on an 8k-node local apply. A subclass may opt in.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Set
from contextlib import contextmanager, suppress
from typing import Any, ClassVar, Concatenate, override

from returns.result import Failure, Result, Success

from atlantide.core.check import FAIL, OK, Check
from atlantide.core.errors import LockError, StateError
from atlantide.state.backend import StateBackend, merge_outputs
from atlantide.state.codec import JSON_OBJ, node_columns, node_from_row, outputs_from_text
from atlantide.state.fencing import scope_conflict
from atlantide.state.leases import Clock, Lease
from atlantide.state.model import StateGraph, StateNode
from atlantide.state.sql.common import Contended, holds_from_rows
from atlantide.state.sql.schema import (
    LOCK_COLUMNS,
    SQLITE_ADDED_COLUMNS,
    SQLITE_INSERT_NODE,
    SQLITE_SCHEMA,
)
from atlantide.util.fs import create_private

__all__ = ["SqliteStateBackend"]

#: Seconds a writer waits for another connection's write lock before giving up.
#: Writes are short (one row or one batch), so a long wait means a stuck peer.
_BUSY_TIMEOUT_S = 30.0


def _create_private(path: str) -> None:
    """Create a missing state file owner-only (0600) before sqlite opens it.

    State holds plain outputs, and sqlite would create the file under the umask
    (usually 0644); see :func:`atlantide.util.fs.create_private`.
    """
    if path in ("", ":memory:"):
        return  # no file: in-memory or private temporary database
    create_private(path)


def _serialized[**P, R](
    method: Callable[Concatenate[SqliteStateBackend, P], R],
) -> Callable[Concatenate[SqliteStateBackend, P], R]:
    """Hold the connection lock for the whole call (module doc)."""

    @functools.wraps(method)
    def locked(self: SqliteStateBackend, /, *args: P.args, **kwargs: P.kwargs) -> R:
        with self._conn_lock:
            return method(self, *args, **kwargs)

    return locked


class SqliteStateBackend(StateBackend):
    #: Safe to offload, but slower (module doc).
    offload_writes: ClassVar[bool] = False

    def __init__(self, path: str, *, clock: Clock = time.time) -> None:
        self._now = clock
        self._path = path
        self._conn_lock = threading.RLock()
        try:
            _create_private(path)
            self._conn = sqlite3.connect(
                path, isolation_level=None, timeout=_BUSY_TIMEOUT_S, check_same_thread=False
            )
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            # NORMAL under WAL syncs at checkpoints, not on every commit (~26% faster
            # on a large apply). A process crash loses nothing; an OS crash or power
            # loss may roll back the last few commits without corrupting the file,
            # and a re-run reads those resources as drift.
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()
        except (sqlite3.Error, OSError) as exc:  # pragma: no cover - defensive
            raise StateError(f"cannot open state at {path!r}: {exc}") from exc

    def _create_schema(self) -> None:
        """Create the tables if this file does not have them yet.

        The script runs in one ``BEGIN IMMEDIATE`` transaction, so two processes
        opening the same new file serialize their DDL. The state lock cannot guard
        this: it lives in a table this may be creating.
        """
        self._conn.executescript(f"BEGIN IMMEDIATE;{SQLITE_SCHEMA}COMMIT;")
        self._add_missing_columns()

    def _columns(self) -> set[str]:
        return {row["name"] for row in self._conn.execute("PRAGMA table_info(nodes)")}

    def _add_missing_columns(self) -> None:
        """Add the :data:`SQLITE_ADDED_COLUMNS` a file created by an older build lacks.

        Checked without a lock first, so opening an up-to-date file takes no write
        lock; re-checked under ``BEGIN IMMEDIATE`` so two processes upgrading the
        same file do not both add a column.
        """
        if all(name in self._columns() for name, _ in SQLITE_ADDED_COLUMNS):
            return
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            present = self._columns()
            for name, ddl in SQLITE_ADDED_COLUMNS:
                if name not in present:
                    self._conn.execute(f"ALTER TABLE nodes ADD COLUMN {ddl}")
            self._conn.execute("COMMIT")
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise

    def _check(self, *touched: str) -> None:
        """Refuse a write this run's lease no longer covers.

        Read inside the caller's transaction, so the holds cannot change between
        the check and the write.
        """
        if self._lease is None:
            return
        self._refuse_unfenced(set(touched), self._read_holds(set(touched)))

    @contextmanager
    def _transaction(self, what: str) -> Iterator[None]:
        """Run a mutation atomically; roll back and re-raise as StateError.

        Any exception rolls back, not only ``sqlite3.Error``: otherwise the
        connection stays inside an open transaction and every later write fails.
        Uses ``BEGIN IMMEDIATE`` because every mutation reads before it writes
        (see the module doc).
        """
        try:
            self._conn.execute("BEGIN IMMEDIATE")
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
    @_serialized
    def load(self) -> StateGraph:
        rows = self._conn.execute("SELECT * FROM nodes").fetchall()
        return StateGraph(nodes={row["id"]: node_from_row(row) for row in rows})

    @override
    @_serialized
    def put(self, node: StateNode) -> None:
        columns = node_columns(node)  # serialize before BEGIN
        with self._transaction(f"put({node.id!r})"):
            self._check(node.id)
            self._conn.execute(SQLITE_INSERT_NODE, columns)
            self._bump_serial()

    @override
    @_serialized
    def put_many(self, nodes: Iterable[StateNode]) -> None:
        """Upsert every node in one transaction (one serial bump for the batch)."""
        rows = [node_columns(node) for node in nodes]
        if not rows:
            return
        with self._transaction(f"put_many({len(rows)} nodes)"):
            self._check(*(row[0] for row in rows))
            self._conn.executemany(SQLITE_INSERT_NODE, rows)
            self._bump_serial()

    @override
    @_serialized
    def delete(self, node_id: str) -> None:
        with self._transaction(f"delete({node_id!r})"):
            self._check(node_id)
            deleted = self._conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,)).rowcount
            if deleted:
                self._bump_serial()

    @override
    @_serialized
    def replace_many(self, delete_ids: Iterable[str], nodes: Iterable[StateNode]) -> None:
        """Deletes and upserts in one transaction, so a rekey cannot half-land."""
        ids = [(node_id,) for node_id in delete_ids]
        rows = [node_columns(node) for node in nodes]  # serialize before BEGIN
        if not ids and not rows:
            return
        with self._transaction(f"replace_many({len(ids)} deleted, {len(rows)} upserted)"):
            self._check(*(i[0] for i in ids), *(row[0] for row in rows))
            self._conn.executemany("DELETE FROM nodes WHERE id = ?", ids)
            self._conn.executemany(SQLITE_INSERT_NODE, rows)
            self._bump_serial()

    @override
    @_serialized
    def serial(self) -> int:
        row = self._conn.execute("SELECT value FROM meta WHERE key='serial'").fetchone()
        return int(row["value"])

    # -- committed stack outputs ------------------------------------------

    @override
    @_serialized
    def set_outputs(self, outputs: Mapping[str, Any], *, remove: Iterable[str] = ()) -> None:
        dropped = set(remove)
        # Read the merge base inside the write-locked transaction: otherwise two
        # concurrent runs on disjoint stacks merge onto the same base and the
        # second commit discards the first run's outputs.
        with self._transaction("set_outputs"):
            merged = merge_outputs(self.outputs(), outputs, dropped)
            self._conn.execute(
                "INSERT OR REPLACE INTO meta(key, value) VALUES ('outputs', ?)",
                (JSON_OBJ.dump_json(merged).decode(),),
            )

    @override
    @_serialized
    def outputs(self) -> dict[str, Any]:
        row = self._conn.execute("SELECT value FROM meta WHERE key='outputs'").fetchone()
        return outputs_from_text(row["value"]) if row else {}

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
    @_serialized
    def acquire_lock(
        self, owner: str, ttl_seconds: float, scope: Set[str]
    ) -> Result[Lease, LockError]:
        now = self._now()
        expires = now + ttl_seconds
        try:
            # BEGIN IMMEDIATE serializes contending acquirers.
            with self._transaction("acquire_lock"):
                fence = self._take_scope(owner, now, expires, scope)
        except Contended as contended:
            return Failure(contended.error)
        return Success(self._minted_lease(owner, expires, scope, fence))

    def _take_scope(self, owner: str, now: float, expires: float, scope: Set[str]) -> int:
        """Write ``owner``'s hold on every node of ``scope``; the fence minted for it.

        Raises :class:`Contended` (rolling the transaction back) when another,
        unexpired owner holds any of them.
        """
        if err := scope_conflict(self._read_holds(scope), owner, now, scope):
            raise Contended(err)
        fence = self._next_fence()
        for node_id in sorted(scope):
            self._conn.execute(
                "INSERT OR REPLACE INTO locks(node_id, owner, expires_at, fence) VALUES (?,?,?,?)",
                (node_id, owner, expires, fence),
            )
        return fence

    @override
    @_serialized
    def release_lock(self, owner: str) -> Result[None, LockError]:
        self._conn.execute("DELETE FROM locks WHERE owner = ?", (owner,))
        return Success(None)

    def _read_holds(self, scope: Set[str]) -> dict[str, Lease]:
        """Leases currently held over any node id in ``scope``."""
        if not scope:
            return {}
        # One JSON-array parameter, not one ``?`` per id: a large scope would
        # exceed SQLite's bound-variable limit (999 before 3.32, 32766 after).
        rows = self._conn.execute(
            f"SELECT {LOCK_COLUMNS} FROM locks WHERE node_id IN (SELECT value FROM json_each(?))",
            (json.dumps(sorted(scope)),),
        ).fetchall()
        return holds_from_rows(rows)

    # -- lock administration ----------------------------------------------

    @override
    @_serialized
    def locks(self) -> dict[str, Lease]:
        return holds_from_rows(self._conn.execute(f"SELECT {LOCK_COLUMNS} FROM locks").fetchall())

    @override
    @_serialized
    def force_unlock(self, node_ids: Set[str]) -> int:
        with self._transaction("force_unlock"):
            return sum(
                self._conn.execute("DELETE FROM locks WHERE node_id = ?", (node_id,)).rowcount
                for node_id in sorted(node_ids)
            )

    # -- preflight ---------------------------------------------------------

    @override
    @_serialized
    def check(self) -> list[Check]:
        """A local file is usable when it opens and its directory is writable."""
        try:
            nodes = self._conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        except sqlite3.Error as exc:
            return [Check("state file", FAIL, f"{self._path} unreadable: {exc}")]
        return [
            Check("state file", OK, f"{self._path} ({nodes} node(s))"),
            Check(
                "sharing",
                OK,
                "local sqlite — single machine; set [state].backend for a shared one",
            ),
        ]

    @override
    @_serialized
    def close(self) -> None:
        self._conn.close()

    @override
    def __repr__(self) -> str:
        return f"SqliteStateBackend({self._path!r})"

    def __del__(self) -> None:
        """Best-effort close for callers that did not call :meth:`close`.

        Errors are suppressed: a finalizer runs on an arbitrary thread during
        teardown, and the connection is released regardless.
        """
        with suppress(Exception):
            self._conn.close()
