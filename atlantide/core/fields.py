"""Per-field mutability declarations read by the diff engine.

- ``mutable()``   -> UPDATE in place (default)
- ``immutable()`` -> REPLACE (delete + recreate)
- ``computed()``  -> provider-set output, never diffed as input

Stored as pydantic ``Field`` metadata (``json_schema_extra``).
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, Field
from pydantic_core import PydanticUndefined

from atlantide.core.types import UNSET

_ATL_KEY = "atlantide"


class Mutability(enum.StrEnum):
    MUTABLE = "mutable"
    IMMUTABLE = "immutable"
    COMPUTED = "computed"


def _field(
    *,
    mutability: Mutability,
    default: Any,
    default_factory: Callable[[], Any] | None,
    sensitive: bool,
    physical_name: bool,
) -> Any:
    extra: dict[str, Any] = {
        _ATL_KEY: {
            "mutability": mutability.value,
            "sensitive": sensitive,
            "physical_name": physical_name,
        }
    }
    if default_factory is not None:
        return Field(default_factory=default_factory, json_schema_extra=extra)
    return Field(default=default, json_schema_extra=extra)


def mutable(
    default: Any = PydanticUndefined,
    *,
    default_factory: Callable[[], Any] | None = None,
    sensitive: bool = False,
    physical_name: bool = False,
) -> Any:
    """Change to this field -> UPDATE in place."""
    return _field(
        mutability=Mutability.MUTABLE,
        default=default,
        default_factory=default_factory,
        sensitive=sensitive,
        physical_name=physical_name,
    )


def immutable(
    default: Any = PydanticUndefined,
    *,
    default_factory: Callable[[], Any] | None = None,
    sensitive: bool = False,
    physical_name: bool = False,
) -> Any:
    """Change to this field -> REPLACE (delete + recreate).

    Set ``physical_name=True`` on the field holding the resource's cloud name
    so an active ``Stack(name_prefix=...)`` can compose it.
    """
    return _field(
        mutability=Mutability.IMMUTABLE,
        default=default,
        default_factory=default_factory,
        sensitive=sensitive,
        physical_name=physical_name,
    )


def computed(*, sensitive: bool = False) -> Any:
    """Provider-set output. Holds UNSET until apply; reading it yields a Ref."""
    return _field(
        mutability=Mutability.COMPUTED,
        default=UNSET,
        default_factory=None,
        sensitive=sensitive,
        physical_name=False,
    )


def secret(
    default: Any = PydanticUndefined,
    *,
    default_factory: Callable[[], Any] | None = None,
    physical_name: bool = False,
) -> Any:
    """A secret input: declare the field type as ``SecretRef | None``.

    The field holds a :class:`~atlantide.core.types.SecretRef` handle (a name),
    never the value. Source, IR, and state carry only the handle; the plaintext is
    resolved from the secrets backend in-memory at apply and redacted in plan/logs.
    """
    return _field(
        mutability=Mutability.MUTABLE,
        default=default,
        default_factory=default_factory,
        sensitive=True,
        physical_name=physical_name,
    )


def _extra_of(model: type[BaseModel], name: str) -> dict[str, Any]:
    extra = model.model_fields[name].json_schema_extra
    if isinstance(extra, dict):
        meta = extra.get(_ATL_KEY)
        if isinstance(meta, dict):
            return meta
    return {}


def field_mutability(model: type[BaseModel]) -> dict[str, Mutability]:
    """Field name -> declared mutability (MUTABLE when undeclared)."""
    result: dict[str, Mutability] = {}
    for name in model.model_fields:
        meta = _extra_of(model, name)
        raw = meta.get("mutability", Mutability.MUTABLE.value)
        result[name] = Mutability(raw)
    return result


def is_sensitive(model: type[BaseModel], name: str) -> bool:
    """Whether a field was declared ``sensitive=True`` (redacted in plan/logs)."""
    return bool(_extra_of(model, name).get("sensitive", False))


def sensitive_fields(model: type[BaseModel]) -> list[str]:
    """Names of every field declared ``sensitive`` (its value is sealed in state)."""
    return [name for name in model.model_fields if is_sensitive(model, name)]


def physical_name_field(model: type[BaseModel]) -> str | None:
    """The field declared ``physical_name=True`` (the cloud name), or ``None``."""
    for name in model.model_fields:
        if _extra_of(model, name).get("physical_name", False):
            return name
    return None
