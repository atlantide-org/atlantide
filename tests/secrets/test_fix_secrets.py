"""Regression tests: SSM transport errors, dangling keyfile links, store saves."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError

from atlantide.core.errors import SecretsError
from atlantide.secrets import KeyMaterial, SecretsRegistry
from atlantide.secrets._aesgcm import load_or_create_key
from atlantide.secrets.keyfile_store import KeyfileValueStore
from atlantide.secrets.ssm import SsmParameterStore
from tests.support import TEST_REGION, fake_aws_credentials


def _raise(exc: Exception) -> Any:
    def boom(**_: Any) -> Any:
        raise exc

    return boom


@pytest.mark.parametrize(
    "exc",
    [NoCredentialsError(), EndpointConnectionError(endpoint_url="https://ssm.invalid")],
)
def test_ssm_resolve_wraps_botocore_errors(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """No credentials or no endpoint must surface as SecretsError, not a traceback."""
    fake_aws_credentials(monkeypatch, region=TEST_REGION)
    store = SsmParameterStore(prefix="/p/", region=TEST_REGION)
    monkeypatch.setattr(store._client, "get_parameter", _raise(exc))
    with pytest.raises(SecretsError, match="cannot reach SSM for '/p/db'") as info:
        store.resolve("db")
    assert info.value.__cause__ is exc


def test_dangling_keyfile_symlink_is_refused_not_recursed(tmp_path: Path) -> None:
    path = tmp_path / "key"
    path.symlink_to(tmp_path / "nowhere")
    with pytest.raises(SecretsError, match="dangling link or unreadable") as info:
        load_or_create_key(path)
    assert str(path) in str(info.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["key"]  # no temp left behind


def test_store_save_does_not_write_through_a_planted_symlink(tmp_path: Path) -> None:
    store_path = tmp_path / "secrets.enc"
    victim = tmp_path / "victim"
    victim.write_bytes(b"victim")
    # The old fixed temp name, planted as a symlink to a file we must not touch.
    (tmp_path / "secrets.enc.tmp").symlink_to(victim)
    store = KeyfileValueStore(store_path, tmp_path / "key")
    store.set("a", "1")
    assert victim.read_bytes() == b"victim"
    assert store.names() == ["a"]
    assert os.stat(store_path).st_mode & 0o777 == 0o600


def test_store_save_leaves_no_temp_file_when_the_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = KeyfileValueStore(tmp_path / "secrets.enc", tmp_path / "key")
    store.set("a", "1")
    before = sorted(p.name for p in tmp_path.iterdir())

    def refuse(src: object, dst: object) -> None:
        raise PermissionError("read-only")

    monkeypatch.setattr(os, "replace", refuse)
    with pytest.raises(PermissionError):
        store.set("b", "2")
    assert sorted(p.name for p in tmp_path.iterdir()) == before
    monkeypatch.undo()
    assert store.names() == ["a"]


# -- decrypt-only paths never create a key ---------------------------------------


def test_unseal_without_a_keyfile_raises_and_creates_nothing(tmp_path: Path) -> None:
    sealer = tmp_path / "sealer.key"
    marker = KeyMaterial(str(sealer)).seal("hunter2")
    missing = tmp_path / "ci.key"
    with pytest.raises(SecretsError, match=r"no keyfile at .*cannot be decrypted without"):
        KeyMaterial(str(missing)).unseal(marker)
    assert not missing.exists()


def test_unseal_with_the_wrong_key_names_the_keyfile(tmp_path: Path) -> None:
    marker = KeyMaterial(str(tmp_path / "a.key")).seal("hunter2")
    other = tmp_path / "b.key"
    load_or_create_key(other)
    with pytest.raises(SecretsError, match=r"cannot decrypt a sealed value with .*b\.key"):
        KeyMaterial(str(other)).unseal(marker)


def test_registry_unseal_without_a_keyfile_creates_nothing(tmp_path: Path) -> None:
    marker = SecretsRegistry(material=KeyMaterial(str(tmp_path / "a.key"))).seal("v")
    missing = tmp_path / "ci.key"
    with pytest.raises(SecretsError, match="no keyfile"):
        SecretsRegistry(material=KeyMaterial(str(missing))).unseal(marker)
    assert not missing.exists()


def test_sealing_still_creates_the_keyfile(tmp_path: Path) -> None:
    path = tmp_path / "k.key"
    KeyMaterial(str(path)).seal("v")
    assert path.exists()


def test_reading_a_store_without_its_keyfile_creates_nothing(tmp_path: Path) -> None:
    key = tmp_path / "k.key"
    KeyfileValueStore(tmp_path / "secrets.enc", key).set("a", "1")
    key.unlink()
    store = KeyfileValueStore(tmp_path / "secrets.enc", key)
    with pytest.raises(SecretsError, match="no keyfile"):
        store.resolve("a")
    with pytest.raises(SecretsError, match="no keyfile"):
        store.set("b", "2")  # would otherwise re-encrypt under a fresh, wrong key
    assert not key.exists()


def test_first_secret_set_still_creates_the_keyfile(tmp_path: Path) -> None:
    key = tmp_path / "k.key"
    store = KeyfileValueStore(tmp_path / "secrets.enc", key)
    assert store.names() == []
    assert not key.exists()
    store.set("a", "1")
    assert key.exists()
    assert store.resolve("a") == "1"
