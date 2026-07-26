"""AES-256-GCM primitives + local key management for the secrets stores.

Encrypts an opaque blob as ``nonce(12) || AES-256-GCM(data)``. Used to protect
the on-disk value-store at rest; the key lives in a sibling ``0600`` keyfile,
auto-generated on first use (creds-free).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from atlantide.core.errors import SecretsError

_NONCE_BYTES = 12
KEY_BYTES = 32


def encrypt(key: bytes, data: bytes, aad: bytes | None = None) -> bytes:
    nonce = os.urandom(_NONCE_BYTES)
    return nonce + AESGCM(key).encrypt(nonce, data, aad)


def decrypt(key: bytes, blob: bytes, aad: bytes | None = None) -> bytes:
    try:
        nonce, ciphertext = blob[:_NONCE_BYTES], blob[_NONCE_BYTES:]
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except (InvalidTag, ValueError) as exc:
        raise SecretsError(f"failed to decrypt secrets store: {exc}") from exc


#: Mode every file holding key material or ciphertext is created with.
OWNER_ONLY_MODE = 0o600

#: Permission bits that must not be set on a keyfile (group/other, any access).
_KEYFILE_FORBIDDEN_MODE = 0o077


def load_or_create_key(path: Path) -> bytes:
    """Load the 32-byte key at ``path``, creating it ``0600`` if absent."""
    if path.exists():
        # Checked on load as well as creation: a key restored from a backup or
        # copied under a lax umask can arrive group- or world-readable.
        mode = path.stat().st_mode & 0o777
        if mode & _KEYFILE_FORBIDDEN_MODE:
            raise SecretsError(
                f"keyfile {str(path)!r} is mode {mode:04o}; it must not be readable by "
                f"group or others — run `chmod 600 {path}`"
            )
        key = path.read_bytes()
        if len(key) != KEY_BYTES:
            raise SecretsError(
                f"keyfile {str(path)!r} holds {len(key)} bytes, expected {KEY_BYTES}"
            )
        return key
    key = os.urandom(KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Written to a same-dir 0600 temp file first, then hard-linked into place:
    # the link is an atomic create-if-absent of a *fully written* keyfile, so a
    # concurrent first run can neither crash on the creation race nor read a
    # half-written key. mkstemp creates the file 0600 before any bytes land, so
    # the key is never world-readable.
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            # Lost the race: another process created the key first — use theirs.
            return load_or_create_key(path)
    finally:
        os.unlink(tmp)
    return key


def salt_from_key(key: bytes) -> bytes:
    """A per-install digest salt derived from the keyfile key.

    Unique per install (the key is random per install) and stable, so rotation
    digests are not brute-forceable from a state file with only the public code.
    Distinct from the encryption use of the key (domain-separated prefix).
    """
    return hashlib.sha256(b"atlantide/secret-salt/v1" + key).digest()
