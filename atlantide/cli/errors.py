"""CLI error plumbing: async-run bridging, diagnostics rendering, exit helpers.

The engine's async path raises ``ExceptionGroup``s; :func:`run_async` funnels
them back into a ``Result`` so commands keep one error-handling shape. The
``fail*`` helpers render and exit non-zero.
"""

from __future__ import annotations

import asyncio
import os
import signal
import traceback
from collections.abc import Callable, Coroutine
from typing import Any, NoReturn, TypeVar

import typer
from returns.result import Failure, Result
from rich.markup import escape

from atlantide.cli.console import out
from atlantide.cli.context import current, json_mode
from atlantide.core import AtlantideError
from atlantide.core.errors import InterruptedRunError

_T = TypeVar("_T")

#: Conventional shell exit code for "terminated by SIGINT" (128 + 2).
_EXIT_INTERRUPTED = 130


def run_async(
    coro: Coroutine[Any, Any, Result[_T, AtlantideError]],
) -> Result[_T, AtlantideError]:
    """Run an engine coroutine, funnelling a provider ExceptionGroup into a Failure.

    The primary typed error (with its ``node_id``/``op`` context and ``__cause__``
    chain) is preserved rather than stringified, so the caller can render which
    resource failed and, under ``--debug``, the full traceback. Any additional
    failed leaves ride along on ``_also_failed`` for rendering.

    Ctrl-C is handled rather than left to Python's default: the default raises
    ``KeyboardInterrupt`` on the main thread, which unwinds past the executor
    without ever cancelling its tasks — so the saga never runs and the traceback
    is the last thing an operator sees mid-apply. See :func:`_install_sigint`.
    """
    try:
        return asyncio.run(_interruptible(coro))
    except BaseException as exc:  # includes the cancellation an interrupt causes
        leaves = flatten_group(exc)
        if any(isinstance(e, asyncio.CancelledError | KeyboardInterrupt) for e in leaves):
            return Failure(_interrupted(leaves))
        typed = [e for e in leaves if isinstance(e, AtlantideError)]
        if typed:
            primary: AtlantideError = typed[0]
            rest = [e for e in leaves if e is not primary]
        else:
            # The synthesized primary already joins every leaf message; listing
            # the leaves again as "also failed" would render each one twice.
            primary = AtlantideError("; ".join(str(e) for e in leaves))
            rest = []
        if rest:
            primary._also_failed = rest  # type: ignore[attr-defined]
        return Failure(primary)


def _interrupted(leaves: list[BaseException]) -> InterruptedRunError:
    """The failure for an interrupted run, carrying anything else that broke.

    A rollback that also failed rides along on ``_also_failed`` (the executor puts
    it there), so the operator sees both the interrupt and what it could not undo.
    """
    error = InterruptedRunError(
        "interrupted — completed resources were rolled back where possible; "
        "run `atlantide plan` to see what is left"
    )
    extra = [e for e in leaves if not isinstance(e, asyncio.CancelledError | KeyboardInterrupt)]
    for leaf in leaves:
        extra.extend(getattr(leaf, "_also_failed", []))
    if extra:
        error._also_failed = extra  # type: ignore[attr-defined]
    return error


async def _interruptible(
    coro: Coroutine[Any, Any, Result[_T, AtlantideError]],
) -> Result[_T, AtlantideError]:
    """Drive ``coro`` as a task an interrupt can cancel cleanly."""
    task: asyncio.Task[Result[_T, AtlantideError]] = asyncio.ensure_future(coro)
    loop = asyncio.get_running_loop()
    restore = _install_sigint(loop, task)
    try:
        return await task
    finally:
        restore()


