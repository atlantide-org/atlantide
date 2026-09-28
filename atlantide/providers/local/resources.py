"""Local resources: a File on disk, a no-op Null, and a read-only SourceFile."""

from __future__ import annotations

import hashlib
from typing import Any, ClassVar

from atlantide.core import Resource, computed, immutable, mutable
from atlantide.core.errors import LanguageError
from atlantide.core.markers import contains_ref
from atlantide.providers.local.paths import PathEscapeError, PathScope


class LocalResource(Resource):
    """Base for local resources; carries the ``local`` provider tag."""

    class Meta:
        provider: ClassVar[str] = "local"


class File(LocalResource):
    """A file on the local filesystem.

    Changing ``path`` replaces the resource; ``content`` updates in place. A
    relative ``path`` resolves against the project root (the working directory
    without an ``atlantide.toml``), and the resolved path must stay inside it (see
    :mod:`atlantide.providers.local.paths`).
    """

    path: str = immutable()
    content: str = mutable(default="")
    checksum: str = computed()


class Null(LocalResource):
    """A resource with no side effects, for graph/testing scaffolds."""

    triggers: dict[str, str] = mutable(default_factory=dict)


class SourceFile(LocalResource):
    """A file read from disk, re-checked on every plan (à la Terraform ``data.local_file``).

    ``checksum`` is the file's sha256, computed at config evaluation and tracked as
    an input, so a changed file plans as an UPDATE that re-reads ``content``.
    ``content`` is a provider-computed output read from disk at apply. The file is
    never written or deleted. ``path`` must be a literal because it is read before
    apply; it resolves and is confined as for :class:`File`.
    """

    path: str = immutable()
    checksum: str = mutable(default="")
    content: str = computed(sensitive=True)

    def __init__(
        self, name: str, /, *, path: str, checksum: str | None = None, **data: Any
    ) -> None:
        # A fresh config read fingerprints the file now so its content enters the
        # Merkle inputs. A rehydrate on deploy passes the artifact's pinned checksum
        # and must not touch disk.
        if checksum is None:
            if not isinstance(path, str) or contains_ref(path):
                raise LanguageError("SourceFile.path must be a literal filesystem path")
            try:
                source = PathScope.discover().resolve(path)
            except PathEscapeError as exc:
                raise LanguageError(f"SourceFile: {exc}") from exc
            # UTF-8 with no newline translation: the text the provider reads at apply.
            with source.open(encoding="utf-8", newline="") as handle:
                checksum = hashlib.sha256(handle.read().encode("utf-8")).hexdigest()
        data["path"] = path
        data["checksum"] = checksum
        # Explicit base call: without the pydantic plugin, mypy resolves a bare
        # super() to BaseModel.__init__ and loses the positional ``name``.
        Resource.__init__(self, name, **data)
