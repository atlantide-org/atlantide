"""How policies get attached to resources.

- ``enforce(name, level=..., types=...)``: called from an Atlas-lang config to
  attach a policy globally or to a set of resource types; records a binding in
  the active resource registry.
- ``@policy(name, level=...)``: class decorator that stacks bindings onto a
  Resource subclass.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from atlantide.core import PolicyBinding, PolicyLevel, Resource
from atlantide.core._describe import describe_type, describe_value
from atlantide.core.errors import PolicyConfigError, RegistryError
from atlantide.core.resource import active_registry

_CLASS_ATTR = "_atl_policy_bindings"


def _policy_name(name: object) -> str:
    """``name``, refused unless it is a string.

    Checked at binding time: a non-string would otherwise fail only at plan time,
    as an unknown policy or an unhashable lookup.
    """
    if not isinstance(name, str):
        raise PolicyConfigError(
            f"policy name must be a string, got {describe_type(name)} {describe_value(name)}"
        )
    return name


def _policy_level(name: str, level: object) -> PolicyLevel:
    """``level`` as a :class:`PolicyLevel`, accepting the enum or its string value.

    A bare ``"mandatory"`` would otherwise be stored as a ``str``: it fails the
    planner's ``is PolicyLevel.MANDATORY`` check, so it never blocks, and a typo
    surfaces only when the plan is rendered.
    """
    if isinstance(level, str):
        try:
            return PolicyLevel(level)
        except ValueError:
            pass
    choices = ", ".join(repr(member.value) for member in PolicyLevel)
    raise PolicyConfigError(
        f"policy {name!r}: `level` must be one of {choices}, "
        f"got {describe_type(level)} {describe_value(level)}"
    )


def _type_names(name: str, types: Iterable[object]) -> frozenset[str]:
    """``types`` as a set of type names, refusing any item that is not a string.

    A class (``types=[Bucket]``) would otherwise be kept as-is and silently match
    no resource, since bindings are matched by type name.
    """
    names: list[str] = []
    for item in types:
        if not isinstance(item, str):
            raise PolicyConfigError(
                f"policy {name!r}: `types` must name resource types as strings, "
                f"got {describe_type(item)} {describe_value(item)}"
            )
        names.append(item)
    return frozenset(names)


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
    name = _policy_name(name)
    level = _policy_level(name, level)
    registry = active_registry()
    if registry is None:
        raise RegistryError("enforce() must be called during config evaluation")
    if isinstance(types, str):
        # Accept a lone type name: `frozenset(str)` is a set of characters.
        types = (types,)
    type_set = _type_names(name, types) if types is not None else None
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


def policy[R: type[Resource]](
    name: str, *, level: PolicyLevel = PolicyLevel.MANDATORY
) -> Callable[[R], R]:
    """Class decorator: bind policy ``name`` to a Resource subclass."""
    name = _policy_name(name)
    level = _policy_level(name, level)

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
