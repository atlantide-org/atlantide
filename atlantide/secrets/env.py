"""Development secrets backend: resolve values from the process environment.

A ``SecretRef("DB_PASSWORD", provider="env")`` reads ``os.environ["DB_PASSWORD"]``
at apply. There is no store file.

Deny by default: the environment holds more than the config's own secrets (cloud
credentials, tokens, anything the shell exported), and any config or third-party
component can name any ref. A name resolves only when it matches one of the
``allow`` glob patterns (``fnmatchcase``, from ``[secrets.env] allow`` in
``atlantide.toml``); with no patterns every lookup fails.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from fnmatch import fnmatchcase
from typing import ClassVar, override

from atlantide.core.check import OK, WARN, Check
from atlantide.core.errors import SecretsError
from atlantide.secrets.base import SecretsProvider

#: Where the allow-list lives; named in every refusal.
ALLOW_SETTING = "[secrets.env] allow"


class EnvSecretsProvider(SecretsProvider):
    """Resolves an allow-listed secret name to the matching environment variable."""

    name: ClassVar[str] = "env"

    def __init__(self, allow: Iterable[str] = ()) -> None:
        self.allow: tuple[str, ...] = tuple(allow)

    def allows(self, name: str) -> bool:
        """Whether ``name`` matches an allow pattern (case-sensitive glob)."""
        return any(fnmatchcase(name, pattern) for pattern in self.allow)

    @override
    def resolve(self, name: str) -> str:
        if not self.allows(name):
            # Checked before the lookup so a refusal does not reveal whether the
            # variable exists.
            raise SecretsError(
                f"env secret {name!r} is not allowed; add a pattern to "
                f"{ALLOW_SETTING} in atlantide.toml"
            )
        value = os.environ.get(name)
        if value is None:
            raise SecretsError(f"secret {name!r} not found in the environment")
        return value

    @override
    def check(self) -> Check:
        """Report the provider as usable: the environment is always available.

        Whether a given variable is set is a per-secret question, not a
        reachability one. An empty allow-list warns: the provider is reachable
        but refuses every name.
        """
        if not self.allow:
            return self._check(WARN, f"every name is refused: {ALLOW_SETTING} is empty")
        return self._check(OK, f"reads the process environment; allowed: {', '.join(self.allow)}")
