"""Backend selection: valid configs build, invalid ones fail early and specifically."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from moto import mock_aws

from atlantide.cli.project import load_project
from atlantide.core.errors import LockError, StateError
from atlantide.state import SqliteStateBackend, StateConfig, make_state_backend
from atlantide.state.factory import DSN_ENV
from atlantide.state.leases import DEFAULT_LOCK_POLICY, DEFAULT_SKEW_MARGIN
from atlantide.state.s3 import S3StateBackend
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


def test_the_skew_margin_reaches_the_s3_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_aws_credentials(monkeypatch, region=REGION)
    (tmp_path / "atlantide.toml").write_text(
        f'[state]\nbackend = "s3"\nbucket = "{BUCKET}"\nkey = "k.json"\n'
        f'lock_table = "{LOCK_TABLE}"\nregion = "{REGION}"\nlock_skew_margin = 5\n'
    )
    config = load_project(tmp_path).state_backend
    assert config.lock_skew_margin == 5.0
    backend = make_state_backend(config, tmp_path / "unused.db")
    assert isinstance(backend, S3StateBackend)
    assert backend._ctx.skew_margin == 5.0

    default = make_state_backend(
        StateConfig(backend="s3", bucket=BUCKET, key="k", lock_table=LOCK_TABLE, region=REGION),
        tmp_path / "unused.db",
    )
    assert isinstance(default, S3StateBackend)
    assert default._ctx.skew_margin == DEFAULT_SKEW_MARGIN


def test_the_journal_settings_reach_the_s3_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_aws_credentials(monkeypatch, region=REGION)
    (tmp_path / "atlantide.toml").write_text(
        f'[state]\nbackend = "s3"\nbucket = "{BUCKET}"\nkey = "k.json"\n'
        f'lock_table = "{LOCK_TABLE}"\nregion = "{REGION}"\n'
        f'journal_table = "heads"\nwrite_concurrency = 4\n'
    )
    config = load_project(tmp_path).state_backend
    backend = make_state_backend(config, tmp_path / "unused.db")
    assert isinstance(backend, S3StateBackend)
    assert backend._ctx.heads_table == "heads"
    assert backend.write_concurrency == 4
    assert S3StateBackend.write_concurrency == 16, "the override is per instance"


@pytest.mark.parametrize("value", ["0", "-1", "true", '"8"', "1.5"])
def test_a_bad_write_concurrency_is_refused(tmp_path: Path, value: str) -> None:
    from atlantide.cli.project import ProjectError

    (tmp_path / "atlantide.toml").write_text(f"[state]\nwrite_concurrency = {value}\n")
    with pytest.raises(ProjectError, match="write_concurrency"):
        load_project(tmp_path)


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


def test_the_skew_margin_reaches_the_postgres_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checked on the constructor call, so no server is needed."""
    pytest.importorskip("psycopg", reason="postgres tests need the postgres extra")
    from atlantide.state.sql import postgres

    built: list[dict[str, Any]] = []
    monkeypatch.setattr(
        postgres,
        "PostgresStateBackend",
        lambda dsn, **kwargs: built.append({"dsn": dsn, **kwargs}),
    )
    config = StateConfig(backend="postgres", dsn="postgresql://db/x", lock_skew_margin=5.0)
    make_state_backend(config, tmp_path / "unused.db")
    make_state_backend(StateConfig(backend="postgres", dsn="postgresql://db/x"), tmp_path / "u.db")
    assert [call["lock_skew_margin"] for call in built] == [5.0, DEFAULT_SKEW_MARGIN]


# -- lease timings ------------------------------------------------------------


def test_lock_defaults_renew_several_times_within_the_ttl() -> None:
    policy = StateConfig().lock_policy()
    assert policy.ttl == DEFAULT_LOCK_POLICY.ttl
    assert policy.ttl / policy.renew_interval >= 3


def test_a_shortened_ttl_drags_the_renew_interval_down_with_it() -> None:
    """Setting only `lock_ttl` must stay valid.

    A user shortening the TTL to reclaim dead runs faster would otherwise inherit
    the default 100s renew interval against a 30s lease. Every run would then lose
    its lease, which reads as flaky contention rather than a misconfiguration.
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
