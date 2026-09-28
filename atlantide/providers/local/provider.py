"""Local provider: disk CRUD for File, disk reads for SourceFile, no-ops for Null."""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, override

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.core.provider import provider_guard
from atlantide.providers.local.paths import PathEscapeError, PathScope
from atlantide.providers.local.resources import File, Null, SourceFile


class LocalProvider(Provider):
    name: ClassVar[str] = "local"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, root: Path | None = None, *, allow_outside_project: bool = False) -> None:
        """Scope local paths to the project root ``root``.

        Relative paths resolve against ``root``, and resolved paths must stay inside
        it unless ``allow_outside_project``. ``None`` makes the working directory at
        construction the root; see :mod:`atlantide.providers.local.paths`.
        """
        self.scope = PathScope(root=root, allow_outside_project=allow_outside_project)

    def _path(self, op: str, res: Resource, path: str) -> Path:
        """``path`` resolved against the project root, or a typed error if it escapes."""
        try:
            return self.scope.resolve(path)
        except PathEscapeError as exc:
            raise ProviderError(str(exc), op=op, resource_type=res.type_name()) from exc

    def _run[R](self, op: str, res: Resource, work: Callable[[], R]) -> R:
        """Run one operation's disk work under :func:`provider_guard` to translate faults."""
        with provider_guard("local", op, res):
            return work()

    @override
    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            source = self._path("create", res, res.path)
            return self._run("create", res, lambda: _read_content(source))
        file = _as_file(res, "create")
        path = self._path("create", res, file.path)
        return self._run("create", res, lambda: _write(path, file))

    @override
    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            source = self._path("read", res, res.path)
            return self._run(
                "read",
                res,
                lambda: _read_content(source) if source.exists() else None,
            )
        file = _as_file(res, "read")
        path = self._path("read", res, file.path)

        def load() -> dict[str, Any] | None:
            return _outputs(file.path, _read_text(path)) if path.exists() else None

        return self._run("read", res, load)

    @override
    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            # An update means the checksum input changed: re-read the content.
            source = self._path("update", res, res.path)
            return self._run("update", res, lambda: _read_content(source))
        file = _as_file(res, "update")
        path = self._path("update", res, file.path)
        return self._run("update", res, lambda: _write(path, file))

    @override
    async def delete(self, ctx: Context, res: Resource) -> None:
        if isinstance(res, Null | SourceFile):
            return  # SourceFile does not own the on-disk file.
        file = _as_file(res, "delete")
        path = self._path("delete", res, file.path)
        self._run("delete", res, lambda: path.unlink(missing_ok=True))


def _outputs(path: str, content: str) -> dict[str, Any]:
    """File CRUD output: path and content checksum."""
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"checksum": checksum, "path": path}


def _read_content(path: Path) -> dict[str, Any]:
    """SourceFile disk read: the file's current content (checksum is a tracked input)."""
    return {"content": _read_text(path)}


def _read_text(path: Path) -> str:
    """The file as UTF-8, the encoding the checksum hashes.

    ``newline=""`` disables newline translation, so a CRLF on disk reads back as
    CRLF and changes the checksum instead of being hidden as ``\\n``.
    """
    with path.open(encoding="utf-8", newline="") as handle:
        return handle.read()


def _write(path: Path, file: File) -> dict[str, Any]:
    """Write ``file.content`` to ``path`` atomically; create and update share it.

    The content goes to a temporary file in the same directory, then
    :func:`os.replace` swaps it in, so a crash never leaves a truncated file. It
    is written as UTF-8 with no newline translation, the bytes the checksum hashes.
    A replaced file keeps its permissions; a new one gets the umask's.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = None
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    # O_EXCL: never write through a file (or symlink) that is already there.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(file.content)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return _outputs(file.path, file.content)


def _as_file(res: Resource, op: str) -> File:
    if not isinstance(res, File):
        raise ProviderError(f"local provider cannot {op} {res.type_name()!r}")
    return res
