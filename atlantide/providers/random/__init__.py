"""atlantide.providers.random: values generated once at apply, pinned in state."""

from atlantide.core import Resource
from atlantide.core.plugin import ProviderPlugin
from atlantide.providers.random.provider import RandomProvider
from atlantide.providers.random.resources import Id, Password, Timestamp, Uuid

_RESOURCE_TYPES: tuple[type[Resource], ...] = (Uuid, Password, Id, Timestamp)
TYPES: dict[str, type[Resource]] = {cls.type_name(): cls for cls in _RESOURCE_TYPES}

#: See :mod:`atlantide.core.plugin`.
PLUGIN = ProviderPlugin(
    name=RandomProvider.name,
    types=TYPES,
    factory=lambda _settings: RandomProvider(),
    module="atlantide.providers.random",
    summary="Values generated once at apply and pinned in state.",
)

__all__ = ["PLUGIN", "TYPES", "Id", "Password", "RandomProvider", "Timestamp", "Uuid"]
