"""A third-party provider package, as one would actually be written.

Nothing here imports from atlantide's internals beyond the public contract, and
nothing is registered by hand — it is discovered the same way the shipped
providers are. That is the point: if this works, the door is open.
"""

from __future__ import annotations

from typing import Any, ClassVar

from atlantide.core import Context, Provider, Resource, computed, immutable, mutable
from atlantide.core.plugin import ProviderPlugin


class Gadget(Resource):
    """A resource type contributed by a third-party provider."""

    class Meta:
        provider = "acme"

    gadget_name: str = immutable(physical_name=True)
    size: int = mutable(default=1)
    serial: str = computed()


class AcmeProvider(Provider):
    name: ClassVar[str] = "acme"
    version: ClassVar[str] = "1.0.0"

    def __init__(self, marker: str = "default") -> None:
        #: Proves the settings table reached the factory.
        self.marker = marker
        self.created: list[str] = []

    async def create(self, ctx: Context, res: Resource) -> dict[str, Any]:
        self.created.append(res.node_id)
        return {"serial": f"{self.marker}-{res.node_id}"}

    async def read(self, ctx: Context, res: Resource) -> dict[str, Any] | None:
        return {"serial": f"{self.marker}-{res.node_id}"}

    async def update(self, ctx: Context, prior: dict[str, Any], res: Resource) -> dict[str, Any]:
        return {"serial": f"{self.marker}-{res.node_id}"}

    async def delete(self, ctx: Context, res: Resource) -> None:
        return None


PLUGIN = ProviderPlugin(
    name="acme",
    types={Gadget.type_name(): Gadget},
    factory=lambda settings: AcmeProvider(marker=str(settings.get("marker", "default"))),
    module="tests.support.fakeplugin",
    summary="A test provider, discovered like any other.",
)
