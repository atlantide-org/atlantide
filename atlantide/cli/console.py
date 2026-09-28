"""The shared Rich consoles all CLI modules print through.

Under ``--json`` stdout must be one parseable document, so human-readable output
(state banner, warnings, diagnostics, errors) goes to :data:`err_console` and only
the JSON payload goes to :data:`console`. :func:`out` reads the mode from
:mod:`atlantide.cli.context`, so print sites do not branch on it.
"""

from rich.console import Console

from atlantide.cli.context import json_mode

console = Console()

#: Human-facing output, kept off stdout so ``--json`` stays parseable.
err_console = Console(stderr=True)

__all__ = ["console", "err_console", "json_mode", "out"]


def out() -> Console:
    """Where human-facing output belongs for the current command.

    stderr under ``--json``, so a warning cannot corrupt the JSON payload.
    """
    return err_console if json_mode() else console
