"""AES-256-GCM primitives and local key management for the secrets stores.

Encrypts an opaque blob as ``nonce(12) || AES-256-GCM(data)`` to protect the
on-disk value store at rest. The key lives in a sibling ``0600`` keyfile,
generated on first use, so no credentials are needed.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from atlantide.core.errors import SecretsError
from atlantide.util.fs import OWNER_ONLY_MODE as OWNER_ONLY_MODE
from atlantide.util.fs import fsync_dir

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


#: Permission bits that must not be set on a keyfile (group/other, any access).
_KEYFILE_FORBIDDEN_MODE = 0o077


def load_or_create_key(path: Path) -> bytes:
    """Load the 32-byte key at ``path``, creating it ``0600`` if absent."""
    if path.exists():
        return _read_key(path)
    key = os.urandom(KEY_BYTES)
    if _publish_key(path, key):
        return key
    # Another process created the key first; use that one. If it still does not
    # resolve, something that is not a keyfile holds the name (a dangling
    # symlink, say), and retrying would loop forever.
    if path.exists():
        return _read_key(path)
    raise SecretsError(
        f"keyfile {str(path)!r} exists but cannot be read (dangling link or unreadable) — "
        f"remove it or point it at the real keyfile"
    )


def load_key(path: Path) -> bytes:
    """Load the 32-byte key at ``path``; never create one.

    For paths that only decrypt: a fresh key could never open what an earlier
    key sealed, so writing one would only leave a wrong keyfile behind.
    """
    if not path.exists():
        raise SecretsError(
            f"no keyfile at {str(path)!r}: sealed values cannot be decrypted without "
            f"the key that sealed them"
        )
    return _read_key(path)


def _read_key(path: Path) -> bytes:
    """The key at ``path``, refused if its mode or length is wrong."""
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
        raise SecretsError(f"keyfile {str(path)!r} holds {len(key)} bytes, expected {KEY_BYTES}")
    return key


def _publish_key(path: Path, key: bytes) -> bool:
    """Create the keyfile at ``path`` holding ``key``; ``False`` if one appeared first.

    The key goes to a same-directory temp file (``mkstemp`` creates it ``0600``
    before any bytes are written), then is hard-linked into place. The link is an
    atomic create-if-absent of a fully written keyfile, so concurrent first runs
    neither crash on the race nor read a partial key. The key is fsynced before
    the link and the directory after it, so a crash cannot leave a 0-byte keyfile
    in place and lose every secret it encrypts.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        try:
            os.write(fd, key)
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(tmp, str(path))
        except FileExistsError:
            return False
    finally:
        os.unlink(tmp)
    fsync_dir(path.parent)
    return True


def salt_from_key(key: bytes) -> bytes:
    """A per-install digest salt derived from the keyfile key.

    Unique per install and stable, so rotation digests in a state file cannot be
    brute-forced with the public code alone. The prefix domain-separates the salt
    from the encryption use of the key.
    """
    return hashlib.sha256(b"atlantide/secret-salt/v1" + key).digest()
