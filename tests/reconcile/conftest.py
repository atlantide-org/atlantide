"""Reconcile test harness: the shared :class:`~tests.support.Harness` bound to the
canonical ``Box`` resource and a default :class:`~tests.support.FakeProvider`.

``Harness(backend)`` is a Box-bound factory kept for call-site brevity across the
reconcile suite; it returns a fully-configured ``tests.support.Harness``.
"""

from __future__ import annotations

from atlantide.core import Lifecycle, Provider
from atlantide.state import StateNode
from atlantide.state.backend import StateBackend
from tests.support import Box, globals_of
from tests.support import Harness as _Harness

GLOBALS = globals_of(Box, Lifecycle=Lifecycle)


def Harness(backend: StateBackend, provider: Provider | None = None) -> _Harness:
    """A Box-bound :class:`tests.support.Harness` over ``backend``."""
    return _Harness.of(Box, provider=provider, backend=backend, globals={"Lifecycle": Lifecycle})


__all__ = ["GLOBALS", "Box", "Harness", "StateNode"]
