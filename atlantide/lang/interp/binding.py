"""The bind-time half of the import policy: which objects config may bind.

`validate` checks the module path statically; this checks the object the
``from <module> import <name>`` actually resolves to.
"""

from __future__ import annotations

import functools
import importlib
import types
from typing import Any

from atlantide.core.provider import Provider
from atlantide.lang.validate import (
    DEFAULT_SURFACE,
    FORBIDDEN_CORE_NAMES,
    LanguageSurface,
    engine_import_message,
    import_allowed,
)

#: Inert data types: instances may be bound from any allowed module, wherever the
#: constant is defined.
_PLAIN_VALUE_TYPES: tuple[type, ...] = (
    str,
    int,
    float,
    bool,
    bytes,
    type(None),
    tuple,
    list,
    dict,
    frozenset,
    set,
)


@functools.cache
def _forbidden_objects() -> tuple[Any, ...]:
    """The objects behind :data:`FORBIDDEN_CORE_NAMES`, compared by identity.

    Compared by identity because the same function is reachable as
    ``atlantide.core.resource.active_registry``, under an alias, or as a
    re-export from a provider or component package.
    """
    core = importlib.import_module("atlantide.core")
    return tuple(getattr(core, name) for name in sorted(FORBIDDEN_CORE_NAMES))


@functools.cache
def _foreign_allowed() -> tuple[Any, ...]:
    """Objects defined outside atlantide that ``atlantide.core`` re-exports as
    config API: the ``returns`` containers that fallible core APIs return."""
    core = importlib.import_module("atlantide.core")
    return tuple(getattr(core, name) for name in ("Result", "Success", "Failure", "is_successful"))


def _origin(obj: Any) -> str | None:
    """The module that defined ``obj`` (a class or function's own ``__module__``;
    an instance reports its class's)."""
    origin = getattr(obj, "__module__", None)
    return origin if isinstance(origin, str) else type(obj).__module__


def bind_rejection(
    obj: Any, name: str, module: str, surface: LanguageSurface = DEFAULT_SURFACE
) -> str | None:
    """Why ``from <module> import <name>`` must not bind ``obj``, or ``None``.

    The bind-time predicate, shared with the import-surface audit
    (`atlantide.lang.surface`), which applies it to every element of the
    containers the allowed modules export. Plain data is accepted here without
    inspecting its contents.
    """
    # A module object (e.g. `from atlantide.lang.interp.binding import importlib`)
    # would expose the stdlib to config.
    if isinstance(obj, types.ModuleType):
        return (
            f"cannot import module {name!r} from {module!r}; "
            "only public classes and functions may be imported"
        )
    # Engine machinery (the live registry, the `--env` selection), compared by
    # identity so an alias or a re-export under another module path matches.
    if any(obj is forbidden for forbidden in _forbidden_objects()):
        return engine_import_message(name, module)
    # A Provider performs the boto3/filesystem calls; the CLI registers and
    # drives providers, not config.
    if (isinstance(obj, type) and issubclass(obj, Provider)) or isinstance(obj, Provider):
        return (
            f"cannot import provider {name!r} from {module!r}; "
            "config declares resources, it does not drive providers"
        )
    # Refuse a name an allowed module imports from elsewhere. The allow-list
    # covers module paths, but a module's namespace also holds its imports:
    # `Path` in `atlantide.providers.aws.resources.compute` is `pathlib.Path`,
    # and `PathScope` re-exported by `atlantide.providers.local` resolves real
    # files. The object must be defined on the config surface, or be an instance
    # of a type defined there. Plain data is inert wherever it is defined; the
    # allowed modules' containers are audited statically.
    if isinstance(obj, _PLAIN_VALUE_TYPES) and not isinstance(obj, type):
        return None
    if any(obj is allowed for allowed in _foreign_allowed()):
        return None
    origin = _origin(obj)
    if origin is not None and import_allowed(origin, surface):
        return None
    return (
        f"cannot import {name!r} from {module!r}: it is defined in "
        f"{origin!r}, which is not config API — config imports only what the "
        "allowed modules define, not what they import"
    )
