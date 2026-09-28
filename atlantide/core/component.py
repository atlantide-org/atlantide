"""Components: library-authored reusable groups of resources (L2 constructs).

A :class:`Component` packages several resources behind one parameterized object,
like Pulumi's ``ComponentResource`` or a CDK Construct. Config authors *use*
components (import and instantiate them) but cannot *define* them in Atlas-lang,
which bans ``class``; components are ordinary Python written by library/provider
authors.

A component owns no IR node of its own: its children self-register as normal flat
resources, so lowering, diff and state handle them like any other resource. Child
logical names are namespaced with the component's name (``{component}-{child}``,
accumulating when components nest), so instantiating a component twice never
collides. The namespacing depends only on the component ``name``, keeping the IR
byte-stable.

    class SecureBucket(Component):
        def __init__(self, name, *, bucket):
            self.bucket = child(S3Bucket, "assets", bucket=bucket)  # id: <stack>:...:name-assets

The subclass ``__init__`` needs no ``super().__init__`` call: its body runs inside
the naming scope automatically.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, override

from atlantide.core._component_prefix import active_prefix as _active_prefix
from atlantide.core._component_prefix import push as _push
from atlantide.core._component_prefix import reset as _reset
from atlantide.core.node_id import require_identifier

if TYPE_CHECKING:
    from atlantide.core.resource import Resource


def child[R: Resource](cls: type[R], name: str, /, **kwargs: Any) -> R:
    """Construct a component child, preserving its concrete type.

    Prefer this to calling ``cls(name, ...)`` directly inside a component: pydantic
    makes mypy synthesize a keyword-only ``__init__`` for a concrete resource, so a
    positional ``name`` fails to type-check. Routing through the ``Resource`` base
    keeps the call typed. The child still namespaces and self-registers normally.
    """
    return cls(name, **kwargs)


def current_component_prefix() -> str | None:
    """The active child-name prefix, or ``None`` outside any component."""
    return _active_prefix()


class Component:
    """Base for library-authored L2 constructs. Subclass and create children in
    ``__init__``; expose their handles as attributes for downstream wiring."""

    name: str
    #: True while this instance's outermost wrapped ``__init__`` runs, so a
    #: ``super().__init__`` chain pushes the name prefix exactly once.
    _atlas_in_init: bool = False

    @override
    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        init = cls.__dict__.get("__init__")
        if init is not None and not getattr(init, "_atlas_scoped", False):
            cls.__init__ = _scoped_init(init)  # type: ignore[method-assign]


def _scoped_init(init: Callable[..., None]) -> Callable[..., None]:
    """Wrap a subclass ``__init__`` so its body runs inside the naming scope."""

    @functools.wraps(init)
    def scoped(self: Component, name: str, /, *args: Any, **kwargs: Any) -> None:
        # Re-entrancy guard: a subclass calling `super().__init__(name, ...)`
        # runs the parent's wrapped init on the same instance, and pushing the
        # prefix again would double it (`name-name-child`).
        if getattr(self, "_atlas_in_init", False):
            init(self, name, *args, **kwargs)
            return
        require_identifier(name, "component")
        self.name = name
        self._atlas_in_init = True
        token = _push(name)
        try:
            init(self, name, *args, **kwargs)
        finally:
            _reset(token)
            self._atlas_in_init = False

    scoped._atlas_scoped = True  # type: ignore[attr-defined]
    return scoped
