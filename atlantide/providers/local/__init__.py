"""atlantide.providers.local: File/Null resources and LocalProvider."""

from collections.abc import Mapping
from typing import Any

from atlantide.core.plugin import ProviderPlugin
from atlantide.core.resource import Resource
from atlantide.providers.local.paths import PathScope
from atlantide.providers.local.provider import LocalProvider
from atlantide.providers.local.resources import File, Null, SourceFile

#: Resource types this provider manages, keyed by ``type_name``.
TYPES: dict[str, type[Resource]] = {
    File.type_name(): File,
    Null.type_name(): Null,
    SourceFile.type_name(): SourceFile,
}


def _build(settings: Mapping[str, Any]) -> LocalProvider:
    """Construct the local provider from its settings table.

    ``root`` is the project root, set by the CLI and absent without a project
    file, in which case the working directory is the root.
    ``allow_outside_project`` comes from ``[provider.local]`` in
    ``atlantide.toml``. See :mod:`atlantide.providers.local.paths`.
    """
    scope = PathScope.from_settings(settings)
    root = None if scope.implicit_root else scope.root
    return LocalProvider(root, allow_outside_project=scope.allow_outside_project)


#: Plugin descriptor advertised through the entry-point group; see
#: :mod:`atlantide.core.plugin`.
PLUGIN = ProviderPlugin(
    name=LocalProvider.name,
    types=TYPES,
    factory=_build,
    module="atlantide.providers.local",
    summary="Files and no-ops on the local machine; needs no credentials.",
)

__all__ = ["PLUGIN", "TYPES", "File", "LocalProvider", "Null", "SourceFile"]
