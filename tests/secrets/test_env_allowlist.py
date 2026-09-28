"""The ``env`` secrets provider is deny-by-default.

Any config (including a third-party component) can name any ``SecretRef``, and
the process environment holds cloud credentials and tokens alongside the config's
own secrets. So an env name resolves only when ``[secrets.env] allow`` lists a
pattern for it, and a refusal never reveals the variable's value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from returns.result import Failure

from atlantide.cli.project import load_project
from atlantide.core import SecretRef
from atlantide.core.errors import AtlantideError, SecretsError
from atlantide.secrets import EnvSecretsProvider, SecretsConfig, make_secrets_registry
from atlantide.secrets.factory import parse_env_allow
from atlantide.state import MemoryStateBackend
from tests.support import Bucket, FakeProvider, engine_for, globals_of

SECRET_VALUE = "AKIA-very-secret-value"


@pytest.fixture
def aws_key(monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET_VALUE)
    return "AWS_SECRET_ACCESS_KEY"


def _registry(tmp_path: Path, config: SecretsConfig) -> Any:
    return make_secrets_registry(
        config, store_path=tmp_path / "atlantide.secrets", key_path=tmp_path / "atlantide.key"
    )


# -- the provider --------------------------------------------------------------


def test_denied_by_default(aws_key: str) -> None:
    with pytest.raises(SecretsError, match=r"\[secrets\.env\] allow") as exc:
        EnvSecretsProvider().resolve(aws_key)
    assert SECRET_VALUE not in str(exc.value)
    assert "not allowed" in str(exc.value)


def test_allowed_via_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_TOKEN", "t0k3n")
    monkeypatch.setenv("DB_PASSWORD", "pw")
    provider = EnvSecretsProvider(allow=["APP_*", "DB_PASSWORD"])
    assert provider.resolve("APP_TOKEN") == "t0k3n"
    assert provider.resolve("DB_PASSWORD") == "pw"


def test_non_matching_name_denied(aws_key: str) -> None:
    provider = EnvSecretsProvider(allow=["APP_*", "DB_PASSWORD"])
    with pytest.raises(SecretsError, match="not allowed") as exc:
        provider.resolve(aws_key)
    assert SECRET_VALUE not in str(exc.value)


def test_patterns_are_case_sensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("app_token", "lower")
    with pytest.raises(SecretsError, match="not allowed"):
        EnvSecretsProvider(allow=["APP_*"]).resolve("app_token")


def test_allowed_but_unset_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("APP_MISSING", raising=False)
    with pytest.raises(SecretsError, match="not found"):
        EnvSecretsProvider(allow=["APP_*"]).resolve("APP_MISSING")


# -- the registry the engine resolves against ---------------------------------


def test_explicit_env_ref_is_gated_when_env_is_not_the_default(
    tmp_path: Path, aws_key: str
) -> None:
    registry = _registry(tmp_path, SecretsConfig())
    with pytest.raises(SecretsError, match="not allowed") as exc:
        registry.resolve(SecretRef(name=aws_key, provider="env"))
    assert SECRET_VALUE not in str(exc.value)


def test_env_as_default_provider_is_gated_too(tmp_path: Path, aws_key: str) -> None:
    registry = _registry(tmp_path, SecretsConfig(provider="env"))
    with pytest.raises(SecretsError, match="not allowed"):
        registry.resolve(SecretRef(name=aws_key))


def test_allow_list_flows_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APP_KEY", "v")
    registry = _registry(tmp_path, SecretsConfig(provider="env", env_allow=("APP_*",)))
    assert registry.resolve(SecretRef(name="APP_KEY")) == "v"
    assert registry.resolve(SecretRef(name="APP_KEY", provider="env")) == "v"


# -- the setting ---------------------------------------------------------------


@pytest.mark.parametrize("bad", ["APP_*", 5, ["APP_*", 3], [""], {"a": 1}, True])
def test_bad_setting_type_rejected(bad: object) -> None:
    with pytest.raises(SecretsError, match=r"\[secrets\.env\] allow .* list of non-empty strings"):
        parse_env_allow(bad)


def test_validate_rejects_a_bad_allow_list() -> None:
    with pytest.raises(SecretsError, match="list of non-empty strings"):
        SecretsConfig(env_allow=("ok", 1)).validate()  # type: ignore[arg-type]


def test_toml_allow_list_is_parsed(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text(
        '[secrets]\nprovider = "env"\n\n[secrets.env]\nallow = ["APP_*", "DB_PASSWORD"]\n'
    )
    assert load_project(tmp_path).secrets == SecretsConfig(
        provider="env", env_allow=("APP_*", "DB_PASSWORD")
    )


def test_missing_allow_list_is_empty(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text('[secrets]\nprovider = "env"\n')
    assert load_project(tmp_path).secrets.env_allow == ()


def test_toml_bad_allow_list_is_a_config_error(tmp_path: Path) -> None:
    (tmp_path / "atlantide.toml").write_text('[secrets.env]\nallow = "APP_*"\n')
    with pytest.raises(AtlantideError, match=r"\[secrets\.env\] allow"):
        load_project(tmp_path)


# -- plan and apply ------------------------------------------------------------

CONFIG = "Bucket('b', bucket_name='b', token=SecretRef('AWS_SECRET_ACCESS_KEY', provider='env'))"


def _engine(tmp_path: Path, config: SecretsConfig) -> Any:
    return engine_for(
        Bucket,
        provider=FakeProvider(),
        backend=MemoryStateBackend(),
        secrets=_registry(tmp_path, config),
    )


def test_plan_refuses_a_denied_env_secret(tmp_path: Path, aws_key: str) -> None:
    engine = _engine(tmp_path, SecretsConfig())
    result = engine.plan(CONFIG, extra_globals=globals_of(Bucket, SecretRef=SecretRef))
    assert isinstance(result, Failure)
    message = str(result.failure())
    assert "AWS_SECRET_ACCESS_KEY" in message
    assert "[secrets.env] allow" in message
    assert SECRET_VALUE not in message


async def test_apply_refuses_a_denied_env_secret(tmp_path: Path, aws_key: str) -> None:
    engine = _engine(tmp_path, SecretsConfig())
    result = await engine.apply(CONFIG, extra_globals=globals_of(Bucket, SecretRef=SecretRef))
    assert isinstance(result, Failure)
    message = str(result.failure())
    assert "[secrets.env] allow" in message
    assert SECRET_VALUE not in message


async def test_apply_resolves_an_allowed_env_secret(tmp_path: Path, aws_key: str) -> None:
    engine = _engine(tmp_path, SecretsConfig(env_allow=("AWS_*",)))
    result = await engine.apply(CONFIG, extra_globals=globals_of(Bucket, SecretRef=SecretRef))
    result.unwrap()
