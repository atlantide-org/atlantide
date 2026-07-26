"""Backend selection: valid configs build, invalid ones fail early and specifically."""

from __future__ import annotations

from pathlib import Path

import pytest
from moto import mock_aws

from atlantide.cli.project import load_project
from atlantide.core.errors import LockError, StateError
from atlantide.state import SqliteStateBackend, StateConfig, make_state_backend
from atlantide.state.backend import DEFAULT_LOCK_POLICY
from atlantide.state.factory import DSN_ENV
from atlantide.state.s3_backend import S3StateBackend
from tests.support import create_state_store, fake_aws_credentials

from .conftest import BUCKET, LOCK_TABLE, REGION


def test_default_is_local_sqlite(tmp_path: Path) -> None:
    config = StateConfig()
    assert not config.is_remote
    backend = make_state_backend(config, tmp_path / "atlantide.db")
    assert isinstance(backend, SqliteStateBackend)
    backend.close()


def test_s3_config_builds_the_s3_backend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_aws_credentials(monkeypatch, region=REGION)
    config = StateConfig(
        backend="s3", bucket=BUCKET, key="prod.json", lock_table=LOCK_TABLE, region=REGION
    )
    assert config.is_remote
    with mock_aws():
        create_state_store(BUCKET, LOCK_TABLE, region=REGION)
        backend = make_state_backend(config, tmp_path / "unused.db")
        assert isinstance(backend, S3StateBackend)
        assert len(backend.load()) == 0
        backend.close()


def test_unknown_backend_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(StateError, match=r"unknown \[state\]\.backend"):
        make_state_backend(StateConfig(backend="ftp"), tmp_path / "s.db")


def test_s3_names_every_missing_key(tmp_path: Path) -> None:
    with pytest.raises(StateError) as exc:
        make_state_backend(StateConfig(backend="s3", bucket="b"), tmp_path / "s.db")
    message = str(exc.value)
    assert "key" in message and "lock_table" in message and "bucket" not in message


def test_postgres_requires_a_dsn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DSN_ENV, raising=False)
    with pytest.raises(StateError, match=DSN_ENV):
        make_state_backend(StateConfig(backend="postgres"), tmp_path / "s.db")


def test_postgres_dsn_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Credentials belong in the environment, not in a committed toml file."""
    monkeypatch.setenv(DSN_ENV, "postgresql://user:pw@db/atlantide")
    config = StateConfig(backend="postgres")
    config.validate()  # no exception: the env var satisfies the requirement
    assert config.resolved_dsn() == "postgresql://user:pw@db/atlantide"
    # An explicit dsn still wins over the environment.
    assert StateConfig(backend="postgres", dsn="postgresql://x").resolved_dsn() == (
        "postgresql://x"
    )


# -- lease timings ------------------------------------------------------------


def test_lock_defaults_renew_several_times_within_the_ttl() -> None:
    policy = StateConfig().lock_policy()
    assert policy.ttl == DEFAULT_LOCK_POLICY.ttl
    assert policy.ttl / policy.renew_interval >= 3


def test_a_shortened_ttl_drags_the_renew_interval_down_with_it() -> None:
    """Setting only `lock_ttl` must stay valid.

    A user shortening the TTL to reclaim dead runs faster would otherwise inherit
    the default 100s interval against a 30s lease — an interval longer than the
    thing it renews, so every run would lose its lease and it would read as flaky
    contention rather than as a misconfiguration.
    """
    policy = StateConfig(lock_ttl=30.0).lock_policy()
    assert policy.ttl == 30.0
    assert policy.renew_interval < policy.ttl
    assert policy.renew_grace < policy.ttl
    policy.validate()


def test_an_explicit_renew_interval_is_honoured() -> None:
    policy = StateConfig(lock_ttl=90.0, lock_renew_interval=10.0).lock_policy()
    assert (policy.ttl, policy.renew_interval) == (90.0, 10.0)


def test_a_renew_interval_longer_than_the_ttl_is_refused() -> None:
    with pytest.raises(LockError, match="shorter than lock_ttl"):
        StateConfig(lock_ttl=30.0, lock_renew_interval=60.0).lock_policy()


def test_lock_timings_are_read_from_the_state_table(tmp_path: Path) -> None:
    """Ints are the natural TOML spelling for a duration, so both parse."""
    (tmp_path / "atlantide.toml").write_text(
        "[state]\nlock_ttl = 120\nlock_renew_interval = 30.5\n"
    )
    config = load_project(tmp_path).state_backend
    assert (config.lock_ttl, config.lock_renew_interval) == (120.0, 30.5)


def test_a_non_numeric_lock_ttl_falls_back_to_the_default(tmp_path: Path) -> None:
    """`true` is an int in Python but not a duration; a bad value must not become
    one silently."""
    (tmp_path / "atlantide.toml").write_text('[state]\nlock_ttl = "soon"\n')
    assert load_project(tmp_path).state_backend.lock_ttl is None
