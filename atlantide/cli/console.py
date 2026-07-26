"""The shared Rich consoles all CLI modules print through.

Two of them, because ``--json`` promises stdout is one parseable document. Rich
cannot be asked mid-stream to make an exception, so anything a human reads — the
state banner, warnings, diagnostics, errors — goes to :data:`err_console`, and
only the JSON payload goes to :data:`console`.

Which mode a command is in is read from :mod:`atlantide.cli.context`, so the
human-facing helpers can route themselves rather than every print site branching.
"""

from rich.console import Console

from atlantide.cli.context import json_mode

console = Console()

#: Human-facing output, kept off stdout so ``--json`` stays parseable.
err_console = Console(stderr=True)

__all__ = ["console", "err_console", "json_mode", "out"]


def out() -> Console:
    """Where human-facing output belongs for the current command.

    stderr under ``--json``, so a warning printed on the way to a successful
    payload cannot corrupt it. The failure that motivates this is narrow and
    nasty: a CI parser that works until the day there is something to warn about.
    """
    return err_console if json_mode() else console
