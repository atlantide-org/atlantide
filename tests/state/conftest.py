"""Backend-parametrized fixtures: every state test runs on memory, sqlite, s3 and
(when a database is offered) postgres.

The point of the parametrization is that :mod:`tests.state.test_backend` is
written once and every backend must satisfy it identically — that is what makes
the state layer swappable rather than merely pluggable.

Postgres needs a real server. :func:`pg_dsn` finds one — an already-running
database named by ``ATLANTIDE_TEST_PG_DSN``, or a container it starts itself via
testcontainers — and skips when neither is available.

The container is started **lazily**, by the first test that asks for the fixture.
That laziness is the whole design: postgres is always in the parameter list, so a
contributor with Docker running gets the postgres tests without configuring
anything, while ``pytest tests/lang`` still costs nothing. CI keeps setting
``ATLANTIDE_TEST_PG_DSN`` against its service container — the env var wins, and
starting a second database inside the runner would be pure waste.
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

__all__ = ["BackendFactory", "FakeClock", "make_backend", "node", "pg_dsn"]

BackendFactory = Callable[..., StateBackend]

PG_DSN_ENV = "ATLANTIDE_TEST_PG_DSN"
REGION = TEST_REGION
BUCKET = "atlantide-test-state"
LOCK_TABLE = "atlantide-test-locks"
#: Schemas the postgres backend fixture owns; dropped before each test.
PG_SCHEMAS = tuple(f"atlantide_test_{nth}" for nth in range(4))

#: Postgres is always offered. Whether it runs is decided by :func:`pg_dsn` at
#: fixture time, not here — deciding at import time would mean starting a
#: container during collection, for every run that never touches state.
_BACKENDS = ["memory", "sqlite", "s3", "postgres"]

#: Pinned to match the service container in ci.yml, so a failure that reproduces
#: locally is a failure on the same server version.
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """A connectable postgres, or a skip.

    Preference order is deliberate: an externally supplied database is used as-is
    (CI, or a contributor pointing at their own server), and only when there is
    none does this start a container. Docker being absent is a skip rather than a
    failure — the other three backends still cover the contract, and requiring
    Docker to run the test suite would be a poor trade.
    """
    if dsn := os.environ.get(PG_DSN_ENV):
        yield dsn
        return

    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # pragma: no cover - depends on the installed extras
        pytest.skip(
            f"postgres tests need either {PG_DSN_ENV} set or the dev extras "
            f"installed (uv sync --extra dev)"
        )

    # Ryuk is testcontainers' reaper sidecar: it kills containers a crashed test
    # run left behind. It cannot map its port under several common Docker setups
    # (Docker Desktop on macOS, colima), and when it fails it takes the whole
    # session down with it — turning "postgres tests run for free" into "postgres
    # tests never run", silently, on the machines most likely to be a laptop.
    #
    # The `with` block below stops the container on every ordinary exit, including
    # test failure and Ctrl-C. What is given up is recovery from a hard kill of
    # pytest itself; that leaks one container, findable with
    # `docker ps --filter ancestor=postgres:16-alpine`.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

    try:
        # `driver=None` keeps the URL as plain `postgresql://`; the default
        # appends `+psycopg2`, which psycopg 3 does not accept.
        with PostgresContainer(PG_IMAGE, driver=None) as container:
            yield container.get_connection_url()
    except Exception as exc:  # pragma: no cover - depends on the local machine
        # Almost always "Docker is not running". Anything else that stops a
        # container from starting is equally not the test's problem, and the
        # message says which so it is not mistaken for a real failure.
        pytest.skip(f"could not start a postgres container ({type(exc).__name__}: {exc})")


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
    dsn = ""
    if request.param == "postgres":
        # Requested here rather than as a parameter, so the container starts only
        # for the postgres round of the parametrization.
        dsn = request.getfixturevalue("pg_dsn")
        drop_postgres_schemas(dsn, *PG_SCHEMAS)

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
                BUCKET,
                f"state{nth}.json",
                lock_table=LOCK_TABLE,
                region=REGION,
                clock=clock,
            )
        else:
            from atlantide.state.postgres_backend import PostgresStateBackend

            backend = PostgresStateBackend(dsn, schema=PG_SCHEMAS[nth], clock=clock)
        created.append(backend)
        return backend

    yield factory
    for backend in created:
        backend.close()
    resources.close()


def drop_postgres_schemas(dsn: str, *schemas: str) -> None:
    """Remove test schemas so each test starts from an empty database.

    Takes the DSN rather than reading the environment, because the database may
    be a container this session started and never named in an env var.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        for schema in schemas:
            quoted = schema.replace('"', '""')
            conn.execute(f'DROP SCHEMA IF EXISTS "{quoted}" CASCADE')
