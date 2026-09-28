"""The type and value checks behind :mod:`atlantide.core.config`.

Private: configs bind the names ``core.config`` defines (``Var``, ``Config``,
``EnvSchema``, ...), not these helpers. This module does not import
``core.config``; it reads a declaration through :class:`Declaration`.

Imports only ``core._describe``, ``core.errors`` and ``core.node_id``, as the
``core.config`` import-graph note states.
"""

from __future__ import annotations

import inspect
import keyword
import sys
import types
import typing
from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol, get_args, get_origin

from atlantide.core._describe import describe_type, describe_value
from atlantide.core.errors import LanguageError
from atlantide.core.node_id import require_identifier

#: Marks "no default declared", distinct from every value including ``None``.
MISSING = object()

#: What a ``var()`` may be declared as. Parameterised generics (``list[str]``)
#: are refused: they evaluate to a ``types.GenericAlias`` that ``isinstance``
#: cannot test against, and element-type checking is out of scope.
SUPPORTED_TYPES: tuple[type, ...] = (str, int, float, bool, list, dict)

#: Names an environment variable may not take: they are the environment's own
#: API, so a variable with one of these names would be shadowed by it.
RESERVED: tuple[str, ...] = ("name", "get", "as_dict")


class Declaration(Protocol):
    """One declared variable, as resolving an environment reads it (a ``Var``)."""

    @property
    def type(self) -> type: ...

    @property
    def default(self) -> Any: ...

    @property
    def required(self) -> bool: ...

    @property
    def nullable(self) -> bool: ...


def require_default_matches(declared_as: str, type_: type, default: Any) -> None:
    """Reject a default the declared type would refuse from an environment.

    Shared by ``var()`` and ``EnvSchema`` fields so both accept the same
    defaults and raise the same error. ``None`` always passes: it makes a
    variable optional and nullable.
    """
    if default is not None and not type_matches(default, type_):
        raise LanguageError(
            f"{declared_as} default {describe_value(default)} is a {describe_type(default)}"
        )


def describe_type_arg(value: Any) -> str:
    """A readable name for whatever is passed where a type is expected."""
    name = getattr(value, "__name__", None)
    return name if isinstance(name, str) else describe_value(value)


def type_matches(value: Any, expected: type) -> bool:
    """``isinstance`` that keeps ``bool`` and ``int`` apart.

    ``isinstance(True, int)`` is ``True``, so a plain check would let
    ``var(int)`` accept ``size=True``. :mod:`atlantide.cli.project` applies the
    same guard.
    """
    if expected is bool:
        return isinstance(value, bool)
    if isinstance(value, bool):
        return False
    if expected is float:
        return isinstance(value, int | float)
    return isinstance(value, expected)


def require_variable_name(name: str, what: str) -> None:
    """Reject a variable name that could not be read back as ``env.<name>``."""
    if not name.isidentifier() or name.startswith("_"):
        raise LanguageError(
            f"{what} {name!r} must be a plain identifier not starting with '_' — "
            f"it is read back as `env.{name}`"
        )
    if keyword.iskeyword(name):
        raise LanguageError(
            f"{what} {name!r} is a Python keyword — `env.{name}` is a syntax error, "
            f"pick another name"
        )
    if name in RESERVED:
        raise LanguageError(
            f"{what} {name!r} collides with an environment's own API "
            f"({', '.join(RESERVED)}) — pick another name"
        )


def own_annotations(cls: type) -> dict[str, Any]:
    """The annotations ``cls`` itself declares, without evaluating unknown names.

    Not ``cls.__dict__["__annotations__"]``: under PEP 649 (Python 3.14) a class
    whose annotations are not postponed stores them lazily, not in its dict.
    """
    if sys.version_info >= (3, 14):
        import annotationlib

        return annotationlib.get_annotations(cls, format=annotationlib.Format.FORWARDREF)
    return inspect.get_annotations(cls)


