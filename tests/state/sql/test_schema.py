"""The SQL generated from the column spec, pinned to the exact text it replaced.

A change to :data:`atlantide.state.sql.schema.NODES` changes what every existing
database is migrated to, so these literals are updated deliberately, never by
regenerating them.
"""

from __future__ import annotations

from atlantide.state.codec import NODE_COLUMNS
from atlantide.state.sql import schema

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
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
    secret_digests_json TEXT NOT NULL,
    ref_digests_json TEXT NOT NULL DEFAULT '{}',
    depends_on_json  TEXT NOT NULL DEFAULT '[]'
);
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

_SQLITE_ADDED_COLUMNS = (
    ("ref_digests_json", "ref_digests_json TEXT NOT NULL DEFAULT '{}'"),
    ("depends_on_json", "depends_on_json TEXT NOT NULL DEFAULT '[]'"),
)

_SQLITE_INSERT_NODE = (
    "INSERT OR REPLACE INTO nodes (id, type, provider, provider_version, input_hash, "
    "outputs_json, properties_json, deps_json, prevent_destroy, status, secret_digests_json, "
    "ref_digests_json, depends_on_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

_POSTGRES_DDL = """
CREATE SCHEMA IF NOT EXISTS {schema};
CREATE TABLE IF NOT EXISTS {schema}.nodes (
    id                  TEXT PRIMARY KEY,
    type                TEXT    NOT NULL,
    provider            TEXT    NOT NULL,
    provider_version    TEXT    NOT NULL,
    input_hash          TEXT    NOT NULL,
    outputs_json        JSONB   NOT NULL,
    properties_json     JSONB   NOT NULL,
    deps_json           JSONB   NOT NULL,
    prevent_destroy     BOOLEAN NOT NULL,
    status              TEXT    NOT NULL,
    secret_digests_json JSONB   NOT NULL,
    ref_digests_json    JSONB   NOT NULL DEFAULT '{{}}'::jsonb,
    depends_on_json     JSONB   NOT NULL DEFAULT '[]'::jsonb
);
-- Columns added after the table first shipped: a no-op on a table created above,
-- the upgrade of one created by an older build (its rows read as "nothing recorded").
ALTER TABLE {schema}.nodes
    ADD COLUMN IF NOT EXISTS ref_digests_json JSONB NOT NULL DEFAULT '{{}}'::jsonb;
ALTER TABLE {schema}.nodes
    ADD COLUMN IF NOT EXISTS depends_on_json JSONB NOT NULL DEFAULT '[]'::jsonb;
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

_POSTGRES_SELECT_NODES = (
    "SELECT id, type, provider, provider_version, input_hash, "
    "outputs_json::text AS outputs_json, properties_json::text AS properties_json, "
    "deps_json::text AS deps_json, prevent_destroy, status, "
    "secret_digests_json::text AS secret_digests_json, "
    "ref_digests_json::text AS ref_digests_json, depends_on_json::text AS depends_on_json "
    "FROM {schema}.nodes"
)

_POSTGRES_INSERT_NODE = (
    "INSERT INTO {schema}.nodes (id, type, provider, provider_version, input_hash, "
    "outputs_json, properties_json, deps_json, prevent_destroy, status, secret_digests_json, "
    "ref_digests_json, depends_on_json) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, "
    "%s::jsonb, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb) ON CONFLICT (id) DO UPDATE SET "
    "type = EXCLUDED.type, provider = EXCLUDED.provider, "
    "provider_version = EXCLUDED.provider_version, input_hash = EXCLUDED.input_hash, "
    "outputs_json = EXCLUDED.outputs_json, properties_json = EXCLUDED.properties_json, "
    "deps_json = EXCLUDED.deps_json, prevent_destroy = EXCLUDED.prevent_destroy, "
    "status = EXCLUDED.status, secret_digests_json = EXCLUDED.secret_digests_json, "
    "ref_digests_json = EXCLUDED.ref_digests_json, depends_on_json = EXCLUDED.depends_on_json"
)


def test_the_spec_lists_the_row_codec_columns_in_order() -> None:
    assert tuple(column.name for column in schema.NODES) == NODE_COLUMNS


def test_sqlite_ddl_and_migration_are_unchanged() -> None:
    assert schema.SQLITE_SCHEMA == _SQLITE_SCHEMA
    assert schema.SQLITE_ADDED_COLUMNS == _SQLITE_ADDED_COLUMNS


def test_postgres_ddl_and_migration_are_unchanged() -> None:
    assert schema.POSTGRES_DDL == _POSTGRES_DDL


def test_node_row_statements_are_unchanged() -> None:
    assert schema.SQLITE_INSERT_NODE == _SQLITE_INSERT_NODE
    assert schema.POSTGRES_SELECT_NODES == _POSTGRES_SELECT_NODES
    assert schema.POSTGRES_INSERT_NODE == _POSTGRES_INSERT_NODE
