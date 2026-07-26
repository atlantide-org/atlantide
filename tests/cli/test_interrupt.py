"""Ctrl-C at the CLI boundary: cancel and clean up, then abandon on the second.

Python's default SIGINT raises `KeyboardInterrupt` on the main thread, which
unwinds straight past the executor without cancelling its tasks — so the saga
never runs and the last thing an operator sees mid-apply is a traceback. These
tests drive a real signal through `run_async` rather than simulating one.
"""

from __future__ import annotations

import asyncio
import os
import signal

import pytest
from returns.result import Failure, Result, Success

from atlantide.cli.errors import run_async
from atlantide.core import AtlantideError
from atlantide.core.errors import InterruptedRunError, ProviderError

pytestmark = pytest.mark.skipif(
    not hasattr(signal, "SIGINT") or os.name == "nt",
    reason="POSIX signal delivery",
)


async def _sigint_after_start() -> Result[str, AtlantideError]:
    """A coroutine that signals its own process, then waits to be cancelled."""
    os.kill(os.getpid(), signal.SIGINT)
    await asyncio.sleep(30)
    return Success("should not get here")  # pragma: no cover


def test_an_interrupt_becomes_a_typed_failure_not_a_traceback() -> None:
    result = run_async(_sigint_after_start())

    assert isinstance(result, Failure)
    assert isinstance(result.failure(), InterruptedRunError)


def test_the_message_says_what_happened_to_the_resources() -> None:
    """An interrupt mid-apply leaves a question — what got built? — and the one
    line the operator sees has to answer it rather than just say "cancelled"."""
    message = str(run_async(_sigint_after_start()).failure())

    assert "rolled back" in message
    assert "plan" in message


def test_an_ordinary_failure_is_still_reported_as_itself() -> None:
    """The widened except clause must not swallow real errors into "interrupted"."""

    async def boom() -> Result[str, AtlantideError]:
        raise ProviderError("bucket already exists", op="create")

    result = run_async(boom())

    assert isinstance(result, Failure)
    error = result.failure()
    assert isinstance(error, ProviderError)
    assert not isinstance(error, InterruptedRunError)


def test_a_successful_run_is_untouched_by_the_handler() -> None:
    async def fine() -> Result[str, AtlantideError]:
        return Success("done")

    assert run_async(fine()).unwrap() == "done"


def test_the_signal_handler_is_removed_afterwards() -> None:
    """Installed per run, not per process: leaving it behind would hijack Ctrl-C
    for whatever the CLI does next, including the confirmation prompts."""
    before = signal.getsignal(signal.SIGINT)

    async def fine() -> Result[str, AtlantideError]:
        return Success("done")

    run_async(fine())

    assert signal.getsignal(signal.SIGINT) is before


def test_a_rollback_failure_rides_along_with_the_interrupt() -> None:
    """Both facts matter: the run was cancelled, *and* something could not be
    undone. Reporting only the first would hide the damage."""

    async def cancelled_with_debris() -> Result[str, AtlantideError]:
        error = asyncio.CancelledError()
        error._also_failed = [ProviderError("could not delete bucket", op="delete")]  # type: ignore[attr-defined]
        raise error

    result = run_async(cancelled_with_debris())

    error = result.failure()
    assert isinstance(error, InterruptedRunError)
    also = getattr(error, "_also_failed", [])
    assert any("could not delete bucket" in str(e) for e in also)


def test_a_second_interrupt_abandons_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """The escape hatch: an operator pressing Ctrl-C twice wants out now, and an
    unkillable "cleaning up" is worse than an honest abandonment.

    `os._exit` is patched to something observable — the real one would take the
    test runner with it, which is precisely the point of using it.
    """
    exited: list[int] = []
    monkeypatch.setattr(os, "_exit", lambda code: exited.append(code))

    async def twice() -> Result[str, AtlantideError]:
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(0)  # let the first be delivered
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.sleep(30)
        return Success("unreachable")  # pragma: no cover

    run_async(twice())

    assert exited == [130], "the conventional exit code for terminated-by-SIGINT"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
