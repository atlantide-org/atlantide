"""Local provider: disk CRUD for File, disk reads for SourceFile, no-ops for Null."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar, TypeVar

from typing_extensions import override

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.core.provider import provider_guard
from atlantide.providers.local.resources import File, Null, SourceFile

_R = TypeVar("_R")


class LocalProvider(Provider):
    name: ClassVar[str] = "local"
    version: ClassVar[str] = "1.0.0"

    def _run(self, op: str, res: Resource, work: Callable[[], _R]) -> _R:
        """Run one op's disk work under the provider guard.

        Every operation is the same three steps — skip the no-op types, narrow
        to the concrete resource, then do the work with faults translated — so
        only the work itself is written per operation.
        """
        with provider_guard("local", op, res):
            return work()

    @override
    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            return self._run("create", res, lambda: _read_content(res.path))
        file = _as_file(res, "create")

        def write() -> dict[str, Any]:
            path = Path(file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.content)
            return _outputs(file.path, file.content)

        return self._run("create", res, write)

    @override
    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            return self._run(
                "read",
                res,
                lambda: _read_content(res.path) if Path(res.path).exists() else None,
            )
        file = _as_file(res, "read")

        def load() -> dict[str, Any] | None:
            path = Path(file.path)
            return _outputs(file.path, path.read_text()) if path.exists() else None

        return self._run("read", res, load)

    @override
    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            # The checksum input changed -> re-read the file's current content.
            return self._run("update", res, lambda: _read_content(res.path))
        file = _as_file(res, "update")

        def write() -> dict[str, Any]:
            Path(file.path).write_text(file.content)
            return _outputs(file.path, file.content)

        return self._run("update", res, write)

    @override
    async def delete(self, ctx: Context, res: Resource) -> None:
        if isinstance(res, Null | SourceFile):
            return  # SourceFile never owns the on-disk file, so nothing to delete.
        file = _as_file(res, "delete")
        self._run("delete", res, lambda: Path(file.path).unlink(missing_ok=True))


def _outputs(path: str, content: str) -> dict[str, Any]:
    """File CRUD output: path and content checksum."""
    checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
    return {"checksum": checksum, "path": path}


def _read_content(path: str) -> dict[str, Any]:
    """SourceFile disk read: the file's current content (checksum is a tracked input)."""
    return {"content": Path(path).read_text()}


def _as_file(res: Resource, op: str) -> File:
    if not isinstance(res, File):
        raise ProviderError(f"local provider cannot {op} {res.type_name()!r}")
    return res
