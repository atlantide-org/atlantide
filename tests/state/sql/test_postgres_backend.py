"""Postgres specifics: connection errors, schema isolation, identifier safety,
server-side lease time, and fenced outputs.

The shared behaviour is covered for every backend in
:mod:`tests.state.test_backend`. Everything here that needs a server takes the
``pg_dsn`` fixture, which uses ``ATLANTIDE_TEST_PG_DSN`` when set, otherwise
starts a container, and skips when neither is available.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from returns.pipeline import is_successful

from atlantide.core.errors import FencedWriteError, StateError
from atlantide.state.sql.postgres import PostgresStateBackend

from ..conftest import FakeClock, drop_postgres_schemas, node

# Not every test here takes `pg_dsn` (`test_unreachable_server_is_a_state_error`
# needs no server), so the driver is checked for the module rather than by the
# fixture. Without it that test still raises `StateError`, but for the wrong
# reason.
pytest.importorskip("psycopg", reason="postgres tests need the postgres extra")


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


# -- lease time -----------------------------------------------------------------
#
# Expiry is decided by the server: every expiry is written and judged against the
# database's clock, so a client's clock, however wrong, decides nothing.

SCHEMA = "atlantide_leases"


@pytest.fixture
def pg(pg_dsn: str) -> Iterator[Callable[..., PostgresStateBackend]]:
    """Backends over one fresh schema, so they contend with each other."""
    drop_postgres_schemas(pg_dsn, SCHEMA)
    made: list[PostgresStateBackend] = []

    def make(**kwargs: Any) -> PostgresStateBackend:
        backend = PostgresStateBackend(pg_dsn, schema=SCHEMA, **kwargs)
        made.append(backend)
        return backend

    yield make
    for backend in made:
        backend.close()
    drop_postgres_schemas(pg_dsn, SCHEMA)


def _skewed(backend: PostgresStateBackend, by: float) -> None:
    """Make ``backend``'s host clock run ``by`` seconds off true time."""
    backend._now = lambda: time.time() + by


def _expire_on_the_server(backend: PostgresStateBackend, seconds_ago: float) -> None:
    """Move every hold's expiry to ``seconds_ago`` in the past, by the server clock."""
    backend._execute(
        "UPDATE {schema}.locks SET expires_at = extract(epoch FROM clock_timestamp()) - %s",
        (seconds_ago,),
    )


