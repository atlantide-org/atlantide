"""Atlas-lang: a deterministic Python-syntax config subset.

Public entrypoint: :func:`evaluate_source`, which validates the subset, runs it
on the Atlas-lang interpreter, and returns the collected resources as a
``Result[ResourceRegistry, AtlantideError]``.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from returns.result import Failure, Result, Success

from atlantide.core._describe import describe_type, describe_value, is_plain_data, scrub_addresses
from atlantide.core.config import EnvSelection, selecting
from atlantide.core.errors import AtlantideError, LanguageError
from atlantide.core.resource import ResourceRegistry, collecting
from atlantide.lang.builtins import build_globals, consumed_inputs
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
    """Validate and evaluate Atlas-lang source into a resource registry.

    ``envs`` narrows which environments a ``Config`` in the source yields;
    ``None`` means every one it declares. ``extra_globals`` injects additional
    names (e.g. resource classes) without an import. Any config-level error is
    returned as a ``Failure`` rather than raised.
    """
    namespace = build_globals(inputs)
    if extra_globals:
        namespace.update(extra_globals)
    api = namespace["atlantide"]

    validated: Result[ast.Module, AtlantideError] = validate_source(source, filename, surface)
    with selecting(envs) as selection:
        evaluated = validated.bind(lambda module: _run_module(module, namespace, fuel, surface))
    # Only the inputs the config read are recorded: an unread input is not part of
    # the plan's identity.
    return evaluated.bind(lambda registry: _finish(registry, consumed_inputs(api), selection))


def _finish(
    registry: ResourceRegistry, consumed: dict[str, Any], selection: EnvSelection
) -> Result[ResourceRegistry, AtlantideError]:
    registry.inputs = dict(consumed)
    registry.envs_declared = selection.declared
    registry.envs_selected = selection.selected
    if selection.requested is not None and not selection.consumed:
        # `--env` narrows nothing when no `Config` consumes it, so it is an error.
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
        return Failure(LanguageError(f"invalid resource inputs: {_validation_message(exc)}"))
    except Exception as exc:
        # Native runtime errors (ZeroDivisionError, KeyError, RecursionError) become
        # a Failure. Their text can embed a memory address (`[1].index(f)` embeds
        # `repr(f)`), so addresses are scrubbed to keep the message deterministic.
        message = scrub_addresses(f"{type(exc).__name__}: {exc}")
        return Failure(LanguageError(f"evaluation error: {message}"))
    return Success(registry)


def _validation_message(exc: ValidationError) -> str:
    """Pydantic's error text, with inputs that are not plain data rendered deterministically.

    Pydantic renders each input by ``repr``, which for a function embeds a memory
    address. Such inputs are rendered through :func:`describe_value` instead, in the
    same layout.
    """
    errors = exc.errors()
    if all(is_plain_data(error["input"]) for error in errors):
        return str(exc)
    count = len(errors)
    lines = [f"{count} validation error{'s' if count != 1 else ''} for {exc.title}"]
    for error in errors:
        value = error["input"]
        lines.append(".".join(str(part) for part in error["loc"]))
        lines.append(
            f"  {error['msg']} [type={error['type']}, input_value={describe_value(value)}, "
            f"input_type={describe_type(value)}]"
        )
    return "\n".join(lines)
