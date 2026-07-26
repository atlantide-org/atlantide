"""atlantide.providers.local: File/Null resources and LocalProvider."""

from atlantide.core.plugin import ProviderPlugin
from atlantide.core.resource import Resource
from atlantide.providers.local.provider import LocalProvider
from atlantide.providers.local.resources import File, Null, SourceFile

#: Resource types this provider manages, keyed by ``type_name``.
TYPES: dict[str, type[Resource]] = {
    File.type_name(): File,
    Null.type_name(): Null,
    SourceFile.type_name(): SourceFile,
}

#: How atlantide discovers this provider. Declared the same way a third-party
#: package declares one — see :mod:`atlantide.core.plugin`.
PLUGIN = ProviderPlugin(
    name=LocalProvider.name,
    types=TYPES,
    factory=lambda _settings: LocalProvider(),
    module="atlantide.providers.local",
    summary="Files and no-ops on the local machine; needs no credentials.",
)

__all__ = ["PLUGIN", "TYPES", "File", "LocalProvider", "Null", "SourceFile"]
