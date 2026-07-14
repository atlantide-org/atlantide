"""How policies get attached to resources.

- ``enforce(name, level=..., types=...)`` — called from an Atlas-lang config to
  attach a policy globally or to a set of resource types; records a binding into
  the active resource registry.
- ``@policy(name, level=...)`` — class decorator that stacks bindings onto a
  Resource subclass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from atlantide.core import PolicyBinding, PolicyLevel, Resource
from atlantide.core.errors import RegistryError
from atlantide.core.resource import active_registry

_CLASS_ATTR = "_atl_policy_bindings"

R = TypeVar("R", bound=type[Resource])


def enforce(
    name: str,
    *,
    level: PolicyLevel = PolicyLevel.MANDATORY,
    types: Iterable[str] | None = None,
) -> None:
    """Attach policy ``name`` to the current config (global, or to ``types``)."""
    registry = active_registry()
    if registry is None:
        raise RegistryError("enforce() must be called during config evaluation")
    registry.add_policy_binding(
        PolicyBinding(name=name, level=level, types=frozenset(types) if types else None)
    )


def policy(name: str, *, level: PolicyLevel = PolicyLevel.MANDATORY) -> Callable[[R], R]:
    """Class decorator: bind policy ``name`` to a Resource subclass."""

    def decorate(cls: R) -> R:
        existing = getattr(cls, _CLASS_ATTR, ())
        binding = PolicyBinding(name=name, level=level, types=frozenset({cls.type_name()}))
        setattr(cls, _CLASS_ATTR, (*existing, binding))
        return cls

    return decorate


def class_bindings(cls: type[Resource]) -> tuple[PolicyBinding, ...]:
    """Policy bindings declared on a Resource subclass via ``@policy``."""
    bindings: tuple[PolicyBinding, ...] = getattr(cls, _CLASS_ATTR, ())
    return bindings
