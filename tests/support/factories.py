"""Small builders that remove hand-written boilerplate from tests.

:func:`make_engine` lives here rather than in ``conftest``: the harness is a
library the suites import, and a library importing pytest glue points the
dependency the wrong way round.
"""

from __future__ import annotations

from typing import Any

from atlantide.core import Provider, ProviderRegistry, Resource
from atlantide.engine import Engine
from atlantide.policy import PolicyRegistry
from atlantide.providers import local
from atlantide.providers import random as random_provider
from atlantide.providers.local import LocalProvider
from atlantide.providers.random import RandomProvider
from atlantide.secrets import SecretsRegistry
from atlantide.state import (
    DEFAULT_LOCK_POLICY,
    LockPolicy,
    MemoryStateBackend,
    StateBackend,
    StateNode,
)
from tests.support.cloud import TEST_REGION
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


def make_engine(
    types: dict[str, type[Resource]],
    *providers: Provider,
    backend: StateBackend | None = None,
    policies: PolicyRegistry | None = None,
    secrets: SecretsRegistry | None = None,
    parallelism: int | None = None,
    lock_policy: LockPolicy = DEFAULT_LOCK_POLICY,
) -> Engine:
    """An Engine over the given providers/types with in-memory-state defaults."""
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return Engine(
        registry,
        backend if backend is not None else MemoryStateBackend(),
        types,
        policies=policies,
        secrets=secrets,
        parallelism=parallelism,
        lock_policy=lock_policy,
    )


def local_engine(**kw: Any) -> Engine:
    """An :class:`Engine` over the local provider (``local.TYPES``, :class:`LocalProvider`).

    ``kw`` is passed to :func:`make_engine` (``backend=``, ``policies=``, ...).
    """
    return make_engine(local.TYPES, LocalProvider(), **kw)


def random_engine(**kw: Any) -> Engine:
    """An :class:`Engine` over the random provider; ``kw`` as for :func:`local_engine`."""
    return make_engine(random_provider.TYPES, RandomProvider(), **kw)


def aws_engine(backend: StateBackend | None = None) -> Engine:
    """An :class:`Engine` over ``AwsProvider(region=TEST_REGION)``, in-memory state by default.

    Needs the moto mock the suite sets up (:func:`tests.support.aws_fixture`) before
    it applies anything; building the engine itself makes no AWS call. The AWS
    provider is imported here, not at module level, so suites that never touch
    AWS do not pay for importing it.
    """
    from atlantide.providers.aws import TYPES, AwsProvider

    return make_engine(TYPES, AwsProvider(region=TEST_REGION), backend=backend)
