"""Postgres specifics: connection errors, schema isolation, and identifier safety.

The shared behaviour is covered for every backend in
:mod:`tests.state.test_backend`, which includes postgres whenever
``ATLANTIDE_TEST_PG_DSN`` points at a database. Everything here that needs a
server is gated on the same variable.
"""

from __future__ import annotations

import os

import pytest

from atlantide.core.errors import StateError
from atlantide.state.postgres_backend import PostgresStateBackend

from .conftest import PG_DSN_ENV, drop_postgres_schemas, node

needs_postgres = pytest.mark.skipif(
    not os.environ.get(PG_DSN_ENV), reason=f"set {PG_DSN_ENV} to run the postgres tests"
)


def test_unreachable_server_is_a_state_error() -> None:
    with pytest.raises(StateError, match="cannot connect"):
        PostgresStateBackend("postgresql://atlantide@127.0.0.1:1/nope")


@needs_postgres
def test_schemas_are_independent() -> None:
    """Two projects can share one database without seeing each other's state."""
    dsn = os.environ[PG_DSN_ENV]
    first = PostgresStateBackend(dsn, schema="atlantide_iso_a")
    second = PostgresStateBackend(dsn, schema="atlantide_iso_b")
    try:
        first.put(node("a"))
        assert "a" in first.load()
        assert len(second.load()) == 0
    finally:
        drop_postgres_schemas("atlantide_iso_a", "atlantide_iso_b")
        first.close()
        second.close()


@needs_postgres
def test_state_is_visible_to_a_second_process() -> None:
    dsn = os.environ[PG_DSN_ENV]
    writer = PostgresStateBackend(dsn, schema="atlantide_share")
    writer.put(node("a", input_hash="h1", dependencies=("x",), status="creating"))
    writer.set_outputs({"dev:url": "https://example.test"})
    writer.close()

    reader = PostgresStateBackend(dsn, schema="atlantide_share")
    try:
        read = reader.load().get("a")
        assert read is not None
        assert (read.input_hash, read.dependencies, read.status) == ("h1", ("x",), "creating")
        assert reader.outputs() == {"dev:url": "https://example.test"}
        assert reader.serial() == 1
    finally:
        reader.close()
        drop_postgres_schemas("atlantide_share")


@needs_postgres
def test_a_dropped_connection_is_re_established() -> None:
    """Long applies outlive server-side idle timeouts; a read must not die with them."""
    dsn = os.environ[PG_DSN_ENV]
    backend = PostgresStateBackend(dsn, schema="atlantide_reconnect")
    try:
        backend.put(node("a"))
        backend._conn.close()  # simulate the server hanging up mid-apply
        assert "a" in backend.load()
    finally:
        backend.close()
        drop_postgres_schemas("atlantide_reconnect")


@needs_postgres
def test_schema_name_is_quoted_not_interpolated() -> None:
    """A schema name is an identifier, so it can never be read as SQL."""
    dsn = os.environ[PG_DSN_ENV]
    hostile = 'weird"; DROP TABLE nodes; --'
    backend = PostgresStateBackend(dsn, schema=hostile)
    try:
        backend.put(node("a"))
        assert "a" in backend.load()
    finally:
        backend.close()
        drop_postgres_schemas(hostile)


