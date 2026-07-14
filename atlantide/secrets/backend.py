"""``SecretsProvider``: the pluggable secret-value resolver.

Every provider declares a ``name`` (matched against a :class:`SecretRef`'s
``provider``) and resolves a secret *name* to its plaintext value at apply time.
The value comes from an external store (a local keyfile value-store, an env var,
a vault); it is never taken from config, the IR, or state.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar


class SecretsProvider(ABC):
    """Resolves a secret name to its plaintext value."""

    name: ClassVar[str]

    @abstractmethod
    def resolve(self, name: str) -> str:
        """Return the plaintext for ``name``; raise ``SecretsError`` if unknown."""
