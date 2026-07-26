"""Sealed-value format: v2 domain separation, legacy blobs, corrupt markers."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from atlantide.core.errors import SecretsError
from atlantide.core.types import SEALED_KEY
from atlantide.secrets._aesgcm import encrypt
from atlantide.secrets.material import SEAL_V2_PREFIX, KeyMaterial, is_sealed_marker


@pytest.fixture
def material(tmp_path: Path) -> KeyMaterial:
    return KeyMaterial(str(tmp_path / "k.key"))


def test_new_seals_are_versioned_and_roundtrip(material: KeyMaterial) -> None:
    marker = material.seal("hunter2")
    assert is_sealed_marker(marker)
    assert marker[SEALED_KEY].startswith(SEAL_V2_PREFIX)
    assert material.unseal(marker) == "hunter2"


def test_legacy_sealed_blobs_still_unseal(material: KeyMaterial) -> None:
    """Values sealed before v2 — bare base64, no AAD — persist in users' state
    and must keep unsealing after the format change."""
    blob = encrypt(material._key_bytes(), b"old secret")
    legacy = {SEALED_KEY: base64.b64encode(blob).decode("ascii")}
    assert not legacy[SEALED_KEY].startswith(SEAL_V2_PREFIX)  # ':' is not base64
    assert material.unseal(legacy) == "old secret"


def test_a_store_file_ciphertext_cannot_be_replayed_as_a_sealed_value(
    material: KeyMaterial,
) -> None:
    """Domain separation: the store file encrypts under the same key with no
    AAD, so without it a store blob wrapped in a marker would unseal."""
    store_blob = encrypt(material._key_bytes(), b'{"name": "value"}')  # as keyfile_store writes
    swapped = {SEALED_KEY: SEAL_V2_PREFIX + base64.b64encode(store_blob).decode("ascii")}
    with pytest.raises(SecretsError):
        material.unseal(swapped)


def test_a_v2_seal_cannot_be_downgraded_to_legacy(material: KeyMaterial) -> None:
    """Stripping the version prefix moves the ciphertext to the AAD-free legacy
    domain; the GCM tag no longer verifies."""
    marker = material.seal("hunter2")
    downgraded = {SEALED_KEY: marker[SEALED_KEY].removeprefix(SEAL_V2_PREFIX)}
    with pytest.raises(SecretsError):
        material.unseal(downgraded)


def test_a_corrupt_marker_raises_secrets_error_not_binascii(material: KeyMaterial) -> None:
    """A damaged payload must surface as SecretsError, not leak binascii.Error."""
    with pytest.raises(SecretsError, match="corrupt sealed value"):
        material.unseal({SEALED_KEY: "!!!not base64!!"})
