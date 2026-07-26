"""Reconcile test harness: the shared :class:`~tests.support.Harness` bound to the
canonical ``Box`` resource and a default :class:`~tests.support.FakeProvider`.

``Harness(backend)`` is a Box-bound factory kept for call-site brevity across the
reconcile suite; it returns a fully-configured ``tests.support.Harness``.
"""

from __future__ import annotations

from atlantide.core import Lifecycle
from atlantide.state import StateNode
from tests.support import Box, globals_of
from tests.support import box_harness as Harness

GLOBALS = globals_of(Box, Lifecycle=Lifecycle)


__all__ = ["GLOBALS", "Box", "Harness", "StateNode"]
