"""The ``nodes`` / ``meta`` / ``locks`` schema, rendered for sqlite and postgres.

:data:`NODES` is the one column spec for the ``nodes`` table: both backends'
``CREATE TABLE``, the migration that upgrades a table created by an older build,
and the node row statements are generated from it. ``meta`` and ``locks`` never
changed shape since they shipped and are kept as per-dialect text.

The postgres text is a :class:`psycopg.sql.SQL` template: ``{schema}`` is bound
to the configured schema as an identifier, so every literal brace is doubled.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

__all__ = [
    "LOCK_COLUMNS",
    "NODES",
    "POSTGRES",
    "POSTGRES_DDL",
    "POSTGRES_INSERT_NODE",
    "POSTGRES_SELECT_NODES",
    "SQLITE",
    "SQLITE_ADDED_COLUMNS",
    "SQLITE_INSERT_NODE",
    "SQLITE_SCHEMA",
    "Column",
    "Dialect",
]

type Kind = Literal["text", "json", "bool"]


@dataclass(frozen=True, slots=True)
class Column:
    """One ``nodes`` column.

    A column with a ``default`` is one added after the table first shipped: the
    default is its "nothing recorded" value, which fills the rows of a table
    created by an older build and which the row codec reads back as it would a
    new row's.
    """

    name: str
    kind: Kind
    default: str | None = None
    primary_key: bool = False


#: The ``nodes`` columns, in :data:`atlantide.state.codec.NODE_COLUMNS` order
#: (the order :func:`~atlantide.state.codec.node_columns` yields values in).
NODES: tuple[Column, ...] = (
    Column("id", "text", primary_key=True),
    Column("type", "text"),
    Column("provider", "text"),
    Column("provider_version", "text"),
    Column("input_hash", "text"),
    Column("outputs_json", "json"),
    Column("properties_json", "json"),
    Column("deps_json", "json"),
    Column("prevent_destroy", "bool"),
    Column("status", "text"),
    Column("secret_digests_json", "json"),
    Column("ref_digests_json", "json", default="{}"),
    Column("depends_on_json", "json", default="[]"),
)

#: The ``locks`` row, as every lease read selects it.
LOCK_COLUMNS = "node_id, owner, expires_at, fence"


@dataclass(frozen=True, slots=True)
class Dialect:
    """How one database spells the column spec.

    The widths are layout only: they keep the generated text identical to what
    earlier builds executed (pinned by ``tests/state/sql/test_schema.py``).
    """

    #: SQL type per column kind.
    types: Mapping[Kind, str]
    #: Width the column names are padded to in ``CREATE TABLE``.
    name_width: int
    #: Width the types of ``NOT NULL`` columns are padded to in ``CREATE TABLE``.
    type_width: int
    #: Whether defaults are cast to the column type (``'{}'::jsonb``).
    cast_defaults: bool
    #: Whether the text is a psycopg template, whose literal braces are doubled.
    template: bool

    def definition(self, column: Column, *, aligned: bool = False) -> str:
        """``column``'s type and constraints, as ``CREATE TABLE`` or ``ADD COLUMN`` takes them."""
        type_ = self.types[column.kind]
        if column.primary_key:
            return f"{type_} PRIMARY KEY"
        text = f"{type_:<{self.type_width if aligned else 0}} NOT NULL"
        if column.default is not None:
            default = f"'{column.default}'"
            if self.cast_defaults:
                default += f"::{type_.lower()}"
            text += f" DEFAULT {self._literal(default)}"
        return text

    def create_nodes(self, table: str) -> str:
        """``CREATE TABLE IF NOT EXISTS`` for ``nodes`` at its current shape."""
        lines = ",\n".join(
            f"    {column.name:<{self.name_width}} {self.definition(column, aligned=True)}"
            for column in NODES
        )
        return f"CREATE TABLE IF NOT EXISTS {table} (\n{lines}\n);\n"

    def added_columns(self) -> tuple[tuple[str, str], ...]:
        """``(name, column definition)`` for every column added after the table shipped."""
        return tuple(
            (column.name, f"{column.name} {self.definition(column)}")
            for column in NODES
            if column.default is not None
        )

    def _literal(self, text: str) -> str:
        return text.replace("{", "{{").replace("}", "}}") if self.template else text


