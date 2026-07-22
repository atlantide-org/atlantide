"""``SecretsProvider``: the pluggable secret-value resolver.

Every provider declares a ``name`` (matched against a :class:`SecretRef`'s
``provider``) and resolves a secret *name* to its plaintext value at apply time.
The value comes from an external store (a local keyfile value-store, an env var,
a vault); it is never taken from config, the IR, or state.

A provider also reports whether it is *usable* — see :meth:`SecretsProvider.check`.
Resolution happens mid-apply, so a provider that turns out to be unreachable or
unreadable fails halfway through a changeset; ``atlantide state check`` asks
first.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from atlantide.core.check import SKIP, Check


class SecretsProvider(ABC):
    """Resolves a secret name to its plaintext value."""

    name: ClassVar[str]

    @abstractmethod
    def resolve(self, name: str) -> str:
        """Return the plaintext for ``name``; raise ``SecretsError`` if unknown."""

    def check(self) -> Check:
        """Report whether this provider could serve a secret right now.

        One result, not a list: a provider has exactly one thing worth verifying
        up front — that it answers — whereas a state backend has a bucket, a lock
        table, and their settings.

        Providers that can fail (a network store with credentials, a local store
        with an encryption key) override this. The default says so rather than
        claiming a pass it did not earn.
        """
        return Check(f"secrets: {self.name}", SKIP, "no reachability check")