def test_a_fast_client_clock_cannot_take_a_live_lease(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    """An hour-fast host would judge every lease lapsed by its own clock."""
    holder, fast = pg(), pg(lock_skew_margin=0.0)
    holder.acquire_lock("holder", 60.0, {"a"}).unwrap()
    _skewed(fast, +3600.0)

    refused = fast.acquire_lock("fast", 60.0, {"a"})
    assert not is_successful(refused)
    assert "holder" in str(refused.failure())


def test_a_slow_client_clock_cannot_keep_a_lapsed_lease(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    """An hour-slow host would judge a lapsed lease live by its own clock."""
    holder, slow = pg(), pg(lock_skew_margin=0.0)
    holder.acquire_lock("holder", 60.0, {"a"}).unwrap()
    _expire_on_the_server(holder, 5.0)
    _skewed(slow, -3600.0)

    assert is_successful(slow.acquire_lock("slow", 60.0, {"a"}))


def test_the_skew_margin_delays_a_takeover_by_the_server_clock(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    holder, patient, eager = pg(), pg(lock_skew_margin=30.0), pg(lock_skew_margin=0.0)
    holder.acquire_lock("holder", 60.0, {"a"}).unwrap()
    _expire_on_the_server(holder, 5.0)  # lapsed, but within a 30s margin

    refused = patient.acquire_lock("patient", 60.0, {"a"})
    assert not is_successful(refused)
    assert "holder" in str(refused.failure()), "a hold within the margin is named"
    assert is_successful(eager.acquire_lock("eager", 60.0, {"a"}))


def test_the_skew_margin_applies_to_an_injected_clock_too(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    clock = FakeClock()
    holder, taker = pg(clock=clock), pg(clock=clock, lock_skew_margin=30.0)
    holder.acquire_lock("holder", 60.0, {"a"}).unwrap()  # expires at 1060

    clock.advance(60.0 + 29.0)
    assert not is_successful(taker.acquire_lock("taker", 60.0, {"a"}))
    clock.advance(2.0)  # 31s past expiry
    assert is_successful(taker.acquire_lock("taker", 60.0, {"a"}))


def test_leases_are_reported_on_the_local_clock(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    """`LeaseGuard` and `state unlock` compare expiries against the local clock,
    so a skewed host must see the true remaining time in its own terms."""
    backend = pg()
    _skewed(backend, -3600.0)
    lease = backend.acquire_lock("me", 60.0, {"a"}).unwrap()
    assert lease.expires_at - backend._now() == pytest.approx(60.0, abs=5.0)
    held = backend.locks()["a"]
    assert held.expires_at - backend._now() == pytest.approx(60.0, abs=5.0)


def test_locks_report_the_fence_each_hold_was_taken_at(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    """`locks()` returns each hold's stored fence. A hold read back as fence `0`
    would look unfenced in `state unlock` and to anything comparing epochs. Fences
    live per row; each must come back as minted, including on a skewed host, where
    the expiry is converted but the fence must not be."""
    backend = pg()
    _skewed(backend, -3600.0)
    backend._execute("UPDATE {schema}.meta SET value = '41' WHERE key = 'fence'")

    first = backend.acquire_lock("me", 60.0, {"a", "b"}).unwrap()
    second = backend.acquire_lock("other", 60.0, {"c"}).unwrap()

    assert (first.fence, second.fence) == (42, 43)
    held = backend.locks()
    assert {node_id: lease.fence for node_id, lease in held.items()} == {"a": 42, "b": 42, "c": 43}
    assert held["a"].expires_at - backend._now() == pytest.approx(60.0, abs=5.0)


def test_a_write_is_fenced_on_the_server_clock(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    """A lapsed lease may not write, whatever the writer's clock says."""
    backend = pg()
    backend.bind_lease(backend.acquire_lock("me", 60.0, {"a"}).unwrap())
    _expire_on_the_server(backend, 5.0)
    _skewed(backend, -3600.0)
    with pytest.raises(FencedWriteError, match="expired"):
        backend.put(node("a"))


def test_a_negative_skew_margin_is_refused() -> None:
    with pytest.raises(StateError, match="lock_skew_margin"):
        PostgresStateBackend("postgresql://unused", lock_skew_margin=-1.0)


# -- outputs --------------------------------------------------------------------


def _bound(backend: PostgresStateBackend, owner: str, scope: set[str]) -> None:
    backend.bind_lease(backend.acquire_lock(owner, 60.0, scope).unwrap())


def test_outputs_are_fenced_on_the_leased_nodes_of_their_stack(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    """Outputs have no lock of their own: publishing a stack's outputs is fenced
    on the nodes of that stack this run holds."""
    clock = FakeClock()
    run_a, run_b = pg(clock=clock, lock_skew_margin=0.0), pg(clock=clock, lock_skew_margin=0.0)
    _bound(run_a, "run-a", {"dev:t:a", "prod:t:p"})
    run_a.set_outputs({"dev:url": "a"})

    clock.advance(61.0)
    _bound(run_b, "run-b", {"dev:t:a"})
    run_b.set_outputs({"dev:url": "b"})

    with pytest.raises(FencedWriteError, match="run-b"):
        run_a.set_outputs({"dev:url": "stale"})
    with pytest.raises(FencedWriteError):
        run_a.set_outputs({}, remove=["dev:url"])  # a removal is a write too
    clock.advance(-61.0)  # back inside run-a's lease on its own stack
    run_a.set_outputs({"prod:url": "p"})  # its own stack is still its own
    run_a.set_outputs({"loose:url": "x"})  # no leased node in the stack: unfenced
    assert pg().outputs() == {"dev:url": "b", "prod:url": "p", "loose:url": "x"}


def test_outputs_from_a_superseded_lease_are_refused(
    pg: Callable[..., PostgresStateBackend],
) -> None:
    backend = pg()
    first = backend.acquire_lock("run-a", 60.0, {"dev:t:a"}).unwrap()
    second = backend.acquire_lock("run-a", 60.0, {"dev:t:a"}).unwrap()

    backend.bind_lease(first)
    with pytest.raises(FencedWriteError, match="superseded"):
        backend.set_outputs({"dev:url": "stale"})
    assert backend.outputs() == {}, "refused in the same transaction as the write"

    backend.bind_lease(second)
    backend.set_outputs({"dev:url": "fresh"})
    assert backend.outputs() == {"dev:url": "fresh"}
