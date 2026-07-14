"""Provider ABC: async CRUD over one family of resources.

Each provider declares a registry ``name`` and semver ``version``. The version
is stamped into each IR node at lowering, pinned in artifacts, and compat-checked
on apply/deploy (major mismatch = hard error).
"""

from __future__ import annotations

import contextlib
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any, ClassVar

from atlantide.core.context import Context
from atlantide.core.errors import AtlantideError, ProviderError
from atlantide.core.resource import Resource


@contextlib.contextmanager
def provider_guard(provider: str, op: str, res: Resource) -> Iterator[None]:
    """Wrap a provider operation, turning any raw error into a
    :class:`ProviderError` tagged with the failing op and resource type.

    Already-typed atlantide errors pass through unchanged; the original
    exception is preserved as ``__cause__`` for a full traceback."""
    try:
        yield
    except AtlantideError:
        raise
    except Exception as exc:
        raise ProviderError(
            f"{provider} {op} of {res.type_name()!r} failed: {exc}",
            op=op,
            resource_type=res.type_name(),
        ) from exc


class Provider(ABC):
    """Async CRUD interface implemented by every provider."""

    name: ClassVar[str]
    version: ClassVar[str]

    @abstractmethod
    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        """Create the resource; return its computed outputs (e.g. ids/arns)."""

    @abstractmethod
    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        """Read live outputs, or None if the resource does not exist."""

    @abstractmethod
    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        """Update in place from ``prior`` outputs to desired inputs; return new outputs."""

    @abstractmethod
    async def delete(self, ctx: Context, res: Resource) -> None:
        """Destroy the resource."""
