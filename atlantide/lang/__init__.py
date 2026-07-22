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
from atlantide.lang.validate import validate_source

__all__ = ["DEFAULT_FUEL", "evaluate_source", "validate_source"]


def evaluate_source(
    source: str,
    filename: str = "<config>",
    *,
    inputs: dict[str, Any] | None = None,
    extra_globals: dict[str, Any] | None = None,
    fuel: int = DEFAULT_FUEL,
) -> Result[ResourceRegistry, AtlantideError]:
    """Validate + evaluate Atlas-lang source into a resource registry.

    ``extra_globals`` injects additional names (e.g. resource classes) without an
    import. Any config-level error is returned as a ``Failure`` rather than raised.
    """
    namespace = build_globals(inputs)
    if extra_globals:
        namespace.update(extra_globals)

    # Validate first; `bind` short-circuits on a validation Failure, so the run
    # step only ever sees a valid module.
    validated: Result[ast.Module, AtlantideError] = validate_source(source, filename)
    return validated.bind(lambda module: _run_module(module, namespace, fuel))


def _run_module(
    module: ast.Module, namespace: dict[str, Any], fuel: int
) -> Result[ResourceRegistry, AtlantideError]:
    """Evaluate a validated module, funnelling every failure into a ``Failure``."""
    try:
        with collecting() as registry:
            Interpreter(fuel=fuel).run(module, Scope(init=namespace))
    except AtlantideError as exc:
        return Failure(exc)
    except ValidationError as exc:
        return Failure(LanguageError(f"invalid resource inputs: {exc}"))
    except Exception as exc:
        # A native runtime error surfaced from config evaluation
        # (ZeroDivisionError, KeyError, ValueError from int('x'), RecursionError).
        # Config-level errors must return a Failure rather than crash the engine,
        # so wrap it as a LanguageError.
        return Failure(LanguageError(f"evaluation error: {type(exc).__name__}: {exc}"))
    return Success(registry)
