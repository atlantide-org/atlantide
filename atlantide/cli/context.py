"""What this invocation was asked for, in one place.

The root flags ``--debug``, ``--profile``, ``--no-plugins`` and ``--audit-log``,
and the per-command ``--json``, are read far from where they are parsed:
:func:`~atlantide.cli.errors.fail` chooses whether to emit an error envelope,
:func:`~atlantide.cli.target.current_project` applies the profile overlay, and
provider discovery checks whether plugins are enabled. A ``typer.Context`` would
have to be threaded through every helper to reach them.

The flags live in a :class:`contextvars.ContextVar` rather than module globals, so
in-process invocations (tests, embedded callers) do not inherit each other's
flags; :func:`using` restores the previous value on exit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path


@dataclass(frozen=True, slots=True)
class RunContext:
    """The invocation-wide answers every command shares.

    Frozen so each flag keeps one meaning for the whole run; :func:`set_json_mode`
    replaces the context rather than mutating a field.
    """

    #: ``--debug``: print the full traceback and cause chain on error.
    debug: bool = False
    #: ``--profile``: which ``[profile.<name>]`` overlay atlantide.toml runs under.
    profile: str | None = None
    #: ``--no-plugins``: ignore installed provider plugins.
    no_plugins: bool = False
    #: ``--audit-log``: where this run's events are appended, if anywhere.
    audit_log: Path | None = None
    #: ``--json``: stdout is one JSON document, so human output goes to stderr.
    json: bool = False


#: ``None`` until the app callback runs, distinguishing "no run has begun" (what a
#: library caller importing this package sees) from "a run with every flag off".
_current: ContextVar[RunContext | None] = ContextVar("atlantide_run", default=None)

_DEFAULT = RunContext()


def current() -> RunContext:
    """This invocation's flags, or all-defaults outside a CLI run."""
    return _current.get() or _DEFAULT


def begin(
    *,
    debug: bool = False,
    profile: str | None = None,
    no_plugins: bool = False,
    audit_log: Path | None = None,
) -> None:
    """Record the root flags. Called once, by the app callback.

    Also resets ``json``, which belongs to a single command and must not carry
    over to the next.
    """
    _current.set(
        RunContext(debug=debug, profile=profile, no_plugins=no_plugins, audit_log=audit_log)
    )


def set_json_mode(*, enabled: bool) -> None:
    """Declare whether stdout carries a JSON document.

    When enabled, human-facing output (banners, warnings, errors) goes to stderr.
    Set per command rather than per run: only some commands offer ``--json``, and a
    subcommand group may differ from its parent.
    """
    _current.set(replace(current(), json=enabled))


def json_mode() -> bool:
    return current().json


@contextmanager
def using(context: RunContext) -> Iterator[RunContext]:
    """Run a block under ``context``, restoring whatever was set before.

    An embedding caller, or a test invoking several commands, gets its own state
    back.
    """
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)
