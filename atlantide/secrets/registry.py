"""``SecretsRegistry``: name -> configured :class:`SecretsProvider`, with a default.

``resolve`` routes a :class:`SecretRef` to its named provider (or the default) to
fetch the plaintext at apply time. Lookups raise :class:`SecretsError`: they run
on the executor's raising path, not the pure ``Result`` planning path.

The registry also carries optional per-install :class:`KeyMaterial`, through which
callers digest rotation values and seal or unseal sensitive computed outputs at
rest. Without material (development, tests), digests use the fixed fallback salt
and sealing returns values unchanged.
"""

from __future__ import annotations

import hmac
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Any

from atlantide.core.errors import SecretsError
from atlantide.core.types import SecretRef
from atlantide.secrets.base import SecretsProvider
from atlantide.secrets.digest import secret_digest
from atlantide.secrets.material import KeyMaterial, is_sealed_marker


class SecretsRegistry:
    """Holds the configured secrets providers, the default backend, and material."""

    def __init__(self, *, material: KeyMaterial | None = None) -> None:
        self._providers: dict[str, SecretsProvider] = {}
        self._default: str | None = None
        self._material = material

    def register(self, provider: SecretsProvider, *, default: bool = False) -> None:
        # ``name`` is a ClassVar the ABC declares but cannot enforce; a missing one
        # raises a SecretsError naming the class instead of an AttributeError.
        name = getattr(provider, "name", "")
        if not name:
            raise SecretsError(f"secrets provider {type(provider).__name__} declares no name")
        if name in self._providers:
            raise SecretsError(f"duplicate secrets provider {name!r}")
        self._providers[name] = provider
        if default or self._default is None:
            self._default = name

    def get(self, name: str) -> SecretsProvider:
        provider = self._providers.get(name)
        if provider is None:
            raise SecretsError(f"unknown secrets provider {name!r}")
        return provider

    def resolve(self, ref: SecretRef) -> str:
        """Resolve a handle to its plaintext via its provider (or the default)."""
        name = ref.provider or self._default
        if name is None:
            raise SecretsError("no secrets provider registered")
        return self.get(name).resolve(ref.name)

    def __contains__(self, name: str) -> bool:
        return name in self._providers

    # -- rotation digests -------------------------------------------------

    @contextmanager
    def creating_key(self) -> Iterator[None]:
        """Arm key creation for a persisting run; see :meth:`KeyMaterial.creating`."""
        with self._material.creating() if self._material is not None else nullcontext():
            yield

    def digest(self, scope: str, plaintext: str) -> str:
        """Digest for a resolved secret value, using the per-install salt if any.

        Without a keyfile this raises unless a persisting run armed creation
        (:meth:`creating_key`).
        """
        if self._material is not None:
            return secret_digest(scope, plaintext, salt=self._material.salt())
        return secret_digest(scope, plaintext)

    def digest_matches(self, scope: str, plaintext: str, stored: str | None) -> bool:
        """Whether ``stored`` is a digest of ``plaintext`` under this install.

        Never creates a key, even in an armed run: a stored digest was salted
        with an existing keyfile, and a missing one raises naming its path.
        """
        if stored is None:
            return False
        if self._material is not None:
            fresh = secret_digest(scope, plaintext, salt=self._material.salt(create=False))
        else:
            fresh = secret_digest(scope, plaintext)
        # Constant-time: these are digests of secret plaintext.
        return hmac.compare_digest(stored, fresh)

    # -- sealing sensitive values at rest ---------------------------------

    def seal(self, value: str) -> Any:
        """Seal a plaintext value for persistence, or return it as-is without material."""
        return self._material.seal(value) if self._material is not None else value

    def unseal(self, value: Any) -> Any:
        """Unseal a ``{"$sealed": ...}`` marker; pass any other value through."""
        if self._material is not None and is_sealed_marker(value):
            return self._material.unseal(value)
        return value
