"""Discovery of provider plugins from installed distributions.

Every provider, including the built-in ones, is found through the
``atlantide.providers`` entry-point group, so built-ins and third-party plugins
share one loading path.

A loaded plugin is admitted only if its entry-point name, declared name, and the
provider of every resource type it declares all match (see
:meth:`~atlantide.core.plugin.ProviderPlugin.identity_errors`). Discovery orders,
deduplicates and reports failures by entry-point name, so that name must equal the
plugin's name.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from importlib.metadata import entry_points

from atlantide.core.plugin import (
    API_VERSION,
    ENTRY_POINT_GROUP,
    ONE_NAME_RULE,
    Discovery,
    PluginError,
    ProviderPlugin,
)

#: When true, skips discovery and loads only the built-in providers, independent
#: of installed plugins. Only ``1``, ``true`` and ``yes`` (any case) count as true;
#: any other value, including ``0`` and ``false``, leaves discovery on.
NO_PLUGINS_ENV = "ATLANTIDE_NO_PLUGINS"

_TRUTHY = frozenset({"1", "true", "yes"})


def discover(*, enabled: bool = True) -> Discovery:
    """Load every advertised plugin, collecting failures rather than raising.

    Entry points are visited in name order so runs on one machine are
    deterministic. A provider name claimed by two plugins is reported as fatal
    rather than resolved by enumeration order.
    """
    if not enabled or _plugins_disabled():
        return _builtins_only()
    advertised = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name)
    if not advertised:
        # No metadata (PyInstaller binary, zipapp, or dist-info without entry points).
        # Keyed on "nothing advertised", not "nothing loaded", so that load failures
        # are still reported.
        return _builtins_only()
    return _admit((entry.name, _describe(entry), _load(entry.name, entry)) for entry in advertised)


def _plugins_disabled() -> bool:
    """Whether :data:`NO_PLUGINS_ENV` is set to a true value."""
    return os.environ.get(NO_PLUGINS_ENV, "").strip().lower() in _TRUTHY


def _admit(
    candidates: Iterable[tuple[str, str, ProviderPlugin | PluginError]],
) -> Discovery:
    """Keep the consistent, uncontested plugins; report the rest.

    Each candidate is ``(entry-point name, origin description, load result)``. An
    identity fault or a contested name is ``fatal`` (see
    :class:`~atlantide.core.plugin.PluginError`). The first claimant of a contested
    name stays listed so ``atlantide providers`` shows both sides; commands that
    build providers refuse to run.
    """
    plugins: list[ProviderPlugin] = []
    errors: list[PluginError] = []
    claimed: dict[str, str] = {}  # provider name -> origin of the plugin that claimed it
    for name, origin, loaded in candidates:
        if isinstance(loaded, PluginError):
            errors.append(loaded)
            continue
        refused = _identity(name, loaded)
        if refused is not None:
            errors.append(refused)
            continue
        if loaded.name in claimed:
            errors.append(
                PluginError(
                    name,
                    f"provider {loaded.name!r} is claimed by both {claimed[loaded.name]} "
                    f"and {origin}; two installed plugins cannot share a name — "
                    f"uninstall one",
                    fatal=True,
                )
            )
            continue
        claimed[loaded.name] = origin
        plugins.append(loaded)
    found = Discovery(plugins=tuple(plugins), errors=tuple(errors))
    # Backstop: a type name declared by two plugins would shadow one of them. The
    # one-name rule already prevents this, since admitted types are keyed
    # `<plugin>.<Class>` and admitted plugin names are distinct.
    if conflicts := found.type_conflicts():
        found = Discovery(plugins=found.plugins, errors=found.errors + conflicts)
    return found


def _identity(name: str, plugin: ProviderPlugin) -> PluginError | None:
    """Return a fatal error when ``plugin`` breaks the one-name rule, else ``None``.

    Reading ``types`` runs plugin code (it may be any mapping), so an exception
    there is reported as a load failure.
    """
    try:
        problems = plugin.identity_errors(entry_point=name)
    except Exception as exc:
        return PluginError(
            name, f"its declared types could not be read: {type(exc).__name__}: {exc}"
        )
    if not problems:
        return None
    return PluginError(name, f"{'; '.join(problems)} ({ONE_NAME_RULE})", fatal=True)


def _describe(entry: object) -> str:
    """Describe an entry point's origin as specifically as its metadata allows.

    Colliding plugins share an entry-point name, so the target and distribution
    are included to distinguish them.
    """
    described = f"entry point {getattr(entry, 'name', '?')!r}"
    value = getattr(entry, "value", None)
    if isinstance(value, str):
        described += f" ({value})"
    dist = getattr(getattr(entry, "dist", None), "name", None)
    if isinstance(dist, str):
        described += f" from distribution {dist!r}"
    return described


def _load(name: str, entry: object) -> ProviderPlugin | PluginError:
    """Resolve one entry point, turning any failure into a reportable error.

    Catches any exception: importing a plugin runs third-party code, and its
    failure must not abort commands that do not use that provider.
    """
    try:
        loaded = entry.load()  # type: ignore[attr-defined]
    except Exception as exc:
        return PluginError(name, f"{type(exc).__name__}: {exc}")
    if not isinstance(loaded, ProviderPlugin):
        return PluginError(
            name,
            f"entry point resolved to {type(loaded).__name__}, not a ProviderPlugin",
        )
    if loaded.api_version != API_VERSION:
        return PluginError(
            name,
            f"declares plugin api {loaded.api_version}, this atlantide speaks "
            f"{API_VERSION} — upgrade one of the two",
        )
    return loaded


def _builtins_only() -> Discovery:
    """Load the built-in providers by direct import.

    Fallback for ``--no-plugins`` and for installs without readable metadata (a
    zipapp, a frozen binary). Uses the same ``PLUGIN`` objects the entry points
    name.
    """
    from atlantide.providers.aws import PLUGIN as AWS
    from atlantide.providers.local import PLUGIN as LOCAL
    from atlantide.providers.random import PLUGIN as RANDOM

    # Checked under the same one-name rule as entry points.
    return _admit((p.name, f"built-in {p.name!r}", p) for p in (AWS, LOCAL, RANDOM))