def annotation_type(owner: str, field: str, annotation: Any) -> tuple[type, bool]:
    """The declared type of an annotated field, and whether it is nullable.

    Accepts one of :data:`SUPPORTED_TYPES`, or ``X | None``. Annotations are
    compared by name (see :func:`_annotation_parts`) rather than resolved with
    ``get_type_hints``, which would evaluate names from the declaring module.
    """
    parts = _annotation_parts(annotation)
    named = [part for part in parts if part != "None"]
    written = " | ".join(parts)
    if len(named) != 1:
        raise LanguageError(
            f"field {field!r} of {owner!r}: only `X | None` may be combined, got {written!r}"
        )
    by_name = {supported.__name__: supported for supported in SUPPORTED_TYPES}
    if named[0] not in by_name:
        raise LanguageError(
            f"field {field!r} of {owner!r} must be one of {', '.join(by_name)}, "
            f"got {written!r} — parameterised generics such as list[str] are not supported"
        )
    return by_name[named[0]], "None" in parts


def _annotation_parts(annotation: Any) -> tuple[str, ...]:
    """An annotation as the names it is written from: ``str | None`` -> ``("str", "None")``.

    An annotation arrives as a string under ``from __future__ import annotations``
    and as an object otherwise; comparing names handles both without evaluating
    the string.
    """
    # An unresolvable name under PEP 649 arrives as a `ForwardRef`; read its text.
    annotation = getattr(annotation, "__forward_arg__", annotation)
    if isinstance(annotation, str):
        return tuple(part.strip() for part in annotation.split("|"))
    if get_origin(annotation) in (types.UnionType, typing.Union):  # e.g. `str | None`
        return tuple(_annotation_name(member) for member in get_args(annotation))
    return (_annotation_name(annotation),)


def _annotation_name(annotation: Any) -> str:
    """One annotation member's name, with ``NoneType`` spelled the way it is written."""
    if annotation is None or annotation is type(None):
        return "None"
    if get_origin(annotation) is not None:
        # A generic such as `list[str]` proxies `__name__` to its origin
        # (`list`), which would accept it as a plain `list`; keep it whole so it
        # is refused as unsupported.
        return str(annotation)
    name = getattr(annotation, "__name__", None)
    return name if isinstance(name, str) else str(annotation)


def resolve_envs[ViewT](
    schema: Mapping[str, Declaration],
    envs: Mapping[str, Mapping[str, Any]],
    view: Callable[[str, dict[str, Any], Sequence[str]], ViewT],
) -> dict[str, ViewT]:
    """Validate each environment against ``schema``, keyed in sorted name order.

    ``schema`` arrives in sorted key order. ``view`` is the class each
    environment is built as: the user's ``EnvSchema`` subclass, or ``EnvView``
    for the ``var()`` form.
    """
    if not envs:
        raise LanguageError("Config() requires at least one environment in envs=")
    declared = tuple(schema)
    resolved: dict[str, ViewT] = {}
    for name in sorted(envs):
        # Environment names become stack names; validating here reports a bad one
        # at its declaration rather than as an invalid stack.
        require_identifier(name, "environment")
        resolved[name] = view(name, _resolve_values(schema, name, envs[name]), declared)
    return resolved


def _resolve_values(
    schema: Mapping[str, Declaration], env_name: str, values: Mapping[str, Any]
) -> dict[str, Any]:
    """One environment's values, defaults filled in and every entry type-checked."""
    if not isinstance(values, Mapping):
        raise LanguageError(
            f"environment {env_name!r} must be a mapping of variable to value, "
            f"got {describe_type(values)}"
        )
    for key in values:
        if key not in schema:
            raise LanguageError(
                f"environment {env_name!r}: unknown variable {describe_value(key)} — "
                f"declared: {', '.join(schema)}"
            )
    return {
        key: _resolve_value(declaration, env_name, key, values)
        for key, declaration in schema.items()
    }


def _resolve_value(
    declaration: Declaration, env_name: str, key: str, values: Mapping[str, Any]
) -> Any:
    """One variable's value: what the environment supplied, or its default."""
    if key not in values:
        if declaration.required:
            raise LanguageError(f"environment {env_name!r} is missing required variable {key!r}")
        return declaration.default

    value = values[key]
    # `None` is accepted only for a nullable variable (one that defaults to
    # `None`, or was annotated `X | None`); elsewhere the type check rejects it.
    if value is None and declaration.nullable:
        return None
    if not type_matches(value, declaration.type):
        raise LanguageError(
            f"environment {env_name!r}: variable {key!r} expects "
            f"{declaration.type.__name__}, got {describe_type(value)} {describe_value(value)}"
        )
    return value