SQLITE = Dialect(
    types={"text": "TEXT", "json": "TEXT", "bool": "INTEGER"},
    name_width=16,
    type_width=0,
    cast_defaults=False,
    template=False,
)

# The JSON columns are jsonb, so the server validates them on write, but they
# are read back as text and the shared row codec decodes them unchanged.
POSTGRES = Dialect(
    types={"text": "TEXT", "json": "JSONB", "bool": "BOOLEAN"},
    name_width=19,
    type_width=7,
    cast_defaults=True,
    template=True,
)

_NAMES = ", ".join(column.name for column in NODES)

# -- sqlite -----------------------------------------------------------------

SQLITE_SCHEMA = (
    "\n"
    + SQLITE.create_nodes("nodes")
    + """\
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS locks (
    node_id TEXT PRIMARY KEY,
    owner   TEXT NOT NULL,
    expires_at REAL NOT NULL,
    fence   INTEGER NOT NULL
);
INSERT OR IGNORE INTO meta(key, value) VALUES ('serial', '0');
INSERT OR IGNORE INTO meta(key, value) VALUES ('fence', '0');
"""
)

#: The columns a file created by an older build may lack, with the DDL that adds
#: each (``ALTER TABLE nodes ADD COLUMN <ddl>``).
SQLITE_ADDED_COLUMNS = SQLITE.added_columns()

SQLITE_INSERT_NODE = (
    f"INSERT OR REPLACE INTO nodes ({_NAMES}) VALUES ({', '.join('?' * len(NODES))})"
)

# -- postgres ---------------------------------------------------------------

POSTGRES_DDL = (
    "\nCREATE SCHEMA IF NOT EXISTS {schema};\n"
    + POSTGRES.create_nodes("{schema}.nodes")
    + "-- Columns added after the table first shipped: a no-op on a table created above,\n"
    + '-- the upgrade of one created by an older build (its rows read as "nothing recorded").\n'
    + "".join(
        f"ALTER TABLE {{schema}}.nodes\n    ADD COLUMN IF NOT EXISTS {ddl};\n"
        for _, ddl in POSTGRES.added_columns()
    )
    + """\
CREATE TABLE IF NOT EXISTS {schema}.meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS {schema}.locks (
    node_id    TEXT PRIMARY KEY,
    owner      TEXT NOT NULL,
    expires_at DOUBLE PRECISION NOT NULL,
    fence      BIGINT NOT NULL
);
INSERT INTO {schema}.meta(key, value) VALUES ('serial', '0') ON CONFLICT DO NOTHING;
INSERT INTO {schema}.meta(key, value) VALUES ('fence', '0') ON CONFLICT DO NOTHING;
"""
)

_PROJECTION = ", ".join(
    f"{column.name}::text AS {column.name}" if column.kind == "json" else column.name
    for column in NODES
)
_PLACEHOLDERS = ", ".join("%s::jsonb" if column.kind == "json" else "%s" for column in NODES)
_ASSIGNMENTS = ", ".join(
    f"{column.name} = EXCLUDED.{column.name}" for column in NODES if not column.primary_key
)

POSTGRES_SELECT_NODES = f"SELECT {_PROJECTION} FROM {{schema}}.nodes"

POSTGRES_INSERT_NODE = (
    f"INSERT INTO {{schema}}.nodes ({_NAMES})"
    f" VALUES ({_PLACEHOLDERS})"
    f" ON CONFLICT (id) DO UPDATE SET {_ASSIGNMENTS}"
)
