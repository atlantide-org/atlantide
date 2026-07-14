"""Components: library-authored reusable groups of resources (L2 constructs).

A :class:`Component` packages several resources behind one parameterized object —
Pulumi's ``ComponentResource`` / CDK's Construct. Config authors *use* components
(import and instantiate them) but cannot *define* them in Atlas-lang, which bans
``class``; components are ordinary Python written by library/provider authors.

A component owns no IR node of its own: its children self-register as normal flat
resources, so lowering/diff/state need no changes. Child logical names are
namespaced with the component's name (``{component}-{child}``, accumulating when
components nest), so instantiating a component twice never collides. The
namespacing is deterministic given the component ``name``, preserving byte-stable
IR.

    class SecureBucket(Component):
        def __init__(self, name, *, bucket):
            self.bucket = child(S3Bucket, "assets", bucket=bucket)  # id: <stack>:...:name-assets

The subclass ``__init__`` needs no ``super().__init__`` call and no boilerplate —
its body runs inside the naming scope automatically.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, TypeVar

from atlantide.core.node_id import require_identifier

if TYPE_CHECKING:
    from atlantide.core.resource import Resource

_R = TypeVar("_R", bound="Resource")

_active_prefix: ContextVar[str | None] = ContextVar("atlantide_component_prefix", default=None)


def child(cls: type[_R], name: str, /, **kwargs: Any) -> _R:
    """Construct a component child, preserving its concrete type.

    Prefer this to calling ``cls(name, ...)`` directly inside a component: pydantic
    makes mypy synthesize a keyword-only ``__init__`` for a concrete resource, so a
    positional ``name`` fails to type-check. Routing through the ``Resource`` base
    keeps the call typed. The child still namespaces and self-registers normally.
    """
    return cls(name, **kwargs)


def current_component_prefix() -> str | None:
    """The active child-name prefix, or ``None`` outside any component."""
    return _active_prefix.get()


def _push(name: str) -> Any:
    """Accumulate ``name`` onto the active prefix; returns a reset token."""
    current = _active_prefix.get()
    return _active_prefix.set(f"{current}-{name}" if current else name)


class Component:
    """Base for library-authored L2 constructs. Subclass and create children in
    ``__init__``; expose their handles as attributes for downstream wiring."""

    name: str

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        init = cls.__dict__.get("__init__")
        if init is not None and not getattr(init, "_atlas_scoped", False):
            cls.__init__ = _scoped_init(init)  # type: ignore[method-assign]


def _scoped_init(init: Callable[..., None]) -> Callable[..., None]:
    """Wrap a subclass ``__init__`` so its body runs inside the naming scope."""

    @functools.wraps(init)
    def scoped(self: Component, name: str, /, *args: Any, **kwargs: Any) -> None:
        require_identifier(name, "component")
        self.name = name
        token = _push(name)
        try:
            init(self, name, *args, **kwargs)
        finally:
            _active_prefix.reset(token)

    scoped._atlas_scoped = True  # type: ignore[attr-defined]
    return scoped
