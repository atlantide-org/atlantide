"""atlantide.providers.local: File/Null resources and LocalProvider."""

from atlantide.core.resource import Resource
from atlantide.providers.local.provider import LocalProvider
from atlantide.providers.local.resources import File, Null, SourceFile

#: Resource types this provider manages, keyed by ``type_name``.
TYPES: dict[str, type[Resource]] = {
    File.type_name(): File,
    Null.type_name(): Null,
    SourceFile.type_name(): SourceFile,
}

__all__ = ["TYPES", "File", "LocalProvider", "Null", "SourceFile"]
