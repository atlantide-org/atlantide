"""How policies get attached to resources.

- ``enforce(name, level=..., types=...)`` — called from an Atlas-lang config to
  attach a policy globally or to a set of resource types; records a binding into
  the active resource registry.
- ``@policy(name, level=...)`` — class decorator that stacks bindings onto a
  Resource subclass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any, TypeVar

from atlantide.core import PolicyBinding, PolicyLevel, Resource
from atlantide.core.errors import PolicyConfigError, RegistryError
from atlantide.core.resource import active_registry

_CLASS_ATTR = "_atl_policy_bindings"

R = TypeVar("R", bound=type[Resource])


def enforce(
    name: str,
    *,
    level: PolicyLevel = PolicyLevel.MANDATORY,
    types: str | Iterable[str] | None = None,
    **params: Any,
) -> None:
    """Attach policy ``name`` to the current config (global, or to ``types``).

    Extra keywords are the policy's arguments, so a parameterised rule is
    configured at the point it is attached::

        enforce("deny-destroy-in-protected", stacks=["prod"])

    The policy reads them from :attr:`PolicyContext.params`; an unrecognised
    argument is the policy's own error to raise.
    """
    registry = active_registry()
    if registry is None:
        raise RegistryError("enforce() must be called during config evaluation")
    if isinstance(types, str):
        # A lone type name is a natural way to call this; `frozenset(str)` would
        # silently become a set of characters and the policy would match nothing.
        types = (types,)
    type_set = frozenset(types) if types is not None else None
    if type_set is not None and not type_set:
        raise PolicyConfigError(
            f"policy {name!r}: `types` is empty — name at least one resource type, "
            "or omit it to apply the policy to every resource"
        )
    registry.add_policy_binding(
        PolicyBinding(
            name=name,
            level=level,
            types=type_set,
            params=params,
        )
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
