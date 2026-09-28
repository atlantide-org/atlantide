"""Per-install secret material: the keyfile key, lazily loaded.

Provides the two per-install values: the digest salt, so rotation digests in a
state file resist dictionary attacks, and the sealer for sensitive values at
rest. The key is loaded on first use, so commands that never seal or digest do
not touch the keyfile. It is created (``0600``) only on a persisting path:
sealing a value, or digesting one while creation is armed (see
:meth:`KeyMaterial.creating`). A read-only digest (a plan comparing against
state) never creates one: a fresh key's salt could not match any stored digest,
and the file left behind would break later unsealing.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from atlantide.core.errors import SecretsError
from atlantide.core.markers import single_key_marker
from atlantide.core.types import SEALED_KEY
from atlantide.secrets._aesgcm import (
    decrypt,
    encrypt,
    load_key,
    load_or_create_key,
    salt_from_key,
)

__all__ = ["SEALED_KEY", "KeyMaterial", "is_sealed_marker"]

#: Version prefix on sealed payloads, the only accepted format. ``:`` is not a
#: base64 character, so an unversioned payload (bare base64 of
#: ``nonce || ciphertext``) can never start with it.
SEAL_V2_PREFIX = "v2:"

#: AAD binding v2 sealed values to this use of the key, so a sealed value and
#: the store-file blob (same key) cannot be replayed as each other.
_SEAL_V2_AAD = b"atlantide/sealed/v2"


def is_sealed_marker(value: Any) -> bool:
    """Whether ``value`` is a ``{"$sealed": "<b64>"}`` at-rest ciphertext marker."""
    return isinstance(single_key_marker(value, SEALED_KEY), str)


class KeyMaterial:
    """Lazily loads the install keyfile; yields the digest salt and seals values."""

    def __init__(self, key_path: str) -> None:
        self._key_path = Path(key_path)
        self._key: bytes | None = None
        #: Whether :meth:`salt` may create a missing key; set by :meth:`creating`.
        self._may_create = False

    @contextmanager
    def creating(self) -> Iterator[None]:
        """Let :meth:`salt` create a missing key for the duration (persisting runs).

        Arms creation without performing it, so a run that digests nothing (a
        no-op apply, a destroy) still leaves no keyfile behind.
        """
        previous, self._may_create = self._may_create, True
        try:
            yield
        finally:
            self._may_create = previous

    def _key_bytes(self, *, create: bool = True) -> bytes:
        """The key; ``create=False`` refuses to generate one (decrypt-only paths)."""
        if self._key is None:
            load = load_or_create_key if create else load_key
            self._key = load(self._key_path)
        return self._key

    def salt(self, *, create: bool | None = None) -> bytes:
        """The digest salt; creates a missing key only when armed or ``create=True``.

        ``create=False`` refuses even when armed, for a comparison against digests
        already in state.
        """
        may_create = self._may_create if create is None else create
        if self._key is None and not may_create and not self._key_path.exists():
            raise SecretsError(
                f"no keyfile at {str(self._key_path)!r}: state holds secret digests salted "
                f"with the keyfile that wrote it — point secrets_key at that keyfile (a new "
                f"key would make every secret read as rotated)"
            )
        return salt_from_key(self._key_bytes(create=may_create))

    def seal(self, value: str) -> dict[str, str]:
        blob = encrypt(self._key_bytes(), value.encode("utf-8"), aad=_SEAL_V2_AAD)
        return {SEALED_KEY: SEAL_V2_PREFIX + base64.b64encode(blob).decode("ascii")}

    def unseal(self, marker: dict[str, Any]) -> str:
        """Unseal a v2 marker; refuse any other payload.

        An unversioned payload carries no AAD, so decrypting it would let another
        blob under the same key be replayed as a sealed value.
        """
        payload = marker[SEALED_KEY]
        if not payload.startswith(SEAL_V2_PREFIX):
            raise SecretsError("legacy sealed value (unversioned, no AAD) is not supported")
        try:
            blob = base64.b64decode(payload[len(SEAL_V2_PREFIX) :])
        except binascii.Error as exc:
            raise SecretsError(f"corrupt sealed value: {exc}") from exc
        key = self._key_bytes(create=False)
        try:
            plaintext = decrypt(key, blob, aad=_SEAL_V2_AAD)
        except SecretsError as exc:
            raise SecretsError(
                f"cannot decrypt a sealed value with {str(self._key_path)!r} — wrong or "
                f"regenerated keyfile, or a damaged value"
            ) from exc.__cause__
        return plaintext.decode("utf-8")
