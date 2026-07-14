"""Resource-type introspection for the ``resources`` and ``schema`` commands.

Reads off the pydantic model and its atlantide field metadata
(:mod:`atlantide.core.fields`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, get_args, get_origin

from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined

from atlantide.core.fields import Mutability, field_mutability, is_sensitive
from atlantide.core.resource import Resource
from atlantide.providers import aws, local, random


def all_types() -> dict[str, type[Resource]]:
    """Every resource type registered across the built-in providers, by type_name."""
    return {**local.TYPES, **random.TYPES, **aws.TYPES}


@dataclass(frozen=True)
class FieldRow:
    """One field of a resource type, flattened for display."""

    name: str
    type: str
    mutability: Mutability
    required: bool
    default: str
    sensitive: bool


def _type_str(annotation: Any) -> str:
    """Readable rendering of a field annotation (``dict[str, str]``, ``bool``, ...)."""
    if annotation is None:
        return "Any"
    origin = get_origin(annotation)
    if origin is None:
        return getattr(annotation, "__name__", str(annotation))
    name = getattr(origin, "__name__", str(origin))
    args = get_args(annotation)
    if args:
        return f"{name}[{', '.join(_type_str(a) for a in args)}]"
    return name


def _default_str(field: FieldInfo) -> str:
    if field.default_factory is not None:
        try:
            return repr(field.default_factory())  # type: ignore[call-arg]
        except Exception:
            return "<factory>"
    if field.default is PydanticUndefined:
        return ""
    return repr(field.default)


def schema_rows(cls: type[Resource]) -> list[FieldRow]:
    """Field rows for a resource type, in declaration order."""
    mutability = field_mutability(cls)
    rows: list[FieldRow] = []
    for name, field in cls.model_fields.items():
        mut = mutability[name]
        computed = mut is Mutability.COMPUTED
        rows.append(
            FieldRow(
                name=name,
                type=_type_str(field.annotation),
                mutability=mut,
                required=field.is_required() and not computed,
                default="" if computed else _default_str(field),
                sensitive=is_sensitive(cls, name),
            )
        )
    return rows
