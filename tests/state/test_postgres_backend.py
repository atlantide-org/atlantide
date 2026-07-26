"""Postgres specifics: connection errors, schema isolation, and identifier safety.

The shared behaviour is covered for every backend in
:mod:`tests.state.test_backend`. Everything here that needs a server takes the
``pg_dsn`` fixture, which uses ``ATLANTIDE_TEST_PG_DSN`` when set, otherwise
starts a container, and skips when neither is available.
"""

from __future__ import annotations

import pytest

from atlantide.core.errors import StateError
from atlantide.state.postgres_backend import PostgresStateBackend

from .conftest import drop_postgres_schemas, node


def test_unreachable_server_is_a_state_error() -> None:
    with pytest.raises(StateError, match="cannot connect"):
        PostgresStateBackend("postgresql://atlantide@127.0.0.1:1/nope")


def test_schemas_are_independent(pg_dsn: str) -> None:
    """Two projects can share one database without seeing each other's state."""
    first = PostgresStateBackend(pg_dsn, schema="atlantide_iso_a")
    second = PostgresStateBackend(pg_dsn, schema="atlantide_iso_b")
    try:
        first.put(node("a"))
        assert "a" in first.load()
        assert len(second.load()) == 0
    finally:
        drop_postgres_schemas(pg_dsn, "atlantide_iso_a", "atlantide_iso_b")
        first.close()
        second.close()


def test_state_is_visible_to_a_second_process(pg_dsn: str) -> None:
    writer = PostgresStateBackend(pg_dsn, schema="atlantide_share")
    writer.put(node("a", input_hash="h1", dependencies=("x",), status="creating"))
    writer.set_outputs({"dev:url": "https://example.test"})
    writer.close()

    reader = PostgresStateBackend(pg_dsn, schema="atlantide_share")
    try:
        read = reader.load().get("a")
        assert read is not None
        assert (read.input_hash, read.dependencies, read.status) == ("h1", ("x",), "creating")
        assert reader.outputs() == {"dev:url": "https://example.test"}
        assert reader.serial() == 1
    finally:
        reader.close()
        drop_postgres_schemas(pg_dsn, "atlantide_share")


def test_a_dropped_connection_is_re_established(pg_dsn: str) -> None:
    """Long applies outlive server-side idle timeouts; a read must not die with them."""
    backend = PostgresStateBackend(pg_dsn, schema="atlantide_reconnect")
    try:
        backend.put(node("a"))
        backend._conn.close()  # simulate the server hanging up mid-apply
        assert "a" in backend.load()
    finally:
        backend.close()
        drop_postgres_schemas(pg_dsn, "atlantide_reconnect")


def test_schema_name_is_quoted_not_interpolated(pg_dsn: str) -> None:
    """A schema name is an identifier, so it can never be read as SQL."""
    hostile = 'weird"; DROP TABLE nodes; --'
    backend = PostgresStateBackend(pg_dsn, schema=hostile)
    try:
        backend.put(node("a"))
        assert "a" in backend.load()
    finally:
        backend.close()
        drop_postgres_schemas(pg_dsn, hostile)
