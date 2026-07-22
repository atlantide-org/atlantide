"""SSM Parameter Store resolution, and the registry it plugs into."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import boto3
import pytest
from botocore.exceptions import ClientError, NoCredentialsError
from moto import mock_aws

from atlantide.core.errors import SecretsError
from atlantide.core.types import SecretRef
from atlantide.secrets import SecretsConfig, make_secrets_registry
from atlantide.secrets.ssm import SsmParameterStore
from tests.support import TEST_REGION, fake_aws_credentials

REGION = TEST_REGION
PREFIX = "/atlantide/prod/"


@pytest.fixture
def ssm(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """A mocked Parameter Store holding one SecureString secret."""
    fake_aws_credentials(monkeypatch, region=REGION)
    with mock_aws():
        client: Any = boto3.client("ssm", region_name=REGION)
        client.put_parameter(
            Name=f"{PREFIX}db_password", Value="hunter2", Type="SecureString"
        )
        yield client


def _store() -> SsmParameterStore:
    return SsmParameterStore(prefix=PREFIX, region=REGION)


def test_resolves_a_decrypted_value(ssm: Any) -> None:
    assert _store().resolve("db_password") == "hunter2"


def test_value_is_memoised_per_run(ssm: Any) -> None:
    """One API call per secret, and one consistent value even across a rotation."""
    store = _store()
    assert store.resolve("db_password") == "hunter2"
    ssm.put_parameter(
        Name=f"{PREFIX}db_password", Value="rotated", Type="SecureString", Overwrite=True
    )
    assert store.resolve("db_password") == "hunter2"
    assert _store().resolve("db_password") == "rotated"  # a new run sees the new value


def test_missing_parameter_names_the_path_and_the_fix(ssm: Any) -> None:
    with pytest.raises(SecretsError) as exc:
        _store().resolve("absent")
    message = str(exc.value)
    assert f"{PREFIX}absent" in message and "put-parameter" in message


def test_string_list_is_rejected(ssm: Any) -> None:
    ssm.put_parameter(Name=f"{PREFIX}many", Value="a,b", Type="StringList")
    with pytest.raises(SecretsError, match="StringList"):
        _store().resolve("many")


def test_plain_string_parameters_resolve(ssm: Any) -> None:
    ssm.put_parameter(Name=f"{PREFIX}plain", Value="visible", Type="String")
    assert _store().resolve("plain") == "visible"


# -- the registry the engine actually resolves against ------------------------


def test_ssm_is_the_default_but_others_stay_reachable(
    ssm: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FROM_ENV", "env-value")
    registry = make_secrets_registry(
        SecretsConfig(provider="ssm", prefix=PREFIX, region=REGION),
        store_path=tmp_path / "atlantide.secrets",
        key_path=tmp_path / "atlantide.key",
    )
    # An unqualified ref goes to the configured default...
    assert registry.resolve(SecretRef(name="db_password")) == "hunter2"
    # ...and an explicit provider still routes where it says.
    assert registry.resolve(SecretRef(name="FROM_ENV", provider="env")) == "env-value"


def test_keyfile_remains_the_default(tmp_path: Any) -> None:
    registry = make_secrets_registry(
        SecretsConfig(),
        store_path=tmp_path / "atlantide.secrets",
        key_path=tmp_path / "atlantide.key",
    )
    store: Any = registry.get("keyfile")
    store.set("token", "abc")
    assert registry.resolve(SecretRef(name="token")) == "abc"


def test_unknown_provider_is_rejected(tmp_path: Any) -> None:
    with pytest.raises(SecretsError, match=r"unknown \[secrets\]\.provider"):
        make_secrets_registry(
            SecretsConfig(provider="vault"),
            store_path=tmp_path / "s",
            key_path=tmp_path / "k",
        )


# -- preflight ----------------------------------------------------------------


def test_check_passes_when_the_store_answers(ssm: Any) -> None:
    """A "no such parameter" is the pass: the endpoint, credentials and
    ssm:GetParameter all had to work to produce it."""
    result = _store().check()
    assert result.status == "ok"
    assert PREFIX in result.detail


def test_check_reports_a_denial_rather_than_calling_it_reachable(
    ssm: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Denied and not-found are told apart by error code, never by message text."""
    store = _store()
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "GetParameter"
    )
    monkeypatch.setattr(store._client, "get_parameter", _raise(denied))
    result = store.check()
    assert result.status == "fail"
    assert "ssm:GetParameter" in result.detail


def test_check_reports_an_unexpected_client_error(
    ssm: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    throttled = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetParameter"
    )
    store = _store()
    monkeypatch.setattr(store._client, "get_parameter", _raise(throttled))
    assert store.check().status == "fail"


def test_check_reports_missing_credentials(ssm: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Credential failures arrive as BotoCoreError, not ClientError."""
    store = _store()
    monkeypatch.setattr(store._client, "get_parameter", _raise(NoCredentialsError()))
    result = store.check()
    assert result.status == "fail"
    assert "cannot reach SSM" in result.detail


def test_check_passes_if_the_probe_name_somehow_exists(ssm: Any) -> None:
    ssm.put_parameter(
        Name=f"{PREFIX}atlantide-preflight-probe", Value="x", Type="String"
    )
    assert _store().check().status == "ok"


def _raise(exc: Exception) -> Any:
    def boom(**_: Any) -> Any:
        raise exc

    return boom
