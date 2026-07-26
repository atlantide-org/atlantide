"""Bridges between the pure ``Result`` layer and the raising async layer.

The engine's two-tier error model (see :mod:`atlantide.engine.engine`) needs
exactly two conversions, and every module in the package needs them — collected
here so call sites stop open-coding ``Failure(x.failure())``.
"""

from __future__ import annotations

from typing import Any, TypeVar

from returns.result import Failure, Result

from atlantide.core import AtlantideError

_T = TypeVar("_T")


def forward_failure(result: Result[Any, AtlantideError]) -> Failure[AtlantideError]:
    """Re-tag a planning ``Failure`` to satisfy the async path's return type.

    The pure ``Result`` cannot be ``.bind``-ed across an ``await``, so each async
    stage unwraps by hand; this centralises that bridge.
    """
    return Failure(result.failure())


def raise_on_failure(result: Result[_T, AtlantideError]) -> _T:
    """Unwrap ``result``, raising its error.

    For the stages that run inside the state lock, where the ``Result`` cannot be
    returned to the caller; the raised error reaches the CLI through ``run_async``.
    """
    if isinstance(result, Failure):
        raise result.failure()
    return result.unwrap()
