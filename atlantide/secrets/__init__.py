"""atlantide.secrets: reference secrets by name; resolve values at apply.

A resource field holds a :class:`~atlantide.core.types.SecretRef` (a name, never a
value). Config, IR, and state carry only the handle; the plaintext is resolved
from a pluggable :class:`SecretsProvider` (keyfile value-store default, env for
dev) in-memory at apply and never persisted. State keeps a salted digest of the
resolved value so a rotation is detectable, and *seals* sensitive computed
outputs at rest (see :class:`~atlantide.secrets.material.KeyMaterial`).
"""

from atlantide.secrets.backend import SecretsProvider
from atlantide.secrets.digest import (
    is_secret_ref_marker,
    secret_digest,
    secret_ref_from_marker,
)
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.secrets.keyfile_store import KeyfileValueStore
from atlantide.secrets.material import KeyMaterial, is_sealed_marker
from atlantide.secrets.registry import SecretsRegistry

__all__ = [
    "EnvSecretsProvider",
    "KeyMaterial",
    "KeyfileValueStore",
    "SecretsProvider",
    "SecretsRegistry",
    "is_sealed_marker",
    "is_secret_ref_marker",
    "secret_digest",
    "secret_ref_from_marker",
]
