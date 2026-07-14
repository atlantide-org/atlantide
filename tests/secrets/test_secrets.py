"""Secrets: value-store roundtrips, env resolution, registry routing, digests, markers."""

from __future__ import annotations

import os

import pytest

from atlantide.core import SecretRef
from atlantide.core.errors import SecretsError
from atlantide.secrets import (
    EnvSecretsProvider,
    KeyfileValueStore,
    SecretsRegistry,
    is_secret_ref_marker,
    secret_digest,
    secret_ref_from_marker,
)


def _store(tmp_path: object, name: str = "s") -> KeyfileValueStore:
    base = str(tmp_path)  # type: ignore[arg-type]
    return KeyfileValueStore(os.path.join(base, f"{name}.enc"), os.path.join(base, f"{name}.key"))


# -- keyfile value-store -----------------------------------------------------


def test_store_set_resolve_roundtrip(tmp_path: object) -> None:
    store = _store(tmp_path)
    store.set("app/signing-key", "hunter2")
    assert store.resolve("app/signing-key") == "hunter2"
    assert store.names() == ["app/signing-key"]


def test_store_file_is_encrypted_no_plaintext(tmp_path: object) -> None:
    base = str(tmp_path)  # type: ignore[arg-type]
    store_path = os.path.join(base, "s.enc")
    store = KeyfileValueStore(store_path, os.path.join(base, "s.key"))
    store.set("k", "SUPERSECRET")
    with open(store_path, "rb") as fh:
        blob = fh.read()
    assert b"SUPERSECRET" not in blob  # value encrypted at rest
    assert oct(os.stat(os.path.join(base, "s.key")).st_mode & 0o777) == "0o600"


def test_store_delete_and_missing(tmp_path: object) -> None:
    store = _store(tmp_path)
    store.set("k", "v")
    assert store.delete("k") is True
    assert store.delete("k") is False  # already gone
    with pytest.raises(SecretsError):
        store.resolve("k")  # missing -> error


def test_store_reopens_with_same_key(tmp_path: object) -> None:
    base = str(tmp_path)  # type: ignore[arg-type]
    paths = (os.path.join(base, "s.enc"), os.path.join(base, "s.key"))
    KeyfileValueStore(*paths).set("k", "v")
    assert KeyfileValueStore(*paths).resolve("k") == "v"  # separate instance, same files


# -- env provider ------------------------------------------------------------


def test_env_provider_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DB_PASSWORD", "s3cr3t")
    assert EnvSecretsProvider().resolve("DB_PASSWORD") == "s3cr3t"


def test_env_provider_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOPE", raising=False)
    with pytest.raises(SecretsError):
        EnvSecretsProvider().resolve("NOPE")


# -- registry routing --------------------------------------------------------


def test_registry_resolves_via_default_and_named(
    tmp_path: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_KEY", "from-env")
    store = _store(tmp_path)
    store.set("app/key", "from-store")
    reg = SecretsRegistry()
    reg.register(store, default=True)
    reg.register(EnvSecretsProvider())

    assert reg.resolve(SecretRef("app/key")) == "from-store"  # default (keyfile)
    assert reg.resolve(SecretRef("API_KEY", provider="env")) == "from-env"  # routed by provider


def test_registry_unknown_provider_and_empty(tmp_path: object) -> None:
    with pytest.raises(SecretsError):
        SecretsRegistry().resolve(SecretRef("x"))  # no provider registered
    reg = SecretsRegistry()
    reg.register(_store(tmp_path), default=True)
    with pytest.raises(SecretsError):
        reg.resolve(SecretRef("x", provider="nope"))  # unknown provider


# -- digest / markers --------------------------------------------------------


def test_digest_deterministic_scoped_hides_value() -> None:
    d = secret_digest("n:f", "hunter2")
    assert d == secret_digest("n:f", "hunter2")  # stable
    assert "hunter2" not in d  # never the value
    assert secret_digest("n:other", "hunter2") != d  # scoped per field


def test_secret_ref_marker_roundtrip() -> None:
    ref = SecretRef("app/key", provider="env")
    marker = ref.canonical()
    assert marker == {"$secret_ref": {"name": "app/key", "provider": "env"}}
    assert is_secret_ref_marker(marker)
    assert not is_secret_ref_marker({"$ref": "a#b"})
    assert secret_ref_from_marker(marker) == ref
