"""atlantide.lang: Atlas-lang — a deterministic Python-syntax config subset.

Public entrypoint: :func:`evaluate_source`, which validates the subset, runs it
on our own interpreter, and returns the collected resources as a
``Result[ResourceRegistry, AtlantideError]``.
"""

from __future__ import annotations

import ast
from typing import Any

from pydantic import ValidationError
from returns.result import Failure, Result, Success

from atlantide.core.errors import AtlantideError, LanguageError
from atlantide.core.resource import ResourceRegistry, collecting
from atlantide.lang.builtins import build_globals
from atlantide.lang.interp import DEFAULT_FUEL, Interpreter, Scope
from atlantide.lang.validate import DEFAULT_SURFACE, LanguageSurface, validate_source

__all__ = [
    "DEFAULT_FUEL",
    "DEFAULT_SURFACE",
    "LanguageSurface",
    "evaluate_source",
    "validate_source",
]


def evaluate_source(
    source: str,
    filename: str = "<config>",
    *,
    inputs: dict[str, Any] | None = None,
    extra_globals: dict[str, Any] | None = None,
    surface: LanguageSurface = DEFAULT_SURFACE,
    fuel: int = DEFAULT_FUEL,
) -> Result[ResourceRegistry, AtlantideError]:
    """Validate + evaluate Atlas-lang source into a resource registry.

    ``extra_globals`` injects additional names (e.g. resource classes) without an
    import. Any config-level error is returned as a ``Failure`` rather than raised.
    """
    namespace = build_globals(inputs)
    if extra_globals:
        namespace.update(extra_globals)
    api = namespace["atlantide"]

    # Validate first; `bind` short-circuits on a validation Failure, so the run
    # step only ever sees a valid module.
    validated: Result[ast.Module, AtlantideError] = validate_source(source, filename, surface)
    evaluated = validated.bind(lambda module: _run_module(module, namespace, fuel, surface))
    # Record what the config actually read, so the caller can show it and an
    # unconsumed input cannot look like part of the plan's identity.
    return evaluated.map(lambda registry: _with_inputs(registry, api.consumed))


def _with_inputs(registry: ResourceRegistry, consumed: dict[str, Any]) -> ResourceRegistry:
    registry.inputs = dict(consumed)
    return registry


def _run_module(
    module: ast.Module,
    namespace: dict[str, Any],
    fuel: int,
    surface: LanguageSurface = DEFAULT_SURFACE,
) -> Result[ResourceRegistry, AtlantideError]:
    """Evaluate a validated module, funnelling every failure into a ``Failure``."""
    try:
        with collecting() as registry:
            Interpreter(fuel=fuel, surface=surface).run(module, Scope(init=namespace))
    except AtlantideError as exc:
        return Failure(exc)
    except ValidationError as exc:
        return Failure(LanguageError(f"invalid resource inputs: {exc}"))
    except Exception as exc:
        # A native runtime error from config evaluation (ZeroDivisionError,
        # KeyError, ValueError from int('x'), RecursionError). Config-level errors
        # must return a Failure rather than crash the engine.
        return Failure(LanguageError(f"evaluation error: {type(exc).__name__}: {exc}"))
    return Success(registry)
