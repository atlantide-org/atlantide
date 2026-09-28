"""Bridges between the pure ``Result`` layer and the raising layer; see ``README.md``."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from returns.result import Failure, Result, Success

from atlantide.core import AtlantideError


def catching[T](fn: Callable[[], T]) -> Result[T, AtlantideError]:
    """Call ``fn``, returning its value as ``Success`` or its ``AtlantideError`` as ``Failure``.

    Bridges a raising helper back into the ``Result`` layer. Only
    ``AtlantideError`` is caught; any other exception is a bug and propagates.
    """
    try:
        return Success(fn())
    except AtlantideError as exc:
        return Failure(exc)


def forward_failure(result: Result[Any, AtlantideError]) -> Failure[AtlantideError]:
    """Re-tag a planning ``Failure`` to satisfy the async path's return type.

    The pure ``Result`` cannot be ``.bind``-ed across an ``await``, so each async
    stage unwraps by hand; this centralises that bridge.
    """
    return Failure(result.failure())


def raise_on_failure[T](result: Result[T, AtlantideError]) -> T:
    """Unwrap ``result``, raising its error.

    For the stages that run inside the state lock, where the ``Result`` cannot be
    returned to the caller; the raised error reaches the CLI through ``run_async``.
    """
    if isinstance(result, Failure):
        raise result.failure()
    return result.unwrap()
