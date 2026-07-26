"""Discovering provider plugins from installed distributions.

Every provider — including the three that ship here — is found through the
``atlantide.providers`` entry-point group. Nothing is hardcoded, so the path a
third party takes is the path that is exercised on every run.
"""

from __future__ import annotations

import os
from importlib.metadata import entry_points
from typing import Any

from atlantide.core.plugin import (
    API_VERSION,
    ENTRY_POINT_GROUP,
    Discovery,
    PluginError,
    ProviderPlugin,
)

#: Set to skip discovery entirely and load only what ships here: reproduces a
#: build independently of what is installed, and bisects a plugin that has
#: broken a run.
NO_PLUGINS_ENV = "ATLANTIDE_NO_PLUGINS"


def discover(*, enabled: bool = True) -> Discovery:
    """Load every advertised plugin, collecting failures rather than raising.

    Entry points are visited in name order so two runs on one machine see the
    same providers in the same order — and so a duplicate name resolves the same
    way twice, rather than depending on however the metadata happened to be
    enumerated.
    """
    if not enabled or os.environ.get(NO_PLUGINS_ENV):
        return _builtins_only()
    advertised = sorted(entry_points(group=ENTRY_POINT_GROUP), key=lambda e: e.name)
    if not advertised:
        # Nothing advertised at all: a PyInstaller binary, a zipapp, a source tree
        # whose dist-info predates the entry points. The built-ins are compiled in
        # regardless, and a tool with no providers is useless.
        #
        # Keyed on "nothing was advertised", not "nothing loaded": entry points
        # that all failed is a different situation, and substituting the built-ins
        # there would hide the rejection that explains the coming failure.
        return _builtins_only()
    return _from_entry_points(advertised)


def _from_entry_points(advertised: list[Any]) -> Discovery:
    plugins: list[ProviderPlugin] = []
    errors: list[PluginError] = []
    claimed: dict[str, str] = {}  # provider name -> the entry point that took it
    for entry in advertised:
        loaded = _load(entry.name, entry)
        if isinstance(loaded, PluginError):
            errors.append(loaded)
            continue
        if loaded.name in claimed:
            errors.append(
                PluginError(
                    entry.name,
                    f"provider {loaded.name!r} is already provided by "
                    f"{claimed[loaded.name]!r}; this one is ignored",
                )
            )
            continue
        claimed[loaded.name] = entry.name
        plugins.append(loaded)
    found = Discovery(plugins=tuple(plugins), errors=tuple(errors))
    # A duplicated *type name* across distinct plugins would silently shadow —
    # a foreign plugin redeclaring `aws.S3Bucket` captures every resource of
    # that type. `types()` keeps the first declaration; the conflict is a
    # reported error like every other plugin fault.
    if conflicts := found.type_conflicts():
        found = Discovery(plugins=found.plugins, errors=found.errors + conflicts)
    return found


def _load(name: str, entry: object) -> ProviderPlugin | PluginError:
    """Resolve one entry point, turning any failure into a reportable error.

    Broad by design: a plugin's import runs arbitrary third-party code, which can
    fail in arbitrary ways, and none of them should take down a command that has
    nothing to do with that provider.
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
    """The shipped providers, imported directly.

    The fallback for ``--no-plugins`` and for an install whose metadata is not
    readable (a zipapp, a frozen binary). Deliberately built from the same
    ``PLUGIN`` objects the entry points name, so the two paths cannot disagree
    about what a built-in provider is.
    """
    from atlantide.providers.aws import PLUGIN as AWS
    from atlantide.providers.local import PLUGIN as LOCAL
    from atlantide.providers.random import PLUGIN as RANDOM

    return Discovery(plugins=(AWS, LOCAL, RANDOM))
