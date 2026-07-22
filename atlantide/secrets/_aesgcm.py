"""AES-256-GCM primitives + local key management for the secrets stores.

Encrypts an opaque blob as ``nonce(12) || AES-256-GCM(data)``. Used to protect
the on-disk value-store at rest; the key lives in a sibling ``0600`` keyfile,
auto-generated on first use (creds-free).
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from atlantide.core.errors import SecretsError

_NONCE_BYTES = 12
KEY_BYTES = 32


def key_id(key: bytes) -> str:
    """Short, stable identifier for a key (detects rotation/mismatch)."""
    return hashlib.sha256(key).hexdigest()[:16]


def encrypt(key: bytes, data: bytes) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, data, None)


def decrypt(key: bytes, blob: bytes) -> bytes:
    try:
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, ciphertext, None)
    except (InvalidTag, ValueError) as exc:
        raise SecretsError(f"failed to decrypt secrets store: {exc}") from exc


def load_or_create_key(path: Path) -> bytes:
    """Load the 32-byte key at ``path``, creating it ``0600`` if absent."""
    if path.exists():
        key = path.read_bytes()
        if len(key) != KEY_BYTES:
            raise SecretsError(
                f"keyfile {str(path)!r} holds {len(key)} bytes, expected {KEY_BYTES}"
            )
        return key
    key = os.urandom(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Restrict to 0600 before any bytes are written, so the key is never
    # world-readable.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    return key


def salt_from_key(key: bytes) -> bytes:
    """A per-install digest salt derived from the keyfile key.

    Unique per install (the key is random per install) and stable, so rotation
    digests are not brute-forceable from a state file with only the public code.
    Distinct from the encryption use of the key (domain-separated prefix).
    """
    return hashlib.sha256(b"atlantide/secret-salt/v1" + key).digest()
