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
from concurrent.futures import ThreadPoolExecutor
from typing import Any, NoReturn

import typer
from returns.result import Failure, Result
from rich.markup import escape

from atlantide.cli.console import out
from atlantide.cli.context import current, json_mode
from atlantide.cli.views.output import emit_error_json
from atlantide.core import AtlantideError
from atlantide.core.errors import InterruptedRunError
from atlantide.core.tuning import io_workers
from atlantide.util.errors import also_failed, attach_also_failed

#: Conventional shell exit code for "terminated by SIGINT" (128 + 2).
_EXIT_INTERRUPTED = 130


def run_async[T](
    coro: Coroutine[Any, Any, Result[T, AtlantideError]],
    *,
    parallelism: int | None = None,
) -> Result[T, AtlantideError]:
    """Run an engine coroutine, converting a provider ExceptionGroup into a Failure.

    The primary typed error keeps its ``node_id``/``op`` context and ``__cause__``
    chain, for rendering and ``--debug`` tracebacks; other failed leaves are
    attached via :func:`~atlantide.util.errors.attach_also_failed`.

    Ctrl-C is routed through :func:`_install_sigint`: the default
    ``KeyboardInterrupt`` on the main thread unwinds past the executor without
    cancelling its tasks, so the saga would not run.

    The loop's default executor is sized by :func:`~atlantide.core.tuning.io_workers`
    from ``parallelism``; ``asyncio.run`` shuts it down with the loop.
    """
    try:
        return asyncio.run(_interruptible(coro, workers=io_workers(parallelism)))
    except BaseException as exc:  # includes the cancellation an interrupt causes
        leaves = flatten_group(exc)
        if any(isinstance(e, asyncio.CancelledError | KeyboardInterrupt) for e in leaves):
            return Failure(_interrupted(leaves))
        typed = [e for e in leaves if isinstance(e, AtlantideError)]
        if typed:
            primary: AtlantideError = typed[0]
            rest = [e for e in leaves if e is not primary]
        else:
            # The synthesized primary already joins every leaf message, so none
            # are attached as also-failed.
            primary = AtlantideError("; ".join(str(e) for e in leaves))
            rest = []
        if rest:
            attach_also_failed(primary, rest)
        return Failure(primary)


def _interrupted(leaves: list[BaseException]) -> InterruptedRunError:
    """The failure for an interrupted run, carrying anything else that broke.

    A rollback failure attached by the executor is carried along, so both the
    interrupt and what could not be undone are reported.
    """
    error = InterruptedRunError(
        "interrupted — completed resources were rolled back where possible; "
        "run `atlantide plan` to see what is left"
    )
    extra = [e for e in leaves if not isinstance(e, asyncio.CancelledError | KeyboardInterrupt)]
    for leaf in leaves:
        extra.extend(also_failed(leaf))
    if extra:
        attach_also_failed(error, extra)
    return error


async def _interruptible[T](
    coro: Coroutine[Any, Any, Result[T, AtlantideError]],
    *,
    workers: int,
) -> Result[T, AtlantideError]:
    """Drive ``coro`` as a task an interrupt can cancel cleanly.

    The handler stays installed until the executor has shut down: a cancelled task
    returns at once, but ``asyncio.run`` then waits on its worker threads (a boto
    call can take minutes), and a second Ctrl-C there must still abandon the run
    rather than raise ``KeyboardInterrupt``.
    """
    loop = asyncio.get_running_loop()
    loop.set_default_executor(
        ThreadPoolExecutor(max_workers=workers, thread_name_prefix="atlantide-io")
    )
    task: asyncio.Task[Result[T, AtlantideError]] = asyncio.ensure_future(coro)
    restore = _install_sigint(loop, task)
    try:
        return await task
    finally:
        try:
            await loop.shutdown_default_executor()
        finally:
            restore()


def _install_sigint(loop: asyncio.AbstractEventLoop, task: asyncio.Task[Any]) -> Callable[[], None]:
    """Route Ctrl-C into cancelling ``task``; return a callable that undoes it.

    The first press cancels, which unwinds the executor through its saga. The
    second abandons the rollback and exits via ``os._exit``, skipping ``finally``
    blocks, including the one that releases the state lock: boto worker threads may
    still be mutating live resources (:func:`asyncio.to_thread` cannot kill them),
    so releasing the lease would admit a concurrent writer. The lock TTL reclaims
    it, or ``atlantide state unlock`` clears it once the run is gone.
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
        # No loop-level signal handling on Windows: use ``signal.signal`` and hop
        # onto the loop thread before touching the task.
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
    _render_extra_failures(err)


def _render_extra_failures(err: BaseException) -> None:
    """One ``and:`` line per failure that rode along on ``err``."""
    for extra in also_failed(err):
        out().print(f"[bold red]  and:[/] {escape(error_prefix(extra))}{escape(str(extra))}")


def fail(message: str) -> NoReturn:
    """Abort with a plain diagnostic.

    The message carries no typed error, so under ``--json`` it is wrapped in a
    generic error envelope.
    """
    if json_mode():
        _emit_error(AtlantideError(message))
    # Escaped: messages quote config keys such as [state].backend, which Rich
    # parses as markup and drops.
    out().print(f"[bold red]error:[/] {escape(message)}")
    raise typer.Exit(1)


def _emit_error(err: BaseException) -> NoReturn:
    """Write the JSON failure envelope to stdout and exit."""
    emit_error_json(err)
    raise typer.Exit(_EXIT_INTERRUPTED if isinstance(err, InterruptedRunError) else 1)


def fail_error(err: AtlantideError) -> NoReturn:
    """Render a structured error (node context + optional traceback) and exit.

    An interrupt exits 130 (the shell convention for SIGINT) rather than 1, so CI
    can distinguish a cancelled run from a failed one.
    """
    if json_mode():
        _emit_error(err)
    if isinstance(err, InterruptedRunError):
        out().print(f"[yellow]interrupted:[/] {escape(str(err))}")
        _render_extra_failures(err)
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


def unwrap_or_exit[T](result: Result[T, AtlantideError]) -> T:
    """Return the success value, or render the failure and exit non-zero."""
    if isinstance(result, Failure):
        fail_error(result.failure())
    return result.unwrap()


def unwrap_or_diag[T](result: Result[T, AtlantideError], source: str) -> T:
    """Like :func:`unwrap_or_exit`, but renders a source-anchored diagnostic."""
    if isinstance(result, Failure):
        fail_diag(result.failure(), source)
    return result.unwrap()
