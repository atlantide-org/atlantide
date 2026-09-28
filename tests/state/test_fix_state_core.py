"""Regressions for the state core: corrupt input surfaces as StateError, the
postgres retry path, preflight probes, large lock scopes, and message/repr fixes."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from atlantide.core.check import FAIL, OK
from atlantide.core.errors import LeaseLostError, StateError
from atlantide.state import Lease, LeaseGuard, SqliteStateBackend
from atlantide.state.codec import (
    NODE_COLUMNS,
    EntryKind,
    EntryOp,
    JournalEntry,
    StateDocument,
    decode,
    decode_entry,
    dumps,
    encode,
    encode_entry,
    loads,
    node_columns,
    node_from_row,
)
from atlantide.state.factory import StateConfig
from tests.support import FakeClock

from .conftest import drop_postgres_schemas, node

# -- codec: gzip ------------------------------------------------------------------


def _flip_body(raw: bytes) -> bytes:
    """Corrupt the deflate body of a gzip stream, leaving the 10-byte header."""
    assert raw[:2] == b"\x1f\x8b"
    body = bytearray(raw)
    for i in range(10, min(len(body) - 8, 60)):
        body[i] ^= 0xFF
    return bytes(body)


def test_a_damaged_gzip_snapshot_is_a_state_error() -> None:
    raw = encode(StateDocument(serial=1, nodes={"a": node("a")}), compress_over=0)
    with pytest.raises(StateError, match="unreadable gzip"):
        decode(_flip_body(raw))


def test_a_damaged_gzip_journal_entry_is_a_state_error() -> None:
    entry = JournalEntry(kind=EntryKind.NODE, name="a", seq=1, fence=1, op=EntryOp.PUT)
    raw = encode_entry(entry, compress_over=0)
    with pytest.raises(StateError, match="unreadable gzip"):
        decode_entry(_flip_body(raw))


# -- codec: strict serial ---------------------------------------------------------


@pytest.mark.parametrize("serial", ["7", 7.9, True])
def test_a_non_integer_serial_is_refused(serial: object) -> None:
    payload = json.loads(dumps(StateDocument(serial=7)))
    payload["serial"] = serial
    with pytest.raises(StateError, match="corrupt remote state"):
        loads(json.dumps(payload).encode())


# -- rows -------------------------------------------------------------------------


def test_a_corrupt_sqlite_row_is_a_state_error_naming_it(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        backend.put(node("a"))
        backend._conn.execute('UPDATE nodes SET deps_json = \'{"not": "a list"}\'')
        with pytest.raises(StateError, match="corrupt state row 'a'"):
            backend.load()
    finally:
        backend.close()


def test_corrupt_sqlite_outputs_are_a_state_error(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        backend._conn.execute("INSERT INTO meta(key, value) VALUES ('outputs', '[1, 2]')")
        with pytest.raises(StateError, match="corrupt state row 'outputs'"):
            backend.outputs()
        with pytest.raises(StateError, match="corrupt state row 'outputs'"):
            backend.set_outputs({"dev:x": 1})
    finally:
        backend.close()


# -- sqlite: large scopes, cheap check ----------------------------------------------


def test_sqlite_reads_holds_over_a_scope_beyond_the_variable_limit(tmp_path: Path) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        scope = frozenset(f"n{i}" for i in range(40_000))
        lease = backend.acquire_lock("me", 60.0, scope).unwrap()
        assert lease.owner == "me"
        holds = backend._read_holds(scope)
        assert len(holds) == len(scope)
        assert {h.owner for h in holds.values()} == {"me"}
    finally:
        backend.close()


def test_sqlite_check_counts_without_loading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    backend = SqliteStateBackend(str(tmp_path / "s.db"))
    try:
        backend.put_many([node("a"), node("b")])

        def no_load() -> None:
            raise AssertionError("check() must not load the whole graph")

        monkeypatch.setattr(backend, "load", no_load)
        first = backend.check()[0]
        assert first.status == OK
        assert "(2 node(s))" in first.detail
    finally:
        backend.close()


# -- postgres: _run and preflight (no server needed) ---------------------------------


class _FakeConn:
    def __init__(self, *, broken: bool = False) -> None:
        self.closed = False
        self.broken = broken
        self.close_calls = 0
        self.rollback_calls = 0

    def close(self) -> None:
        self.close_calls += 1
        self.closed = True

    def rollback(self) -> None:
        self.rollback_calls += 1


def _psycopg() -> Any:
    return pytest.importorskip("psycopg")


def _bare_postgres(conn: _FakeConn, fresh: list[_FakeConn]) -> Any:
    from atlantide.state.sql.postgres import PostgresStateBackend

    backend = PostgresStateBackend.__new__(PostgresStateBackend)
    backend._psycopg = _psycopg()
    backend._conn_lock = threading.RLock()
    backend._conn = conn
    backend._dsn = "postgresql://u:pw@db/x"
    backend._schema = "Mixed_Case"

    def connect() -> _FakeConn:
        new = _FakeConn()
        fresh.append(new)
        return new

    backend._connect = connect
    return backend


def test_a_lock_timeout_on_a_live_connection_is_a_state_error_without_a_retry() -> None:
    old, fresh = _FakeConn(), []
    backend = _bare_postgres(old, fresh)
    calls: list[Any] = []

    def work(conn: Any) -> None:
        calls.append(conn)
        raise _psycopg().errors.LockNotAvailable("could not obtain lock")

    with pytest.raises(StateError, match="could not obtain lock"):
        backend._run(work)
    assert calls == [old]
    assert fresh == []
    assert backend._conn is old
    assert old.close_calls == 0


@pytest.mark.parametrize("error", ["DeadlockDetected", "SerializationFailure"])
def test_an_aborted_transaction_is_retried_once_on_the_same_connection(error: str) -> None:
    old, fresh = _FakeConn(), []
    backend = _bare_postgres(old, fresh)
    calls: list[Any] = []

    def work(conn: Any) -> str:
        calls.append(conn)
        if len(calls) == 1:
            raise getattr(_psycopg().errors, error)("aborted")
        return "ok"

    assert backend._run(work) == "ok"
    assert calls == [old, old]
    assert old.rollback_calls == 1
    assert fresh == [] and old.close_calls == 0


def test_a_second_deadlock_is_a_state_error_on_the_same_connection() -> None:
    old, fresh = _FakeConn(), []
    backend = _bare_postgres(old, fresh)
    calls: list[Any] = []

    def work(conn: Any) -> None:
        calls.append(conn)
        raise _psycopg().errors.DeadlockDetected("deadlock detected")

    with pytest.raises(StateError, match="deadlock detected"):
        backend._run(work)
    assert calls == [old, old]
    assert fresh == [] and backend._conn is old


def test_a_broken_connection_is_closed_and_replaced_once() -> None:
    old, fresh = _FakeConn(broken=True), []
    backend = _bare_postgres(old, fresh)
    calls: list[Any] = []

    def work(conn: Any) -> str:
        calls.append(conn)
        if conn is old:
            raise _psycopg().OperationalError("server closed the connection")
        return "ok"

    assert backend._run(work) == "ok"
    assert old.close_calls == 1
    assert len(fresh) == 1 and backend._conn is fresh[0]
    assert calls == [old, fresh[0]]


def test_a_failed_retry_is_a_state_error_not_a_raw_driver_error() -> None:
    old, fresh = _FakeConn(broken=True), []
    backend = _bare_postgres(old, fresh)

    def work(conn: Any) -> None:
        raise _psycopg().OperationalError("still down")

    with pytest.raises(StateError, match="still down"):
        backend._run(work)
    assert len(fresh) == 1


def test_the_write_probe_quotes_the_schema_server_side() -> None:
    backend = _bare_postgres(_FakeConn(), [])
    seen: list[tuple[str, Any]] = []

    def fetch_one(text: str, params: Any = ()) -> dict[str, bool]:
        seen.append((text, params))
        return {"ok": True}

    def fetch_all(text: str, params: Any = ()) -> list[dict[str, str]]:
        return [{"tablename": name} for name in ("nodes", "meta", "locks")]

    backend._fetch_one = fetch_one
    backend._fetch_all = fetch_all
    assert [check.status for check in backend.check()] == [OK, OK, OK]
    text, params = seen[-1]  # the write probe runs last
    assert "format('%%I.nodes'" in text
    assert params == ("Mixed_Case",)


def test_a_failing_preflight_probe_is_a_fail_row() -> None:
    backend = _bare_postgres(_FakeConn(), [])

    def fetch_one(text: str, params: Any = ()) -> Any:
        if "has_table_privilege" in text:
            raise StateError('relation "Mixed_Case.nodes" does not exist')
        return {"ok": 1}

    def fetch_all(text: str, params: Any = ()) -> Any:
        raise StateError("permission denied for pg_tables")

    backend._fetch_one = fetch_one
    backend._fetch_all = fetch_all
    connection, tables, writable = backend.check()
    assert connection.status == OK
    assert (tables.name, tables.status) == ("tables", FAIL)
    assert "permission denied" in tables.detail
    assert (writable.name, writable.status) == ("write access", FAIL)


def test_a_mixed_case_schema_passes_preflight(pg_dsn: str) -> None:
    from atlantide.state.sql.postgres import PostgresStateBackend

    schema = "Atlantide_Test_Mixed"
    drop_postgres_schemas(pg_dsn, schema)
    backend = PostgresStateBackend(pg_dsn, schema=schema)
    try:
        assert [c.status for c in backend.check()] == [OK, OK, OK]
    finally:
        backend.close()
        drop_postgres_schemas(pg_dsn, schema)


# -- lease guard message ------------------------------------------------------------


def test_the_guard_says_expires_in_inside_the_grace_window() -> None:
    clock = FakeClock()
    guard = LeaseGuard(grace=30.0, clock=clock)
    guard.renewed(Lease(owner="me", expires_at=clock() + 20.0))
    with pytest.raises(LeaseLostError) as info:
        guard.check()
    assert "expires in 20s" in str(info.value)
    assert "ago" not in str(info.value)


def test_the_guard_says_ago_once_expired() -> None:
    clock = FakeClock()
    guard = LeaseGuard(grace=30.0, clock=clock)
    guard.renewed(Lease(owner="me", expires_at=clock() - 5.0))
    with pytest.raises(LeaseLostError, match="expired 5s ago"):
        guard.check()


# -- config repr ----------------------------------------------------------------------


def test_the_state_config_repr_hides_the_dsn() -> None:
    config = StateConfig(backend="postgres", dsn="postgresql://u:s3cr3t@db/x")
    assert "s3cr3t" not in repr(config)


# -- depends_on persistence -----------------------------------------------------------


def test_sqlite_persists_depends_on(tmp_path: Path) -> None:
    path = str(tmp_path / "s.db")
    backend = SqliteStateBackend(path)
    try:
        backend.put(node("a", depends_on=("x", "y")))
        backend.put(node("b"))
    finally:
        backend.close()
    again = SqliteStateBackend(path)
    try:
        assert again.load().get("a").depends_on == ("x", "y")
        assert again.load().get("b").depends_on == ()
    finally:
        again.close()


def test_an_old_schema_sqlite_file_gains_depends_on(tmp_path: Path) -> None:
    """A file from before ``ref_digests_json`` and ``depends_on_json`` opens, its
    rows read with no ordering edges, and new rows store theirs."""
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE nodes (
            id TEXT PRIMARY KEY, type TEXT NOT NULL, provider TEXT NOT NULL,
            provider_version TEXT NOT NULL, input_hash TEXT NOT NULL,
            outputs_json TEXT NOT NULL, properties_json TEXT NOT NULL,
            deps_json TEXT NOT NULL, prevent_destroy INTEGER NOT NULL,
            status TEXT NOT NULL, secret_digests_json TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO nodes VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("a", "test.T", "test", "1.0.0", "h0", "{}", "{}", "[]", 0, "created", "{}"),
    )
    conn.commit()
    conn.close()

    backend = SqliteStateBackend(path)
    try:
        old = backend.load().get("a")
        assert old is not None and old.depends_on == () and old.ref_digests == {}
        backend.put(node("b", depends_on=("a",)))
        assert backend.load().get("b").depends_on == ("a",)
    finally:
        backend.close()


def test_a_row_without_the_depends_on_column_decodes() -> None:
    row = dict(zip(NODE_COLUMNS, node_columns(node("a")), strict=True))
    del row["depends_on_json"]
    assert node_from_row(row).depends_on == ()
    row["depends_on_json"] = None
    assert node_from_row(row).depends_on == ()


def test_postgres_persists_depends_on(pg_dsn: str) -> None:
    from atlantide.state.sql.postgres import PostgresStateBackend

    schema = "atlantide_test_depends_on"
    drop_postgres_schemas(pg_dsn, schema)
    backend = PostgresStateBackend(pg_dsn, schema=schema)
    try:
        backend.put(node("a", depends_on=("x", "y")))
        backend.put_many([node("b")])
        graph = backend.load()
        assert graph.get("a").depends_on == ("x", "y")
        assert graph.get("b").depends_on == ()
    finally:
        backend.close()
        drop_postgres_schemas(pg_dsn, schema)
