"""Random provider: generates a value at apply, echoes the pinned value on read.

There is no external store — the value lives in state. ``create`` generates it;
``read`` echoes the value restored onto the resource (so refresh reports IN_SYNC);
``update`` is unreachable (all inputs are immutable, so a change is a REPLACE) and
keeps the prior value.
"""

from __future__ import annotations

import string
import uuid
from datetime import UTC, datetime
from secrets import choice, token_hex
from typing import Any, ClassVar

from atlantide.core import Context, Provider, Resource
from atlantide.core.errors import ProviderError
from atlantide.providers.random.resources import Id, Password, Timestamp, Uuid

_PASSWORD_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*()-_=+"


class RandomProvider(Provider):
    name: ClassVar[str] = "random"
    version: ClassVar[str] = "1.0.0"

    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        return {"result": _generate(res)}

    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        # No external system; echo the pinned value (restored onto res from state).
        result = getattr(res, "result", None)
        return {"result": result} if isinstance(result, str) else None

    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        # All inputs are immutable, so a change is a REPLACE — update just keeps the value.
        return dict(prior)

    async def delete(self, ctx: Context, res: Resource) -> None:
        return None


def _generate(res: Resource) -> str:
    if isinstance(res, Uuid):
        return str(uuid.uuid4())
    if isinstance(res, Password):
        return "".join(choice(_PASSWORD_ALPHABET) for _ in range(res.length))
    if isinstance(res, Id):
        return token_hex(res.byte_length)
    if isinstance(res, Timestamp):
        return datetime.now(UTC).isoformat()
    raise ProviderError(f"random provider cannot generate {res.type_name()!r}")
