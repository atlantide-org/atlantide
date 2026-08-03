"""atlantide.lang: Atlas-lang — a deterministic Python-syntax config subset.

Public entrypoint: :func:`evaluate_source`, which validates the subset, runs it
on our own interpreter, and returns the collected resources as a
``Result[ResourceRegistry, AtlantideError]``.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from returns.result import Failure, Result, Success

from atlantide.core.config import EnvSelection, selecting
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
    envs: Sequence[str] | None = None,
    extra_globals: dict[str, Any] | None = None,
    surface: LanguageSurface = DEFAULT_SURFACE,
    fuel: int = DEFAULT_FUEL,
) -> Result[ResourceRegistry, AtlantideError]:
    """Validate + evaluate Atlas-lang source into a resource registry.

    ``envs`` narrows which environments a ``Config`` in the source yields;
    ``None`` means every one it declares. ``extra_globals`` injects additional
    names (e.g. resource classes) without an import. Any config-level error is
    returned as a ``Failure`` rather than raised.
    """
    namespace = build_globals(inputs)
    if extra_globals:
        namespace.update(extra_globals)
    api = namespace["atlantide"]

    # Validate first; `bind` short-circuits on a validation Failure, so the run
    # step only ever sees a valid module.
    validated: Result[ast.Module, AtlantideError] = validate_source(source, filename, surface)
    with selecting(envs) as selection:
        evaluated = validated.bind(lambda module: _run_module(module, namespace, fuel, surface))
    # Record what the config actually read, so the caller can show it and an
    # unconsumed input cannot look like part of the plan's identity.
    return evaluated.bind(lambda registry: _finish(registry, api.consumed, selection))


def _finish(
    registry: ResourceRegistry, consumed: dict[str, Any], selection: EnvSelection
) -> Result[ResourceRegistry, AtlantideError]:
    registry.inputs = dict(consumed)
    registry.envs_declared = selection.declared
    registry.envs_selected = selection.selected
    if selection.requested is not None and not selection.consumed:
        # The only place that knows both that `--env` was asked for and that no
        # `Config` answered it; without this the run would narrow nothing and
        # still report success.
        return Failure(
            LanguageError(
                "--env was given but the config declares no Config(...) — "
                "environments come from a Config, see `atlantide.core.Config`"
            )
        )
    return Success(registry)


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
