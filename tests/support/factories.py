"""Small builders that remove hand-written boilerplate from tests."""

from __future__ import annotations

from typing import Any

from atlantide.core import Provider, Resource
from atlantide.engine import Engine
from atlantide.state.backend import StateNode
from tests.conftest import make_engine
from tests.support.providers import FakeProvider


def types_of(*classes: type[Resource]) -> dict[str, type[Resource]]:
    """``{type_name: cls}`` for the given resource classes (the engine's TYPES)."""
    return {cls.type_name(): cls for cls in classes}


def globals_of(*classes: type[Resource], **extra: Any) -> dict[str, Any]:
    """``{ClassName: cls}`` plus any ``extra`` names, for Atlas-lang ``extra_globals``."""
    return {cls.__name__: cls for cls in classes} | extra


def state_node(
    name: str,
    *,
    type: str,
    provider: str = "test",
    provider_version: str = "1.0.0",
    outputs: dict[str, Any] | None = None,
    properties: dict[str, Any] | None = None,
    input_hash: str = "h",
    **kw: Any,
) -> StateNode:
    """A ``StateNode`` with id ``default:{type}:{name}`` (``type`` is ``provider.Class``)."""
    return StateNode(
        id=f"default:{type}:{name}",
        type=type,
        provider=provider,
        provider_version=provider_version,
        input_hash=input_hash,
        outputs=outputs or {},
        properties=properties or {},
        **kw,
    )


def engine_for(
    *resource_classes: type[Resource],
    provider: Provider | None = None,
    **kw: Any,
) -> Engine:
    """An :class:`Engine` over the given resource classes and a single provider.

    Sugar over :func:`tests.conftest.make_engine` that derives TYPES; defaults the
    provider to a bare :class:`FakeProvider`. Multi-provider tests use ``make_engine``.
    """
    return make_engine(types_of(*resource_classes), provider or FakeProvider(), **kw)
