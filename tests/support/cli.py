"""Drive the CLI the way a user does, and fail loudly when a command did not work.

A bare ``runner.invoke`` leaves the exit-code assertion to each call site. When
it is omitted, as it often is for setup steps, a failed setup ``apply`` surfaces
several assertions later as a symptom rather than the cause, with the error text
discarded.

:meth:`Cli.ok` makes the assertion the default and puts the command in the
message, so a failure says which invocation broke and what it printed. Arguments
are stringified, so a ``Path`` can be passed as itself.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from typing import Any

from click.testing import Result
from typer.testing import CliRunner

from atlantide.cli.main import app

#: Rich falls back to an 80-column terminal when stdout is not a tty, so a line
#: containing a ``tmp_path`` wraps at a column set by the temp directory's length,
#: which differs between machines and splits substrings tests assert on. A fixed
#: width makes CLI output depend on the command, not on where pytest put its files.
_WIDE = "200"


@dataclass(frozen=True, slots=True)
class Cli:
    """A CliRunner bound to the atlantide app."""

    runner: CliRunner = field(default_factory=CliRunner)

    def run(self, *args: Any, **kwargs: Any) -> Result:
        """Invoke and return the result whatever the exit code.

        For tests about the exit code (an aborted prompt, a ``--detailed-exitcode``
        of 2), where asserting it is the test.
        """
        env = {"COLUMNS": _WIDE, **(kwargs.pop("env", None) or {})}
        return self.runner.invoke(app, [str(arg) for arg in args], env=env, **kwargs)

    def ok(self, *args: Any, **kwargs: Any) -> Result:
        """Invoke and require success, quoting the command and its output if not."""
        result = self.run(*args, **kwargs)
        assert result.exit_code == 0, _explain(args, result)
        return result

    def fails(self, *args: Any, code: int = 1, **kwargs: Any) -> Result:
        """Invoke and require it to exit with ``code``."""
        result = self.run(*args, **kwargs)
        assert result.exit_code == code, _explain(args, result, expected=code)
        return result


def _explain(args: tuple[Any, ...], result: Result, *, expected: int = 0) -> str:
    command = shlex.join(["atlantide", *(str(arg) for arg in args)])
    return (
        f"{command}\n"
        f"  exited {result.exit_code}, expected {expected}\n"
        f"  output: {result.output or '(none)'}"
    )
