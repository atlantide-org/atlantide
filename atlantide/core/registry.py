"""Provider registry: name -> configured Provider instance, semver-checked.

Fallible operations return ``Result[T, RegistryError]`` rather than raising, so
callers compose lookups/checks with ``.bind``/``.map``.
"""

from __future__ import annotations

import re

from returns.result import Failure, Result, Success

from atlantide.core.errors import RegistryError
from atlantide.core.provider import Provider

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")

Semver = tuple[int, int, int]


def parse_semver(version: str) -> Result[Semver, RegistryError]:
    match = _SEMVER_RE.match(version)
    if match is None:
        return Failure(RegistryError(f"invalid semver {version!r}: expected MAJOR.MINOR.PATCH"))
    major, minor, patch = match.groups()
    return Success((int(major), int(minor), int(patch)))


def check_compatible(pinned: str, actual: str) -> Result[None, RegistryError]:
    """Success unless ``actual`` cannot satisfy a plan pinned at ``pinned``.

    Semver compatibility is directional: same major and not older. A plan built
    against ``1.5.0`` encodes resources and fields ``1.0.0`` may not have, so a
    downgrade is the mismatch the pin exists to catch. ``0.x`` carries no
    compatibility guarantee and must match exactly.
    """
    parsed = parse_semver(pinned).bind(
        lambda pinned_v: parse_semver(actual).map(lambda actual_v: (pinned_v, actual_v))
    )

    def _requirement(pinned_v: Semver, actual_v: Semver) -> str | None:
        """What ``actual_v`` fails to satisfy, or ``None`` when it is compatible."""
        if 0 in (pinned_v[0], actual_v[0]):
            if pinned_v != actual_v:
                return "0.x releases are not compatible across versions"
        elif pinned_v[0] != actual_v[0] or actual_v < pinned_v:
            return f"needs {pinned_v[0]}.x at or above {pinned}"
        return None

    def _compatible(versions: tuple[Semver, Semver]) -> Result[None, RegistryError]:
        why = _requirement(*versions)
        if why is None:
            return Success(None)
        return Failure(
            RegistryError(
                f"provider version incompatible: plan pinned {pinned}, registered {actual} ({why})"
            )
        )

    return parsed.bind(_compatible)


class ProviderRegistry:
    """Maps provider name -> configured instance."""

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> Result[None, RegistryError]:
        name = getattr(provider, "name", "")
        if not name:
            return Failure(RegistryError(f"provider {type(provider).__name__} declares no name"))
        # Validate the semver, then insert; propagate a bad version.
        return parse_semver(getattr(provider, "version", "")).bind(
            lambda _: self._register_named(name, provider)
        )

    def _register_named(self, name: str, provider: Provider) -> Result[None, RegistryError]:
        if name in self._providers:
            return Failure(RegistryError(f"duplicate provider {name!r}"))
        self._providers[name] = provider
        return Success(None)

    def get(self, name: str) -> Result[Provider, RegistryError]:
        provider = self._providers.get(name)
        if provider is None:
            return Failure(RegistryError(f"unknown provider {name!r}"))
        return Success(provider)

    def check_compatible(self, name: str, pinned_version: str) -> Result[Provider, RegistryError]:
        """Resolve ``name`` and verify the registered build satisfies the pin."""
        return self.get(name).bind(
            lambda provider: check_compatible(pinned_version, provider.version).map(
                lambda _: provider
            )
        )

    def __contains__(self, name: str) -> bool:
        return name in self._providers
