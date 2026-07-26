"""Resource-type introspection for the ``resources`` and ``schema`` commands.

Reads off the pydantic model and its atlantide field metadata
(:mod:`atlantide.core.fields`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any, get_args, get_origin

import typer
from pydantic.fields import FieldInfo
from pydantic_core import PydanticUndefined
from rich.table import Table

from atlantide.cli.console import console
from atlantide.cli.errors import fail
from atlantide.cli.render import MUT_COLOR
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


# -- the commands that render the above ---------------------------------------

app = typer.Typer()


@app.command()
def resources() -> None:
    """List every resource type across the built-in providers."""
    types = all_types()
    table = Table(title="Resource types")
    table.add_column("type", style="bold")
    table.add_column("provider")
    table.add_column("fields", justify="right")
    for type_name in sorted(types):
        cls = types[type_name]
        table.add_row(type_name, cls.provider_name() or "-", str(len(schema_rows(cls))))
    console.print(table)


@app.command()
def schema(
    type_name: Annotated[str, typer.Argument(help="Resource type, e.g. aws.S3Bucket.")],
) -> None:
    """Show the fields of one resource type (type, mutability, default, sensitivity)."""
    types = all_types()
    cls = types.get(type_name)
    if cls is None:
        available = ", ".join(sorted(types))
        fail(f"unknown type {type_name!r}. Available: {available}")
    table = Table(title=type_name)
    table.add_column("field", style="bold")
    table.add_column("type")
    table.add_column("mutability")
    table.add_column("required")
    table.add_column("default")
    table.add_column("sensitive")
    for row in schema_rows(cls):
        color = MUT_COLOR[row.mutability]
        table.add_row(
            row.name,
            row.type,
            f"[{color}]{row.mutability.value}[/]",
            "yes" if row.required else "",
            row.default,
            "yes" if row.sensitive else "",
        )
    console.print(table)
