"""Store-file format: v1 domain separation, legacy stores refused, durable keyfile.

The store file and ``{"$sealed": ...}`` values are AES-GCM under the same key. A
store blob that decrypts as a sealed value would let anyone who can write shared
state plant one and have ``output --reveal`` print every secret.
"""

from __future__ import annotations

import base64
import json
import os
import stat
from pathlib import Path

import pytest

from atlantide.core.errors import SecretsError
from atlantide.core.types import SEALED_KEY
from atlantide.secrets import KeyfileValueStore
from atlantide.secrets._aesgcm import encrypt, load_or_create_key
from atlantide.secrets.keyfile_store import STORE_V1_MAGIC
from atlantide.secrets.material import SEAL_V2_PREFIX, KeyMaterial


def _paths(tmp_path: Path) -> tuple[Path, Path]:
    return tmp_path / "s.enc", tmp_path / "s.key"


def _as_marker(blob: bytes, prefix: str = "") -> dict[str, str]:
    return {SEALED_KEY: prefix + base64.b64encode(blob).decode("ascii")}


def _write_legacy_store(store: Path, key_path: Path, values: dict[str, str]) -> None:
    """A pre-v1 store file: bare ``nonce || ciphertext``, no AAD."""
    key = load_or_create_key(key_path)
    store.write_bytes(encrypt(key, json.dumps(values, sort_keys=True).encode("utf-8")))


def test_new_store_files_carry_the_v1_header(tmp_path: Path) -> None:
    store_path, key_path = _paths(tmp_path)
    KeyfileValueStore(store_path, key_path).set("k", "v")
    assert store_path.read_bytes().startswith(STORE_V1_MAGIC)


@pytest.mark.parametrize("strip_header", [False, True])
@pytest.mark.parametrize("prefix", ["", SEAL_V2_PREFIX])
def test_a_store_file_planted_as_a_sealed_marker_does_not_unseal(
    tmp_path: Path, strip_header: bool, prefix: str
) -> None:
    """The attack: wrap the store file's bytes in a marker in shared state."""
    store_path, key_path = _paths(tmp_path)
    KeyfileValueStore(store_path, key_path).set("db/password", "hunter2")
    blob = store_path.read_bytes()
    if strip_header:
        blob = blob[len(STORE_V1_MAGIC) :]
    with pytest.raises(SecretsError):
        KeyMaterial(str(key_path)).unseal(_as_marker(blob, prefix))


def test_a_legacy_store_is_refused_not_migrated(tmp_path: Path) -> None:
    """A headerless store is rejected with a clear error and left untouched."""
    store_path, key_path = _paths(tmp_path)
    _write_legacy_store(store_path, key_path, {"db/password": "hunter2"})
    legacy_blob = store_path.read_bytes()

    store = KeyfileValueStore(store_path, key_path)
    with pytest.raises(SecretsError, match="legacy headerless store"):
        store.resolve("db/password")
    with pytest.raises(SecretsError, match="legacy headerless store"):
        store.set("b", "2")  # a write does not silently convert it either
    assert store_path.read_bytes() == legacy_blob  # never rewritten in place
    # And the AAD-free bytes do not unseal as a sealed value.
    with pytest.raises(SecretsError):
        KeyMaterial(str(key_path)).unseal(_as_marker(legacy_blob))


def test_check_reports_a_legacy_store(tmp_path: Path) -> None:
    store_path, key_path = _paths(tmp_path)
    _write_legacy_store(store_path, key_path, {"a": "1"})
    check = KeyfileValueStore(store_path, key_path).check()
    assert check.failed
    assert "legacy headerless store" in check.detail


def test_a_v1_header_on_an_aad_free_blob_does_not_decrypt(tmp_path: Path) -> None:
    """Prefixing a legacy blob with the header must not bypass the store AAD."""
    store_path, key_path = _paths(tmp_path)
    _write_legacy_store(store_path, key_path, {"a": "1"})
    store_path.write_bytes(STORE_V1_MAGIC + store_path.read_bytes())
    with pytest.raises(SecretsError):
        KeyfileValueStore(store_path, key_path).resolve("a")


def test_keyfile_creation_fsyncs_the_key_and_its_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crash after the link must not leave a 0-byte keyfile: that loses every
    secret the key encrypts."""
    synced: list[str] = []
    real_fsync = os.fsync

    def recording_fsync(fd: int) -> None:
        synced.append("dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    load_or_create_key(tmp_path / "k.key")
    assert synced == ["file", "dir"]
