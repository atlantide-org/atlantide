"""Backend-parametrized fixtures: every state test runs on memory, sqlite, s3 and
(when a database is offered) postgres.

The point of the parametrization is that :mod:`tests.state.test_backend` is
written once and every backend must satisfy it identically — that is what makes
the state layer swappable rather than merely pluggable.

Postgres needs a real server, so it joins the parameter list only when
``ATLANTIDE_TEST_PG_DSN`` is set; otherwise it is skipped and the rest still run.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack
from typing import Any

import pytest
from moto import mock_aws

from atlantide.state import MemoryStateBackend, SqliteStateBackend, StateBackend, StateNode
from atlantide.state.s3_backend import S3StateBackend
from tests.support import TEST_REGION, FakeClock, create_state_store, fake_aws_credentials

__all__ = ["BackendFactory", "FakeClock", "make_backend", "node"]

BackendFactory = Callable[..., StateBackend]

PG_DSN_ENV = "ATLANTIDE_TEST_PG_DSN"
REGION = TEST_REGION
BUCKET = "atlantide-test-state"
LOCK_TABLE = "atlantide-test-locks"
#: Schemas the postgres backend fixture owns; dropped before each test.
PG_SCHEMAS = tuple(f"atlantide_test_{nth}" for nth in range(4))

_BACKENDS = ["memory", "sqlite", "s3"]
if os.environ.get(PG_DSN_ENV):
    _BACKENDS.append("postgres")


def node(node_id: str, **overrides: Any) -> StateNode:
    """A minimal :class:`StateNode`, keyed by a bare id (not a stack-qualified one)."""
    return StateNode(
        **{
            "id": node_id,
            "type": "test.T",
            "provider": "test",
            "provider_version": "1.0.0",
            "input_hash": "h0",
            "outputs": {"arn": f"arn::{node_id}"},
            **overrides,
        }
    )


@pytest.fixture(params=_BACKENDS)
def make_backend(
    request: pytest.FixtureRequest, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> Iterator[BackendFactory]:
    created: list[StateBackend] = []
    resources = ExitStack()
    if request.param == "s3":
        fake_aws_credentials(monkeypatch, region=REGION)
        resources.enter_context(mock_aws())
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
    if request.param == "postgres":
        drop_postgres_schemas(*PG_SCHEMAS)

    def factory(clock: Callable[[], float] = time.time) -> StateBackend:
        # A distinct file / key / schema per backend, so a test taking two
        # backends gets two independent stores.
        nth = len(created)
        if request.param == "memory":
            backend: StateBackend = MemoryStateBackend(clock=clock)
        elif request.param == "sqlite":
            backend = SqliteStateBackend(str(tmp_path / f"state{nth}.db"), clock=clock)
        elif request.param == "s3":
            backend = S3StateBackend(
                BUCKET, f"state{nth}.json", lock_table=LOCK_TABLE,
                region=REGION, clock=clock,
            )
        else:
            from atlantide.state.postgres_backend import PostgresStateBackend

            backend = PostgresStateBackend(
                os.environ[PG_DSN_ENV], schema=PG_SCHEMAS[nth], clock=clock
            )
        created.append(backend)
        return backend

    yield factory
    for backend in created:
        backend.close()
    resources.close()


def drop_postgres_schemas(*schemas: str) -> None:
    """Remove test schemas so each test starts from an empty database."""
    import psycopg

    with psycopg.connect(os.environ[PG_DSN_ENV], autocommit=True) as conn:
        for schema in schemas:
            quoted = schema.replace('"', '""')
            conn.execute(f'DROP SCHEMA IF EXISTS "{quoted}" CASCADE')
