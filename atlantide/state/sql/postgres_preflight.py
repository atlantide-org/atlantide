"""``state check`` for the postgres backend: reachable, tables present, writable.

Each probe goes through the backend's ``_fetch_one`` / ``_fetch_all``, so a driver
error arrives as :class:`StateError` and becomes that probe's FAIL row.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from atlantide.core.check import FAIL, OK, WARN, Check
from atlantide.core.errors import StateError

__all__ = ["Fetch", "preflight", "tables_check", "write_check"]

#: The backend's ``_fetch_one`` / ``_fetch_all``: statement text and parameters in.
type Fetch = Callable[[str, Sequence[Any]], Any]


def preflight(schema: str, fetch_one: Fetch, fetch_all: Fetch) -> list[Check]:
    """Confirm the server is reachable and this role can read and write state."""
    try:
        fetch_one("SELECT 1 AS ok", ())
    except StateError as exc:
        return [Check("connection", FAIL, str(exc))]
    return [
        Check("connection", OK, f"schema {schema}"),
        _failing_as_check("tables", lambda: tables_check(schema, fetch_all)),
        _failing_as_check("write access", lambda: write_check(schema, fetch_one)),
    ]


def tables_check(schema: str, fetch_all: Fetch) -> Check:
    rows = fetch_all("SELECT tablename FROM pg_tables WHERE schemaname = %s", (schema,))
    present = {row["tablename"] for row in rows}
    missing = sorted({"nodes", "meta", "locks"} - present)
    if missing:
        return Check(
            "tables",
            FAIL,
            f"missing {', '.join(missing)} in schema {schema!r} — the "
            f"backend creates them on connect, so this role likely lacks CREATE",
        )
    return Check("tables", OK, "nodes, meta, locks")


def write_check(schema: str, fetch_one: Fetch) -> Check:
    """Check INSERT access: a read-only role can plan but fails mid-apply."""
    # format('%I') quotes the schema as the DDL does, so a mixed-case or
    # otherwise quoted schema name resolves to the same table.
    row = fetch_one(
        "SELECT has_table_privilege(format('%%I.nodes', %s::text), 'INSERT') AS ok",
        (schema,),
    )
    if row is not None and row["ok"]:
        return Check("write access", OK, "INSERT granted on nodes")
    return Check(
        "write access",
        WARN,
        f"this role cannot INSERT into {schema}.nodes — plan works, apply will fail",
    )


def _failing_as_check(name: str, probe: Callable[[], Check]) -> Check:
    """Run one preflight probe; a backend error is its FAIL row, not an exception."""
    try:
        return probe()
    except StateError as exc:
        return Check(name, FAIL, str(exc))
