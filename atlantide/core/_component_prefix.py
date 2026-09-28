"""The active component child-name prefix.

A leaf shared by :mod:`atlantide.core.component`, which extends the prefix while a
component's ``__init__`` runs, and :mod:`atlantide.core.resource`, which
namespaces a child's logical name with it. Living here keeps ``resource`` from
importing ``component``, whose ``child`` is typed against ``Resource``.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

_active_prefix: ContextVar[str | None] = ContextVar("atlantide_component_prefix", default=None)


def active_prefix() -> str | None:
    """The active child-name prefix, or ``None`` outside any component."""
    return _active_prefix.get()


def push(name: str) -> Token[str | None]:
    """Accumulate ``name`` onto the active prefix; returns a reset token."""
    current = _active_prefix.get()
    return _active_prefix.set(f"{current}-{name}" if current else name)


def reset(token: Token[str | None]) -> None:
    """Restore the prefix that was active before the :func:`push` that made ``token``."""
    _active_prefix.reset(token)
