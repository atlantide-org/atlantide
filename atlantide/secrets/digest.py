"""Secret-reference markers and the rotation digest.

A :class:`~atlantide.core.types.SecretRef` serializes to ``{"$secret_ref":
{"name", "provider"}}`` in canonical inputs, the IR, and persisted state — a
handle, never a value. State also keeps a salted digest of the *resolved* value
so a rotation (same name, new value) is detectable without storing the value.

The salt is scoped per ``{node_id}:{field}``, so the same value under two fields
yields different digests and cannot be correlated across fields. A per-install
salt (see :class:`~atlantide.secrets.material.KeyMaterial`) is used where
available; :data:`_FALLBACK_SALT` is the fixed fallback for callers that supply
no salt.
"""

from __future__ import annotations

import hashlib
from typing import Any

from atlantide.core.types import SecretRef

_MARKER_KEY = "$secret_ref"

_FALLBACK_SALT = b"atlantide/secret/v1"


def secret_digest(scope: str, plaintext: str, *, salt: bytes = _FALLBACK_SALT) -> str:
    """Stable per-scope digest of a resolved secret value (hex sha256)."""
    hasher = hashlib.sha256()
    hasher.update(salt)
    hasher.update(scope.encode("utf-8"))
    hasher.update(b"\0")
    hasher.update(plaintext.encode("utf-8"))
    return hasher.hexdigest()


def is_secret_ref_marker(value: Any) -> bool:
    """Whether ``value`` is a ``{"$secret_ref": {...}}`` handle marker."""
    return (
        isinstance(value, dict)
        and len(value) == 1
        and isinstance(value.get(_MARKER_KEY), dict)
    )


def secret_ref_from_marker(value: Any) -> SecretRef:
    """Parse a ``{"$secret_ref": {...}}`` marker back into a :class:`SecretRef`."""
    inner = value[_MARKER_KEY]
    return SecretRef(name=str(inner["name"]), provider=inner.get("provider"))
