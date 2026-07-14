"""Stacks: named namespaces for resources.

A :class:`Stack` is a context manager. Resources created inside it are prefixed
with the stack name in their ``node_id`` (``{stack}:{type}:{name}``), so the same
logical name can exist in several stacks (e.g. ``dev`` and ``prod``) without
colliding. Stacks nest; the innermost active stack wins. Stacks namespace within
one shared state store.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Literal

from atlantide.core.errors import RegistryError
from atlantide.core.node_id import require_identifier
from atlantide.core.types import StackOutputRef

DEFAULT_STACK = "default"

_active_stack: ContextVar[str] = ContextVar("atlantide_stack", default=DEFAULT_STACK)
# None default (not {}) avoids a shared mutable default; treated as empty.
_active_tags: ContextVar[dict[str, str] | None] = ContextVar(
    "atlantide_stack_tags", default=None
)
_active_region: ContextVar[str | None] = ContextVar("atlantide_stack_region", default=None)
_active_name_prefix: ContextVar[str | None] = ContextVar(
    "atlantide_stack_name_prefix", default=None
)


def current_stack() -> str:
    """The name of the innermost active stack (``"default"`` if none)."""
    return _active_stack.get()


def current_stack_tags() -> dict[str, str]:
    """Merged tags of the active stack chain (outer -> inner)."""
    return dict(_active_tags.get() or {})


def current_stack_region() -> str | None:
    """The innermost active stack's default region, or ``None``."""
    return _active_region.get()


def current_stack_name_prefix() -> str | None:
    """The innermost active stack's cloud-name prefix, or ``None``."""
    return _active_name_prefix.get()


@contextmanager
def region(name: str) -> Iterator[None]:
    """Override the active region for resources created in the body.

    A lightweight sub-scope of :class:`Stack` (region only): resources with a
    ``region`` field created inside inherit ``name`` unless they pass their own,
    e.g. an ACM certificate / CloudFront-facing bucket in ``us-east-1`` within a
    stack whose default region is elsewhere. Nests and restores on exit.
    """
    if not name:
        raise RegistryError("region() requires a non-empty region")
    token = _active_region.set(name)
    try:
        yield
    finally:
        _active_region.reset(token)


class Stack:
    """Context manager that scopes resources created in its body.

    ``tags`` are merged into every resource in the body that has a ``tags`` field;
    nested stacks merge (inner wins), and a resource's own tags win over the
    stack's.

    ``region`` is **required** — it is the default for every resource in the body
    that has a ``region`` field and did not pass one explicitly. ``name_prefix``
    composes the cloud name of resources whose name field is marked
    ``physical_name`` into
    ``{name_prefix}-{base}-{stack}``, falling back to the enclosing stack's value
    when omitted (inner wins).
    """

    def __init__(
        self,
        name: str,
        *,
        region: str,
        tags: dict[str, str] | None = None,
        name_prefix: str | None = None,
    ) -> None:
        require_identifier(name, "stack")
        if not region:
            raise RegistryError(f"stack {name!r} requires a non-empty region")
        self.name = name
        self.tags = dict(tags or {})
        self.region = region
        self.name_prefix = name_prefix
        self._tokens: list[tuple[ContextVar[Any], Any]] = []

    def __enter__(self) -> Stack:
        # name_prefix left as None inherits the enclosing stack's value.
        region = self.region
        prefix = self.name_prefix if self.name_prefix is not None else current_stack_name_prefix()
        self._tokens = [
            (_active_stack, _active_stack.set(self.name)),
            (_active_tags, _active_tags.set({**current_stack_tags(), **self.tags})),
            (_active_region, _active_region.set(region)),
            (_active_name_prefix, _active_name_prefix.set(prefix)),
        ]
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        for var, token in reversed(self._tokens):
            var.reset(token)
        return False


class StackReference:
    """Read another stack's committed outputs (like Terraform's remote state).

    ``StackReference("prod").output("vpc_id")`` yields a :class:`StackOutputRef`
    handle the engine resolves from the ``prod`` stack's persisted outputs at
    apply. The referenced stack must already be applied into the same state store.
    """

    def __init__(self, stack: str) -> None:
        self.stack = stack

    def output(self, name: str) -> StackOutputRef:
        return StackOutputRef(self.stack, name)

    def __getitem__(self, name: str) -> StackOutputRef:
        return StackOutputRef(self.stack, name)
