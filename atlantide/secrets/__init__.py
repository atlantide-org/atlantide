"""atlantide.secrets: reference secrets by name; resolve values at apply.

Config, IR and state carry only a :class:`~atlantide.core.types.SecretRef` handle;
the plaintext is resolved in memory at apply from a pluggable
:class:`SecretsProvider` and never persisted. See ``README.md`` for the providers,
the rotation digest and sealed outputs.
"""

from atlantide.secrets.base import SecretsProvider
from atlantide.secrets.digest import (
    is_secret_ref_marker,
    secret_digest,
    secret_ref_from_marker,
)
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.secrets.factory import SecretsConfig, make_secrets_registry
from atlantide.secrets.keyfile_store import KeyfileValueStore
from atlantide.secrets.material import KeyMaterial, is_sealed_marker
from atlantide.secrets.registry import SecretsRegistry

__all__ = [
    "EnvSecretsProvider",
    "KeyMaterial",
    "KeyfileValueStore",
    "SecretsConfig",
    "SecretsProvider",
    "SecretsRegistry",
    "is_sealed_marker",
    "is_secret_ref_marker",
    "make_secrets_registry",
    "secret_digest",
    "secret_ref_from_marker",
]
