"""Local resources: a File on disk, a no-op Null, and a read-only SourceFile."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar

from atlantide.core import Resource, computed, immutable, mutable
from atlantide.core.errors import LanguageError
from atlantide.core.markers import contains_ref


class LocalResource(Resource):
    """Base for local resources; carries the ``local`` provider tag."""

    class Meta:
        provider: ClassVar[str] = "local"


class File(LocalResource):
    """A file on the local filesystem.

    ``path`` is immutable (a rename replaces the resource); ``content`` updates
    in place. ``checksum`` is a provider-computed output.
    """

    path: str = immutable()
    content: str = mutable(default="")
    checksum: str = computed()


class Null(LocalResource):
    """A resource with no side effects, for graph/testing scaffolds."""

    triggers: dict[str, str] = mutable(default_factory=dict)


class SourceFile(LocalResource):
    """A file read from disk, re-checked on every plan (à la Terraform ``data.local_file``).

    ``checksum`` is the file's sha256, read at config-evaluation time and tracked
    as an input: when the file changes the checksum changes, so plan/apply see an
    UPDATE and re-read ``content``. ``content`` is a provider-computed output,
    (re-)read from disk at apply. Unlike :class:`File`, this never writes or
    deletes the file. ``path`` must be a literal (the read precedes apply).
    """

    path: str = immutable()
    checksum: str = mutable(default="")   # sha256 of the file; the fingerprint that drives re-read
    content: str = computed()             # file bytes, read by the provider at apply

    def __init__(
        self, name: str, /, *, path: str, checksum: str | None = None, **data: Any
    ) -> None:
        # A fresh config read (no pinned checksum) fingerprints the file now, so
        # its bytes enter the Merkle inputs and every plan re-checks it. A rehydrate
        # (deploy) passes the artifact's pinned checksum and must not touch disk.
        if checksum is None:
            if not isinstance(path, str) or contains_ref(path):
                raise LanguageError("SourceFile.path must be a literal filesystem path")
            checksum = hashlib.sha256(Path(path).read_text().encode("utf-8")).hexdigest()
        data["path"] = path
        data["checksum"] = checksum
        # Call the base initializer explicitly: mypy (no pydantic plugin) resolves
        # a bare super() to BaseModel.__init__ and loses the positional ``name``.
        Resource.__init__(self, name, **data)
