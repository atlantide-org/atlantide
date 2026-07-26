"""The sqlite connection must be usable after a transaction goes wrong.

It runs in autocommit outside ``BEGIN``, so an exception escaping mid-transaction
leaves it there and fails every later write with something unrelated to the cause.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from atlantide.core.errors import StateError
from atlantide.state import SqliteStateBackend


def test_a_non_sqlite_error_does_not_strand_an_open_transaction(tmp_path: Path) -> None:
    """The connection is in autocommit outside BEGIN, so an escaping exception
    would leave it mid-transaction and fail every later write."""
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    with pytest.raises(RuntimeError), backend._transaction("boom"):
        raise RuntimeError("serialization blew up")

    # The connection is usable: a normal write still commits.
    backend.set_outputs({"s:k": "v"})
    assert backend.outputs() == {"s:k": "v"}


def test_a_sqlite_error_still_becomes_a_state_error(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    with pytest.raises(StateError), backend._transaction("bad sql"):
        backend._conn.execute("SELECT * FROM nope")


class _FailingBegin:
    """Wraps the connection so BEGIN raises, as a locked database would."""

    def __init__(self, conn: Any) -> None:
        self._wrapped = conn

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.startswith("BEGIN"):
            raise sqlite3.OperationalError("database is locked")
        return self._wrapped.execute(sql, *args)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._wrapped, name)


def test_a_failed_begin_surfaces_the_original_error(tmp_path: Path) -> None:
    """When BEGIN itself fails there is no transaction to roll back; a bare
    ROLLBACK raises 'cannot rollback - no transaction is active' and masks
    the cause instead of letting it surface as a StateError."""
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    backend._conn = _FailingBegin(backend._conn)  # type: ignore[assignment]
    with pytest.raises(StateError, match="database is locked"), backend._transaction("write"):
        pass  # never reached: BEGIN fails on entry


def test_a_failed_begin_in_acquire_lock_surfaces_the_original_error(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    backend._conn = _FailingBegin(backend._conn)  # type: ignore[assignment]
    with pytest.raises(StateError, match=r"acquire_lock failed.*database is locked"):
        backend.acquire_lock("alice", 30, {"a"})
