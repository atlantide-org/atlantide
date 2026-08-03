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

from atlantide.core.config import EnvSchema
from atlantide.core.errors import LanguageError, RegistryError
from atlantide.core.node_id import require_identifier
from atlantide.core.types import StackOutputRef

DEFAULT_STACK = "default"

_active_stack: ContextVar[str] = ContextVar("atlantide_stack", default=DEFAULT_STACK)
# None default (not {}) avoids a shared mutable default; treated as empty.
_active_tags: ContextVar[dict[str, str] | None] = ContextVar("atlantide_stack_tags", default=None)
_active_region: ContextVar[str | None] = ContextVar("atlantide_stack_region", default=None)
_active_name_prefix: ContextVar[str | None] = ContextVar(
    "atlantide_stack_name_prefix", default=None
)
_active_config: ContextVar[EnvSchema | None] = ContextVar("atlantide_stack_config", default=None)


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


def current_config() -> EnvSchema | None:
    """The innermost active stack's environment config, or ``None``.

    Ambient for the same reason ``region`` and ``tags`` are: a
    :class:`~atlantide.core.component.Component` reads it without every caller
    threading it through a constructor.
    """
    return _active_config.get()


def _well_known(config: EnvSchema | None, key: str, expected: type) -> Any:
    """Read a well-known key (``region``/``tags``/``name_prefix``) off an env.

    A wrong type is reported here, named with its environment, rather than as a
    pydantic error on whichever resource was declared first.
    """
    if config is None:
        return None
    value = config.get(key)
    if value is not None and not isinstance(value, expected):
        raise LanguageError(
            f"environment {config.name!r}: {key!r} must be {expected.__name__}, "
            f"got {type(value).__name__} {value!r}"
        )
    return value


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

    ``region`` is **required**, from either the ``region=`` argument or a
    ``config=`` environment that declares one — it is the default for every
    resource in the body that has a ``region`` field and did not pass one
    explicitly. ``name_prefix`` composes the cloud name of resources whose name
    field is marked ``physical_name`` into ``{name_prefix}-{base}-{stack}``,
    falling back to the enclosing stack's value when omitted (inner wins).

    ``config`` is one environment out of a :class:`~atlantide.core.config.Config`
    (``for env in config.envs(): with Stack(env.name, config=env)``). Its
    well-known ``region``/``tags``/``name_prefix`` keys fill in the matching
    arguments, and the environment is ambient in the body via
    :func:`current_config`. An explicit argument wins over the config.
    """

    def __init__(
        self,
        name: str,
        *,
        region: str | None = None,
        tags: dict[str, str] | None = None,
        name_prefix: str | None = None,
        config: EnvSchema | None = None,
    ) -> None:
        require_identifier(name, "stack")
        # An explicit argument overrides the environment, except `tags`, which merge.
        env_region = _well_known(config, "region", str)
        env_tags = _well_known(config, "tags", dict) or {}
        env_prefix = _well_known(config, "name_prefix", str)

        self.name = name
        self.region = region if region is not None else env_region
        self.tags = {**env_tags, **(tags or {})}
        self.name_prefix = name_prefix if name_prefix is not None else env_prefix
        self.config = config

        if not self.region:
            raise RegistryError(
                f"stack {name!r} requires a non-empty region — pass region=, or a "
                f"config= whose environment declares one"
            )
        # One token set per active `with`, and the entry stack itself lives in a
        # ContextVar: tokens belong to an entry *in one context*. An instance-
        # level list interleaves across asyncio tasks — task A's `__exit__`
        # would pop task B's tokens and `reset()` them in the wrong context
        # (ValueError), leaking A's own settings for the rest of its context.
        self._entries: ContextVar[tuple[tuple[tuple[ContextVar[Any], Any], ...], ...]] = ContextVar(
            f"atlantide_stack_entries_{name}_{id(self)}", default=()
        )

    def __enter__(self) -> Stack:
        # name_prefix left as None inherits the enclosing stack's value.
        region = self.region
        prefix = self.name_prefix if self.name_prefix is not None else current_stack_name_prefix()
        entry = (
            (_active_stack, _active_stack.set(self.name)),
            (_active_tags, _active_tags.set({**current_stack_tags(), **self.tags})),
            (_active_region, _active_region.set(region)),
            (_active_name_prefix, _active_name_prefix.set(prefix)),
            (_active_config, _active_config.set(self.config)),
        )
        self._entries.set((*self._entries.get(), entry))
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        entries = self._entries.get()
        if not entries:
            return False
        self._entries.set(entries[:-1])
        for var, token in reversed(entries[-1]):
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
