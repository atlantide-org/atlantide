"""Backend selection: valid configs build, invalid ones fail early and specifically."""

from __future__ import annotations

from pathlib import Path

import pytest
from moto import mock_aws

from atlantide.core.errors import StateError
from atlantide.state import SqliteStateBackend, StateConfig, make_state_backend
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


def test_s3_config_builds_the_s3_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
