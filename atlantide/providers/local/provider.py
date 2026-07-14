"""Local provider: disk CRUD for File, disk reads for SourceFile, no-ops for Null."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, ClassVar

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.core.provider import provider_guard
from atlantide.providers.local.resources import File, Null, SourceFile


class LocalProvider(Provider):
    name: ClassVar[str] = "local"
    version: ClassVar[str] = "1.0.0"

    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            with provider_guard("local", "create", res):
                return _read_content(res.path)
        file = _as_file(res, "create")
        with provider_guard("local", "create", res):
            path = Path(file.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(file.content)
        return _outputs(file.path, file.content)

    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            with provider_guard("local", "read", res):
                if not Path(res.path).exists():
                    return None
                return _read_content(res.path)
        file = _as_file(res, "read")
        with provider_guard("local", "read", res):
            path = Path(file.path)
            if not path.exists():
                return None
            return _outputs(file.path, path.read_text())

    async def update(
        self, ctx: Context, prior: dict[str, Any], res: Resource
    ) -> dict[str, Any]:
        if isinstance(res, Null):
            return {}
        if isinstance(res, SourceFile):
            # The checksum input changed -> re-read the file's current content.
            with provider_guard("local", "update", res):
                return _read_content(res.path)
        file = _as_file(res, "update")
        with provider_guard("local", "update", res):
            Path(file.path).write_text(file.content)
        return _outputs(file.path, file.content)

    async def delete(self, ctx: Context, res: Resource) -> None:
        if isinstance(res, Null | SourceFile):
            return  # SourceFile never owns the on-disk file, so nothing to delete.
        file = _as_file(res, "delete")
        with provider_guard("local", "delete", res):
            Path(file.path).unlink(missing_ok=True)


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
