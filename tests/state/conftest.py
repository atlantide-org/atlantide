"""Backend-parametrized fixtures: every state test runs on memory, sqlite, s3 and
(when a database is available) postgres.

:mod:`tests.state.test_backend` is written once and every backend must satisfy it
identically, which keeps the state layer swappable.

Postgres needs a real server. :func:`pg_dsn` uses the database named by
``ATLANTIDE_TEST_PG_DSN``, or starts a container via testcontainers, and skips
when neither is available.

The container starts lazily, on the first test that requests the fixture. Postgres
is always in the parameter list, so a contributor with Docker running gets the
postgres tests without configuration, while ``pytest tests/lang`` pays nothing.
CI sets ``ATLANTIDE_TEST_PG_DSN`` against its service container; the env var takes
precedence, so no second database starts inside the runner.
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
from atlantide.state.s3 import S3StateBackend
from tests.support import TEST_REGION, FakeClock, create_state_store, fake_aws_credentials

__all__ = ["BackendFactory", "FakeClock", "make_backend", "node", "pg_dsn"]

type BackendFactory = Callable[..., StateBackend]

PG_DSN_ENV = "ATLANTIDE_TEST_PG_DSN"
REGION = TEST_REGION
BUCKET = "atlantide-test-state"
LOCK_TABLE = "atlantide-test-locks"
#: Schemas the postgres backend fixture owns; dropped before each test.
PG_SCHEMAS = tuple(f"atlantide_test_{nth}" for nth in range(4))

#: Postgres is always listed; :func:`pg_dsn` decides at fixture time whether it
#: runs. Deciding at import time would start a container during collection, even
#: for runs that never touch state.
_BACKENDS = ["memory", "sqlite", "s3", "postgres"]

#: Pinned to match the service container in ci.yml, so a failure that reproduces
#: locally is a failure on the same server version.
PG_IMAGE = "postgres:16-alpine"


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """A connectable postgres, or a skip.

    An externally supplied database (CI, or a contributor's own server) is used
    as-is; a container starts only when there is none. Missing Docker is a skip,
    not a failure: the other three backends still cover the contract.
    """
    # Checked first: without the driver, a run that finds a database fails deep
    # inside the backend with a bare ImportError instead of skipping.
    try:
        import psycopg  # noqa: F401
    except ImportError:  # pragma: no cover - depends on the installed extras
        pytest.skip("postgres tests need the postgres extra (uv sync --extra postgres)")

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

    # Ryuk, testcontainers' reaper sidecar, removes containers left by crashed
    # runs. It cannot map its port under several common Docker setups (Docker
    # Desktop on macOS, colima), and its failure aborts the whole session, which
    # silently disables the postgres tests.
    #
    # The `with` block below stops the container on every ordinary exit, including
    # test failure and Ctrl-C. A hard kill of pytest leaks one container, findable
    # with `docker ps --filter ancestor=postgres:16-alpine`.
    os.environ.setdefault("TESTCONTAINERS_RYUK_DISABLED", "true")

    try:
        # `driver=None` keeps the URL as plain `postgresql://`; the default
        # appends `+psycopg2`, which psycopg 3 does not accept.
        with PostgresContainer(PG_IMAGE, driver=None) as container:
            yield container.get_connection_url()
    except Exception as exc:  # pragma: no cover - depends on the local machine
        # Usually "Docker is not running". Any failure to start the container is
        # environmental; the message names it so the skip is not mistaken for a
        # real failure.
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

    def factory(clock: Callable[[], float] | None = None) -> StateBackend:
        # A distinct file / key / schema per backend, so a test taking two
        # backends gets two independent stores.
        nth = len(created)
        local = clock if clock is not None else time.time
        if request.param == "memory":
            backend: StateBackend = MemoryStateBackend(clock=local)
        elif request.param == "sqlite":
            backend = SqliteStateBackend(str(tmp_path / f"state{nth}.db"), clock=local)
        elif request.param == "s3":
            backend = S3StateBackend(
                BUCKET,
                f"state{nth}.json",
                lock_table=LOCK_TABLE,
                region=REGION,
                # The shared contract expires leases at their exact expiry, as
                # the other backends do; the S3 skew margin has its own tests.
                lock_skew_margin=0.0,
                clock=local,
            )
        else:
            from atlantide.state.sql.postgres import PostgresStateBackend

            # No clock given: lease time is the server's, as in production. A
            # test that injects one gets it instead, for deterministic expiry.
            backend = PostgresStateBackend(
                dsn, schema=PG_SCHEMAS[nth], lock_skew_margin=0.0, clock=clock
            )
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
