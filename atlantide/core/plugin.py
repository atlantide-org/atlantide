"""The contract a third-party provider package implements.

A provider is an ordinary Python distribution that advertises one entry point in
the ``atlantide.providers`` group:

    [project.entry-points."atlantide.providers"]
    acme = "acme_atlantide:PLUGIN"

pointing at a :class:`ProviderPlugin`. The built-in providers declare themselves
the same way, so built-in and third-party providers share one loading path.

**One name.** The entry-point name, :attr:`ProviderPlugin.name`, the provider
every declared resource type names in ``Meta.provider``, and the ``name`` of the
provider the factory builds must all be the same string. Resources are routed to
a provider by that name and state rows record it, so a mismatch lets one plugin
handle another provider's resources. Discovery refuses a plugin whose
declarations disagree (:meth:`ProviderPlugin.identity_errors`), and the CLI
refuses a factory that builds a provider under another name
(:meth:`ProviderPlugin.provider_error`). Either refusal aborts every command that
builds providers.

**Trust.** A plugin is ordinary Python running in this process, with the same
access as atlantide itself. The Atlas-lang sandbox constrains *config*, not
*plugins*: installing one carries the same trust as installing any dependency.
The one-name rule stops a misdeclared or same-named plugin from handling another
provider's resources; it does not constrain code that is already running.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from atlantide.core.provider import Provider
from atlantide.core.resource import Resource

#: The plugin interface this build understands. A plugin declaring a different
#: version is refused at load rather than failing later with an attribute error.
API_VERSION = 1

ENTRY_POINT_GROUP = "atlantide.providers"

#: Appended to every identity refusal (see :meth:`ProviderPlugin.identity_errors`)
#: so the message states the fix.
ONE_NAME_RULE = "a plugin's entry point, name, resource types and provider must all share one name"


@dataclass(frozen=True, slots=True)
class ProviderPlugin:
    """One provider package, as atlantide needs to see it.

    ``factory`` takes the provider's own settings table from ``atlantide.toml``
    (``[provider.<name>]``) and returns the :class:`Provider`. A raw mapping lets
    a third party accept settings this codebase does not define.

    ``module`` is the import path config is allowed to name. Resource types live
    there; the provider implementation does not, since it makes network and
    filesystem calls config must not reach (``atlantide.providers.aws.provider``
    is off-limits to config for the same reason).
    """

    name: str
    types: Mapping[str, type[Resource]]
    factory: Callable[[Mapping[str, Any]], Provider]
    module: str
    api_version: int = API_VERSION
    #: Free-text description shown by ``atlantide providers``.
    summary: str = ""

    def identity_errors(self, entry_point: str | None = None) -> tuple[str, ...]:
        """Every way this plugin's declarations disagree with its own name.

        Static: checked at discovery, before any factory runs. ``entry_point`` is
        the name the plugin was advertised under (the key in the distribution's
        ``[project.entry-points."atlantide.providers"]`` table), when there is
        one. Each declared type must be a :class:`Resource` whose
        ``Meta.provider`` is :attr:`name`, keyed by its own ``type_name()``: state
        rows resolve through the key, so a key naming another provider's type would
        capture that type's rows. Empty when the plugin is consistent.
        """
        problems: list[str] = []
        if not isinstance(self.name, str) or not self.name:
            problems.append(f"plugin name {self.name!r} is not a non-empty string")
        if entry_point is not None and entry_point != self.name:
            problems.append(f"entry point {entry_point!r} loads a plugin named {self.name!r}")
        for key, cls in self.types.items():
            problem = _type_error(self.name, key, cls)
            if problem is not None:
                problems.append(problem)
        return tuple(problems)

    def provider_error(self, provider: object) -> str | None:
        """Why ``provider``, as built by :attr:`factory`, is not this plugin's.

        Dynamic: only the built object can answer it, so it is checked after the
        factory runs and before the provider is registered. ``None`` when the
        provider's ``name`` is this plugin's name.
        """
        built = getattr(provider, "name", None)
        if built == self.name:
            return None
        if not built:
            return (
                f"its factory built a {type(provider).__name__} with no provider name, "
                f"not provider {self.name!r} ({ONE_NAME_RULE})"
            )
        return f"its factory built provider {built!r}, not {self.name!r} ({ONE_NAME_RULE})"


def _type_error(plugin: str, key: object, cls: object) -> str | None:
    """Why ``key -> cls`` is not a type ``plugin`` may declare, or ``None``."""
    if not isinstance(cls, type):
        return f"type {key!r} is a {type(cls).__name__} instance, not a Resource subclass"
    where = f"{cls.__module__}.{cls.__qualname__}"
    if not issubclass(cls, Resource):
        return f"type {key!r} ({where}) is not a Resource subclass"
    owner = cls.provider_name()
    if owner != plugin:
        return f"type {key!r} ({where}) belongs to provider {owner!r}, not {plugin!r}"
    if key != cls.type_name():
        return f"type key {key!r} names {where}, whose type is {cls.type_name()!r}"
    return None


@dataclass(frozen=True, slots=True)
class PluginError:
    """Why one entry point could not be loaded.

    Collected rather than raised, so a broken plugin does not stop
    ``atlantide --version`` or ``atlantide state unlock``, the commands used to
    fix it.

    ``fatal`` marks a plugin refused over its *identity*: its declarations
    disagree about whose provider it is, or another installed plugin claims the
    same name. A plugin that fails to load contributes nothing, and a run that
    does not need it proceeds with a warning; an identity conflict leaves it
    unclear which code handles that provider's resources. Commands that build
    providers abort on it, as they do on a registration failure;
    ``atlantide providers`` and the ``state`` commands still run so the fault
    stays diagnosable.
    """

    name: str
    detail: str
    fatal: bool = False


@dataclass(frozen=True, slots=True)
class Discovery:
    """What one scan of the entry points found."""

    plugins: tuple[ProviderPlugin, ...] = ()
    errors: tuple[PluginError, ...] = field(default_factory=tuple)

    def types(self) -> dict[str, type[Resource]]:
        """Every resource type across every loaded plugin, by type name.

        A duplicate type name across plugins keeps the first declaration and is
        reported by :meth:`type_conflicts`, so a third-party plugin cannot
        redeclare ``aws.S3Bucket`` and capture every resource of that type unseen.
        """
        return self._merge_types()[0]

    def type_conflicts(self) -> tuple[PluginError, ...]:
        """One error per type name declared by more than one plugin."""
        return self._merge_types()[1]

    def _merge_types(self) -> tuple[dict[str, type[Resource]], tuple[PluginError, ...]]:
        """The merged type map and the duplicate-type errors, from one walk."""
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
