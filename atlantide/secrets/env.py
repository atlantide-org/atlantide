"""Dev secrets backend: resolve values straight from the environment.

A ``SecretRef("DB_PASSWORD", provider="env")`` reads ``os.environ["DB_PASSWORD"]``
at apply. No store file; proves the resolver is swappable behind the same ABC.
"""

from __future__ import annotations

import os
from typing import ClassVar

from typing_extensions import override

from atlantide.core.check import OK, Check
from atlantide.core.errors import SecretsError
from atlantide.secrets.backend import SecretsProvider


class EnvSecretsProvider(SecretsProvider):
    """Resolves a secret name to the value of the matching environment variable."""

    name: ClassVar[str] = "env"

    @override
    def resolve(self, name: str) -> str:
        value = os.environ.get(name)
        if value is None:
            raise SecretsError(f"secret {name!r} not found in the environment")
        return value

    @override
    def check(self) -> Check:
        """Always usable: the environment is already here, whatever is in it.

        Whether a *particular* variable is set is a per-secret question the plan
        answers, not a reachability one.
        """
        return self._check(OK, "reads the process environment")
