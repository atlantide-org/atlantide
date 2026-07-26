"""The harness's own CLI driver.

Worth testing directly rather than relying on the suites that use it: its
assertions are guards over commands that normally succeed, so they stay silent
until the day something breaks — which is exactly when the message has to be
good, and the only time nobody is watching.
"""

from __future__ import annotations

import pytest

from tests.support import Cli

cli = Cli()


def test_ok_returns_the_result_of_a_successful_command() -> None:
    result = cli.ok("--version")

    assert result.exit_code == 0
    assert "atlantide" in result.output


def test_ok_fails_when_the_command_fails() -> None:
    """The whole point: a setup step that quietly failed used to surface as an
    unrelated assertion several lines later, with the error text discarded."""
    with pytest.raises(AssertionError):
        cli.ok("schema", "does.NotExist")


def test_the_failure_message_names_the_command_and_shows_the_output() -> None:
    """A bare `assert result.exit_code == 0` says "0 != 1" and nothing else."""
    with pytest.raises(AssertionError) as caught:
        cli.ok("schema", "does.NotExist")

    message = str(caught.value)
    assert "atlantide schema does.NotExist" in message
    assert "exited 1, expected 0" in message
    assert "unknown type" in message  # the CLI's own diagnostic, not swallowed


def test_run_reports_a_failure_without_raising() -> None:
    """For the tests that are *about* the exit code."""
    result = cli.run("schema", "does.NotExist")

    assert result.exit_code == 1


def test_fails_requires_the_expected_code() -> None:
    cli.fails("schema", "does.NotExist")

    with pytest.raises(AssertionError):
        cli.fails("--version")  # succeeds, so `fails` must complain


def test_arguments_are_stringified() -> None:
    """So a `Path` can be passed as itself instead of wrapped in `str()` at every
    call site — which was most of the noise in the old invocations."""
    from pathlib import Path

    result = cli.run("schema", Path("does.NotExist"))

    assert result.exit_code == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