def _install_sigint(loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any]) -> Callable[[], None]:
    """Route Ctrl-C into cancelling ``task``; return a callable that undoes it.

    First press cancels, which unwinds the executor through its saga. Second press
    gives up on that and exits immediately — an operator pressing Ctrl-C twice is
    saying they want out now, and an unkillable "cleaning up" is worse than an
    honest abandonment.

    ``os._exit`` is deliberate on the second press: it skips ``finally`` blocks,
    *including the one that releases the state lock*. That is correct rather than
    sloppy. An abandoned run may still have boto worker threads mutating live
    resources — :func:`asyncio.to_thread` cannot kill them — so releasing the lease
    would invite a second writer alongside a first that is still going. The TTL
    reclaims it, and ``atlantide state unlock`` is there for an operator who knows
    the run is gone.
    """
    pressed = 0

    def interrupt() -> None:
        nonlocal pressed
        pressed += 1
        if pressed == 1:
            out().print(
                "\n[yellow]interrupt[/] — cancelling; resources already created will "
                "be rolled back. Press Ctrl-C again to abandon."
            )
            task.cancel()
            return
        out().print(
            "\n[bold red]abandoning[/] — state may not describe the live resources. "
            "Run `atlantide refresh` before applying again; the state lock will "
            "lapse on its own, or clear it with `atlantide state unlock`."
        )
        os._exit(_EXIT_INTERRUPTED)

    try:
        loop.add_signal_handler(signal.SIGINT, interrupt)
    except (NotImplementedError, AttributeError):  # pragma: no cover - Windows only
        # Windows has no loop-level signal handling; fall back to the classic
        # handler, hopping onto the loop thread to touch the task safely.
        previous = signal.signal(signal.SIGINT, lambda *_: loop.call_soon_threadsafe(interrupt))

        def restore_handler() -> None:
            signal.signal(signal.SIGINT, previous)

        return restore_handler

    def remove_handler() -> None:
        loop.remove_signal_handler(signal.SIGINT)

    return remove_handler


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
    if not current().debug:
        return
    rendered = "".join(traceback.format_exception(type(err), err, err.__traceback__))
    out().print(f"[dim]{escape(rendered.rstrip())}[/]", highlight=False)


def render_error(err: BaseException) -> None:
    """Print the red ``error:`` line(s) with node context; no exit."""
    out().print(f"[bold red]error:[/] {escape(error_prefix(err))}{escape(str(err))}")
    for extra in getattr(err, "_also_failed", []):
        out().print(f"[bold red]  and:[/] {escape(error_prefix(extra))}{escape(str(extra))}")


def fail(message: str) -> NoReturn:
    """Abort with a plain diagnostic.

    Messages here bypass the error taxonomy, so under ``--json`` they are wrapped
    in a generic envelope rather than left as text a consumer cannot parse.
    """
    if json_mode():
        _emit_error(AtlantideError(message))
    # Escaped: these messages quote config keys such as [state].backend, which
    # Rich would otherwise read as markup and swallow.
    out().print(f"[bold red]error:[/] {escape(message)}")
    raise typer.Exit(1)


def _emit_error(err: BaseException) -> NoReturn:
    """Write the JSON failure envelope to stdout and exit."""
    from atlantide.cli.json_out import emit_error_json

    emit_error_json(err)
    raise typer.Exit(_EXIT_INTERRUPTED if isinstance(err, InterruptedRunError) else 1)


def fail_error(err: AtlantideError) -> NoReturn:
    """Render a structured error (node context + optional traceback) and exit.

    An interrupt exits 130 (the shell convention for SIGINT) rather than 1, so a
    CI job can tell "someone cancelled this" from "this deployment is broken".
    """
    if json_mode():
        _emit_error(err)
    if isinstance(err, InterruptedRunError):
        out().print(f"[yellow]interrupted:[/] {escape(str(err))}")
        for extra in getattr(err, "_also_failed", []):
            out().print(f"[bold red]  and:[/] {escape(error_prefix(extra))}{escape(str(extra))}")
        maybe_traceback(err)
        raise typer.Exit(_EXIT_INTERRUPTED)
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
    if json_mode():
        _emit_error(err)
    render_error(err)
    line = getattr(err, "line", None)
    col = getattr(err, "col", None)
    lines = source.splitlines()
    if isinstance(line, int) and 1 <= line <= len(lines):
        gutter = f"{line:>4} | "
        out().print(f"[dim]{gutter}[/]{escape(lines[line - 1])}", highlight=False)
        caret_pad = " " * (len(gutter) + max((col or 1) - 1, 0))
        out().print(f"{caret_pad}[bold red]^[/]")
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
