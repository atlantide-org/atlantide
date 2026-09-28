"""The postgres backend's statements, and the slice of psycopg it is typed against.

Each statement is a :class:`psycopg.sql.SQL` template whose ``{schema}`` the
backend binds to the configured schema. The table DDL and the node row
statements are generated in :mod:`atlantide.state.sql.schema`. psycopg is an
optional extra imported lazily by the backend, so its surface is described here
as Protocols.
"""

from __future__ import annotations

from collections.abc import Iterable
from contextlib import AbstractContextManager
from typing import Any, Protocol

from atlantide.state.sql.schema import LOCK_COLUMNS

__all__ = [
    "BUMP_FENCE",
    "BUMP_SERIAL",
    "DELETE_NODE",
    "HELD_FOR_UPDATE",
    "LOCK",
    "LOCK_HOLDER",
    "OUTPUTS_FOR_UPDATE",
    "SEED_OUTPUTS",
    "SERVER_NOW",
    "UPDATE_OUTPUTS",
    "Conn",
    "Cursor",
]


class Cursor(Protocol):
    """The slice of a psycopg cursor the backend uses."""

    @property
    def rowcount(self) -> int: ...

    def fetchone(self) -> Any: ...

    def fetchall(self) -> list[Any]: ...

    def executemany(self, query: Any, params_seq: Iterable[Any]) -> None: ...


class Conn(Protocol):
    """The slice of a psycopg connection the backend uses."""

    def execute(self, query: Any, params: Any = ...) -> Cursor: ...

    def cursor(self) -> Cursor: ...

    def transaction(self) -> AbstractContextManager[Any]: ...

    def close(self) -> None: ...

    def rollback(self) -> None: ...

    @property
    def closed(self) -> bool: ...

    @property
    def broken(self) -> bool: ...


DELETE_NODE = "DELETE FROM {schema}.nodes WHERE id = %s"

BUMP_SERIAL = (
    "UPDATE {schema}.meta SET value = (CAST(value AS BIGINT) + 1)::TEXT WHERE key = 'serial'"
)

#: The holds over the nodes about to be written, locked FOR UPDATE so they cannot
#: change between this read and the write in the same transaction.
HELD_FOR_UPDATE = f"SELECT {LOCK_COLUMNS} FROM {{schema}}.locks WHERE node_id = ANY(%s) FOR UPDATE"

#: The server's wall clock as epoch seconds. ``clock_timestamp()``, not ``now()``:
#: ``now()`` is fixed at transaction start, and after waiting on row locks it
#: would judge a lapsed lease live.
SERVER_NOW = "SELECT extract(epoch FROM clock_timestamp())::float8 AS now"

# The outputs row is read-modified-written under a row lock. It is created first
# so `FOR UPDATE` has a row to lock on a fresh store.
SEED_OUTPUTS = (
    "INSERT INTO {schema}.meta(key, value) VALUES ('outputs', '{{}}') ON CONFLICT (key) DO NOTHING"
)
OUTPUTS_FOR_UPDATE = "SELECT value FROM {schema}.meta WHERE key = 'outputs' FOR UPDATE"
UPDATE_OUTPUTS = "UPDATE {schema}.meta SET value = %s WHERE key = 'outputs'"

#: Mint the next epoch. Runs inside the acquire transaction, so two contending
#: acquirers cannot be handed the same one.
BUMP_FENCE = (
    "UPDATE {schema}.meta SET value = (CAST(value AS BIGINT) + 1)::TEXT "
    "WHERE key = 'fence' RETURNING value"
)

#: Take one node: unheld, held by the same owner, or expired. "Expired" is judged
#: ``lock_skew_margin`` late: the last parameter is ``now - margin``, like S3's
#: ``:stale``. An empty RETURNING means a live, different owner holds the row.
LOCK = """
INSERT INTO {schema}.locks (node_id, owner, expires_at, fence) VALUES (%s, %s, %s, %s)
ON CONFLICT (node_id) DO UPDATE SET owner = EXCLUDED.owner,
    expires_at = EXCLUDED.expires_at, fence = EXCLUDED.fence
WHERE {schema}.locks.owner = EXCLUDED.owner OR {schema}.locks.expires_at < %s
RETURNING node_id
"""

#: Who holds one node, to name them when :data:`LOCK` was refused.
LOCK_HOLDER = "SELECT owner, expires_at, fence FROM {schema}.locks WHERE node_id = %s"
