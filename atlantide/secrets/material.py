"""Per-install secret material: the keyfile key, lazily loaded.

Provides the two things that must be tied to one install: the digest *salt*
(so rotation digests can't be dictionary-attacked from a state file) and the
*sealer* for sensitive values at rest. The key is loaded (or created ``0600``)
on first use, so read-only commands that never seal or digest touch no keyfile.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from atlantide.core.errors import SecretsError
from atlantide.core.types import SEALED_KEY
from atlantide.secrets._aesgcm import decrypt, encrypt, load_or_create_key, salt_from_key

__all__ = ["SEALED_KEY", "KeyMaterial", "is_sealed_marker"]

#: Version prefix on new sealed payloads. ``:`` is not a base64 character, so a
#: legacy payload (bare base64 of ``nonce || ciphertext``) can never start with it.
SEAL_V2_PREFIX = "v2:"

#: AAD binding v2 sealed values to this use of the key. Without it a sealed
#: value and the store-file blob are ciphertexts under the same key with no
#: domain separation, so one could be replayed as the other.
_SEAL_V2_AAD = b"atlantide/sealed/v2"


def is_sealed_marker(value: Any) -> bool:
    """Whether ``value`` is a ``{"$sealed": "<b64>"}`` at-rest ciphertext marker."""
    return isinstance(value, dict) and len(value) == 1 and isinstance(value.get(SEALED_KEY), str)


class KeyMaterial:
    """Lazily loads the install keyfile; yields the digest salt and seals values."""

    def __init__(self, key_path: str) -> None:
        self._key_path = Path(key_path)
        self._key: bytes | None = None

    def _key_bytes(self) -> bytes:
        if self._key is None:
            self._key = load_or_create_key(self._key_path)
        return self._key

    def salt(self) -> bytes:
        return salt_from_key(self._key_bytes())

    def seal(self, value: str) -> dict[str, str]:
        blob = encrypt(self._key_bytes(), value.encode("utf-8"), aad=_SEAL_V2_AAD)
        return {SEALED_KEY: SEAL_V2_PREFIX + base64.b64encode(blob).decode("ascii")}

    def unseal(self, marker: dict[str, Any]) -> str:
        """Unseal a marker: v2 (domain-separated AAD) or legacy (bare base64)."""
        payload = marker[SEALED_KEY]
        versioned = payload.startswith(SEAL_V2_PREFIX)
        try:
            blob = base64.b64decode(payload[len(SEAL_V2_PREFIX) :] if versioned else payload)
        except binascii.Error as exc:
            raise SecretsError(f"corrupt sealed value: {exc}") from exc
        aad = _SEAL_V2_AAD if versioned else None
        return decrypt(self._key_bytes(), blob, aad=aad).decode("utf-8")
