"""Declarative selection of the secrets provider.

:class:`SecretsConfig` is the parsed ``[secrets]`` table from ``atlantide.toml``;
:func:`make_secrets_registry` builds the registry the engine resolves against.

Every provider is registered, so a :class:`~atlantide.core.types.SecretRef` may
name one explicitly; ``[secrets].provider`` selects the default for refs that do
not. Registration opens no resources: the keyfile is read lazily on first use.

The registry always carries per-install :class:`KeyMaterial` — the digest salt
and the sealer for sensitive outputs at rest — which is local regardless of
where secret values are resolved from. See the README on sharing the keyfile
when state is remote.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from atlantide.core.errors import SecretsError
from atlantide.secrets.env import EnvSecretsProvider
from atlantide.secrets.keyfile_store import KeyfileValueStore
from atlantide.secrets.material import KeyMaterial
from atlantide.secrets.registry import SecretsRegistry

KEYFILE = "keyfile"
ENV = "env"
SSM = "ssm"
PROVIDERS = (KEYFILE, ENV, SSM)


@dataclass(frozen=True)
class SecretsConfig:
    """The ``[secrets]`` table. Defaults to the local keyfile value-store."""

    provider: str = KEYFILE
    #: Prepended to a secret's name to form the remote path (ssm).
    prefix: str = ""
    region: str | None = None
    profile: str | None = None
    endpoint: str | None = None

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise SecretsError(
                f"unknown [secrets].provider {self.provider!r} — expected one of "
                f"{', '.join(PROVIDERS)}"
            )


def make_secrets_registry(
    config: SecretsConfig, *, store_path: Path, key_path: Path
) -> SecretsRegistry:
    """Build the registry: every provider available, ``config.provider`` the default."""
    config.validate()
    registry = SecretsRegistry(material=KeyMaterial(str(key_path)))
    registry.register(
        KeyfileValueStore(store_path, key_path), default=config.provider == KEYFILE
    )
    registry.register(EnvSecretsProvider(), default=config.provider == ENV)
    if config.provider == SSM:
        from atlantide.secrets.ssm import SsmParameterStore

        # An unset region or profile falls through to the standard AWS resolution
        # chain: environment, shared config, instance metadata.
        registry.register(
            SsmParameterStore(
                prefix=config.prefix,
                region=config.region,
                profile=config.profile,
                endpoint_url=config.endpoint,
            ),
            default=True,
        )
    return registry
