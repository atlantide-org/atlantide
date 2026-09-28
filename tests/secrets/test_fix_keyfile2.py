"""A digest salt never creates the install key outside a persisting run.

A read-only command (plan, validate, a dry run) that created a fresh random key
would salt its comparisons with it, so every secret stored under the real key
reads as rotated, and the wrong key left on disk later breaks unsealing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from atlantide.core.errors import SecretsError
from atlantide.secrets import KeyMaterial, SecretsRegistry


def test_salt_without_a_keyfile_raises_naming_it_and_creates_nothing(tmp_path: Path) -> None:
    key = tmp_path / "atlantide.key"
    with pytest.raises(SecretsError, match="no keyfile at") as caught:
        KeyMaterial(str(key)).salt()
    assert str(key) in str(caught.value)
    assert not key.exists()


def test_a_bare_digest_creates_no_key(tmp_path: Path) -> None:
    key = tmp_path / "atlantide.key"
    with pytest.raises(SecretsError):
        SecretsRegistry(material=KeyMaterial(str(key))).digest("n:f", "hunter2")
    assert not key.exists()


def test_an_armed_digest_creates_the_key_and_it_is_stable(tmp_path: Path) -> None:
    key = tmp_path / "atlantide.key"
    secrets = SecretsRegistry(material=KeyMaterial(str(key)))
    with secrets.creating_key():
        stored = secrets.digest("n:f", "hunter2")
    assert key.exists()
    # A later, unarmed install reading the same keyfile verifies the digest.
    again = SecretsRegistry(material=KeyMaterial(str(key)))
    assert again.digest_matches("n:f", "hunter2", stored)


def test_arming_is_scoped(tmp_path: Path) -> None:
    key = tmp_path / "atlantide.key"
    secrets = SecretsRegistry(material=KeyMaterial(str(key)))
    with secrets.creating_key():
        pass
    with pytest.raises(SecretsError):
        secrets.digest("n:f", "v")
    assert not key.exists()


def test_digest_matches_never_creates_even_when_armed(tmp_path: Path) -> None:
    """A stored digest was salted by an existing key; a new one could never match."""
    key = tmp_path / "atlantide.key"
    secrets = SecretsRegistry(material=KeyMaterial(str(key)))
    with secrets.creating_key(), pytest.raises(SecretsError, match="no keyfile at"):
        secrets.digest_matches("n:f", "v", "stored-digest")
    assert not key.exists()


def test_digest_matches_with_nothing_stored_never_reads_the_key(tmp_path: Path) -> None:
    key = tmp_path / "atlantide.key"
    assert not SecretsRegistry(material=KeyMaterial(str(key))).digest_matches("n:f", "v", None)
    assert not key.exists()


def test_arming_without_material_is_a_no_op() -> None:
    secrets = SecretsRegistry()
    with secrets.creating_key():
        assert secrets.digest("n:f", "v") == SecretsRegistry().digest("n:f", "v")
