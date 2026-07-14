"""Per-install secret material: the keyfile key, lazily loaded.

Provides the two things that must be tied to one install: the digest *salt*
(so rotation digests can't be dictionary-attacked from a state file) and the
*sealer* for sensitive values at rest. The key is loaded (or created ``0600``)
on first use, so read-only commands that never seal or digest touch no keyfile.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from atlantide.secrets._aesgcm import decrypt, encrypt, load_or_create_key, salt_from_key

SEALED_KEY = "$sealed"


def is_sealed_marker(value: Any) -> bool:
    """Whether ``value`` is a ``{"$sealed": "<b64>"}`` at-rest ciphertext marker."""
    return (
        isinstance(value, dict)
        and len(value) == 1
        and isinstance(value.get(SEALED_KEY), str)
    )


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
        blob = encrypt(self._key_bytes(), value.encode("utf-8"))
        return {SEALED_KEY: base64.b64encode(blob).decode("ascii")}

    def unseal(self, marker: dict[str, Any]) -> str:
        blob = base64.b64decode(marker[SEALED_KEY])
        return decrypt(self._key_bytes(), blob).decode("utf-8")
