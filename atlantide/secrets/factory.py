"""Declarative selection of the secrets provider.

:class:`SecretsConfig` is the parsed ``[secrets]`` table from ``atlantide.toml``;
:func:`make_secrets_registry` builds the registry the engine resolves against.

The ``keyfile`` and ``env`` providers are always registered (``ssm`` only when
selected), so a :class:`~atlantide.core.types.SecretRef` may name one explicitly;
``[secrets].provider`` selects the default for refs that do not. Registration
opens no resources: the keyfile is read lazily on first use.

The ``env`` provider is deny-by-default whether or not it is the default: it
resolves only names matching ``[secrets.env] allow`` (see
:mod:`atlantide.secrets.env`).

The registry always carries per-install :class:`KeyMaterial` (the digest salt and
the sealer for sensitive outputs at rest), which stays local whichever provider
resolves values. Every install reading the same state needs the same keyfile to
unseal outputs and match digests.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from atlantide.core.errors import SecretsError
from atlantide.secrets.env import ALLOW_SETTING, EnvSecretsProvider
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
    #: ``[secrets.env] allow``: glob patterns of env var names the ``env``
    #: provider may read. Empty (the default) allows none.
    env_allow: tuple[str, ...] = ()

    def validate(self) -> None:
        if self.provider not in PROVIDERS:
            raise SecretsError(
                f"unknown [secrets].provider {self.provider!r} — expected one of "
                f"{', '.join(PROVIDERS)}"
            )
        parse_env_allow(self.env_allow)


def parse_env_allow(value: object) -> tuple[str, ...]:
    """``[secrets.env] allow`` as a tuple of patterns; ``None`` (unset) is empty.

    Anything but a list of non-empty strings raises, so a malformed allow-list
    is reported instead of denying every name.
    """
    if value is None:
        return ()
    if isinstance(value, list | tuple):
        items = cast("list[object] | tuple[object, ...]", value)
        if all(isinstance(item, str) and item for item in items):
            return tuple(cast("list[str]", list(items)))
    raise SecretsError(
        f"{ALLOW_SETTING} in atlantide.toml must be a list of non-empty strings "
        f"(glob patterns of env var names), got {value!r}"
    )


def make_secrets_registry(
    config: SecretsConfig, *, store_path: Path, key_path: Path
) -> SecretsRegistry:
    """Build the registry with ``config.provider`` as the default provider."""
    config.validate()
    registry = SecretsRegistry(material=KeyMaterial(str(key_path)))
    registry.register(KeyfileValueStore(store_path, key_path), default=config.provider == KEYFILE)
    registry.register(EnvSecretsProvider(config.env_allow), default=config.provider == ENV)
    if config.provider == SSM:
        from atlantide.secrets.ssm import SsmParameterStore

        # An unset region or profile falls back to the standard AWS resolution
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
