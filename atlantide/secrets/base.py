"""``SecretsProvider``: the pluggable secret-value resolver.

Every provider declares a ``name`` (matched against a
:class:`~atlantide.core.types.SecretRef`'s ``provider``) and resolves a secret
*name* to its plaintext value at apply time. The value comes from an external
store (a local keyfile value-store, an env var, a vault); it is never taken from
config, the IR, or state.

A provider also reports whether it is usable (:meth:`SecretsProvider.check`).
Resolution happens mid-apply, so an unreachable or unreadable provider fails
partway through a changeset; ``atlantide state check`` runs this check first.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

from atlantide.core.check import SKIP, Check, Status


class SecretsProvider(ABC):
    """Resolves a secret name to its plaintext value."""

    name: ClassVar[str]

    @abstractmethod
    def resolve(self, name: str) -> str:
        """Return the plaintext for ``name``; raise ``SecretsError`` if unknown."""

    def check(self) -> Check:
        """Report whether this provider could serve a secret right now.

        Returns a single result: the only thing to verify up front is that the
        provider answers. Providers that can fail (a network store with
        credentials, a local store with an encryption key) override this; the
        default reports a skip, not a pass.
        """
        return self._check(SKIP, "no reachability check")

    def _check(self, status: Status, detail: str) -> Check:
        """One preflight row for this provider, named the same way for all of them."""
        return Check(f"secrets: {self.name}", status, detail)
