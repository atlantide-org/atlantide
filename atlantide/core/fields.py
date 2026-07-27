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


#: Per-class field-metadata cache. Keyed by the model class itself; pydantic
#: model classes never change their ``model_fields`` after creation, so a
#: cached scan is safe — and every diff/plan/refresh consults these maps per
#: node, which made the repeated per-call re-parse the hottest cold code here.
_META_CACHE: dict[type[BaseModel], dict[str, dict[str, Any]]] = {}


def _atl_meta(model: type[BaseModel]) -> dict[str, dict[str, Any]]:
    """Field name -> atlantide metadata for every field, scanned once per class."""
    cached = _META_CACHE.get(model)
    if cached is None:
        cached = {}
        for name, info in model.model_fields.items():
            extra = info.json_schema_extra
            meta = extra.get(_ATL_KEY) if isinstance(extra, dict) else None
            cached[name] = meta if isinstance(meta, dict) else {}
        _META_CACHE[model] = cached
    return cached


#: Derived views of :data:`_META_CACHE`, cached for the same reason it is: both
#: are read once per node per run — ``field_mutability`` from ``Resource``'s
#: ``input_values``, on every attribute pass over every resource — and rebuilding
#: the mapping each time was pure repeat work over an input that cannot change.
#: Returned by reference, so callers must treat them as read-only.
_MUTABILITY_CACHE: dict[type[BaseModel], dict[str, Mutability]] = {}
_SENSITIVE_CACHE: dict[type[BaseModel], list[str]] = {}


def field_mutability(model: type[BaseModel]) -> dict[str, Mutability]:
    """Field name -> declared mutability (MUTABLE when undeclared). Do not mutate."""
    cached = _MUTABILITY_CACHE.get(model)
    if cached is None:
        cached = {
            name: Mutability(meta.get("mutability", Mutability.MUTABLE.value))
            for name, meta in _atl_meta(model).items()
        }
        _MUTABILITY_CACHE[model] = cached
    return cached


def is_sensitive(model: type[BaseModel], name: str) -> bool:
    """Whether a field was declared ``sensitive=True`` (redacted in plan/logs)."""
    return bool(_atl_meta(model)[name].get("sensitive", False))


def sensitive_fields(model: type[BaseModel]) -> list[str]:
    """Names of every field declared ``sensitive`` (sealed in state). Do not mutate."""
    cached = _SENSITIVE_CACHE.get(model)
    if cached is None:
        cached = [name for name, meta in _atl_meta(model).items() if meta.get("sensitive", False)]
        _SENSITIVE_CACHE[model] = cached
    return cached


def physical_name_field(model: type[BaseModel]) -> str | None:
    """The field declared ``physical_name=True`` (the cloud name), or ``None``."""
    return next(
        (name for name, meta in _atl_meta(model).items() if meta.get("physical_name", False)),
        None,
    )
