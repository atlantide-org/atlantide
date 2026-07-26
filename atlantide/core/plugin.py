"""The contract a third-party provider package implements.

A provider is an ordinary Python distribution that advertises one entry point in
the ``atlantide.providers`` group:

    [project.entry-points."atlantide.providers"]
    acme = "acme_atlantide:PLUGIN"

pointing at a :class:`ProviderPlugin`. The built-in providers declare themselves
the same way, deliberately: a door only the shipped providers can walk through is
a door nobody can test, and it would drift the moment the two paths diverged.

**Trust.** A plugin is ordinary Python running in this process, with the same
access as atlantide itself. The Atlas-lang sandbox constrains a malicious
*config*; it does nothing about a malicious *plugin*. Installing one is the same
decision as installing any dependency, and should be made the same way.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from atlantide.core.provider import Provider
from atlantide.core.resource import Resource

#: The plugin interface this build understands. A plugin declaring a different one
#: is refused rather than half-loaded; a mismatch would otherwise surface as an
#: attribute error deep in a run.
API_VERSION = 1

#: The entry-point group plugins advertise themselves in.
ENTRY_POINT_GROUP = "atlantide.providers"


@dataclass(frozen=True, slots=True)
class ProviderPlugin:
    """One provider package, as atlantide needs to see it.

    ``factory`` takes the provider's own settings table from ``atlantide.toml``
    (``[provider.<name>]``) and returns the :class:`Provider`. It takes a raw
    mapping rather than typed arguments so a third party can accept settings this
    codebase knows nothing about — the alternative is every new provider needing
    a change here, which is the thing being fixed.

    ``module`` is the import path config is allowed to name. Resource types live
    there; the provider implementation should not, for the same reason
    ``atlantide.providers.aws.provider`` is off-limits to config — it holds the
    network and filesystem calls.
    """

    name: str
    types: Mapping[str, type[Resource]]
    factory: Callable[[Mapping[str, Any]], Provider]
    module: str
    api_version: int = API_VERSION
    #: Shown by ``atlantide providers``; free text, for humans.
    summary: str = ""


@dataclass(frozen=True, slots=True)
class PluginError:
    """Why one entry point could not be loaded.

    Collected rather than raised. A broken third-party plugin must not stop
    ``atlantide --version`` or ``atlantide state unlock`` from working — those
    are exactly the commands someone runs while trying to fix it.
    """

    name: str
    detail: str


@dataclass(frozen=True, slots=True)
class Discovery:
    """What one scan of the entry points found."""

    plugins: tuple[ProviderPlugin, ...] = ()
    errors: tuple[PluginError, ...] = field(default_factory=tuple)

    def types(self) -> dict[str, type[Resource]]:
        """Every resource type across every loaded plugin, by type name.

        A duplicate type name across plugins keeps the first declaration; the
        shadowing is recorded in :attr:`errors` rather than resolved silently —
        last-wins would let a third-party plugin redeclare ``aws.S3Bucket`` and
        capture every resource of that type with no diagnostic anywhere.
        """
        return self._merge_types()[0]

    def type_conflicts(self) -> tuple[PluginError, ...]:
        """One error per type name declared by more than one plugin."""
        return self._merge_types()[1]

    def _merge_types(self) -> tuple[dict[str, type[Resource]], tuple[PluginError, ...]]:
        """One walk answering both questions: the merged map and the shadowings."""
        merged: dict[str, type[Resource]] = {}
        owners: dict[str, str] = {}
        conflicts: list[PluginError] = []
        for plugin in self.plugins:
            for type_name, cls in plugin.types.items():
                if type_name in owners:
                    conflicts.append(
                        PluginError(
                            name=plugin.name,
                            detail=(
                                f"type {type_name!r} is already provided by "
                                f"{owners[type_name]!r}; the duplicate is ignored"
                            ),
                        )
                    )
                else:
                    owners[type_name] = plugin.name
                    merged[type_name] = cls
        return merged, tuple(conflicts)

    def modules(self) -> tuple[str, ...]:
        """Import paths config may name, for the language's allow-list."""
        return tuple(sorted({plugin.module for plugin in self.plugins}))
