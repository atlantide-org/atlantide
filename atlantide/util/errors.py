"""Failures that ride along on another exception.

When a run fails and the rollback fails too, the primary exception carries the
rollback failures so the CLI can print both. The extras are stored as an
attribute because a cancellation cannot be wrapped in an :class:`ExceptionGroup`:
callers' ``except CancelledError`` clauses would not match it.
"""

from __future__ import annotations

from collections.abc import Iterable

__all__ = ["ALSO_FAILED_ATTR", "also_failed", "attach_also_failed"]

#: Attribute holding the extra failures. Tests reference the name directly.
ALSO_FAILED_ATTR = "_also_failed"


def attach_also_failed(exc: BaseException, errors: Iterable[BaseException]) -> None:
    """Record ``errors`` as having failed alongside ``exc`` (replacing any earlier list)."""
    setattr(exc, ALSO_FAILED_ATTR, list(errors))


def also_failed(exc: BaseException) -> list[BaseException]:
    """The failures recorded on ``exc`` by :func:`attach_also_failed`, else ``[]``."""
    errors = getattr(exc, ALSO_FAILED_ATTR, None)
    return list(errors) if isinstance(errors, list) else []
