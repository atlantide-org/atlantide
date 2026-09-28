"""A ``nodes`` table created by a build before ``ref_digests_json`` still opens.

The backend adds the column on open; rows already there read back with an empty
record (which the diff treats as "unknown"), and new rows store theirs.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from atlantide.state import SqliteStateBackend

from ..conftest import drop_postgres_schemas, node

#: The ``nodes`` table as the previous build created it.
_OLD_NODES = """
CREATE TABLE nodes (
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
"""

_OLD_ROW = (
    "a", "test.T", "test", "1.0.0", "h0", '{"arn":"arn::a"}',
    '{"x":{"$ref":"b#out"}}', "[]", 0, "created", "{}",
)  # fmt: skip

_RECORDED = {"x": "sha256:" + "ab" * 32}


def test_sqlite_adds_the_column_to_an_older_file(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.executescript(_OLD_NODES)
    conn.execute(f"INSERT INTO nodes VALUES ({', '.join('?' * len(_OLD_ROW))})", _OLD_ROW)
    conn.commit()
    conn.close()

    backend = SqliteStateBackend(path)
    try:
        old = backend.load().get("a")
        assert old is not None and old.ref_digests == {}
        assert old.properties == {"x": {"$ref": "b#out"}}
        backend.put(node("b", ref_digests=_RECORDED))
        assert backend.load().get("b").ref_digests == _RECORDED
    finally:
        backend.close()
    # A second open of the upgraded file changes nothing.
    again = SqliteStateBackend(path)
    try:
        assert again.load().get("b").ref_digests == _RECORDED
    finally:
        again.close()


def test_postgres_adds_the_column_to_an_older_schema(pg_dsn: str) -> None:
    psycopg = pytest.importorskip("psycopg")
    from atlantide.state.sql.postgres import PostgresStateBackend

    schema = "atlantide_upgrade"
    drop_postgres_schemas(pg_dsn, schema)
    # As the previous build's DDL created it: every JSON column jsonb.
    old_ddl = f"""
    CREATE TABLE {schema}.nodes (
        id TEXT PRIMARY KEY, type TEXT NOT NULL, provider TEXT NOT NULL,
        provider_version TEXT NOT NULL, input_hash TEXT NOT NULL,
        outputs_json JSONB NOT NULL, properties_json JSONB NOT NULL,
        deps_json JSONB NOT NULL, prevent_destroy BOOLEAN NOT NULL,
        status TEXT NOT NULL, secret_digests_json JSONB NOT NULL
    )"""
    with psycopg.connect(pg_dsn, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {schema}")
        conn.execute(old_ddl)
        conn.execute(
            f"INSERT INTO {schema}.nodes VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
            "%s::jsonb, %s, %s, %s::jsonb)",
            (*_OLD_ROW[:8], False, *_OLD_ROW[9:]),
        )
    backend = PostgresStateBackend(pg_dsn, schema=schema)
    try:
        old = backend.load().get("a")
        assert old is not None and old.ref_digests == {}
        backend.put(node("b", ref_digests=_RECORDED))
        assert backend.load().get("b").ref_digests == _RECORDED
    finally:
        backend.close()
        drop_postgres_schemas(pg_dsn, schema)
