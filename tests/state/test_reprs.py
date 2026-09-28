"""Backend reprs name the store for a debugger or a log line, never a credential."""

from __future__ import annotations

from pathlib import Path

from atlantide.state import MemoryStateBackend, SqliteStateBackend
from atlantide.state.sql.postgres import PostgresStateBackend

from .conftest import node


def test_the_memory_backend_counts_its_nodes() -> None:
    backend = MemoryStateBackend()
    backend.put(node("a"))
    assert repr(backend) == "MemoryStateBackend(1 nodes)"


def test_the_sqlite_backend_names_its_file(tmp_path: Path) -> None:
    path = str(tmp_path / "s.db")
    backend = SqliteStateBackend(path)
    try:
        assert repr(backend) == f"SqliteStateBackend({path!r})"
    finally:
        backend.close()


def test_the_postgres_backend_shows_the_host_and_never_the_dsn() -> None:
    # Built without connecting: the repr reads only the configuration.
    backend = PostgresStateBackend.__new__(PostgresStateBackend)
    backend._dsn = "postgresql://user:s3cr3t@db.internal:5432/state"
    backend._schema = "atlantide"
    assert repr(backend) == "PostgresStateBackend('db.internal:5432', schema='atlantide')"
    assert "s3cr3t" not in repr(backend)
