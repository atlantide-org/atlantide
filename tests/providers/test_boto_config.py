"""Every AWS client is built with explicit timeouts, retries, and a sized pool.

botocore's defaults are wrong for an apply in two ways that do not announce
themselves: a 10-connection pool silently serialises a parallel apply, and no
read timeout means a hung call hangs the run forever while holding its lease.
Neither shows up as an error, so both are pinned here.
"""

from __future__ import annotations

from typing import Any

import pytest
from botocore.config import Config

from atlantide.graph.schedule import DEFAULT_PARALLELISM
from atlantide.providers.aws.config import MIN_POOL, boto_config
from atlantide.providers.aws.provider import AwsProvider
from tests.support import TEST_REGION, fake_aws_credentials


def _captured(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Record the kwargs every ``Session.client`` call is made with."""
    seen: list[dict[str, Any]] = []
    import boto3

    original = boto3.Session.client

    def spy(self: Any, *args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(boto3.Session, "client", spy)
    return seen


def test_the_connection_pool_is_at_least_the_apply_s_parallelism() -> None:
    """The defect this exists for: 32 concurrent nodes against a 10-connection
    pool queue behind it, and the only symptom is a slow apply."""
    config = boto_config(parallelism=32)
    assert config.max_pool_connections >= 32


def test_a_small_parallelism_never_shrinks_the_pool_below_botocore_s_default() -> None:
    assert boto_config(parallelism=1).max_pool_connections == MIN_POOL


def test_timeouts_and_adaptive_retries_are_set() -> None:
    config = boto_config()
    assert config.connect_timeout and config.read_timeout
    assert config.retries is not None
    # Adaptive adds client-side rate limiting, so throttled clients back off
    # together rather than each rediscovering the limit.
    assert config.retries["mode"] == "adaptive"


def test_calls_are_attributable_in_cloudtrail() -> None:
    assert "atlantide/" in (boto_config().user_agent_extra or "")


def test_the_provider_hands_its_config_to_every_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client built without the config would silently opt out of all of it."""
    fake_aws_credentials(monkeypatch, region=TEST_REGION)
    seen = _captured(monkeypatch)
    provider = AwsProvider(region=TEST_REGION, parallelism=24)

    provider._client(None, "s3", TEST_REGION)
    provider._client(None, "sqs", TEST_REGION)

    assert len(seen) == 2
    for kwargs in seen:
        config = kwargs["config"]
        assert isinstance(config, Config)
        assert config.max_pool_connections >= 24
        assert config.read_timeout


def test_the_state_backend_and_secrets_clients_are_bounded_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hung state write or secret lookup strands a run holding its lease, so
    these are worth as much as the provider's clients."""
    fake_aws_credentials(monkeypatch, region=TEST_REGION)
    seen = _captured(monkeypatch)

    from atlantide.secrets.ssm import SsmParameterStore
    from atlantide.state.s3_backend import S3StateBackend

    S3StateBackend("b", "k", lock_table="t", region=TEST_REGION)
    SsmParameterStore(region=TEST_REGION)

    assert len(seen) == 3  # s3, dynamodb, ssm
    for kwargs in seen:
        config = kwargs["config"]
        assert config.read_timeout and config.connect_timeout
        assert config.retries is not None and config.retries["mode"] == "adaptive"


def test_a_directly_built_provider_still_gets_a_usable_pool() -> None:
    """Embedding callers that never pass parallelism must not land on a pool of
    10 while the scheduler runs many more."""
    assert AwsProvider()._config.max_pool_connections >= min(32, DEFAULT_PARALLELISM)


def test_the_parallelism_flag_reaches_the_connection_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    """`--parallelism` is resolved after the registry used to be built, so it was
    easy for the flag to size the scheduler and not the pool it runs against."""
    from atlantide.cli.project import ProjectConfig
    from atlantide.cli.wiring import providers as _providers

    fake_aws_credentials(monkeypatch, region=TEST_REGION)
    seen = _captured(monkeypatch)

    registry, _ = _providers(ProjectConfig(), region=TEST_REGION, parallelism=48)
    provider = registry.get("aws").unwrap()
    provider._client(None, "s3", TEST_REGION)

    assert seen[-1]["config"].max_pool_connections >= 48


def test_toml_parallelism_reaches_the_pool_when_no_flag_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from atlantide.cli.project import ProjectConfig
    from atlantide.cli.wiring import providers as _providers

    fake_aws_credentials(monkeypatch, region=TEST_REGION)
    seen = _captured(monkeypatch)

    registry, _ = _providers(ProjectConfig(parallelism=17), region=TEST_REGION)
    registry.get("aws").unwrap()._client(None, "s3", TEST_REGION)

    assert seen[-1]["config"].max_pool_connections >= 17


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
