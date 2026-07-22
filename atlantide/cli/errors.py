"""CLI error plumbing: async-run bridging, diagnostics rendering, exit helpers.

The engine's async path raises ``ExceptionGroup``s; :func:`run_async` funnels
them back into a ``Result`` so commands keep one error-handling shape. The
``fail*`` helpers render and exit non-zero.
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Coroutine
from typing import Any, NoReturn, TypeVar

import typer
from returns.result import Failure, Result
from rich.markup import escape

from atlantide.cli.console import console
from atlantide.core import AtlantideError

_T = TypeVar("_T")

#: Set by the ``--debug`` root flag; when true, errors also print a full traceback.
_debug = False


def set_debug(enabled: bool) -> None:
    """Record the root ``--debug`` flag for :func:`maybe_traceback`."""
    global _debug
    _debug = enabled


def run_async(
    coro: Coroutine[Any, Any, Result[_T, AtlantideError]],
) -> Result[_T, AtlantideError]:
    """Run an engine coroutine, funnelling a provider ExceptionGroup into a Failure.

    The primary typed error (with its ``node_id``/``op`` context and ``__cause__``
    chain) is preserved rather than stringified, so the caller can render which
    resource failed and, under ``--debug``, the full traceback. Any additional
    failed leaves ride along on ``_also_failed`` for rendering.
    """
    try:
        return asyncio.run(coro)
    except Exception as exc:  # provider failures arrive as an ExceptionGroup
        leaves = flatten_group(exc)
        typed = [e for e in leaves if isinstance(e, AtlantideError)]
        primary: AtlantideError = (
            typed[0] if typed else AtlantideError("; ".join(str(e) for e in leaves))
        )
        rest = [e for e in leaves if e is not primary]
        if rest:
            primary._also_failed = rest  # type: ignore[attr-defined]
        return Failure(primary)


def flatten_group(exc: BaseException) -> list[BaseException]:
    """Flatten (possibly nested) ExceptionGroups into a flat list of leaf errors."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for e in exc.exceptions for leaf in flatten_group(e)]
    return [exc]


def error_prefix(err: BaseException) -> str:
    """``"[node <id> op=<op>] "`` when the error carries provider context, else ``""``."""
    bits = []
    if node_id := getattr(err, "node_id", None):
        bits.append(f"node {node_id}")
    if op := getattr(err, "op", None):
        bits.append(f"op={op}")
    return f"[{' '.join(bits)}] " if bits else ""


def maybe_traceback(err: BaseException) -> None:
    """Under ``--debug``, print the full traceback and ``__cause__`` chain."""
    if not _debug:
        return
    rendered = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    console.print(f"[dim]{escape(rendered.rstrip())}[/]", highlight=False)


def render_error(err: BaseException) -> None:
    """Print the red ``error:`` line(s) with node context; no exit."""
    console.print(f"[bold red]error:[/] {escape(error_prefix(err))}{escape(str(err))}")
    for extra in getattr(err, "_also_failed", []):
        console.print(f"[bold red]  and:[/] {escape(error_prefix(extra))}{escape(str(extra))}")


def fail(message: str) -> NoReturn:
    # Escaped: these messages quote config keys such as [state].backend, which
    # Rich would otherwise read as markup and swallow.
    console.print(f"[bold red]error:[/] {escape(message)}")
    raise typer.Exit(1)


def fail_error(err: AtlantideError) -> NoReturn:
    """Render a structured error (node context + optional traceback) and exit."""
    render_error(err)
    maybe_traceback(err)
    raise typer.Exit(1)


def require_choice(value: str, choices: tuple[str, ...], flag: str) -> None:
    """Exit with a uniform diagnostic when ``value`` is not one of ``choices``."""
    if value not in choices:
        expected = " or ".join(repr(c) for c in choices)
        fail(f"unknown {flag} {value!r} (expected {expected})")


def fail_diag(err: AtlantideError, source: str) -> NoReturn:
    """Render an error with a source snippet + caret when it carries a line/col."""
    render_error(err)
    line = getattr(err, "line", None)
    col = getattr(err, "col", None)
    lines = source.splitlines()
    if isinstance(line, int) and 1 <= line <= len(lines):
        gutter = f"{line:>4} | "
        console.print(f"[dim]{gutter}[/]{escape(lines[line - 1])}", highlight=False)
        caret_pad = " " * (len(gutter) + max((col or 1) - 1, 0))
        console.print(f"{caret_pad}[bold red]^[/]")
    maybe_traceback(err)
    raise typer.Exit(1)


def unwrap_or_exit(result: Result[_T, AtlantideError]) -> _T:
    """Return the success value, or render the failure and exit non-zero."""
    if isinstance(result, Failure):
        fail_error(result.failure())
    return result.unwrap()


def unwrap_or_diag(result: Result[_T, AtlantideError], source: str) -> _T:
    """Like :func:`unwrap_or_exit`, but renders a source-anchored diagnostic."""
    if isinstance(result, Failure):
        fail_diag(result.failure(), source)
    return result.unwrap()
