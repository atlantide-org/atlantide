"""The sqlite connection must be usable after a transaction goes wrong.

It runs in autocommit outside ``BEGIN``, so an exception escaping mid-transaction
leaves it there and fails every later write with something unrelated to the cause.
"""

from __future__ import annotations

import multiprocessing
import os
import sqlite3
import threading
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from atlantide.core.errors import StateError
from atlantide.state import SqliteStateBackend

from ..conftest import node


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


# -- concurrent writers ----------------------------------------------------


def _leased(path: Path, owner: str, scope: set[str]) -> SqliteStateBackend:
    """A backend bound to a fresh lease over ``scope``, so every write reads
    the ``locks`` table before it writes."""
    backend = SqliteStateBackend(str(path))
    lease = backend.acquire_lock(owner, 60, scope).unwrap()
    backend.bind_lease(lease)
    return backend


def test_a_commit_between_the_fence_read_and_the_write_does_not_fail_the_write(
    tmp_path: Path,
) -> None:
    """Two applies on disjoint nodes sharing one file.

    With a deferred BEGIN the fence check starts a read snapshot; if the other
    process commits before this write, SQLite cannot upgrade the stale snapshot
    and returns "database is locked" immediately (the busy timeout does not
    apply). The provider has already created the resource, so it would go
    untracked. The write transaction must take the write lock up front instead.
    """
    path = tmp_path / "s.db"
    alice = _leased(path, "alice", {"a"})
    bob = _leased(path, "bob", {"b"})

    read_holds = alice._read_holds

    def read_then_let_bob_commit(scope: Any) -> Any:
        holds = read_holds(scope)
        # Bob tries to commit in the window. Under BEGIN IMMEDIATE he cannot
        # (alice holds the write lock) and gives up after a short wait; under a
        # deferred BEGIN he succeeds and alice's write fails.
        bob._conn.execute("PRAGMA busy_timeout=50")
        with suppress(StateError):
            bob.put(node("b"))
        return holds

    alice._read_holds = read_then_let_bob_commit  # type: ignore[method-assign]
    alice.put(node("a"))  # must not raise

    alice._read_holds = read_holds  # type: ignore[method-assign]
    bob._conn.execute("PRAGMA busy_timeout=5000")
    bob.put(node("b"))
    assert set(SqliteStateBackend(str(path)).load().nodes) == {"a", "b"}


def test_a_writer_waits_for_a_concurrent_writer_instead_of_failing(tmp_path: Path) -> None:
    """Another connection holding the write lock makes a put wait (busy
    timeout), not fail."""
    path = tmp_path / "s.db"
    alice = _leased(path, "alice", {"a"})
    other = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE meta SET value = value WHERE key = 'serial'")
    release = threading.Timer(0.3, lambda: other.execute("COMMIT"))
    release.start()
    try:
        alice.put(node("a"))
    finally:
        release.join()
        other.close()
    assert "a" in alice.load().nodes


def _hammer(path: str, owner: str, count: int) -> None:
    backend = SqliteStateBackend(path)
    lease = backend.acquire_lock(owner, 60, {f"{owner}-{i}" for i in range(count)}).unwrap()
    backend.bind_lease(lease)
    for i in range(count):
        backend.put(node(f"{owner}-{i}"))
    backend.close()


def test_concurrent_processes_on_disjoint_nodes_all_land(tmp_path: Path) -> None:
    path = str(tmp_path / "s.db")
    SqliteStateBackend(path).close()  # create the schema before racing
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_hammer, args=(path, owner, 60)) for owner in ("p", "q", "r")]
    for proc in procs:
        proc.start()
    for proc in procs:
        proc.join(60)
    assert [proc.exitcode for proc in procs] == [0, 0, 0]
    assert len(SqliteStateBackend(path).load().nodes) == 180


# -- file permissions ------------------------------------------------------


@pytest.mark.skipif(os.name != "posix", reason="unix permission bits")
def test_a_new_state_file_and_its_wal_are_owner_only(tmp_path: Path) -> None:
    """State holds plain outputs: a newly created file must not be world-readable
    whatever the umask, and SQLite gives -wal/-shm the db file's mode."""
    path = tmp_path / "s.db"
    old = os.umask(0o022)
    try:
        backend = SqliteStateBackend(str(path))
        backend.put(node("a"))
    finally:
        os.umask(old)
    assert path.stat().st_mode & 0o777 == 0o600
    wal = Path(f"{path}-wal")
    assert wal.exists()
    assert wal.stat().st_mode & 0o777 == 0o600
    backend.close()


@pytest.mark.skipif(os.name != "posix", reason="unix permission bits")
def test_an_existing_state_file_keeps_its_mode(tmp_path: Path) -> None:
    path = tmp_path / "s.db"
    SqliteStateBackend(str(path)).close()
    path.chmod(0o640)
    SqliteStateBackend(str(path)).close()
    assert path.stat().st_mode & 0o777 == 0o640


def test_the_connection_is_tuned_for_wal(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    assert backend._conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert backend._conn.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL
    assert backend._conn.execute("PRAGMA busy_timeout").fetchone()[0] > 0
