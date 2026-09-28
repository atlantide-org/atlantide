"""Static audit of the containers the config import surface exports.

The interpreter's bind-time check (:func:`atlantide.lang.interp.bind_rejection`)
accepts a list/tuple/dict/set as plain data without inspecting its elements, which
keeps deep scans off the import path. ``from atlantide.providers.x import TABLE;
TABLE[0]`` still hands config whatever ``TABLE`` holds: a ``pathlib.Path``, a
provider, an engine function. :func:`audit_import_surface` therefore imports every
module config may import, walks every public module-level container, and applies
the bind predicate to each key and value. CI asserts it finds nothing.
"""

from __future__ import annotations

import importlib
import pkgutil
import types
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from atlantide.lang.interp import bind_rejection
from atlantide.lang.validate import DEFAULT_SURFACE, LanguageSurface, import_allowed

#: Exact scalar types, inert whatever their value.
_SCALARS: frozenset[type] = frozenset({str, int, float, bool, bytes, type(None)})
#: Exact container types whose elements are walked.
_CONTAINERS: frozenset[type] = frozenset({list, tuple, dict, set, frozenset})


@dataclass(frozen=True, slots=True)
class SurfaceViolation:
    """One element of an exported container that config must not reach."""

    module: str
    #: Where the element sits, e.g. ``TABLE[0]['key']`` or ``KINDS{<key>}``.
    path: str
    #: Qualified type name of the offending element.
    type_name: str
    reason: str


def importable_modules(surface: LanguageSurface = DEFAULT_SURFACE) -> list[str]:
    """Every module under the allowed prefixes that ``import_allowed`` admits.

    Packages are recursed into only when importable themselves: a forbidden
    package (``...aws.handlers``) is neither imported nor descended.
    """
    found: set[str] = set()
    pending = [prefix for prefix in surface.prefixes() if import_allowed(prefix, surface)]
    if import_allowed("atlantide", surface):
        found.add("atlantide")
    while pending:
        name = pending.pop()
        if name in found:
            continue
        module = importlib.import_module(name)
        found.add(name)
        path = getattr(module, "__path__", None)
        if path is None:
            continue
        pending.extend(
            info.name
            for info in pkgutil.iter_modules(path, name + ".")
            if import_allowed(info.name, surface)
        )
    return sorted(found)


def audit_import_surface(surface: LanguageSurface = DEFAULT_SURFACE) -> list[SurfaceViolation]:
    """Violations across every importable module; empty when the surface is sound."""
    violations: list[SurfaceViolation] = []
    for name in importable_modules(surface):
        violations.extend(audit_module(importlib.import_module(name), surface))
    return violations


def audit_module(
    module: types.ModuleType, surface: LanguageSurface = DEFAULT_SURFACE
) -> list[SurfaceViolation]:
    """Violations in one module's public module-level containers."""
    violations: list[SurfaceViolation] = []
    for attr, value in sorted(vars(module).items()):
        # Config imports public names only (see `private_import_message`).
        if attr.startswith("_") or not _is_container(value):
            continue
        violations.extend(_walk(value, attr, module.__name__, surface, set()))
    return violations


def _is_container(value: Any) -> bool:
    return isinstance(value, (list, tuple, set, frozenset, Mapping)) and not isinstance(value, type)


def _type_name(value: Any) -> str:
    cls = type(value)
    return f"{cls.__module__}.{cls.__qualname__}"


def _elements(value: Any, path: str) -> Iterator[tuple[Any, str]]:
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key, f"{path}{{{key!r}}}"
            yield item, f"{path}[{key!r}]"
    elif isinstance(value, (set, frozenset)):
        for item in value:
            yield item, f"{path}{{{item!r}}}"
    else:
        for index, item in enumerate(value):
            yield item, f"{path}[{index}]"


def _walk(
    value: Any, path: str, module: str, surface: LanguageSurface, seen: set[int]
) -> Iterator[SurfaceViolation]:
    cls = type(value)
    if cls in _SCALARS:
        return
    if not _is_container(value):
        reason = bind_rejection(value, path, module, surface)
        if reason is not None:
            yield SurfaceViolation(module, path, _type_name(value), reason)
        return
    # A container subclass (NamedTuple, defaultdict, a dict with `__missing__`)
    # carries behaviour, so its class goes through the bind check as well.
    if cls not in _CONTAINERS:
        reason = bind_rejection(cls, path, module, surface)
        if reason is not None:
            yield SurfaceViolation(module, path, _type_name(value), reason)
    if id(value) in seen:
        return
    seen.add(id(value))
    for item, item_path in _elements(value, path):
        yield from _walk(item, item_path, module, surface, seen)
